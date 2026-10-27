"""Offline suite for the executor, stores, and adapter conversion."""
from __future__ import annotations
import asyncio, json, logging, tempfile
from pathlib import Path

logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, member, run

from corbin.ai.classifier import ScriptedClassifier
from corbin.core.context import (Channel, Embed, Guild, Platform, Response,
                                 Surface)
from corbin.core.dispatch import DispatchResult, Dispatcher, Intent
from corbin.core.permissions import Perm
from corbin.core.registry import Param, Registry, Risk
from corbin.core.risk import Decision
from corbin.core.stores import ActivityStore, WarningStore
from corbin.platforms.fluxer.adapter import _as_dt, to_member, to_message, to_user
from corbin.runtime import Executor

CHANNEL = Channel(id="c1", name="general", guild_id="g1")
GUILD = Guild(id="g1", name="Test")


class FakeAdapter:
    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.sent: list[Response] = []
        self.fail = fail

    async def ban(self, gid, uid, *, reason=None, delete_message_seconds=0):
        if self.fail == "ban":
            raise PermissionError("missing Ban Members")
        self.calls.append(("ban", gid, uid, reason))

    async def unban(self, gid, uid):
        self.calls.append(("unban", gid, uid))

    async def kick(self, gid, uid, *, reason=None):
        self.calls.append(("kick", gid, uid, reason))

    async def purge(self, cid, count):
        if self.fail == "purge":
            raise RuntimeError("boom")
        self.calls.append(("purge", cid, count))
        return count

    async def send(self, channel_id, response):
        self.sent.append(response)
        return "m1"

    async def fetch_member(self, gid, uid):
        return None


def build() -> tuple[Registry, FakeAdapter, Executor, Dispatcher]:
    reg = Registry()

    @reg.command(name="ban", description="Ban", risk=Risk.DESTRUCTIVE,
                 permission=Perm.BAN_MEMBERS,
                 params=(Param("user", "who", kind="user"),
                         Param("reason", "why", required=False)))
    async def ban(ctx, user, reason=None):
        await ctx.extras["adapter"].ban(ctx.guild_id, user, reason=reason)
        return f"Banned {user}"

    @reg.command(name="kick", description="Kick", risk=Risk.DESTRUCTIVE,
                 permission=Perm.KICK_MEMBERS,
                 params=(Param("user", "who", kind="user"),))
    async def kick(ctx, user):
        await ctx.extras["adapter"].kick(ctx.guild_id, user)
        return f"Kicked {user}"

    @reg.command(name="clear", description="Clear", risk=Risk.DESTRUCTIVE,
                 permission=Perm.MANAGE_MESSAGES,
                 params=(Param("count", "n", kind="integer"),))
    async def clear(ctx, count):
        n = await ctx.extras["adapter"].purge(ctx.channel.id, count)
        return f"Cleared {n}"

    @reg.command(name="card", description="Show a card", risk=Risk.SAFE)
    async def card(ctx):
        return Embed(title="Card", description="body")

    adapter = FakeAdapter()
    dispatcher = Dispatcher(reg, ScriptedClassifier())
    return reg, adapter, Executor(adapter, dispatcher,
                                  platform=Platform.FLUXER), dispatcher


