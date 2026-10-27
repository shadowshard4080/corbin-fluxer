"""Offline suite for reaction roles: parsing, validation, placement, the
store, adding to an existing message, and the question flow."""
from __future__ import annotations
import asyncio, logging, time, types
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.commands import reaction_role_cmds as cmds
from corbin.core import reaction_roles as rr
from corbin.core.context import Member, Role, User
from corbin.core.permissions import Perm

GID = "100"
WOLF = "<:wolf:1234567890>"


class FakeSettings:
    def __init__(self):
        self.data = {}

    def get(self, g, k, d=None):
        return self.data.get(str(g), {}).get(k, d)

    def set(self, g, k, v):
        self.data.setdefault(str(g), {})[k] = v

    def unset(self, g, k):
        self.data.get(str(g), {}).pop(k, None)

    def guilds(self):
        return iter(tuple(self.data))


ROLES = {
    GID: Role(GID, "@everyone", 0, 0),
    "1": Role("1", "Gamer", 0, 1),
    "2": Role("2", "Artist", 0, 2),
    "3": Role("3", "Night Owls", 0, 3),
    "4": Role("4", "Mod", int(Perm.BAN_MEMBERS), 4),
    "5": Role("5", "Corbin", 0, 9),
    "6": Role("6", "Big Deal", 0, 12),
    "7": Role("7", "Artist Alley", 0, 2),
}


def member(uid, *role_ids, owner=False):
    return Member(user=User(uid, uid), guild_id=GID,
                  roles=tuple(ROLES[r] for r in role_ids),
                  is_guild_owner=owner)


ADMIN = member("u1", "6")


def suite_parsing(r: Results) -> None:
    custom = {"wolf": WOLF}
    pairs, problems = rr.parse_pairs(
        "here you go:\n🎮 Gamer\n🎨 @Artist\n<:wolf:1234567890> Night Owls\n"
        "Gamer2 🕹️\n- 👩‍🎨 Painters\n1️⃣ First", custom)
    got = [(p.emoji.key, p.role_text) for p in pairs]
    r.check("one pair per line, emoji first or last", got, [
        ("🎮", "Gamer"), ("🎨", "@Artist"), ("1234567890", "Night Owls"),
        ("🕹", "Gamer2"), ("👩‍🎨", "Painters"), ("1⃣", "First")])
    r.check("a line with no emoji is ignored, not reported", problems, [])
    r.check("a custom emoji reacts with its full token", pairs[2].emoji.shown,
            WOLF)
    r.ok("a ZWJ sequence is one emoji", pairs[4].emoji.shown == "👩‍🎨")

    pairs, _ = rr.parse_pairs(":wolf: Night Owls", custom)
    r.check("a flattened :name: resolves through the guild's emoji",
            pairs[0].emoji.shown, WOLF)
    pairs, problems = rr.parse_pairs(":nope: Night Owls", custom)
    r.ok("an unknown :name: is reported", not pairs and problems)

    pairs, _ = rr.parse_pairs("🎮 Gamer, 🎨 Artist")
    r.check("several pairs on one line",
            [(p.emoji.key, p.role_text) for p in pairs],
            [("🎮", "Gamer"), ("🎨", "Artist")])
    pairs, _ = rr.parse_pairs("🎮 - the Gamer role")
    r.check("separators and 'the ... role' are trimmed", pairs[0].role_text,
            "Gamer")
    pairs, _ = rr.parse_pairs("✅ <@&2>")
    r.check("a role mention keeps its closing >", pairs[0].role_text,
            "<@&2>")
    r.check("...and resolves",
            rr.resolve_role(pairs[0].role_text, ROLES)[0].name, "Artist")
    pairs, problems = rr.parse_pairs("🎮")
    r.ok("an emoji with no role asks which", not pairs and problems)
    r.check("plain chat yields nothing",
            rr.parse_pairs("sure, give me a sec"), ([], []))


