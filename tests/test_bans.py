"""Offline suite for timed bans and chatbans."""
from __future__ import annotations
import json, logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.bans import (MAX_BAN_DAYS, BanStore, ChatbanStore,
                              humanise, parse_duration)


def suite_duration(r: Results) -> None:
    r.check("minutes", parse_duration("30m"), timedelta(minutes=30))
    r.check("hours", parse_duration("2h"), timedelta(hours=2))
    r.check("days", parse_duration("7d"), timedelta(days=7))
    r.check("weeks", parse_duration("3w"), timedelta(weeks=3))
    r.check("combined", parse_duration("1d 12h"), timedelta(days=1, hours=12))
    r.check("spaces optional", parse_duration("1d12h"),
            timedelta(days=1, hours=12))
    r.check("case-insensitive", parse_duration("2H"), timedelta(hours=2))

    # A bare number is ambiguous, and guessing wrong on a ban length is not
    # recoverable, so it is refused rather than assumed.
    r.check("bare number refused", parse_duration("5"), None)
    r.check("nonsense refused", parse_duration("banana"), None)
    r.check("empty refused", parse_duration(""), None)
    r.check("None refused", parse_duration(None), None)
    r.check("zero refused", parse_duration("0m"), None)
    r.check("capped at a year", parse_duration("999d"),
            timedelta(days=MAX_BAN_DAYS))

    r.check("days and hours", humanise(timedelta(days=2, hours=3)), "2d 3h")
    r.check("minutes only", humanise(timedelta(minutes=45)), "45m")
    r.check("past is expired", humanise(timedelta(seconds=-5)), "expired")
    r.check("very short", humanise(timedelta(seconds=30)), "under a minute")


def suite_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = BanStore(tmp)
        r.check("starts empty", len(s), 0)
        r.check("nothing due", s.due(), [])

        b = s.add("g", "u", timedelta(hours=2), reason="spam",
                  banner_id="mod")
        r.check("stored", len(s), 1)
        r.ok("not due yet", not b.due)
        r.ok("remaining is positive", b.remaining.total_seconds() > 0)
        r.check("found", s.get("g", "u").reason, "spam")
        r.check("other guild not found", s.get("zz", "u"), None)
        r.check("per-guild listing", len(s.for_guild("g")), 1)
        r.check("other guild empty", len(s.for_guild("zz")), 0)

        # Must survive a restart, or bans never lift.
        r.check("persists across reload", len(BanStore(tmp)), 1)
        r.check("reload keeps the reason",
                BanStore(tmp).get("g", "u").reason, "spam")

        s.add("g", "expired", timedelta(seconds=-10))
        due = s.due()
        r.check("expired ban is due", len(due), 1)
        r.check("the right one", due[0].user_id, "expired")

        r.ok("remove works", s.remove("g", "u"))
        r.ok("remove twice is False", not s.remove("g", "u"))
        r.check("no temp files", list(Path(tmp).glob("*.tmp")), [])

        # Same user id in two guilds must be tracked separately.
        s.add("g1", "same", timedelta(hours=1))
        s.add("g2", "same", timedelta(hours=1))
        r.check("keyed per guild", len(s.for_guild("g1")), 1)
        r.ok("independent", s.get("g2", "same") is not None)
        s.remove("g1", "same")
        r.ok("removing one leaves the other", s.get("g2", "same") is not None)


def suite_chatban(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        c = ChatbanStore(tmp)
        r.check("starts empty", len(c), 0)
        r.ok("nobody banned", "u1" not in c)

        r.ok("toggle on returns True", c.toggle("u1"))
        r.ok("now banned", "u1" in c)
        r.check("int id also matches", "u1" in c, True)
        r.ok("toggle off returns False", not c.toggle("u1"))
        r.ok("no longer banned", "u1" not in c)

        c.toggle("u1"); c.toggle("u2")
        r.check("two banned", len(c), 2)
        r.check("listed sorted", c.all(), ["u1", "u2"])
        r.check("persists", len(ChatbanStore(tmp)), 2)

        # The Discord build's file is a bare JSON list; keep that shape.
        raw = json.loads((Path(tmp) / "banned_users.json").read_text())
        r.ok("stored as a list", isinstance(raw, list))

        bad = Path(tmp) / "banned_users.json"
        bad.write_text("{not json")
        r.check("corrupt file starts empty", len(ChatbanStore(tmp)), 0)


def suite_chatban_in_dm(r: Results) -> None:
    """The chatban list is global and keyed by id, so the owner can use it
    from a DM. A name can't be looked up there, and must never be stored
    as if it were an id."""
    import asyncio
    from types import SimpleNamespace
    from _harness import member
    from corbin.core.context import Surface
    from corbin.core.permissions import Perm
    from corbin.core.risk import Decision, assess
    import corbin.commands.moderation  # noqa: registers chatban
    from corbin.core.registry import registry

    spec = registry.get("chatban")
    r.ok("chatban works in DMs", spec is not None and not spec.guild_only)
    owner, alice = member("o"), member("alice", perms=Perm.NONE)
    v = assess(spec, surface=Surface.PREFIX, invoker=owner,
               args={"user": "123456789"}, is_bot_owner=True, in_guild=False)
    r.check("the owner's confirmed chatban runs from a DM", v.decision,
            Decision.EXECUTE)
    v = assess(spec, surface=Surface.PREFIX, invoker=alice,
               args={"user": "123456789"}, in_guild=False)
    r.check("...and is still owner-only there", v.decision, Decision.DENIED)

    with tempfile.TemporaryDirectory() as tmp:
        store = ChatbanStore(tmp)
        ctx = SimpleNamespace(extras={"chatbans": store}, guild_id=None)
        out = asyncio.run(spec.handler(ctx, user="123456789"))
        r.ok("an id is chatbanned", "123456789" in store and "stop" in out)
        out = asyncio.run(spec.handler(ctx, user="Steve"))
        r.ok("a bare name in a DM is refused, not stored",
             "Steve" not in store and len(store) == 1 and "id" in out)


SUITES = [
    ("ban durations", suite_duration),
    ("timed ban store", suite_store),
    ("chatbans", suite_chatban),
    ("chatbans: from a DM", suite_chatban_in_dm),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
