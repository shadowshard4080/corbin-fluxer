"""Offline suite for the #do-not-type honeypot."""
from __future__ import annotations
import asyncio, logging, tempfile, time
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.honeypot import (ALLOW, BAN_DELETE_SECONDS, CHANNEL_NAME,
                                  DENY, EXEMPT, HoneypotGuard,
                                  ensure_channel, is_exempt)
from corbin.core.permissions import Perm
from corbin.core.settings import ChatSettings


class _Member:
    def __init__(self, perms=Perm.NONE, owner=False, uid="u1"):
        self.permissions = perms
        self.is_guild_owner = owner
        self.id = uid


def suite_permissions(r: Results) -> None:
    # SEND_MESSAGES in ALLOW is the entire point: a locked channel catches
    # nothing. This must never be "fixed" into a deny.
    r.ok("send is granted", bool(ALLOW & Perm.SEND_MESSAGES))
    r.ok("view is granted", bool(ALLOW & Perm.VIEW_CHANNEL))
    r.check("allow grants nothing else", int(ALLOW),
            int(Perm.SEND_MESSAGES | Perm.VIEW_CHANNEL))

    for perm in (Perm.ATTACH_FILES, Perm.EMBED_LINKS, Perm.ADD_REACTIONS,
                 Perm.MENTION_EVERYONE, Perm.MANAGE_MESSAGES):
        r.ok(f"{perm.name} denied", bool(DENY & perm))
    r.check("allow and deny never overlap", int(ALLOW) & int(DENY), 0)


def suite_exemption(r: Results) -> None:
    r.ok("plain member is banned", not is_exempt(_Member(Perm.SEND_MESSAGES)))
    for perm in (Perm.ADMINISTRATOR, Perm.MANAGE_GUILD, Perm.BAN_MEMBERS,
                 Perm.KICK_MEMBERS, Perm.MODERATE_MEMBERS):
        r.ok(f"{perm.name} is exempt", is_exempt(_Member(perm)))
    r.ok("guild owner exempt", is_exempt(_Member(owner=True)))
    r.ok("the bot owner is not exempt just for being the bot owner",
         not is_exempt(_Member(Perm.SEND_MESSAGES, uid="999")))
    # Never ban on a guess: an unresolvable member is treated as exempt.
    r.ok("unknown member is exempt", is_exempt(None))

    class Broken:
        id = "x"
        is_guild_owner = False

        @property
        def permissions(self):
            raise RuntimeError("boom")

    r.ok("a member that raises is exempt", is_exempt(Broken()))


def suite_guard(r: Results) -> None:
    g = HoneypotGuard(ttl=10.0)
    r.ok("first claim wins", g.claim("g", "u"))
    # A bot dumping five messages must produce one ban, not five.
    r.ok("duplicate refused", not g.claim("g", "u"))
    r.ok("and again", not g.claim("g", "u"))
    r.ok("a different user is independent", g.claim("g", "u2"))
    r.ok("same user elsewhere is independent", g.claim("g2", "u"))

    g.release("g", "u")
    r.ok("released user can be claimed again", g.claim("g", "u"))

    expired = HoneypotGuard(ttl=0.01)
    expired.claim("g", "u")
    time.sleep(0.03)
    r.ok("a stale claim expires", expired.claim("g", "u"))
    r.check("stale entries are swept", len(expired), 1)


def suite_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = ChatSettings(Path(tmp) / "c.json")
        r.check("none by default", s.honeypot("g"), None)
        r.ok("nothing is the honeypot", not s.is_honeypot("g", "c1"))

        s.set_honeypot("g", "c1")
        r.check("stored", s.honeypot("g"), "c1")
        r.ok("recognised", s.is_honeypot("g", "c1"))
        r.ok("other channels are not", not s.is_honeypot("g", "c2"))
        r.ok("other guilds are not", not s.is_honeypot("g2", "c1"))
        r.ok("no guild is safe", not s.is_honeypot(None, "c1"))
        r.ok("persists", ChatSettings(Path(tmp) / "c.json").honeypot("g") == "c1")

        s.set_honeypot("g", None)
        r.check("cleared", s.honeypot("g"), None)


