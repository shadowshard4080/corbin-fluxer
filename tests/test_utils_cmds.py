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

    # A plain tinyfox animal goes to the hourly-animal post, not Google.
    from corbin.core import scheduled

    fetched, googled = [], []
    real_fetch, real_google = scheduled.fetch_animal, util_cmds._fetch_images

    async def fake_fetch(key, **kw):
        fetched.append(key)
        return None if key == "down" else b"\x89PNG fake"

    async def fake_google(query):
        googled.append(query)
        return items, None

    scheduled.fetch_animal, util_cmds._fetch_images = fake_fetch, fake_google
    try:
        got = asyncio.run(util_cmds.image_search(ctx(), "cute foxes"))
        r.check("an animal query fetches from tinyfox", fetched, ["fox"])
        r.check("...not Google", googled, [])
        r.ok("...and uploads the picture", got.files
             and got.files[0][0] == "fox.png")
        r.check("...in the hourly-animal style", got.embed.footer,
                "Powered by tinyfox.dev")
        again = asyncio.run(util_cmds.next_image(ctx()))
        r.check("next image fetches another of the same animal", fetched,
                ["fox", "fox"])
        r.ok("...as another upload", again.files)

        fetched.clear()
        got = asyncio.run(util_cmds.image_search(ctx(), "fox news logo"))
        r.check("a specific query goes to Google", googled,
                ["fox news logo"])
        r.check("...without touching tinyfox", fetched, [])

        googled.clear()
        scheduled.ANIMALS["down"] = "Broken"
        scheduled.ANIMAL_ALIASES["brokenbeast"] = "down"
        asyncio.run(util_cmds.image_search(ctx(), "brokenbeast"))
        r.check("tinyfox down: falls back to Google", googled,
                ["brokenbeast"])
    finally:
        scheduled.fetch_animal = real_fetch
        util_cmds._fetch_images = real_google
        scheduled.ANIMALS.pop("down", None)
        scheduled.ANIMAL_ALIASES.pop("brokenbeast", None)
        util_cmds._IMAGE_RESULTS.clear()


def suite_echo(r: Results) -> None:
    """echo posts what the owner wrote, not the classifier's tidy copy."""
    import asyncio
    from types import SimpleNamespace

    from corbin.commands import util_cmds as u
    from corbin.core.permissions import Perm
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
            u.restore_mentions("hey @Kit and @Kitson",
                               {"Kit": "1", "Kitson": "2"}),
            "hey <@1> and <@2>")
    r.check("names ending in an emoji still match",
            u.restore_mentions("hi @Lux🐺!", {"Lux🐺": "3"}), "hi <@3>!")

    spec = registry.get("echo")
    r.ok("echo is a staff command, not an owner pass",
         spec is not None and not spec.owner_only
         and spec.permission == Perm.MANAGE_GUILD)

    sent = []

    class Adapter:
        async def send(self, channel, response):
            sent.append((channel, response.content))
            return "m1"

        async def resolve_guild_id(self, channel):
            return {"777": "g1", "888": "g2"}.get(str(channel))

    def ctx(raw):
        return SimpleNamespace(guild_id="g1", extras={
            "adapter": Adapter(), "raw_text": raw,
            "mention_ids": {"Kit": "1"},
            "channel_index": {"announcements": "777"}})

    out = asyncio.run(u.echo(ctx('echo "Big news, @Kit!\\nMore soon." in '
                                 '#announcements'),
                             "announcements", "big news, @kit! more soon."))
    r.check("posted to the named channel, verbatim, pinging, with \\n",
            sent[-1], ("777", "Big news, <@1>!\nMore soon."))
    r.ok("...and confirms where", "777" in out)
    r.ok("an unknown channel is refused",
         "can't find" in asyncio.run(u.echo(ctx(""), "nowhere", "hi")))
    r.ok("another server's channel is refused, whoever asks",
         "this server's own" in asyncio.run(u.echo(ctx(None), "888", "hi"))
         and sent[-1][0] != "888")
    r.ok("an over-long message is refused",
         "limit" in asyncio.run(u.echo(ctx(None), "777", "x" * 2001)))


