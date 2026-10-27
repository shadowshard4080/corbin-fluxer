"""Offline suite for role assignment through the adapter.

Assignment goes over REST, not through a ``GuildMember`` object. The
member lookup is an extra step that can miss, and when it did the old
code reported "fluxer.py exposes no member add_role method" -- blaming
the SDK for a cold cache, and sending a debugging session in entirely
the wrong direction. These checks pin that a missing member no longer
stops a role being granted, and that when something IS genuinely
missing the message says which.
"""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.context import Role
from corbin.platforms.fluxer.adapter import FluxerAdapter

TIERS = ["Moongazer", "Starfallen Warden", "Godlike"]


class RestHTTP:
    def __init__(self): self.calls = []
    async def add_guild_member_role(self, g, u, r):
        self.calls.append(("add", str(r)))
    async def remove_guild_member_role(self, g, u, r):
        self.calls.append(("remove", str(r)))


class LegacyHTTP:
    """An SDK predating the REST helpers."""


def adapter(http, member=...):
    a = FluxerAdapter(type("C", (), {"_http": http})())
    if member is not ...:
        a._raw_member = lambda g, u: asyncio.sleep(0, result=member)
    a.role_index = lambda g: asyncio.sleep(0, result={
        "R1": Role(id="R1", name="Starfallen Warden", position=5,
                   permissions=0),
        "R2": Role(id="R2", name="Moongazer", position=3, permissions=0)})
    return a


def suite_rest_path(r: Results) -> None:
    http = RestHTTP()
    a = adapter(http, member=None)

    asyncio.run(a.add_role("G", "U", "555"))
    r.check("a role is granted with no member object at all",
            http.calls, [("add", "555")])
    asyncio.run(a.remove_role("G", "U", "555"))
    r.check("...and removed the same way", http.calls[-1], ("remove", "555"))


def suite_rank_roles(r: Results) -> None:
    http = RestHTTP()
    a = adapter(http, member=None)
    err = asyncio.run(a.apply_rank_role("G", "U", "Starfallen Warden", TIERS))
    r.check("rank role applied despite an unresolvable member", err, None)
    r.check("...the right one", http.calls, [("add", "R1")])
    r.ok("...and no blind removals when the held list is unknown",
         not [c for c in http.calls if c[0] == "remove"])

    class Member:
        roles = ["R2"]           # holds the previous tier
    http = RestHTTP()
    a = adapter(http, member=Member())
    err = asyncio.run(a.apply_rank_role("G", "U", "Starfallen Warden", TIERS))
    r.check("no error with a known member", err, None)
    r.check("new tier granted", ("add", "R1") in http.calls, True)
    r.check("old tier stripped", ("remove", "R2") in http.calls, True)

    class Current:
        roles = ["R1"]           # already correct
    http = RestHTTP()
    a = adapter(http, member=Current())
    asyncio.run(a.apply_rank_role("G", "U", "Starfallen Warden", TIERS))
    r.check("re-applying the role they already hold does nothing",
            http.calls, [])


def suite_failures(r: Results) -> None:
    class Refusing(RestHTTP):
        async def add_guild_member_role(self, g, u, role):
            raise PermissionError("role is above the bot's own")

    a = adapter(Refusing(), member=None)
    err = asyncio.run(a.apply_rank_role("G", "U", "Starfallen Warden", TIERS))
    r.ok("a refused grant is reported, not swallowed", bool(err))
    r.ok("...naming the role", "Starfallen Warden" in (err or ""))

    class Member:
        added = []
        async def add_role(self, rid, reason=None):
            Member.added.append(rid)
    a = adapter(LegacyHTTP(), member=Member())
    asyncio.run(a.add_role("G", "U", "77"))
    r.check("an SDK without the REST route falls back to the member",
            Member.added, [77])

    a = adapter(LegacyHTTP(), member=None)
    try:
        asyncio.run(a.add_role("G", "U", "77"))
        message = ""
    except RuntimeError as exc:
        message = str(exc)
    r.ok("no route and no member says the member is missing",
         "not found" in message)
    r.ok("...and does not blame the SDK for it",
         "exposes no" not in message)