def suite_executor(r: Results) -> None:
    reg, adapter, ex, disp = build()
    mod = member("mod", perms=Perm.BAN_MEMBERS | Perm.KICK_MEMBERS
                 | Perm.MANAGE_MESSAGES, position=10)

    def go(spec_name, args, decision=Decision.EXECUTE):
        result = DispatchResult(handled=True, decision=decision,
                                spec=reg.get(spec_name), args=args)
        return asyncio.run(ex.run(result, invoker=mod, channel=CHANNEL,
                                  guild=GUILD))

    r.check("ban reports back", go("ban", {"user": "u9", "reason": "spam"}),
            "Banned u9")
    r.check("ban reached the adapter", adapter.calls[-1],
            ("ban", "g1", "u9", "spam"))
    r.check("optional arg omitted cleanly", go("ban", {"user": "u8"}), "Banned u8")

    # undo registration
    r.check("ban registers an undo", len(disp.store), 1)
    entry = disp.store.take_undo("c1", "mod")
    r.ok("undo entry belongs to invoker", entry is not None)
    asyncio.run(entry.undo())
    r.check("undo actually unbans", adapter.calls[-1], ("unban", "g1", "u8"))

    _, adapter2, ex2, disp2 = build()
    result = DispatchResult(handled=True, decision=Decision.EXECUTE,
                            spec=reg.get("kick"), args={"user": "u7"})
    asyncio.run(ex2.run(result, invoker=mod, channel=CHANNEL, guild=GUILD))
    r.check("kick offers no fake undo", len(disp2.store), 0)

    # embeds are sent, not stringified
    reg3, adapter3, ex3, _ = build()
    out = asyncio.run(ex3.run(
        DispatchResult(handled=True, decision=Decision.EXECUTE,
                       spec=reg3.get("card"), args={}),
        invoker=mod, channel=CHANNEL, guild=GUILD))
    r.check("embed handler returns no text", out, None)
    r.check("embed was sent", len(adapter3.sent), 1)
    r.ok("embed carried through", adapter3.sent[0].embed.title == "Card")

    # failures are reported, never swallowed
    reg4, adapter4, ex4, _ = build()
    adapter4.fail = "ban"
    out = asyncio.run(ex4.run(
        DispatchResult(handled=True, decision=Decision.EXECUTE,
                       spec=reg4.get("ban"), args={"user": "u1"}),
        invoker=mod, channel=CHANNEL, guild=GUILD))
    r.ok("platform refusal surfaces to the user", "wouldn't let me" in out)

    reg5, adapter5, ex5, _ = build()
    adapter5.fail = "purge"
    out = asyncio.run(ex5.run(
        DispatchResult(handled=True, decision=Decision.EXECUTE,
                       spec=reg5.get("clear"), args={"count": 5}),
        invoker=mod, channel=CHANNEL, guild=GUILD))
    r.ok("handler crash surfaces to the user", "That failed" in out)
    r.ok("crash names the exception", "RuntimeError" in out)

    # non-execute decisions pass their message through untouched
    for decision, msg in ((Decision.CONFIRM, "sure?"), (Decision.DENIED, "no perms")):
        out = asyncio.run(ex.run(
            DispatchResult(handled=True, decision=decision, spec=reg.get("ban"),
                           args={}, message=msg),
            invoker=mod, channel=CHANNEL, guild=GUILD))
        r.check(f"{decision.value} passes its message", out, msg)


def suite_stores(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "spamblock_warnings.json").write_text(json.dumps(
            {"g1": {"u1": [{"id": "a"}, {"id": "b"}], "u2": []}}))
        w = WarningStore(root)
        r.check("counts warnings", w.count("g1", "u1"), 2)
        r.check("empty list is zero", w.count("g1", "u2"), 0)
        r.check("unknown user is zero", w.count("g1", "nobody"), 0)
        r.check("unknown guild is zero", w.count("g9", "u1"), 0)
        r.check("no guild is zero", w.count(None, "u1"), 0)

        udir = root / "user_data" / "g1"
        udir.mkdir(parents=True)
        (udir / "u1.json").write_text(json.dumps({"messages": 1234, "xp": 99}))
        (udir / "u3.json").write_text("{ this is not json")
        a = ActivityStore(root)
        r.check("reads message count", a.message_count("g1", "u1"), 1234)
        r.check("reads xp", a.xp("g1", "u1"), 99)
        r.check("missing file is zero", a.message_count("g1", "u2"), 0)
        r.check("corrupt file is zero, not a crash", a.message_count("g1", "u3"), 0)

        missing = WarningStore(root / "nope")
        r.check("missing store file is zero", missing.count("g1", "u1"), 0)