def suite_roles(r: Results) -> None:
    role, _ = rr.resolve_role("<@&2>", ROLES)
    r.check("a role mention", role.name, "Artist")
    role, _ = rr.resolve_role("gamer", ROLES)
    r.check("a name, any case", role.name, "Gamer")
    role, _ = rr.resolve_role("@Night", ROLES)
    r.check("a unique partial name", role.name, "Night Owls")
    role, why = rr.resolve_role("Art", ROLES)
    r.ok("an ambiguous partial asks", role is None and "which" in why)
    role, _ = rr.resolve_role("Artist", ROLES)
    r.check("an exact name beats partials", role.name, "Artist")
    role, why = rr.resolve_role("Ghosts", ROLES)
    r.ok("an unknown name says so", role is None and "no role" in why)

    def ref(rid, bot_top=9, invoker_top=12, exempt=False):
        return rr.refusal(ROLES[rid], guild_id=GID, bot_top=bot_top,
                          invoker_top=invoker_top, invoker_exempt=exempt)

    r.ok("@everyone is refused", ref(GID) is not None)
    r.ok("a moderation role is refused", "moderation" in ref("4"))
    r.ok("a role at or above mine is refused", "my highest" in ref("5"))
    r.ok("a role above the asker's is refused",
         "your highest" in ref("3", invoker_top=2))
    r.ok("...unless they own the server",
         ref("3", invoker_top=2, exempt=True) is None)
    r.ok("an ordinary role is fine", ref("1") is None)


CHANNELS = [
    {"id": "10", "name": "do-not-type", "type": 0, "position": 0},
    {"id": "20", "name": "Info", "type": 4, "position": 0},
    {"id": "21", "name": "📢-announcements", "type": 0, "position": 0,
     "parent_id": "20"},
    {"id": "22", "name": "server-rules", "type": 0, "position": 1,
     "parent_id": "20"},
    {"id": "23", "name": "faq", "type": 0, "position": 2, "parent_id": "20"},
    {"id": "30", "name": "Chat", "type": 4, "position": 1},
    {"id": "31", "name": "general", "type": 0, "position": 0,
     "parent_id": "30"},
]


def suite_placement(r: Results) -> None:
    order = [c["name"] for c in rr.display_order(CHANNELS)]
    r.check("display order: loose channels, then each category", order,
            ["do-not-type", "📢-announcements", "server-rules", "faq",
             "general"])
    r.check("the anchor is the lowest match on screen",
            rr.pick_anchor(CHANNELS)["name"], "server-rules")
    parent, positions, anchor = rr.placement(CHANNELS, "99")
    r.check("same category as the anchor", parent, "20")
    r.check("right after it, siblings renumbered",
            [p["id"] for p in positions], ["21", "22", "99", "23"])
    r.check("positions count from 0", [p["position"] for p in positions],
            [0, 1, 2, 3])
    bare = [c for c in CHANNELS if c["id"] in ("30", "31")]
    parent, positions, anchor = rr.placement(bare, "99")
    r.ok("no anchor: top of the list, no category",
         parent is None and anchor is None and positions[0]["id"] == "99")
    r.ok("only do-not-type: right under it",
         [p["id"] for p in rr.placement(CHANNELS[:1], "99")[1]]
         == ["10", "99"])


class FakeHTTP:
    def __init__(self, channels, bulk_ok=True):
        self.channels = channels
        self.bulk_ok = bulk_ok
        self.calls = []

    async def get_guild_channels(self, gid):
        return self.channels

    async def create_guild_channel(self, gid, **kw):
        self.calls.append(("create", kw))
        return {"id": "99"}

    async def edit_channel_permissions(self, cid, oid, **kw):
        self.calls.append(("perms", oid, kw))

    def _route(self, method, path, **kw):
        return (method, path, kw)

    async def request(self, route, json=None):
        if not self.bulk_ok:
            raise RuntimeError("405")
        self.calls.append(("bulk", route, json))

    async def modify_channel(self, cid, **kw):
        self.calls.append(("modify", cid, kw))

    async def get_guild_emojis(self, gid):
        return [{"id": "1234567890", "name": "wolf"}]


def suite_channel(r: Results) -> None:
    http = FakeHTTP(CHANNELS)
    cid, created, anchor, problems = asyncio.run(
        rr.ensure_channel(http, GID, "555"))
    r.ok("a new channel is made", cid == "99" and created and not problems)
    create = [c for c in http.calls if c[0] == "create"][0][1]
    r.check("...in the anchor's category", create["parent_id"], "20")
    perms = [c for c in http.calls if c[0] == "perms"]
    everyone = next(p for p in perms if p[1] == GID)[2]
    deny = Perm(int(everyone["deny"]))
    r.ok("everyone can't post or add new emoji",
         deny & Perm.SEND_MESSAGES and deny & Perm.ADD_REACTIONS)
    r.ok("...but can read", Perm(int(everyone["allow"]))
         & Perm.READ_MESSAGE_HISTORY)
    me = next(p for p in perms if p[1] == "555")[2]
    r.ok("he can post and react there himself", me["type"] == 1 and
         Perm(int(me["allow"])) & Perm.ADD_REACTIONS)
    bulk = [c for c in http.calls if c[0] == "bulk"]
    r.ok("placed with the bulk position call", bulk and
         [p["id"] for p in bulk[0][2]] == ["21", "22", "99", "23"])

    fallback = FakeHTTP(CHANNELS, bulk_ok=False)
    asyncio.run(rr.ensure_channel(fallback, GID))
    mod = [c for c in fallback.calls if c[0] == "modify"]
    r.ok("bulk refused: anchor + 1 on the one channel",
         mod and mod[0][2]["position"] == 2)

    existing = FakeHTTP(CHANNELS + [{"id": "50", "name": "reaction-roles",
                                     "type": 0, "position": 9}])
    cid, created, _, _ = asyncio.run(rr.ensure_channel(existing, GID))
    r.ok("an existing #reaction-roles is adopted untouched",
         cid == "50" and not created and not existing.calls)