def suite_moderation(r: Results) -> None:
    """Ban, kick, unban and timeout had the same flaw as add_role."""
    import datetime as dt

    calls = []

    class HTTP:
        async def ban_guild_member(self, g, u, **kw):
            calls.append(("ban", kw))
        async def kick_guild_member(self, g, u, **kw):
            calls.append(("kick", kw))
        async def unban_guild_member(self, g, u, **kw):
            calls.append(("unban", kw))
        async def timeout_guild_member(self, g, u, **kw):
            calls.append(("timeout", kw))

    a = FluxerAdapter(type("C", (), {"_http": HTTP()})())
    # Neither lookup resolves: the situation that broke role assignment.
    a._guild = lambda g: asyncio.sleep(0, result=None)
    a._raw_member = lambda g, u: asyncio.sleep(0, result=None)

    asyncio.run(a.ban("G", "U", reason="scam",
                      delete_message_seconds=604800))
    r.check("ban works with no guild object", calls[-1][0], "ban")
    # This was accepted and then dropped; the honeypot passes it to
    # sweep a scammer's posts on the way out.
    r.check("delete_message_seconds is actually forwarded",
            calls[-1][1].get("delete_message_seconds"), 604800)

    asyncio.run(a.kick("G", "U", reason="bye"))
    r.check("kick works, with its reason", calls[-1][1].get("reason"), "bye")
    asyncio.run(a.unban("G", "U"))
    r.check("unban works", calls[-1][0], "unban")

    when = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    asyncio.run(a.timeout("G", "U", when))
    r.check("timeout sends an ISO deadline",
            calls[-1][1].get("until"), when.isoformat())
    asyncio.run(a.timeout("G", "U", None))
    r.check("lifting a timeout sends null", calls[-1][1], {"until": None})

    class Guild:
        seen = []
        async def ban(self, uid, reason=None, delete_message_seconds=0):
            Guild.seen.append(delete_message_seconds)

    legacy = FluxerAdapter(type("C", (), {"_http": object()})())
    legacy._guild = lambda g: asyncio.sleep(0, result=Guild())
    asyncio.run(legacy.ban("G", "U", delete_message_seconds=60))
    r.check("an older SDK still bans through the guild object",
            Guild.seen, [60])

    bare = FluxerAdapter(type("C", (), {"_http": object()})())
    bare._guild = lambda g: asyncio.sleep(0, result=None)
    try:
        asyncio.run(bare.kick("G", "U"))
        message = ""
    except RuntimeError as exc:
        message = str(exc)
    r.ok("with nothing available, the error names the guild",
         "not found" in message)


def suite_name_resolution(r: Results) -> None:
    """A user param may arrive as a name, a mention or an id."""
    a = FluxerAdapter(type("C", (), {"_http": object()})())
    a.member_name_index = lambda g: asyncio.sleep(0, result={
        "juniper": "U1", "juniper#5928": "U1", "pip": "U2"})
    a.role_name_index = lambda g: asyncio.sleep(0, result={"artist": "R2"})

    def resolve(value, kind="user"):
        return asyncio.run(a.resolve_named("G", kind, value))

    r.check("a bare username resolves", resolve("juniper"), "U1")
    # This is what actually failed: the @ made it a name nobody indexed,
    # and it went into the URL as though it were an id.
    r.check("an @-prefixed username resolves", resolve("@juniper"), "U1")
    r.check("name#discriminator resolves", resolve("@juniper#5928"), "U1")
    r.check("a stale discriminator falls back to the username",
            resolve("juniper#0000"), "U1")
    r.check("a mention token yields its id", resolve("<@1234>"), "1234")
    r.check("a nickname mention too", resolve("<@!1234>"), "1234")
    r.check("an id passes through", resolve("444444444444444444"),
            "444444444444444444")
    r.check("an unknown name is returned unchanged, not guessed",
            resolve("nobody"), "nobody")
    r.check("roles still resolve", resolve("the Artist", "role"),
            "the Artist")
    r.check("...by exact name", resolve("Artist", "role"), "R2")