def suite_conversion(r: Results) -> None:
    class U:
        id = 5; username = "pip"; bot = False
        created_at = "2024-01-05T00:00:00Z"

    class Role_:
        id = 7; name = "Mod"; permissions = int(Perm.BAN_MEMBERS); position = 3

    class M:
        user = U(); roles = [Role_()]; joined_at = "2025-06-01T00:00:00+00:00"

    u = to_user(U())
    r.check("user id stringified", u.id, "5")
    r.check("display falls back to username", u.display_name, "pip")
    r.ok("created_at parsed", u.created_at is not None)

    m = to_member(M(), guild_id="g1", guild_owner_id="99")
    r.check("roles converted", [x.name for x in m.roles], ["Mod"])
    r.check("permissions folded", m.permissions, Perm.BAN_MEMBERS)
    r.ok("not owner", not m.is_guild_owner)
    r.ok("owner detected", to_member(M(), guild_id="g1",
                                     guild_owner_id="5").is_guild_owner)

    r.check("missing fields degrade, not crash", to_user(object()).name, "unknown")
    r.ok("iso Z parsed", _as_dt("2024-01-05T00:00:00Z") is not None)
    r.ok("epoch parsed", _as_dt(1700000000) is not None)
    r.ok("garbage date is None", _as_dt("last tuesday") is None)
    r.ok("None date is None", _as_dt(None) is None)

    class Msg:
        id = 1; content = "hi"; author = U(); channel = None
        channel_id = 42; attachments = []; guild_id = 7
        created_at = None; reference = None

    from corbin.platforms.fluxer.adapter import to_attachment
    a = to_attachment({"url": "http://x/a.png", "filename": "a.png", "size": 9})
    r.check("dict attachment url", a.url, "http://x/a.png")
    r.check("dict attachment name", a.filename, "a.png")
    r.check("dict attachment size", a.size, 9)

    class Att:
        url = "http://y/b.png"; filename = "SPOILER_b.png"
        content_type = "image/png"; size = 4

    b = to_attachment(Att())
    r.check("object attachment url", b.url, "http://y/b.png")
    r.ok("spoiler detected", b.is_spoiler)

    msg = to_message(Msg())
    r.check("message content", msg.content, "hi")
    r.check("channel id fallback", msg.channel.id, "42")
    r.check("no reference", msg.reference_id, None)


def suite_typing(r: Results) -> None:
    from corbin.platforms.fluxer.adapter import FluxerAdapter, TYPING_COOLDOWN

    class Chan:
        def __init__(self, fail=False):
            self.fail = fail; self.hits = 0
        async def trigger_typing(self):
            self.hits += 1
            if self.fail:
                raise RuntimeError("429 rate limited")

    class Client:
        def __init__(self, chan): self.chan = chan
        def get_channel(self, cid): return self.chan

    good = Chan()
    a = FluxerAdapter(Client(good))
    r.ok("typing fires", asyncio.run(a.trigger_typing("c1")))
    r.check("one request sent", good.hits, 1)

    bad = Chan(fail=True)
    b = FluxerAdapter(Client(bad))
    r.ok("failure returns False, never raises",
         asyncio.run(b.trigger_typing("c2")) is False)
    r.ok("circuit breaker armed", "c2" in b._typing_disabled_until)
    r.ok("second attempt short-circuits",
         asyncio.run(b.trigger_typing("c2")) is False)
    r.check("no second request while broken", bad.hits, 1)
    r.ok("cooldown is 15 minutes", TYPING_COOLDOWN == 900.0)

    # a broken channel must not disable a healthy one
    r.ok("breaker is per-channel", "c1" not in b._typing_disabled_until)

    class NoTyping:
        pass

    c = FluxerAdapter(Client(NoTyping()))
    r.ok("missing method degrades quietly",
         asyncio.run(c.trigger_typing("c3")) is False)

    d = FluxerAdapter(Client(Chan()))
    async def _block():
        async with d.typing("c9"):
            await asyncio.sleep(0.01)
        return True
    r.ok("context manager enters and exits cleanly", asyncio.run(_block()))

    # Typing starts the moment a message arrives, so entering must not
    # wait on the network, and the handler's block nests the inner ones.
    class SlowChan(Chan):
        async def trigger_typing(self):
            self.hits += 1
            await asyncio.sleep(0.2)

    slow = SlowChan()
    e = FluxerAdapter(Client(slow))

    async def _nested():
        loop = asyncio.get_running_loop()
        began = loop.time()
        async with e.typing("c1"):
            entered = loop.time() - began
            await asyncio.sleep(0)
            async with e.typing("c1"):
                await asyncio.sleep(0.01)
            inner_left = "c1" in e._typing_tasks
        return entered, inner_left, "c1" in e._typing_tasks

    entered, inner_left, still = asyncio.run(_nested())
    r.ok("entering does not wait for the request", entered < 0.05)
    r.ok("an inner block keeps the outer indicator", inner_left)
    r.check("nesting shares one request", slow.hits, 1)
    r.ok("the last exit stops it", not still)
    r.check("no refs left behind", e._typing_refs, {})


