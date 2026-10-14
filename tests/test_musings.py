"""Offline suite for unprompted musings."""
from __future__ import annotations
import asyncio, datetime as dt, logging, random, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai import musings as M
from corbin.ai.musings import (MusingPlanner, Musings, parse_musing,
                               pick_channel)
from corbin.ai.presence import PresenceEngine

UTC = dt.timezone.utc
# Noon UTC on a fixed day; the planner runs in UTC here for determinism.
NOON = dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC).timestamp()


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def _planner(clock, seed=1, path=None):
    return MusingPlanner(path, rng=random.Random(seed), clock=clock, tz=UTC)


def suite_planner(r: Results) -> None:
    # Over many simulated days: never more than 3, some days none, and the
    # average lands near the weights' mean (~1.2).
    clock = _Clock(NOON)
    p = _planner(clock, seed=7)
    counts = []
    for day in range(400):
        clock.t = NOON + day * 86400
        counts.append(len(p._guild("g")["slots"]))
    r.ok("never more than three a day", max(counts) <= 3)
    r.ok("some days have none", 0 in counts)
    r.ok("averages about one a day",
         0.9 <= sum(counts) / len(counts) <= 1.5)

    # Slots fall inside the day, at varied times.
    day_start = dt.datetime(2026, 7, 15, tzinfo=UTC).timestamp()
    clock.t = day_start + 1
    p2 = _planner(clock, seed=3)
    slots = []
    for d in range(30):
        clock.t = day_start + d * 86400 + 1
        g = p2._guild("g")
        slots += [(s - (day_start + d * 86400)) / 3600 for s in g["slots"]]
    r.ok("slots stay within their own day",
         all(0 <= h < 24 for h in slots))
    r.ok("no fixed time of day", len({round(h) for h in slots}) > 5)

    # Due, caps and gap, with a hand-made plan.
    clock.t = NOON
    p3 = _planner(clock)
    g = p3._guild("g")
    g["slots"] = [NOON - 60, NOON - 30, NOON - 10, NOON + 3600]
    r.ok("a passed slot is due", p3.due("g"))
    p3.consume("g", "hot dogs")
    r.ok("the gap blocks the next one", not p3.due("g"))
    r.check("topic remembered", p3.recent_topics("g"), ["hot dogs"])
    clock.t = NOON + M.MIN_GAP
    r.ok("due again after the gap", p3.due("g"))
    g["sent"] = [NOON - 1000, NOON - 2000, NOON - 3000]
    r.ok("three in 24h blocks a fourth", not p3.due("g"))

    # A skip uses the slot without counting as a post.
    clock.t = NOON
    p4 = _planner(clock)
    p4._guild("g")["slots"] = [NOON - 5]
    p4.consume("g")
    r.ok("a skip consumes the slot", not p4.due("g"))
    r.check("...but records no post", p4._guild("g")["sent"], [])

    # Persistence: a restart keeps the day's plan and the cap.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / M.STATE_FILE
        a = _planner(clock, path=path)
        a._guild("g")["slots"] = [NOON - 5]
        a.consume("g", "owls")
        b = _planner(clock, seed=99, path=path)
        r.check("restart keeps the topics", b.recent_topics("g"), ["owls"])
        r.ok("...and the cap", not b.due("g"))


def suite_parse(r: Results) -> None:
    r.check("SKIP is silence", parse_musing("SKIP"), None)
    r.check("empty is silence", parse_musing(""), None)
    r.check("no MESSAGE line is silence", parse_musing("hot dogs!"), None)
    r.check("parses topic and message",
            parse_musing("TOPIC: National Hot Dog Day\n"
                         "MESSAGE: today is National Hot Dog Day, apparently."),
            ("today is National Hot Dog Day, apparently.",
             "National Hot Dog Day"))
    r.check("a multi-line message survives",
            parse_musing("TOPIC: ravens\nMESSAGE: line one\nline two")[0],
            "line one\nline two")
    r.check("MESSAGE: SKIP is silence",
            parse_musing("TOPIC: x\nMESSAGE: SKIP"), None)


