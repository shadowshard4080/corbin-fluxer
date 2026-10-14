"""Offline suite for organic participation."""
from __future__ import annotations
import asyncio, datetime as dt, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.presence import (MIN_INTERVAL, PresenceEngine,
                                numbered_transcript, parse_decision,
                                presence_window, reply_target)


def _e(author, content, *, is_bot=False, age=0):
    return {"author": author, "content": content, "is_bot": is_bot,
            "timestamp": dt.datetime.now(dt.timezone.utc)
                         - dt.timedelta(seconds=age)}


def suite_window(r: Results) -> None:
    fresh = [_e("A", f"m{i}") for i in range(5)]
    r.check("all fresh kept", len(presence_window(fresh)), 5)

    mixed = [_e("A", "old", age=60 * 60), _e("B", "new")]
    r.check("stale dropped", [e["content"] for e in presence_window(mixed)],
            ["new"])

    # If everything is stale we still return something: an empty window
    # would make the evaluator see nothing and never speak again.
    allstale = [_e("A", "x", age=99999), _e("B", "y", age=99999)]
    r.check("all-stale falls back to the raw slice",
            len(presence_window(allstale)), 2)

    many = [_e("A", f"m{i}") for i in range(50)]
    r.check("window is capped", len(presence_window(many)), 20)
    r.check("cap keeps the newest", presence_window(many)[-1]["content"], "m49")


def suite_transcript(r: Results) -> None:
    entries = [_e("Sam", "hello"), _e("Alex", "hi there")]
    text, indexed = numbered_transcript(entries)
    r.ok("numbered from 1", text.startswith("#1 [Sam]: hello"))
    r.ok("second line numbered", "#2 [Alex]: hi there" in text)
    r.check("indexed matches", len(indexed), 2)
    long_text, _ = numbered_transcript([_e("A", "x" * 900)], max_len=50)
    r.ok("content truncated", len(long_text) < 120)
    r.check("empty input", numbered_transcript([])[0], "")


def suite_decision(r: Results) -> None:
    indexed = [_e("Sam", "anyone know?"), _e("Corbin", "hi", is_bot=True)]

    r.check("PASS stays quiet", parse_decision("PASS", indexed)[0], None)
    r.check("empty stays quiet", parse_decision("", indexed)[0], None)
    r.check("garbage stays quiet", parse_decision("maybe?", indexed)[0], None)

    target, reason = parse_decision("RESPOND #1 | they asked a question",
                                    indexed)
    r.check("picks the right entry", target["author"], "Sam")
    r.check("reason captured", reason, "they asked a question")
    r.ok("hash optional", parse_decision("RESPOND 1", indexed)[0] is not None)
    # "pass" inside the reason used to veto the RESPOND outright.
    target, reason = parse_decision(
        "RESPOND #1 | unanswered, don't let it pass", indexed)
    r.ok("'pass' in the reason does not veto a RESPOND", target is not None)
    r.check("a colon separator works too",
            parse_decision("RESPOND #1: asked a question", indexed)[1],
            "asked a question")
    r.check("only the first line is the decision",
            parse_decision("PASS\nRESPOND #1 | actually", indexed)[0], None)

    r.check("out of range is refused",
            parse_decision("RESPOND #99", indexed)[0], None)
    r.check("zero is refused", parse_decision("RESPOND #0", indexed)[0], None)
    # Replying to yourself is the classic loop.
    r.check("will not target Corbin's own message",
            parse_decision("RESPOND #2", indexed)[0], None)


def suite_reply_target(r: Results) -> None:
    sam, alex = _e("Sam", "what's for dinner"), _e("Alex", "no idea")
    entries = [sam, alex]

    r.check("no target stays None", reply_target("hi", entries, None), None)
    r.check("naming the target keeps it",
            reply_target("Sam, ramen", entries, sam)["author"], "Sam")
    r.check("naming someone else moves the arrow",
            reply_target("Alex has the right idea", entries, sam)["author"],
            "Alex")
    r.check("naming nobody keeps the original",
            reply_target("ramen sounds good", entries, sam)["author"], "Sam")
    r.check("naming both keeps the original",
            reply_target("Sam and Alex both right", entries, sam)["author"],
            "Sam")