def suite_store(r: Results) -> None:
    s = rr.ReactionRoleStore(FakeSettings())
    s.create(GID, "m1", "c1", {"🎮": {"role": "1", "emoji": "🎮"}})
    r.check("found by message", s.find("m1"), (GID, s.get(GID, "m1")))
    r.check("role for an emoji", s.role_for(GID, "m1", "🎮"), "1")
    r.check("unmapped emoji: nothing", s.role_for(GID, "m1", "🎨"), None)
    added, replaced = s.merge(GID, "m1", [
        (rr.Emoji("🎨", "🎨"), "2"), (rr.Emoji("🎮", "🎮"), "3")])
    r.check("merge adds new emoji", added, ["🎨"])
    r.check("...and re-points an existing one", replaced, ["🎮"])
    r.check("one emoji, one role", s.role_for(GID, "m1", "🎮"), "3")
    time.sleep(0.01)
    s.create(GID, "m2", "c2", {})
    r.check("newest message is the default target", s.target(GID), "m2")
    r.check("...or the newest in a named channel", s.target(GID, "c1"), "m1")
    r.check("none in that channel", s.target(GID, "c9"), None)
    r.check("remove one", s.remove(GID, "m1", "🎨")["role"], "2")
    r.ok("deleting the message forgets it", s.drop_message("m2"))
    r.check("...and only it", list(s.messages(GID)), ["m1"])
    r.ok("an unknown message is not ours", not s.drop_message("zz"))

    # Reaction routing reads the gateway emoji the way bot.py does.
    custom = types.SimpleNamespace(id=1234567890, name="wolf", unicode=None)
    plain = types.SimpleNamespace(id=None, name=":video_game:",
                                  unicode="🎮️")
    r.check("a custom reaction keys by id", rr.event_key(custom),
            "1234567890")
    r.check("a unicode reaction keys by the character",
            rr.event_key(plain), "🎮")
    r.check("...which maps to the role", s.role_for(GID, "m1",
                                                    rr.event_key(plain)), "3")


class FakeAdapter:
    def __init__(self):
        self.client = types.SimpleNamespace(
            user=types.SimpleNamespace(id="555"))
        self.sent, self.edits, self.reacts, self.unreacts = [], [], [], []
        self.forgot = []
        self.http = FakeHTTP([])

    def _rest(self):
        return self.http

    def forget_roles(self, gid):
        self.forgot.append(gid)

    async def role_index(self, gid):
        return ROLES

    async def fetch_member(self, gid, uid):
        return member("555", "5")

    async def send(self, cid, response):
        self.sent.append((cid, response))
        return f"m{len(self.sent)}"

    async def edit_message(self, cid, mid, response):
        self.edits.append((cid, mid, response))
        return True

    async def add_reaction(self, cid, mid, emoji):
        self.reacts.append(emoji)
        return True

    async def remove_user_reaction(self, cid, mid, emoji, uid):
        self.unreacts.append((emoji, uid))
        return True

    async def channel_names(self, ids):
        return {i: "reaction-roles" for i in ids}