def suite_guild_resolution(r: Results) -> None:
    from corbin.platforms.fluxer.adapter import _message_guild_id, to_channel

    class G: id = 77
    class Chan:
        guild_id = 77; id = 5; name = "general"; type = 0
    class MsgCached:
        guild_id = 77; channel = Chan()
    class MsgUncached:
        # what fluxer 0.4.2 actually hands us on a channel-cache miss
        guild_id = None; channel = Chan()
    class MsgPrivateGuild:
        guild_id = None; _guild = G(); channel = None
    class MsgDM:
        guild_id = None; channel = None

    r.check("direct guild_id used", _message_guild_id(MsgCached(), Chan()), "77")
    r.check("falls back to channel.guild_id",
            _message_guild_id(MsgUncached(), Chan()), "77")
    r.check("falls back to _guild.id",
            _message_guild_id(MsgPrivateGuild(), None), "77")
    r.check("genuine DM stays None", _message_guild_id(MsgDM(), None), None)

    # A channel without a guild_id must NOT be reported as a DM: that is what
    # filed guild settings under the literal key "None".
    class NoGuildChan:
        id = 9; name = "x"; guild_id = None; type = 0
    r.ok("missing guild_id is not a DM", not to_channel(NoGuildChan()).is_dm)

    class RealDM:
        id = 9; name = "x"; guild_id = None; type = 1; is_dm = True
    r.ok("explicit DM flag respected", to_channel(RealDM()).is_dm)


def suite_weather_params(r: Results) -> None:
    from corbin.commands.info import _location_params

    r.check("bare city", _location_params("Oslo"), {"q": "Oslo"})
    r.check("US state gets a country",
            _location_params("Denver, CO"), {"q": "Denver,CO,US"})
    r.check("lowercase state normalised",
            _location_params("denver, co"), {"q": "denver,CO,US"})
    r.check("postcode uses zip=, not q=",
            _location_params("80202"), {"zip": "80202,US"})
    r.check("explicit country preserved",
            _location_params("Portland, OR, US"), {"q": "Portland,OR,US"})
    r.check("non-US two-part left alone",
            _location_params("Paris, FR"), {"q": "Paris, FR"})
    r.check("empty is harmless", _location_params(""), {"q": ""})


def suite_local_cleanup(r: Results) -> None:
    from corbin.ai.ollama import clean_output, strip_pause_dashes

    r.check("strips trailing think tag",
            clean_output("Hello.\n</think>"), "Hello.")
    r.check("strips a whole think block",
            clean_output("<think>hidden</think>Visible."), "Visible.")
    r.check("leaves clean text alone", clean_output("Just text."), "Just text.")
    r.check("empty stays empty", clean_output(""), "")

    r.check("em-dash pause becomes a comma",
            strip_pause_dashes("after midnight — it is different"),
            "after midnight, it is different")
    r.check("en-dash pause too",
            strip_pause_dashes("Eliot – the way he writes"),
            "Eliot, the way he writes")
    r.check("numeric range survives",
            strip_pause_dashes("1939—1945"), "1939—1945")
    r.check("hyphenated word survives",
            strip_pause_dashes("well-known"), "well-known")
    r.check("unspaced dash between words survives",
            strip_pause_dashes("one—two"), "one—two")
    r.check("no double comma after terminal punctuation",
            strip_pause_dashes("Done. — Next"), "Done. Next")
    r.check("no dash is a no-op",
            strip_pause_dashes("plain text"), "plain text")
    r.check("empty is safe", strip_pause_dashes(""), "")
    r.ok("cleanup runs dash-stripping too",
         "—" not in clean_output("a — b"))