class _HTTP:
    def __init__(self, channels=None, fail=None):
        self.channels = channels or []
        self.fail = fail
        self.created = []
        self.perms = []
        self.modified = []

    async def get_guild_channels(self, gid):
        if self.fail == "list":
            raise RuntimeError("no")
        return self.channels

    async def create_guild_channel(self, gid, **kw):
        if self.fail == "create":
            raise RuntimeError("denied")
        self.created.append(kw)
        return {"id": "new1", **kw}

    async def modify_channel(self, cid, **kw):
        self.modified.append((cid, kw))

    async def edit_channel_permissions(self, cid, oid, **kw):
        if self.fail == "perms":
            raise RuntimeError("denied")
        self.perms.append((cid, oid, kw))


def suite_ensure(r: Results) -> None:
    http = _HTTP()
    cid, problems = asyncio.run(ensure_channel(http, "g1"))
    r.check("channel created", cid, "new1")
    r.check("no problems", problems, [])
    kw = http.created[0]
    r.check("named correctly", kw["name"], CHANNEL_NAME)
    # Uncategorised AND position 0, or it will not sit at the very top.
    r.check("uncategorised", kw["parent_id"], None)
    r.check("top position", kw["position"], 0)
    r.check("overwrite targets @everyone (the guild id)",
            http.perms[0][1], "g1")
    r.check("allow bitmask", http.perms[0][2]["allow"], str(int(ALLOW)))
    r.check("deny bitmask", http.perms[0][2]["deny"], str(int(DENY)))

    # Re-running must adopt, never duplicate.
    existing = _HTTP(channels=[{"id": "c9", "name": CHANNEL_NAME}])
    cid2, _ = asyncio.run(ensure_channel(existing, "g1"))
    r.check("adopts an existing channel", cid2, "c9")
    r.check("creates nothing new", len(existing.created), 0)
    r.ok("re-applies position", existing.modified[0][1]["position"] == 0)
    r.ok("re-applies permissions", len(existing.perms) == 1)

    failed = _HTTP(fail="create")
    cid3, problems3 = asyncio.run(ensure_channel(failed, "g1"))
    r.check("creation failure reported", cid3, None)
    r.ok("problem described", "couldn't create" in problems3[0])

    perm_fail = _HTTP(fail="perms")
    cid4, problems4 = asyncio.run(ensure_channel(perm_fail, "g1"))
    r.ok("channel still made", cid4 is not None)
    r.ok("permission failure surfaced",
         any("permissions" in p for p in problems4))

    r.check("delete window is a day", BAN_DELETE_SECONDS, 86400)


def suite_deleted_channel(r: Results) -> None:
    """Deleting the trap channel clears the setting, not just logs it."""
    import ast

    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8-sig")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
    handler = fns.get("on_channel_delete")
    r.ok("the channel-delete listener exists", handler is not None)
    calls = {getattr(c.func, "id", getattr(c.func, "attr", ""))
             for c in ast.walk(handler) if isinstance(c, ast.Call)} \
        if handler else set()
    r.ok("...and clears a deleted honeypot",
         "_forget_deleted_honeypot" in calls)
    helper = ast.get_source_segment(src, fns["_forget_deleted_honeypot"]) \
        if "_forget_deleted_honeypot" in fns else ""
    r.ok("...only when it IS the honeypot", "is_honeypot(" in helper)
    r.ok("...by unsetting it", "set_honeypot(gid, None)" in helper)

    with tempfile.TemporaryDirectory() as tmp:
        settings = ChatSettings(Path(tmp) / "s.json")
        settings.set_honeypot("g", "123")
        settings.set_honeypot("g", None)
        r.check("unsetting leaves no dangling id", settings.honeypot("g"),
                None)


SUITES = [
    ("honeypot permissions", suite_permissions),
    ("honeypot exemption", suite_exemption),
    ("honeypot burst guard", suite_guard),
    ("honeypot settings", suite_settings),
    ("honeypot channel setup", suite_ensure),
    ("honeypot channel deleted", suite_deleted_channel),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