def suite_diagnosis(r: Results) -> None:
    """A 404 must say which id was wrong."""
    from corbin.core.context import Role

    class HTTP:
        def __init__(self, member_ok): self.member_ok = member_ok
        async def add_guild_member_role(self, g, u, role):
            raise RuntimeError("404 UNKNOWN_MEMBER")
        async def get_guild_member(self, g, u):
            if not self.member_ok:
                raise RuntimeError("404")
            return {"user": {"id": u}}

    roles = {"R1": Role(id="R1", name="Verified", position=2,
                        permissions=0)}

    def attempt(member_ok, role_id):
        a = FluxerAdapter(type("C", (), {"_http": HTTP(member_ok)})())
        a.role_index = lambda g: asyncio.sleep(0, result=roles)
        try:
            asyncio.run(a.add_role("G7", "U9", role_id))
            return ""
        except RuntimeError as exc:
            return str(exc)

    missing_member = attempt(False, "R1")
    r.ok("a missing member is named as such",
         "isn't a member" in missing_member)
    r.ok("...and the guild id is shown, so a wrong guild is visible",
         "G7" in missing_member)

    missing_role = attempt(True, "NOPE")
    r.ok("a missing role is named as such", "no role NOPE" in missing_role)
    r.ok("...and the real roles are listed", "Verified" in missing_role)

    r.ok("neither missing leaves the server's own error intact",
         "UNKNOWN_MEMBER" in attempt(True, "R1"))


def suite_swapped_args(r: Results) -> None:
    """"assign <role> to <user>" reaches the handler back to front.

    Two bare snowflakes and no way to tell them apart from wording, so
    the classifier fills the parameters in the order they are written
    (role first) while they are declared user first. The repair checks
    against the guild's real roles rather than trusting either.
    """
    from corbin.core.context import (Channel, CommandContext, Guild,
                                     Member, Platform, Role, Surface, User)
    import corbin.commands.moderation as mod

    ROLE, USER = "333333333333333333", "444444444444444444"
    roles = {ROLE: Role(id=ROLE, name="awoo", position=3, permissions=0)}
    done = []

    class Adapter:
        async def role_index(self, g): return roles
        async def add_role(self, g, u, role): done.append((u, role))

    class Responder:
        async def send(self, _r): return "m"

    def ctx():
        return CommandContext(
            platform=Platform.FLUXER, surface=Surface.AI,
            invoker=Member(user=User(id="a", name="A"), guild_id="G"),
            channel=Channel(id="c", name="n"),
            guild=Guild(id="G", name="Den"), responder=Responder(),
            extras={"adapter": Adapter()})

    done.clear()
    asyncio.run(mod.assignrole(ctx(), user=ROLE, role=USER))
    r.check("swapped arguments are corrected", done[-1], (USER, ROLE))

    done.clear()
    asyncio.run(mod.assignrole(ctx(), user=USER, role=ROLE))
    r.check("correct arguments are left alone", done[-1], (USER, ROLE))

    done.clear()
    asyncio.run(mod.assignrole(ctx(), user="111", role="222"))
    r.check("no swap when neither id is a known role",
            done[-1], ("111", "222"))

    done.clear()
    asyncio.run(mod.assignrole(ctx(), user=ROLE, role=ROLE))
    r.check("no swap when both are the same role", done[-1], (ROLE, ROLE))


def suite_role_list_message(r: Results) -> None:
    """A truncated role list must not read as the whole set."""
    from corbin.core.context import Role

    class HTTP:
        async def add_guild_member_role(self, g, u, role):
            raise RuntimeError("404")
        async def get_guild_member(self, g, u):
            return {"user": {"id": u}}

    names = ["Corbin", "Godlike", "Guardians", "awoo", "zebra"] + \
        [f"Role{i}" for i in range(15)]
    roles = {f"R{i}": Role(id=f"R{i}", name=n, position=i, permissions=0)
             for i, n in enumerate(names)}
    a = FluxerAdapter(type("C", (), {"_http": HTTP()})())
    a.role_index = lambda g: asyncio.sleep(0, result=roles)
    try:
        asyncio.run(a.add_role("G", "U", "NOPE"))
        message = ""
    except RuntimeError as exc:
        message = str(exc)

    r.ok("the total count is stated", f"The {len(names)} roles" in message)
    r.ok("the cut is marked", "and 8 more" in message)
    # Case-sensitive sorting pushed every lowercase role past the cut,
    # which is how "awoo" looked absent from a server that had it.
    r.ok("lowercase names are not exiled to the end", "awoo" in message)


SUITES = [
    ("role assignment over REST", suite_rest_path),
    ("rank role application", suite_rank_roles),
    ("role assignment failures", suite_failures),
    ("moderation actions", suite_moderation),
    ("user name resolution", suite_name_resolution),
    ("role failure diagnosis", suite_diagnosis),
    ("swapped assignrole arguments", suite_swapped_args),
    ("role list message", suite_role_list_message),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