def suite_channel(r: Results) -> None:
    now = NOON
    quiet = {"a": now - 3600, "b": now - 60}
    last = quiet.get
    r.check("busiest channel once quiet",
            pick_channel([("a", now - 3600)], last, now), "a")
    r.check("still mid-conversation means not yet",
            pick_channel([("a", now - 3600), ("b", now - 60)], last, now),
            None)
    r.check("a dead server gets nothing",
            pick_channel([("a", now - M.ACTIVE_WITHIN - 1)],
                         lambda c: now - M.ACTIVE_WITHIN - 1, now), None)
    r.check("no channels, no musing", pick_channel([], last, now), None)

    # A named general chat wins over the busiest channel...
    r.check("the general chat, not the busiest",
            pick_channel([("a", now - 3600), ("g", now - 7200)],
                         lambda c: now - 3600, now, general="g"), "g")
    # ...but still waits for it to go quiet...
    r.check("general chat mid-conversation means not yet",
            pick_channel([("a", now - 3600)], lambda c: now - 60, now,
                         general="g"), None)
    # ...and a dead server still gets nothing.
    r.check("general chat in a dead server gets nothing",
            pick_channel([("a", now - M.ACTIVE_WITHIN - 1)],
                         lambda c: None, now, general="g"), None)


class _LLM:
    class constants:
        MODEL_FAST = "fast"; MODEL_MAIN = "main"
        EFFORT_FAST = "none"; EFFORT_MAIN = "low"; EFFORT_PRESENCE = "low"

    def __init__(self, text):
        self.text, self.calls = text, []

    async def call(self, **kw):
        self.calls.append(kw)
        return type("R", (), {"text": self.text, "truncated": False})()


class _Conv:
    soul = "SOUL"

    def __init__(self, entries): self._e = entries
    def entries(self, cid): return list(self._e.get(cid, []))


class _Memory:
    def __init__(self, facts):
        self.facts = facts

    def get_server(self, gid):
        return type("S", (), {"facts": self.facts})()


class _Always(random.Random):
    """An rng whose weighted choice is always ``kind``."""

    def __init__(self, kind):
        super().__init__(0)
        self.kind = kind

    def choices(self, population, weights=None, **kw):
        return [self.kind if self.kind in population else population[0]]