def suite_reminder_zones(r: Results) -> None:
    """A clock time uses the stated zone, then the learned one, else asks.

    It used to assume UTC, so "remind me at 9pm" fired at 2 pm Pacific.
    """
    import asyncio

    from corbin.commands import reminder_cmds as rc
    from corbin.core.context import (Channel, CommandContext, Member,
                                     Platform, Surface, User)
    from corbin.core.memory import MemoryStore
    from corbin.core.reminders import split_zone

    zone, rest = split_zone("tomorrow at 9pm PST")
    r.check("PST is pulled out", str(zone), "America/Los_Angeles")
    r.check("the rest still parses", rest, "tomorrow at 9pm")
    zone, rest = split_zone("at 9am Europe/London")
    r.check("IANA names are pulled out", str(zone), "Europe/London")
    r.check("ordinary words are not zones",
            split_zone("tonight at 8")[0], None)

    with tempfile.TemporaryDirectory() as tmp:
        memory = MemoryStore(tmp)
        store = ReminderStore(tmp)

        def ctx(uid):
            return CommandContext(
                platform=Platform.FLUXER, surface=Surface.AI,
                invoker=Member(user=User(id=uid, name=uid), guild_id="g"),
                channel=Channel(id="c", name="general", guild_id="g"),
                guild=None, responder=None,
                extras={"memory": memory, "reminders": store})

        out = asyncio.run(rc.remind(ctx("u1"), "tomorrow at 9pm", "x"))
        r.ok("an unknown zone is asked about", "timezone" in out.lower())
        r.check("and nothing is stored", len(store), 0)

        out = asyncio.run(rc.remind(ctx("u1"), "in 2 hours", "x"))
        r.ok("relative times need no zone", "Noted" in out)

        out = asyncio.run(rc.remind(ctx("u1"), "tomorrow at 9pm PST", "y"))
        r.ok("a stated zone is used", "21:00 P" in out)
        fire = store.for_user("u1")[-1].fire_at
        r.check("stored as their 9pm in UTC", fire.hour, 4)
        r.check("the stated zone is remembered",
                memory.get_user("u1").timezone, "America/Los_Angeles")

        out = asyncio.run(rc.remind(ctx("u1"), "tomorrow at 8am", "z"))
        r.ok("the remembered zone is used next time", "08:00 P" in out)

        out = asyncio.run(rc.remind(ctx("u2"), "at 9am", "w",
                                    timezone="EST"))
        r.ok("the timezone param works", "09:00 E" in out)
        out = asyncio.run(rc.remind(ctx("u3"), "at 9am", "w",
                                    timezone="Mars/Olympus"))
        r.ok("a bad timezone is refused", "isn't a timezone" in out)