def suite_adding(r: Results) -> None:
    adapter, store = FakeAdapter(), rr.ReactionRoleStore(FakeSettings())
    pairs, _ = rr.parse_pairs("🎮 Gamer\n🔨 Mod")
    reply, n = asyncio.run(cmds.apply(
        adapter, store, guild_id=GID, channel_id="c1", message_id=None,
        pairs=pairs, invoker=ADMIN))
    r.check("one valid pair applied", n, 1)
    r.check("the role cache is dropped first, so a just-moved role "
            "isn't refused on stale positions", adapter.forgot, [GID])
    r.ok("the refused one is explained", "moderation" in reply)
    r.check("a message was posted", len(adapter.sent), 1)
    r.check("and he reacted", adapter.reacts, ["🎮"])
    mid = store.target(GID)

    pairs, _ = rr.parse_pairs("🎨 Artist")
    asyncio.run(cmds.apply(adapter, store, guild_id=GID, channel_id="c1",
                           message_id=mid, pairs=pairs, invoker=ADMIN))
    r.check("adding to it edits, no second message", len(adapter.sent), 1)
    r.check("only the new reaction is added", adapter.reacts, ["🎮", "🎨"])
    shown = adapter.edits[-1][2].embed.description
    r.ok("the edited embed lists both", "<@&1>" in shown and "<@&2>" in shown)

    ctx = types.SimpleNamespace(
        guild_id=GID, invoker=ADMIN, channel=types.SimpleNamespace(id="c0"),
        extras={"adapter": adapter, "reaction_roles": store,
                "raw_text": "Corbin, add :wolf: Night Owls to the reaction "
                            "roles"})
    reply = asyncio.run(cmds.reaction_roles_add(ctx, pairs="Night Owls"))
    r.ok("the add command falls back to the person's own words",
         "<@&3>" in reply)
    r.ok("...with the custom emoji resolved", WOLF in adapter.reacts)

    reply = asyncio.run(cmds.reaction_roles_remove(ctx, target="🎨"))
    r.ok("remove by emoji", "Took 🎨" in reply)
    r.check("he takes his own reaction off", adapter.unreacts, [("🎨", "555")])
    r.check("the mapping is gone", store.role_for(GID, mid, "🎨"), None)
    reply = asyncio.run(cmds.reaction_roles_remove(ctx, target="Night Owls"))
    r.ok("remove by role name", "<@&3>" in reply)


def suite_question(r: Results) -> None:
    adapter, store = FakeAdapter(), rr.ReactionRoleStore(FakeSettings())
    questions = {}
    extras = {"reaction_role_questions": questions}

    def go(text, who=ADMIN, channel="c0"):
        return asyncio.run(cmds.answer(
            questions, store, adapter, guild_id=GID, channel_id=channel,
            member=who, raw_content=text))

    r.check("nobody asked: not handled", go("🎮 Gamer"), None)
    cmds.ask(extras, channel_id="c0", user_id="u1", guild_id=GID,
             target="c1", message_id=None)
    r.check("someone else's message: not handled",
            go("🎮 Gamer", who=member("u2", "1")), None)
    r.check("chat without pairs falls through", go("one sec"), None)
    reply = go("🔨 Mod")
    r.ok("nothing usable: explained, still open",
         "Try again" in reply and questions)
    reply = go("🎮 Gamer\n🎨 Artist")
    r.ok("an answer sets them up", "<@&1>" in reply and "<@&2>" in reply)
    r.check("and closes the question", questions, {})

    cmds.ask(extras, channel_id="c0", user_id="u1", guild_id=GID,
             target="c1", message_id=store.target(GID))
    go("<:wolf:1234567890> Night Owls")
    r.check("re-running adds to the same message", len(adapter.sent), 1)

    cmds.ask(extras, channel_id="c0", user_id="u1", guild_id=GID,
             target="c1", message_id=None)
    r.ok("cancel closes it", "dropped" in go("cancel") and not questions)
    cmds.ask(extras, channel_id="c0", user_id="u1", guild_id=GID,
             target="c1", message_id=None)
    questions[("c0", "u1")]["deadline"] = time.monotonic() - 1
    r.ok("an expired question lets go",
         go("🎮 Gamer") is None and not questions)


class MemberHTTP(FakeHTTP):
    """Answers the member list with ``members`` ({user id: [role ids]})."""

    def __init__(self, members, fail=False):
        super().__init__([])
        self.members, self.fail = members, fail

    async def request(self, route, json=None):
        if self.fail:
            raise RuntimeError("403")
        if "/members" in route[1]:
            return [{"user": {"id": u}, "roles": list(rs)}
                    for u, rs in self.members.items()]
        return await super().request(route, json)


class DMAdapter(FakeAdapter):
    def __init__(self, members):
        super().__init__()
        self.http = MemberHTTP(members)
        self.removed, self.dms = [], []

    async def remove_role(self, gid, uid, rid):
        self.removed.append((uid, rid))
        self.http.members[uid] = [r for r in self.http.members[uid]
                                  if r != rid]

    async def send_dm(self, uid, response):
        self.dms.append((uid, response.content))
        return "dm1"