def suite_engine(r: Results) -> None:
    old = dt.datetime.fromtimestamp(NOON, UTC) - dt.timedelta(hours=1)
    conv = _Conv({"c1": [{"author": "Sam", "content": "night all",
                          "is_bot": False, "timestamp": old}]})
    llm = _LLM("TOPIC: hot dogs\nMESSAGE: hot dogs are a sandwich, fight me.")
    clock = _Clock(dt.datetime.now(UTC).timestamp())
    planner = _planner(clock)
    planner._guild("g")["slots"] = [clock.t - 5]
    sent = []

    async def send(cid, text, target):
        sent.append((cid, text, target))

    eng = PresenceEngine(llm, conv, send=send, is_enabled=lambda g, c: True)
    eng.musings = Musings(llm, conv, planner, memory=_Memory([]),
                          rng=_Always("open"))
    eng.activity = {"g": {"c1": clock.t - 3600}}
    r.check("a due musing posts once", asyncio.run(eng.muse()), 1)
    r.check("...in the active channel, replying to nobody",
            sent, [("c1", "hot dogs are a sandwich, fight me.", None)])
    r.ok("...on the main model", llm.calls[0]["model"] == "main")
    r.ok("...and the slot is spent", not planner.due("g"))
    r.ok("it puts the channel on cooldown", eng.on_cooldown("g", "c1"))

    # Presence off for the channel: nothing, and the slot is kept.
    planner2 = _planner(clock)
    planner2._guild("g")["slots"] = [clock.t - 5]
    eng2 = PresenceEngine(llm, conv, send=send, is_enabled=lambda g, c: False)
    eng2.musings = Musings(llm, conv, planner2)
    eng2.activity = {"g": {"c1": clock.t - 3600}}
    r.check("presence off means no musing", asyncio.run(eng2.muse()), 0)
    r.ok("...and the slot waits", planner2.due("g"))

    # No server notes: still something on his mind.
    planner3 = _planner(clock)
    planner3._guild("g")["slots"] = [clock.t - 5]
    llm_open = _LLM("TOPIC: why cats knock things over\nMESSAGE: hm.")
    m = Musings(llm_open, conv, planner3, memory=_Memory([]))
    out = asyncio.run(m.compose("g", "c1"))
    r.check("nothing to draw on is an open musing", out[1],
            "why cats knock things over")
    prompt = llm_open.calls[0]["messages"][0]["content"]
    r.ok("...which never sees the last conversation", "night all" not in prompt)
    r.ok("...or today's date", planner3.today_label() not in prompt)

    # Server notes: a server musing, notes in the prompt.
    llm2 = _LLM("TOPIC: raven mimicry\nMESSAGE: ravens can mimic car alarms.")
    m2 = Musings(llm2, conv, planner3,
                 memory=_Memory(["the server loves corvids"]))
    out = asyncio.run(m2.compose("g", "c1", kind="server"))
    r.check("a server musing", out[1], "raven mimicry")
    r.ok("...built from the server's notes",
         "the server loves corvids" in llm2.calls[0]["messages"][0]["content"])
    r.ok("...and not the last conversation",
         "night all" not in llm2.calls[0]["messages"][0]["content"])
    r.check("a kind with nothing behind it falls back to open",
            asyncio.run(Musings(_LLM("TOPIC: x\nMESSAGE: y"), conv,
                                planner3).compose("g", "c1", kind="server")),
            ("y", "x"))

    # Open musings lead.
    m3 = Musings(llm, conv, planner3, memory=_Memory(["x"]),
                 rng=random.Random(5))
    picks = [m3._pick_kind(True) for _ in range(2000)]
    r.ok("open is the most common kind",
         picks.count("open") > picks.count("server") > 0)
    r.check("only the two kinds exist", sorted(set(picks)),
            ["open", "server"])
    r.check("no server notes means open only",
            {m3._pick_kind(False) for _ in range(200)}, {"open"})

    # The general chat gets the musing, not the always-on channel he was
    # last talked to in.
    sent.clear()
    planner5 = _planner(clock)
    planner5._guild("g")["slots"] = [clock.t - 5]
    eng5 = PresenceEngine(llm_open, conv, send=send,
                          is_enabled=lambda g, c: True)
    eng5.musings = Musings(llm_open, conv, planner5, memory=_Memory([]),
                           general_channel=lambda g: "gen")
    eng5.activity = {"g": {"c1": clock.t - 600}}
    r.check("posts in the general chat", (asyncio.run(eng5.muse()),
                                          [c for c, *_ in sent]),
            (1, ["gen"]))

    # No general chat: the always-on channel is skipped.
    sent.clear()
    planner6 = _planner(clock)
    planner6._guild("g")["slots"] = [clock.t - 5]
    eng6 = PresenceEngine(llm_open, conv, send=send,
                          is_enabled=lambda g, c: True)
    eng6.musings = Musings(llm_open, conv, planner6, memory=_Memory([]),
                           always_on=lambda g, c: c == "ai")
    eng6.activity = {"g": {"ai": clock.t - 1200, "c1": clock.t - 3600}}
    asyncio.run(eng6.muse())
    r.check("skips the always-on channel", [c for c, *_ in sent], ["c1"])

    # A SKIP spends the slot without posting.
    planner4 = _planner(clock)
    planner4._guild("g")["slots"] = [clock.t - 5]
    eng4 = PresenceEngine(_LLM("SKIP"), conv, send=send,
                          is_enabled=lambda g, c: True)
    eng4.musings = Musings(_LLM("SKIP"), conv, planner4,
                           memory=_Memory(["likes games"]))
    eng4.activity = {"g": {"c1": clock.t - 3600}}
    before = len(sent)
    r.check("a SKIP posts nothing", (asyncio.run(eng4.muse()),
                                     len(sent) - before), (0, 0))
    r.ok("...and spends the slot", not planner4.due("g"))


SUITES = [
    ("musing schedule", suite_planner),
    ("musing parsing", suite_parse),
    ("musing channel choice", suite_channel),
    ("musing engine", suite_engine),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