def suite_server_list(r: Results) -> None:
    """The owner's "what servers are you in?" (util_cmds.servers) and the
    adapter list behind it."""
    import asyncio, types
    from corbin.commands.util_cmds import servers
    from corbin.core.context import (Channel, CommandContext, Guild, Member,
                                     Platform, Response, Surface, User)

    class Adapter:
        def __init__(self, rows=None, fail=None):
            self.rows, self.fail = rows, fail

        async def guild_list(self):
            if self.fail:
                raise RuntimeError(self.fail)
            return self.rows

    class Allow:
        def enabled(self, gid):
            return {"den": frozenset({"voice", "streaming"}),
                    "two": frozenset({"voice"})}.get(gid, frozenset())

    class _R:
        async def send(self, r): return "m"

    def ctx(adapter, guild=None):
        return CommandContext(
            platform=Platform.FLUXER, surface=Surface.AI,
            invoker=Member(user=User(id="owner", name="Owner"),
                           guild_id=guild or ""),
            channel=Channel(id="c", name="x", guild_id=guild,
                            is_dm=guild is None),
            guild=Guild(id=guild, name="Den") if guild else None,
            responder=_R(),
            extras={"adapter": adapter, "features": Allow()})

    rows = [{"id": "den", "name": "The Lounge", "members": 1204},
            {"id": "two", "name": "Lux's Place", "members": None},
            {"id": "three", "name": "Quiet", "members": 3}]
    out = asyncio.run(servers(ctx(Adapter(rows))))
    text = "\n".join(f.value for f in out.embed.fields)
    r.check("in a DM: a plain reply, not re-DMed",
            (isinstance(out, Response), out.private), (True, False))
    r.check("the count, with members summed where known",
            (out.embed.title, out.embed.description),
            ("I'm in 3 servers", "1,207 members between them."))
    r.ok("each server: name, id, members, premium marks",
         "**The Lounge** · `den` · 1,204 members · 🎙️ 📺" in text
         and "`two` · members unknown · 🎙️" in text
         and "**Quiet** · `three` · 3 members" in text
         and "Quiet** · `three` · 3 members ·" not in text)
    asked_in_server = asyncio.run(servers(ctx(Adapter(rows), guild="den")))
    r.ok("asked in a server: sent by DM instead of posted there",
         asked_in_server.private)
    failed = asyncio.run(servers(ctx(Adapter(fail="503"))))
    r.ok("a failed lookup says so, never 'no servers'",
         "couldn't list my servers: 503" in failed.content)

    from corbin.core.registry import registry
    spec = {s.name: s for s in registry.ai_invocable()}["servers"]
    r.ok("owner only, and works in DMs", spec.owner_only
         and not spec.guild_only)

    # Owner's rule: nobody else may ask, admins and server owners included.
    from _harness import member
    from corbin.core.permissions import Perm
    from corbin.core.risk import Decision, assess
    for who, m in (("a member", member("x")),
                   ("a server admin", member("adm", perms=Perm.ADMINISTRATOR)),
                   ("a server's owner", member("own", owner=True))):
        v = assess(spec, surface=Surface.AI, invoker=m, args={})
        r.ok(f"{who} is refused", v.decision in (Decision.REFUSED,
                                                Decision.DENIED))
    v = assess(spec, surface=Surface.AI, invoker=member("owner"), args={},
               is_bot_owner=True)
    r.check("the bot owner runs it", v.decision, Decision.EXECUTE)
    from corbin.ai.command_help import CommandHelp
    h = CommandHelp(registry, bot_owner_id="owner")
    r.ok("not in anyone else's help, admin or not", all(
        "list the servers Corbin is in" not in h.overview(m, True, "g1")
        for m in (member("x"), member("adm", perms=Perm.ADMINISTRATOR))))
    r.ok("...but in the owner's", "list the servers Corbin is in" in
         h.overview(member("owner"), False))

    # The adapter: REST first, the gateway cache fills gaps, both down
    # raises rather than answering "none".
    from corbin.platforms.fluxer.adapter import FluxerAdapter

    class Http:
        def __init__(self, rows=None, fail=False):
            self.rows, self.fail = rows, fail

        async def get_current_user_guilds(self):
            if self.fail:
                raise ConnectionError("down")
            return self.rows

    def adapter(http, cached=()):
        a = FluxerAdapter.__new__(FluxerAdapter)
        a.client = types.SimpleNamespace(
            guilds=[types.SimpleNamespace(id=g, name=n) for g, n in cached])
        a._rest = lambda: http

        async def count(gid):
            return {"b": 7}.get(gid)
        a.member_count = count
        return a
    got = asyncio.run(adapter(Http([{"id": "b", "name": "beta"},
                                    {"id": "a", "name": "Alpha"}]),
                              cached=[("c", "gamma")]).guild_list())
    r.check("REST plus anything only the cache knows, sorted by name",
            [(g["id"], g["members"]) for g in got],
            [("a", None), ("b", 7), ("c", None)])
    got = asyncio.run(adapter(Http(fail=True),
                              cached=[("c", "gamma")]).guild_list())
    r.check("REST down: the cache", [g["id"] for g in got], ["c"])
    try:
        asyncio.run(adapter(Http(fail=True)).guild_list())
        raised = False
    except RuntimeError:
        raised = True
    r.ok("both down: raises, never an empty list", raised)


def suite_image_topic(r: Results) -> None:
    """Image search runs only when the message is about images; ordinary
    chat the classifier misread goes to conversation instead."""
    from corbin.commands import util_cmds
    from corbin.core.registry import registry

    specs = {s.name: s for s in registry.ai_invocable()}
    search = specs[next(n for n in specs
                        if n.replace("-", "_") == "image_search")]
    nxt = specs[next(n for n in specs
                     if n.replace("-", "_") == "next_image")]
    r.ok("image search is gated", search.topic == util_cmds.IMAGE_TOPIC)
    r.ok("every image-search example passes its gate",
         all(search.on_topic(e) for e in search.examples))
    r.ok("every next-image example passes its gate",
         all(nxt.on_topic(e) for e in nxt.examples))
    for text in ("find me a picture of a raven", "show me pics of otters",
                 "image search northern lights", "can you find a gif of a cat",
                 "send a photo of the eiffel tower"):
        r.ok(f"searches: {text!r}", search.on_topic(text))
    for text in ("what does a raven eat", "show me how to fix this",
                 "look up the weather in paris", "find the best pizza nearby",
                 "who is the president of france", "a fox"):
        r.ok(f"chats: {text!r}", not search.on_topic(text))
    for text in ("next image", "another one", "show me another one",
                 "one more please", "Corbin, next one"):
        r.ok(f"next image: {text!r}", nxt.on_topic(text))
    for text in ("lol another one bites the dust", "next week works for me",
                 "one more thing about the server", "I want more coffee"):
        r.ok(f"no next image: {text!r}", not nxt.on_topic(text))


SUITES = [
    ("owner's server list", suite_server_list),
    ("reminder time parsing", suite_time_parsing),
    ("reminder timezones", suite_reminder_zones),
    ("image search paging", suite_image_paging),
    ("image search topic gate", suite_image_topic),
    ("reminder store", suite_store),
    ("verse translation hints", suite_verse_hints),
    ("progress bar", suite_progress),
    ("echo", suite_echo),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
