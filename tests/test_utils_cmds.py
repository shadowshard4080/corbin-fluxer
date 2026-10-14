"""Offline suite for reminders and the small utilities."""
from __future__ import annotations
import logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.commands.verse_cmds import detect_translation
from corbin.core.reminders import (MAX_PER_USER, ReminderStore, parse_when,
                                   user_timezone)

UTC = timezone.utc
NOW = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)


def suite_time_parsing(r: Results) -> None:
    def at(text):
        return parse_when(text, now=NOW)

    r.check("minutes", at("in 20 minutes"), NOW + timedelta(minutes=20))
    r.check("hours", at("in 3 hours"), NOW + timedelta(hours=3))
    r.check("days", at("in 2 days"), NOW + timedelta(days=2))
    r.check("weeks", at("in 1 week"), NOW + timedelta(weeks=7 // 7))
    r.check("abbreviations", at("in 5 min"), NOW + timedelta(minutes=5))

    r.check("tomorrow with am", at("tomorrow at 9am").hour, 9)
    r.check("tomorrow is the next day", at("tomorrow at 9am").day, 24)
    r.check("24h clock", at("tomorrow 21:30").hour, 21)
    r.check("pm converts", at("today at 6pm").hour, 18)
    r.check("tonight implies pm", at("tonight at 8").hour, 20)
    r.check("iso date and time", at("2026-12-25 08:00").day, 25)
    r.check("bare iso date defaults to 9am",
            at("2026-12-25").hour, 9)

    # A bare time that already passed today must mean tomorrow, not a
    # reminder that fires instantly.
    past = parse_when("at 9am", now=NOW)
    r.check("a passed time rolls to tomorrow", past.day, 24)
    future = parse_when("at 9pm", now=NOW)
    r.check("a later time stays today", future.day, 23)

    # Unparseable input is refused: a reminder at the wrong time is worse
    # than one that was never set, because the user believes it is handled.
    for bad in ("banana", "", "   ", "sometime"):
        r.check(f"refuses {bad!r}", at(bad), None)

    tz = user_timezone("America/Denver")
    denver = parse_when("tomorrow at 9am", tz=tz, now=NOW)
    r.check("their 9am, in UTC", denver.hour, 15)
    r.check("bad timezone falls back to UTC",
            user_timezone("Not/AZone"), UTC)
    r.check("no timezone is UTC", user_timezone(None), UTC)


def suite_store(r: Results) -> None:
    # Fixture times must be relative to REAL now: `due` compares against
    # datetime.now(), not the frozen NOW used for parsing tests.
    real = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as tmp:
        s = ReminderStore(tmp)
        r.check("starts empty", len(s), 0)

        item = s.add(user_id="u1", channel_id="c1", guild_id="g",
                     text="check the oven",
                     fire_at=real + timedelta(hours=1))
        r.ok("added", item is not None)
        r.check("listed for the user", len(s.for_user("u1")), 1)
        r.check("not for others", len(s.for_user("u2")), 0)
        r.check("persists", len(ReminderStore(tmp)), 1)

        r.check("nothing due yet", len(s.due()), 0)
        s.add(user_id="u1", channel_id="c1", guild_id="g", text="past",
              fire_at=real - timedelta(minutes=1))
        r.check("overdue is due", len(s.due()), 1)
        popped = s.pop_due()
        r.check("popped", len(popped), 1)
        r.check("and removed", len(s.due()), 0)
        r.check("the other survives", len(s.for_user("u1")), 1)

        # Cancelling is scoped, so nobody can delete someone else's.
        r.check("cannot cancel another user's",
                s.remove(item.id, user_id="u2"), None)
        r.ok("owner can cancel", s.remove(item.id, user_id="u1") is not None)
        r.check("gone", len(s.for_user("u1")), 0)
        r.check("unknown id", s.remove("nope", user_id="u1"), None)

        for i in range(MAX_PER_USER):
            s.add(user_id="u3", channel_id="c", guild_id=None,
                  text=str(i), fire_at=real + timedelta(hours=i + 1))
        r.check("cap enforced",
                s.add(user_id="u3", channel_id="c", guild_id=None,
                      text="over", fire_at=real + timedelta(days=1)), None)
        r.check("sorted by fire time",
                [x.text for x in s.for_user("u3")[:3]], ["0", "1", "2"])


def suite_verse_hints(r: Results) -> None:
    # Auto-detection pre-empts the dispatcher, so it has to honour a
    # translation asked for in the same message or it is unreachable.
    r.check("plain reference", detect_translation("Romans 1:1"), None)
    r.check("commentary", detect_translation("Romans 1:1 commentary"),
            "commentary")
    r.check("meaning counts as commentary",
            detect_translation("what does Romans 8:28 mean"), "commentary")
    r.check("hebrew", detect_translation("Genesis 1:1 in hebrew"), "hebrew")
    r.check("greek", detect_translation("Psalm 23:1 in greek"), "greek")
    r.check("nasb", detect_translation("John 3:16 nasb"), "nasb")
    r.check("kjv", detect_translation("John 3:16 king james"), "kjv")
    r.check("unrelated text", detect_translation("just John 3:16 thanks"),
            None)


def suite_progress(r: Results) -> None:
    from corbin.commands.util_cmds import _bar

    r.ok("empty bar", _bar(0.0).startswith("░"))
    r.ok("full bar", set(_bar(1.0)) == {"█"})
    r.ok("half bar has both", "█" in _bar(0.5) and "░" in _bar(0.5))
    r.check("fixed width", len(_bar(0.37)), 24)
    r.check("clamps over one", len(_bar(5.0)), 24)
    r.check("clamps under zero", len(_bar(-1.0)), 24)


def suite_image_paging(r: Results) -> None:
    """Pagination without components: a follow-up command, per user."""
    import asyncio

    from corbin.commands import util_cmds
    from corbin.core.context import (Channel, CommandContext, Member,
                                     Platform, Surface, User)

    class _R:
        async def send(self, x): return "m"
        async def fetch_member(self, g, u): return None

    def ctx(uid="u1", cid="c1"):
        return CommandContext(
            platform=Platform.FLUXER, surface=Surface.AI,
            invoker=Member(user=User(id=uid, name=uid), guild_id="g"),
            channel=Channel(id=cid, name="general", guild_id="g"),
            guild=None, responder=_R(), extras={})

    util_cmds._IMAGE_RESULTS.clear()
    r.ok("no prior search is explained",
         "haven't searched" in asyncio.run(util_cmds.next_image(ctx())))

    items = [{"title": f"pic {i}", "link": f"http://img/{i}",
              "image": {"contextLink": f"http://page/{i}"}} for i in range(4)]
    util_cmds._IMAGE_RESULTS[("c1", "u1")] = ("clocks", items, 0)

    second = asyncio.run(util_cmds.next_image(ctx()))
    r.check("advances to the second", second.image_url, "http://img/1")
    r.ok("footer counts", "2 of 4" in second.footer)
    third = asyncio.run(util_cmds.next_image(ctx()))
    r.check("and the third", third.image_url, "http://img/2")

    asyncio.run(util_cmds.next_image(ctx()))
    wrapped = asyncio.run(util_cmds.next_image(ctx()))
    r.check("wraps to the start", wrapped.image_url, "http://img/0")
    r.ok("says so", "start" in (wrapped.description or ""))

    # Results are per user and per channel: one person paging must not
    # move someone else's position.
    r.ok("another user has no results",
         "haven't searched" in asyncio.run(util_cmds.next_image(ctx("u2"))))
    r.ok("another channel has none",
         "haven't searched" in
         asyncio.run(util_cmds.next_image(ctx("u1", "c2"))))

    util_cmds._IMAGE_RESULTS[("c1", "u3")] = ("x", [items[0]], 0)
    r.ok("single result says so",
         "only turned up" in asyncio.run(util_cmds.next_image(ctx("u3"))))
    util_cmds._IMAGE_RESULTS.clear()


def suite_echo(r: Results) -> None:
    """echo posts what the owner wrote, not the classifier's tidy copy."""
    import asyncio
    from types import SimpleNamespace

    from corbin.commands import util_cmds as u
    from corbin.core.registry import registry

    r.check("a quoted span is used exactly",
            u.verbatim("server maintenance tonight at 9",
                       'echo "Server Maintenance tonight at 9!" in #news'),
            "Server Maintenance tonight at 9!")
    r.check("curly quotes too",
            u.verbatim("hi all", "say “Hi all 🐺” in general"), "Hi all 🐺")
    r.check("a code block wins, formatting intact",
            u.verbatim("x", "post in #rules:\n```\n**Rule 1**\n- be kind\n```"),
            "**Rule 1**\n- be kind")
    r.check("unquoted: the original's own spelling",
            u.verbatim("hello everyone", "say Hello Everyone in #general"),
            "Hello Everyone")
    r.check("no original: the extracted text",
            u.verbatim("hello", None), "hello")
    r.check("a quote that doesn't contain it is ignored",
            u.verbatim("the real text", 'say the real text, not "this"'),
            "the real text")

    r.check("mentions ping again",
            u.restore_mentions("hey @Robin and @Robinson",
                               {"Robin": "1", "Robinson": "2"}),
            "hey <@1> and <@2>")
    r.check("names ending in an emoji still match",
            u.restore_mentions("hi @Sam🐺!", {"Sam🐺": "3"}), "hi <@3>!")

    spec = registry.get("echo")
    r.ok("echo is owner-only", spec is not None and spec.owner_only)

    sent = []

    class Adapter:
        async def send(self, channel, response):
            sent.append((channel, response.content))
            return "m1"

    def ctx(raw):
        return SimpleNamespace(extras={
            "adapter": Adapter(), "raw_text": raw,
            "mention_ids": {"Robin": "1"},
            "channel_index": {"announcements": "777"}})

    out = asyncio.run(u.echo(ctx('echo "Big news, @Robin!\\nMore soon." in '
                                 '#announcements'),
                             "announcements", "big news, @robin! more soon."))
    r.check("posted to the named channel, verbatim, pinging, with \\n",
            sent[-1], ("777", "Big news, <@1>!\nMore soon."))
    r.ok("...and confirms where", "777" in out)
    r.ok("an unknown channel is refused",
         "can't find" in asyncio.run(u.echo(ctx(""), "nowhere", "hi")))
    r.ok("an over-long message is refused",
         "limit" in asyncio.run(u.echo(ctx(None), "777", "x" * 2001)))


SUITES = [
    ("reminder time parsing", suite_time_parsing),
    ("image search paging", suite_image_paging),
    ("reminder store", suite_store),
    ("verse translation hints", suite_verse_hints),
    ("progress bar", suite_progress),
    ("echo", suite_echo),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