def suite_degenerate_output(r: Results) -> None:
    """The two loops the local model actually posted, and normal replies."""
    from corbin.ai.ollama import degenerate_reason

    lobotomy = (
        "Yeah, I've been doing lobotomies on my ravens for a while, it's "
        "just the standard way to keep them healthy.\n\nI've been doing "
        "lobotomies on my corvids for a while, and I've been doing "
        "lobotomies on my corvids for a while. It's just a thing I do. "
        "I've been doing lobotomies on my corvids for a while, and I've "
        "been doing lobotomies on my corvids for a while. It's just a "
        "thing I do.")
    aquarium = (
        "For plants, I'd suggest:\n"
        "- A few different colored flowers (red, blue, green)\n"
        "- A couple of different colored mushrooms\n"
        "- A few different colored mushrooms with different colored flowers\n"
        "- A few different colored mushrooms with different colored flowers "
        "and mushrooms\n\nFor fish, I'd suggest:\n"
        "- A few different colored fish (red, blue, green)\n"
        "- A couple of different colored fish with different colored flowers\n"
        "- A few different colored fish with different colored flowers and "
        "mushrooms\n\nI'd suggest starting with a few different colored "
        "flowers and a few different colored fish.")
    r.ok("sentence loop is caught", bool(degenerate_reason(lobotomy)))
    r.ok("phrase-soup list is caught", bool(degenerate_reason(aquarium)))

    good = {
        "advice": (
            "For a 10 gallon, java fern and anubias are the easy wins since "
            "you tie them to rock or driftwood, and they barely need light. "
            "For fish, a small school of chili rasboras or ember tetras, six "
            "or so, plus a few pygmy corydoras. Cycle the tank for a few "
            "weeks first or you'll be fighting ammonia spikes all month."),
        "list": (
            "Good starter fish for a 10 gallon:\n- 6 ember tetras, they "
            "school and stay small\n- 6 chili rasboras, tiny and colorful\n"
            "- 3 pygmy corydoras for the bottom\n- a few amano shrimp for "
            "algae\n- one nerite snail for the glass\nKeep stocking light "
            "at first and add the shrimp last."),
        "comparison": (
            "The 4070 has 12GB of VRAM and the 4080 has 16GB of VRAM. The "
            "4070 draws about 200W under load and the 4080 draws about 320W "
            "under load. For 1440p the 4070 is plenty, for 4K I'd pick the "
            "4080, and the gap is roughly four hundred dollars right now."),
        "short": "Yep.",
        "empty": "",
    }
    for name, text in good.items():
        r.check(f"normal reply passes: {name}", degenerate_reason(text), None)


def suite_embed_limits(r: Results) -> None:
    """Fluxer enforces Discord-identical embed limits as hard 400s.

    Measured 2026-09-23: field value >1024, description >4096 or title >256
    rejects the ENTIRE message. /my_memory with 21 facts hit this and the
    user simply got nothing.
    """
    from corbin.core.context import (EMBED_FIELD_VALUE_LIMIT, Embed,
                                     EMBED_TITLE_LIMIT, clip)
    from corbin.platforms.fluxer.adapter import render_embed

    r.check("clip leaves short text", clip("abc", 10), "abc")
    r.check("clip trims to the limit", len(clip("x" * 100, 10)), 10)
    r.ok("clip marks the cut", clip("x" * 100, 10).endswith("\u2026"))
    r.check("clip passes None through", clip(None, 10), None)

    e = Embed(title="t")
    e.add_list_field("Facts", [f"fact number {i} " + "y" * 60
                               for i in range(30)])
    r.ok("splits into several fields", len(e.fields) > 1)
    r.ok("every field within the limit",
         all(len(f.value) <= EMBED_FIELD_VALUE_LIMIT for f in e.fields))
    r.check("first keeps the plain name", e.fields[0].name, "Facts")
    r.check("overflow is numbered", e.fields[1].name, "Facts (2)")
    joined = "\n".join(f.value for f in e.fields)
    r.ok("no content lost in the split",
         all(f"fact number {i} " in joined for i in range(30)))

    single = Embed(title="t")
    single.add_list_field("F", ["short"])
    r.check("one short line is one field", len(single.fields), 1)
    empty = Embed(title="t")
    empty.add_list_field("F", [])
    r.check("no lines adds no field", len(empty.fields), 0)

    over = Embed(title="x" * 400, description="d" * 5000)
    over.add_field("n" * 400, "v" * 2000)
    built = render_embed(over, _FakeEmbed)
    r.ok("title clipped at the boundary",
         len(built.kwargs["title"]) <= EMBED_TITLE_LIMIT)
    r.ok("description clipped", len(built.kwargs["description"]) <= 4096)
    r.ok("field value clipped",
         all(len(f["value"]) <= EMBED_FIELD_VALUE_LIMIT
             for f in built.added))
    r.ok("field name clipped",
         all(len(f["name"]) <= 256 for f in built.added))

    blank = Embed(title="t")
    blank.add_field("n", "")
    built2 = render_embed(blank, _FakeEmbed)
    r.ok("empty field value replaced (also a 400)",
         built2.added[0]["value"] != "")

    many = Embed(title="t")
    for i in range(40):
        many.add_field(f"f{i}", "v")
    r.ok("field count capped at 25",
         len(render_embed(many, _FakeEmbed).added) <= 25)