class _Stub:
    """Minimal llm/conversation doubles."""

    class constants:
        MODEL_FAST = "fast"; MODEL_MAIN = "main"
        EFFORT_FAST = "none"; EFFORT_MAIN = "low"; EFFORT_PRESENCE = "low"

    def __init__(self, decision="PASS", reply="sure"):
        self.decision, self.reply = decision, reply
        self.calls = []

    async def call(self, *, model, **kw):
        self.calls.append(model)
        text = self.decision if model == "fast" else self.reply
        return type("R", (), {"text": text, "truncated": False})()


class _Conv:
    soul = ""
    def __init__(self, entries): self._e = entries
    def entries(self, cid): return list(self._e)


def suite_engine(r: Results) -> None:
    sent = []

    async def send(cid, text, target):
        sent.append((cid, text, target))

    def enabled(g, c): return True

    entries = [_e("Sam", "does anyone know how to fix this?"),
               _e("Alex", "no clue")]

    llm = _Stub(decision="RESPOND #1 | unanswered question", reply="try -v")
    eng = PresenceEngine(llm, _Conv(entries), send=send, is_enabled=enabled)
    r.ok("speaks when told to", asyncio.run(eng.consider("g", "c")))
    r.check("message sent", sent[0][1], "try -v")
    r.check("arrow points at the asker", sent[0][2]["author"], "Sam")
    r.ok("cooldown now set", eng.on_cooldown("g", "c"))
    sent.clear()
    r.ok("second attempt blocked by cooldown",
         not asyncio.run(eng.consider("g", "c")))
    r.check("nothing sent", len(sent), 0)

    quiet = PresenceEngine(_Stub(decision="PASS"), _Conv(entries),
                           send=send, is_enabled=enabled)
    r.ok("PASS stays silent", not asyncio.run(quiet.consider("g", "c")))

    off = PresenceEngine(_Stub(decision="RESPOND #1"), _Conv(entries),
                         send=send, is_enabled=lambda g, c: False)
    r.ok("disabled guild never speaks",
         not asyncio.run(off.consider("g", "c")))

    # Corbin must not answer his own last message.
    after_bot = [_e("Sam", "hi"), _e("Corbin", "hey", is_bot=True)]
    trailing = PresenceEngine(_Stub(decision="RESPOND #1"), _Conv(after_bot),
                              send=send, is_enabled=enabled)
    r.ok("silent when Corbin spoke last",
         not asyncio.run(trailing.consider("g", "c")))

    thin = PresenceEngine(_Stub(decision="RESPOND #1"), _Conv([_e("A", "yo")]),
                          send=send, is_enabled=enabled)
    r.ok("needs more than one message",
         not asyncio.run(thin.consider("g", "c")))

    # dirty/cooldown bookkeeping shared with the direct-reply path
    eng2 = PresenceEngine(_Stub(), _Conv(entries), send=send,
                          is_enabled=enabled)
    eng2.mark_dirty("g", "c")
    r.check("dirty tracked", len(eng2.dirty), 1)
    eng2.mark_spoke("g", "c")
    r.check("speaking clears dirty", len(eng2.dirty), 0)
    r.ok("speaking starts the cooldown", eng2.on_cooldown("g", "c"))
    eng2.mark_dirty(None, "c")
    r.check("no guild is ignored", len(eng2.dirty), 0)
    r.check("cooldown default", MIN_INTERVAL, 90.0)


SUITES = [
    ("presence window", suite_window),
    ("presence transcript", suite_transcript),
    ("presence decision", suite_decision),
    ("presence reply target", suite_reply_target),
    ("presence engine", suite_engine),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
