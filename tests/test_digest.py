"""Offline suite for the usage digest."""
from __future__ import annotations
import logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.commands.digest_cmds import render
from corbin.core.digest import Counters, DigestStore, DIGEST_HOUR

TZ = timezone(timedelta(hours=-8))


def suite_counting(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = DigestStore(tmp)
        r.check("starts empty", s.counters.total, 0)

        for _ in range(3):
            s.record("message", user_id="u1", user_name="Lux",
                     guild_id="g", guild_name="Den")
        s.record("command", user_id="u2", user_name="Juniper", guild_id="g",
                 guild_name="Den")
        r.check("messages counted", s.counters.messages_sent, 3)
        r.check("commands counted separately", s.counters.commands_run, 1)
        r.check("total", s.counters.total, 4)
        r.check("two people", len(s.counters.users), 2)
        r.check("one guild", len(s.counters.guilds), 1)
        r.check("guild count aggregates",
                s.counters.guilds["g"]["count"], 4)

        r.check("top user ranked first", s.counters.top_users()[0][0], "u1")
        r.check("names retained",
                s.counters.top_users()[0][1]["name"], "Lux")

        s.record("message", user_id="u3", user_name="NoGuild", guild_id=None)
        r.check("a DM still counts the person", len(s.counters.users), 3)
        r.check("but adds no guild", len(s.counters.guilds), 1)

        r.check("persists across reload", DigestStore(tmp).counters.total, 5)


def suite_reset(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = DigestStore(tmp)
        for _ in range(5):
            s.record("message", user_id="u1", user_name="Lux", guild_id="g")

        posted = s.snapshot_and_reset("2026-01-01")
        r.check("snapshot has everything", posted.total, 5)
        r.check("window drained", s.counters.total, 0)
        r.check("users cleared", len(s.counters.users), 0)
        r.check("marker recorded", s.last_sent, "2026-01-01")

        # The reset subtracts the posted snapshot rather than wiping, so
        # anything counted DURING the send survives.
        live = DigestStore(tmp)
        for _ in range(2):
            live.record("message", user_id="u9", user_name="During",
                        guild_id="g")
        snapshot = Counters(messages_sent=1, users={"u9": {"count": 1}})
        live.counters.subtract(snapshot)
        r.check("in-flight activity survives", live.counters.messages_sent, 1)
        r.check("user partially decremented",
                live.counters.users["u9"]["count"], 1)

        gone = Counters(messages_sent=5, users={"u9": {"count": 99}})
        live.counters.subtract(gone)
        r.ok("over-subtraction cannot go negative",
             live.counters.messages_sent == 0)
        r.ok("emptied users are dropped", "u9" not in live.counters.users)


def suite_due(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = DigestStore(tmp)
        before = datetime(2026, 1, 1, DIGEST_HOUR - 1, tzinfo=TZ)
        at = datetime(2026, 1, 1, DIGEST_HOUR, tzinfo=TZ)
        later = datetime(2026, 1, 1, 23, tzinfo=TZ)

        r.ok("not due before the hour", not s.is_due(before))
        r.ok("due on the hour", s.is_due(at))
        r.ok("still due later that evening", s.is_due(later))

        s.snapshot_and_reset("2026-01-01")
        r.ok("not due twice the same day", not s.is_due(later))
        r.ok("due again the next day",
             s.is_due(datetime(2026, 1, 2, DIGEST_HOUR, tzinfo=TZ)))


def suite_render(r: Results) -> None:
    c = Counters(messages_sent=10, commands_run=4,
                 users={f"u{i}": {"name": f"P{i}", "count": 20 - i}
                        for i in range(15)},
                 guilds={"g1": {"name": "Den", "count": 9}})

    overview = render(c, "overview")
    values = {f.name: f.value for f in overview.fields}
    r.check("messages shown", values["Messages sent"], "10")
    r.check("commands shown", values["Commands run"], "4")
    r.check("people counted", values["People"], "15")

    users = render(c, "users")
    r.ok("users section lists people",
         any("Most active" in f.name for f in users.fields))
    r.ok("top ten only", all(
        f.value.count("\n") < 10 for f in users.fields))

    servers = render(c, "servers")
    r.ok("servers section names the guild",
         any("Den" in f.value for f in servers.fields))

    empty = render(Counters(), "overview")
    r.ok("empty window says so", "Nothing recorded" in (empty.description or ""))
    r.ok("empty users section is graceful",
         "No activity" in (render(Counters(), "users").description or ""))


def suite_new_counters(r: Results) -> None:
    import asyncio, tempfile
    from corbin.ai.search import SearchPipeline
    from corbin.core.voice import Session, VoiceManager

    with tempfile.TemporaryDirectory() as tmp:
        store = DigestStore(tmp)
        store.record_search(); store.record_search()
        store.record_voice(120.0)
        r.check("searches counted", store.counters.web_searches, 2)
        r.check("voice counted", store.counters.voice_seconds, 120.0)
        again = DigestStore(tmp)
        r.check("searches persist", again.counters.web_searches, 2)
        r.check("voice persists", again.counters.voice_seconds, 120.0)

        store.record_search()
        posted = store.snapshot_and_reset("2026-10-16")
        r.check("posted carries searches", posted.web_searches, 3)
        r.check("reset clears searches", store.counters.web_searches, 0)
        r.check("reset clears voice", store.counters.voice_seconds, 0.0)

    # Voice time is credited on leave, and a live session can be split.
    credited = []
    vm = VoiceManager(bot=None)
    vm.on_voice_time = credited.append

    class Client:
        _room = None
        class _gateway:
            @staticmethod
            async def update_voice_state(**_): pass
        async def stop(self): pass

    s = Session(guild_id="g", channel_id="c", client=Client())
    s.counted_from -= 600
    vm._sessions["g"] = s
    mid = vm.collect_voice_time()
    r.ok("a live session is collected", 599 < mid < 602)
    r.ok("...and restarts its count", vm.collect_voice_time() < 1)
    s.counted_from -= 60
    asyncio.run(vm.disconnect("g"))
    r.ok("leaving credits the remainder",
         len(credited) == 1 and 59 < credited[0] < 62)

    # The pipeline's hook fires once per search, even a failed one.
    hits = []
    pipe = SearchPipeline.__new__(SearchPipeline)
    pipe.on_search = lambda: hits.append(1)

    class Impl:
        async def _perform_live_search(self, q):
            raise RuntimeError("down")
    pipe._impl = Impl()
    r.check("failed search returns None", asyncio.run(pipe.run("x")), None)
    r.check("...but is still counted", len(hits), 1)


def suite_commands_counted(r: Results) -> None:
    """The digest's "Commands run" was always 0: nothing recorded one.
    Executor.on_command now fires after every command that ran, and not
    for one that failed."""
    import asyncio, tempfile
    from corbin.core.context import Channel, Member, Surface, User
    from corbin.core.digest import DigestStore
    from corbin.core.dispatch import DispatchResult
    from corbin.core.registry import CommandSpec, Risk
    from corbin.core.risk import Decision
    from corbin.runtime import Executor

    async def ok(ctx):
        return "fine"

    async def boom(ctx):
        raise RuntimeError("no")

    with tempfile.TemporaryDirectory() as tmp:
        store = DigestStore(tmp)
        ex = Executor(adapter=None, dispatcher=None)
        ex.on_command = lambda spec, ctx: store.record(
            "command", user_id=ctx.invoker.id, user_name="Quill",
            guild_id=ctx.guild_id, guild_name="Den")
        member = Member(user=User(id="u1", name="Quill"), guild_id="g1")
        chan = Channel(id="c1", name="x", guild_id="g1")

        def run(handler):
            spec = CommandSpec(name="t", description="d", handler=handler,
                               risk=Risk.SAFE)
            res = DispatchResult(handled=True, decision=Decision.EXECUTE,
                                 spec=spec, args={})
            return asyncio.run(ex.run(res, invoker=member, channel=chan,
                                      guild=None, surface=Surface.AI))
        run(ok)
        run(ok)
        run(boom)
        r.check("two commands ran, one failed: 2 counted",
                store.counters.commands_run, 2)
        r.check("...and they count for the user too",
                store.counters.users["u1"]["count"], 2)


SUITES = [
    ("digest: commands are counted", suite_commands_counted),
    ("digest counting", suite_counting),
    ("digest reset", suite_reset),
    ("digest scheduling", suite_due),
    ("digest rendering", suite_render),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