class _FakeEmbed:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.added = []

    def add_field(self, name=None, value=None, inline=False):
        self.added.append({"name": name, "value": value, "inline": inline})
        return self


def suite_private_notice(r: Results) -> None:
    from corbin.core.context import Response
    from corbin.platforms.fluxer.adapter import FluxerAdapter

    class Chan:
        def __init__(self): self.sent = []
        async def send(self, **kw):
            self.sent.append(kw)
            return type("M", (), {"id": "m1"})()

    class Client:
        def __init__(self, chan, user): self.chan, self._u = chan, user
        def get_channel(self, cid): return self.chan
        def get_user(self, uid): return self._u

    class User:
        def __init__(self, ok=True): self.ok, self.dms = ok, []
        async def send(self, **kw):
            if not self.ok:
                raise RuntimeError("DMs closed")
            self.dms.append(kw)
            return type("M", (), {"id": "dm1"})()

    # DM succeeds -> content goes to the DM, a short notice to the channel.
    chan, user = Chan(), User(ok=True)
    a = FluxerAdapter(Client(chan, user))
    resp = Response(content="secret facts", private=True)
    setattr(resp, "_target_user_id", "u1")
    r.check("returns the DM id", asyncio.run(a.send("c1", resp)), "dm1")
    r.check("content went to the DM", user.dms[0]["content"], "secret facts")
    r.check("channel got exactly one notice", len(chan.sent), 1)
    r.check("notice is the pointer, not the content",
            chan.sent[0]["content"], "Sent that to your DMs.")
    r.ok("secret never hit the channel",
         all("secret facts" not in str(m) for m in chan.sent))

    # Custom wording.
    chan2, user2 = Chan(), User(ok=True)
    a2 = FluxerAdapter(Client(chan2, user2))
    resp2 = Response(content="x", private=True, private_notice="Check DMs.")
    setattr(resp2, "_target_user_id", "u1")
    asyncio.run(a2.send("c1", resp2))
    r.check("custom notice used", chan2.sent[0]["content"], "Check DMs.")

    # Suppressed notice.
    chan3, user3 = Chan(), User(ok=True)
    a3 = FluxerAdapter(Client(chan3, user3))
    resp3 = Response(content="x", private=True, private_notice="")
    setattr(resp3, "_target_user_id", "u1")
    asyncio.run(a3.send("c1", resp3))
    r.check("empty notice posts nothing", len(chan3.sent), 0)

    # DM fails -> falls back to the channel, and must NOT also post a notice
    # pointing at a DM that never arrived.
    chan4, user4 = Chan(), User(ok=False)
    a4 = FluxerAdapter(Client(chan4, user4))
    resp4 = Response(content="fallback body", private=True)
    setattr(resp4, "_target_user_id", "u1")
    asyncio.run(a4.send("c1", resp4))
    r.check("content posted in channel", len(chan4.sent), 1)
    r.check("it is the body, not a notice",
            chan4.sent[0]["content"], "fallback body")
    r.ok("user marked DM-blocked", "u1" in a4._dm_blocked)

    # Non-private responses are untouched.
    chan5, user5 = Chan(), User()
    a5 = FluxerAdapter(Client(chan5, user5))
    asyncio.run(a5.send("c1", Response(content="public")))
    r.check("public message posts once", len(chan5.sent), 1)
    r.check("no DM sent", len(user5.dms), 0)


