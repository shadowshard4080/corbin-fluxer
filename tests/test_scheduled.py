"""Offline suite for scheduled content."""
from __future__ import annotations
import asyncio, logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.scheduled import (ANIMALS, Quote, Scheduler,
                                   animal_image_url, is_due, next_run_at,
                                   pick_animal, resolve_animal)
from corbin.core.settings import ChatSettings

UTC = timezone.utc


def suite_animals(r: Results) -> None:
    r.check("full roster", len(ANIMALS), 32)
    r.check("resolve by api key", resolve_animal("yeen"), "yeen")
    r.check("resolve by display name", resolve_animal("Hyena"), "yeen")
    r.check("case-insensitive", resolve_animal("snow leopard"), "snep")
    r.check("unknown is None", resolve_animal("dragon"), None)
    r.check("empty is None", resolve_animal(""), None)
    r.check("None is None", resolve_animal(None), None)

    key, label = pick_animal("fox")
    r.check("preferred honoured", (key, label), ("fox", "Fox"))
    rk, rl = pick_animal(None)
    r.ok("random still valid", rk in ANIMALS and rl == ANIMALS[rk])
    bk, _ = pick_animal("dragon")
    r.ok("unknown falls back to random", bk in ANIMALS)
    r.ok("url uses the api key", animal_image_url("fox").endswith("animal=fox"))


def suite_quote(r: Results) -> None:
    q = Quote("Be kind.", "Someone")
    rendered = q.render()
    r.ok("quote text present", "Be kind." in rendered)
    r.ok("author attributed", "Someone" in rendered)
    r.ok("rendered as a blockquote", rendered.startswith(">"))
    # Author names contain hyphens, which is why the old
    # `quote.split(" - ")` parsing was removed.
    hy = Quote("Words.", "Jean-Paul Sartre")
    r.ok("hyphenated author survives", "Jean-Paul Sartre" in hy.render())


def suite_timing(r: Results) -> None:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    r.check("a passed time rolls to tomorrow",
            next_run_at(now, 8, 0).day, 2)
    r.check("a future time stays today", next_run_at(now, 20, 0).day, 1)

    r.ok("due when never sent", is_due(now, 8, 0, None))
    r.ok("not due twice in a day", not is_due(now, 8, 0, "2026-01-01"))
    r.ok("not due before the time", not is_due(now, 20, 0, None))
    # Catch-up: a restart or slow tick must not skip the day entirely.
    r.ok("catches up after an outage", is_due(now, 2, 0, "2025-12-30"))
    r.ok("due exactly on the minute", is_due(now, 9, 0, None))


def suite_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = ChatSettings(Path(tmp) / "c.json")
        r.check("nothing scheduled by default", s.guilds_with("daily_quote"), [])

        s.set_daily_quote("g", enabled=True, channel_id="1", hour=9, minute=30)
        r.check("guild is now due-able", s.guilds_with("daily_quote"), ["g"])
        cfg = s.daily_quote("g")
        r.check("hour stored", cfg["hour"], 9)
        r.check("minute stored", cfg["minute"], 30)

        s.set_daily_quote("g2", enabled=True, channel_id="2")
        r.check("defaults to 08:00", s.daily_quote("g2")["hour"], 8)
        s.set_daily_quote("g3", enabled=True, channel_id="3", hour=99)
        r.check("hour clamped", s.daily_quote("g3")["hour"], 23)

        # Enabled but channel-less must not be scheduled.
        s.set_daily_quote("g4", enabled=True)
        r.ok("channel-less guild excluded", "g4" not in s.guilds_with("daily_quote"))

        s.mark_scheduled_sent("g", "daily_quote", "2026-01-01")
        r.check("marker stored", s.scheduled_marker("g", "daily_quote"),
                "2026-01-01")

        s.set_daily_quote("g", enabled=False)
        r.ok("disabled guild drops out", "g" not in s.guilds_with("daily_quote"))
        r.check("channel retained while off", s.daily_quote("g")["channel_id"], "1")

        s.set_hourly_animals("h", enabled=True, channel_id="9", animal="fox")
        r.check("animals tracked separately", s.guilds_with("hourly_animals"),
                ["h"])
        r.check("chosen animal stored", s.hourly_animals("h")["animal"], "fox")
        r.check("quote list unaffected", s.guilds_with("daily_quote"),
                ["g2", "g3"])


def suite_scheduler(r: Results) -> None:
    calls = []

    async def good(): calls.append("good")

    async def boom(): raise RuntimeError("nope")

    s = Scheduler(tick_seconds=0.01)
    s.add("good", good)
    s.add("boom", boom)
    s.add("good2", good)
    asyncio.run(s.tick())
    # One failing job must not stop the others.
    r.check("all jobs attempted despite a failure", calls, ["good", "good"])

    empty = Scheduler()
    asyncio.run(empty.tick())
    r.ok("no jobs is fine", True)


def suite_animal_delivery(r: Results) -> None:
    """The hourly animal must be uploaded, not linked.

    tinyfox serves a different animal on every request (verified live:
    four requests, four distinct images), but the URL never changes.
    Desktop refetches it and sees variety; the mobile client caches by
    URL and shows the same wolf forever. Uploading the bytes gives
    every post its own attachment URL, so there is nothing to collide.
    """
    import ast as _ast
    from pathlib import Path

    from corbin.core.scheduled import animal_image_url

    r.check("the url is a fixed endpoint, not a per-image link",
            animal_image_url("wolf"),
            "https://api.tinyfox.dev/img?animal=wolf")

    root = Path(__file__).resolve().parent.parent
    tree = _ast.parse((root / "bot.py").read_text(encoding="utf-8"))
    job = None
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == "_hourly_animal_job":
            job = node
            break
    if job is None:
        r.ok("found the hourly animal job", False)
        return
    r.ok("found the hourly animal job", True)

    source = _ast.dump(job)
    kwargs = {k.arg for n in _ast.walk(job)
              if isinstance(n, _ast.Call) for k in n.keywords if k.arg}
    r.ok("the bytes are fetched", "_fetch_bytes" in source)
    r.ok("...and uploaded as a file", "files" in kwargs)
    # This is the bug: a static URL handed to the embed.
    r.ok("the embed does NOT link the image url",
         "image_url" not in kwargs)
    r.ok("credit is kept", "footer" in kwargs)


SUITES = [
    ("hourly animal delivery", suite_animal_delivery),
    ("animal roster", suite_animals),
    ("quote rendering", suite_quote),
    ("schedule timing", suite_timing),
    ("scheduled settings", suite_settings),
    ("scheduler resilience", suite_scheduler),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