def suite_dm_optin(r: Results) -> None:
    from corbin.commands import util_cmds

    members = {"10": [], "11": ["3"], "12": []}
    adapter = DMAdapter(members)
    store = rr.ReactionRoleStore(FakeSettings())
    ctx = types.SimpleNamespace(
        guild_id=GID, invoker=ADMIN, channel=types.SimpleNamespace(id="c0"),
        guild=types.SimpleNamespace(name="The Den"),
        extras={"adapter": adapter, "reaction_roles": store,
                "raw_text": ""})
    reply = asyncio.run(cmds.reaction_roles_dm(ctx, pairs="📬 Gamer"))
    r.ok("no reaction-roles message yet: says to set one up first",
         "set up reaction roles" in reply)

    pairs, _ = rr.parse_pairs("🎮 Gamer")
    asyncio.run(cmds.apply(adapter, store, guild_id=GID, channel_id="c1",
                           message_id=None, pairs=pairs, invoker=ADMIN))
    mid = store.target(GID)
    reply = asyncio.run(cmds.reaction_roles_dm(ctx, pairs="📬 Night Owls"))
    r.ok("a role someone already holds is refused: they never opted in",
         "1 person already has" in reply
         and not store.is_dm_role(GID, "3"))

    reply = asyncio.run(cmds.reaction_roles_dm(ctx, pairs="📬 Artist"))
    r.ok("a role nobody holds becomes an opt-in DM role",
         store.is_dm_role(GID, "2") and "Done" in reply)
    shown = adapter.edits[-1][2].embed
    r.ok("the message says what reacting for it means",
         "DMs from staff" in shown.description
         and "your yes" in shown.footer)
    r.check("a plain reaction role isn't a DM role",
            store.is_dm_role(GID, "1"), False)

    pairs, _ = rr.parse_pairs("🖌️ Artist")
    reply, n = asyncio.run(cmds.apply(
        adapter, store, guild_id=GID, channel_id="c1", message_id=mid,
        pairs=pairs, invoker=ADMIN))
    r.ok("a second, unlabelled emoji for a DM role is refused",
         n == 0 and "keeps its one emoji" in reply)
    pairs, _ = rr.parse_pairs("📬 Artist")
    asyncio.run(cmds.apply(adapter, store, guild_id=GID, channel_id="c1",
                           message_id=mid, pairs=pairs, invoker=ADMIN))
    r.ok("re-adding it as it was keeps the mark",
         store.is_dm_role(GID, "2"))

    # Consent comes only from the member's own reaction.
    store.set_consent(GID, "2", "10", True)
    members["10"].append("2")
    members["12"].append("2")          # a moderator gave it to 12
    r.check("the guard flags a role given without a reaction",
            store.unconsented(GID, "12", ["1", "2"]), ["2"])
    r.check("...and leaves the one who reacted alone",
            store.unconsented(GID, "10", ["2"]), [])
    r.check("...and plain roles alone", store.unconsented(GID, "12", ["1"]),
            [])

    reply = asyncio.run(util_cmds.messager(ctx, role="1", message="hi"))
    r.ok("messager refuses a role that isn't opt-in",
         "isn't an opt-in DM role" in reply and not adapter.dms)
    reply = asyncio.run(util_cmds.messager(ctx, role="2", message="hi"))
    r.check("messager DMs only the person who opted in",
            [u for u, _ in adapter.dms], ["10"])
    r.ok("the DM says why they got it and how to stop",
         "Take that reaction off" in adapter.dms[0][1])
    r.check("the mod-given holder had the role taken back",
            adapter.removed, [("12", "2")])
    r.ok("and the reply says so", "without opting in" in reply)

    store.set_consent(GID, "2", "10", False)
    r.check("taking the reaction off withdraws consent",
            store.consented(GID, "2"), set())

    adapter.http.fail = True
    reply = asyncio.run(util_cmds.messager(ctx, role="2", message="hi"))
    r.ok("an unreadable member list sends nothing, and says so",
         "sent nothing" in reply and len(adapter.dms) == 1)
    adapter.http.fail = False

    store.set_consent(GID, "2", "10", True)
    reply = asyncio.run(cmds.reaction_roles_remove(ctx, target="📬"))
    r.ok("taking it off the message unmarks it",
         not store.is_dm_role(GID, "2") and "no longer" in reply)
    r.check("...and forgets who had opted in",
            store._consents(GID), {})
    reply = asyncio.run(util_cmds.messager(ctx, role="2", message="hi"))
    r.ok("so the messager won't DM it any more",
         "isn't an opt-in DM role" in reply)


SUITES = [
    ("reaction roles: parsing", suite_parsing),
    ("reaction roles: role checks", suite_roles),
    ("reaction roles: placement", suite_placement),
    ("reaction roles: channel", suite_channel),
    ("reaction roles: store and events", suite_store),
    ("reaction roles: adding to a message", suite_adding),
    ("reaction roles: the question", suite_question),
    ("reaction roles: opt-in DM roles and the messager", suite_dm_optin),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