def suite_back_link(r: Results) -> None:
    from corbin.core.context import Embed, Response
    from corbin.platforms.fluxer.adapter import FluxerAdapter, channel_url

    r.check("builds a channel url", channel_url("g1", "c1"),
            "https://web.fluxer.app/channels/g1/c1")
    r.check("includes a message when given",
            channel_url("g1", "c1", "m1"),
            "https://web.fluxer.app/channels/g1/c1/m1")
    r.check("no guild means no link", channel_url(None, "c1"), None)
    r.check("no channel means no link", channel_url("g1", ""), None)

    class Chan:
        guild_id = "g1"; id = "c1"; name = "general"
        def __init__(self): self.sent = []
        async def send(self, **kw):
            self.sent.append(kw); return type("M", (), {"id": "m"})()

    class Client:
        def __init__(self, chan): self.chan = chan
        def get_channel(self, cid): return self.chan

    a = FluxerAdapter(Client(Chan()))

    plain = Response(content="your facts")
    out = asyncio.run(a._with_back_link(plain, "c1"))
    r.ok("link appended to text", "web.fluxer.app/channels/g1/c1" in out.content)
    r.ok("original text kept", out.content.startswith("your facts"))
    r.check("original not mutated", plain.content, "your facts")

    emb = Embed(title="Memory")
    emb.add_field("Facts", "a")
    withemb = Response(embed=emb)
    out2 = asyncio.run(a._with_back_link(withemb, "c1"))
    names = [f.name for f in out2.embed.fields]
    r.ok("embed gets a jump field", "Asked in" in names)
    r.ok("link is markdown so it renders clickable",
         any("](https://" in f.value for f in out2.embed.fields))
    r.check("original embed untouched", len(emb.fields), 1)

    class NoGuild(Chan):
        guild_id = None

    b = FluxerAdapter(Client(NoGuild()))
    plain2 = Response(content="hi")
    out3 = asyncio.run(b._with_back_link(plain2, "c1"))
    r.check("a DM with no resolvable guild is unchanged", out3.content, "hi")


def suite_now_playing(r: Results) -> None:
    from corbin.commands.radio_cmds import parse_now_playing

    # NowPlaying.txt is HTML despite the .txt extension.
    real = ('<span style="font-size:70%; color:#fff;">NOW PLAYING ON '
            'KZLF-FM<br></span>\n<div style="color:#999;">Marvin Gaye - '
            'Sexual Healing [1983]</div>')
    r.check("track extracted from the div", parse_now_playing(real),
            "Marvin Gaye - Sexual Healing [1983]")
    r.ok("no markup survives", "<" not in (parse_now_playing(real) or ""))
    r.ok("header text stripped",
         "NOW PLAYING" not in (parse_now_playing(real) or ""))
    r.check("plain text passes through",
            parse_now_playing("Artist - Song"), "Artist - Song")
    r.check("entities decoded",
            parse_now_playing("<div>Simon &amp; Garfunkel</div>"),
            "Simon & Garfunkel")
    r.check("whitespace collapsed",
            parse_now_playing("<div>a   b\n c</div>"), "a b c")
    r.check("empty is None", parse_now_playing(""), None)
    r.check("markup-only is None", parse_now_playing("<div></div>"), None)
    r.ok("over-long is clipped",
         len(parse_now_playing("<div>" + "x" * 500 + "</div>")) == 200)


SUITES = [
    ("executor", suite_executor),
    ("radio now-playing", suite_now_playing),
    ("dm back-link", suite_back_link),
    ("private notice", suite_private_notice),
    ("embed limits", suite_embed_limits),
    ("local output cleanup", suite_local_cleanup),
    ("degenerate output guard", suite_degenerate_output),
    ("guild id resolution", suite_guild_resolution),
    ("weather location params", suite_weather_params),
    ("typing indicator", suite_typing),
    ("stores", suite_stores),
    ("adapter conversion", suite_conversion),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
