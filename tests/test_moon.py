"""Offline suite for moon phases."""
from __future__ import annotations
import logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.moon import (MOON_NAMES, PHASES, MoonCalculator,
                              find_ephemeris, illumination, moon_name,
                              phase_name)
from corbin.core.settings import ChatSettings

REPO = str(Path(__file__).resolve().parent.parent / "data")


def suite_pure(r: Results) -> None:
    r.check("eight named phases", len(PHASES), 8)
    r.check("new at 0", phase_name(0)[0], "New moon")
    r.check("first quarter at 90", phase_name(90)[0], "First quarter")
    r.check("full at 180", phase_name(180)[0], "Full moon")
    r.check("last quarter at 270", phase_name(270)[0], "Last quarter")
    # The scale wraps, so just shy of 360 is new, not waning crescent.
    r.check("wraps around zero", phase_name(359)[0], "New moon")
    r.check("nearest wins", phase_name(179)[0], "Full moon")
    r.ok("emoji returned", phase_name(180)[1] == "\U0001F315")

    r.check("new moon is dark", round(illumination(0) * 100), 0)
    r.check("full moon is lit", round(illumination(180) * 100), 100)
    r.check("quarter is half", round(illumination(90) * 100), 50)
    r.ok("monotonic to full",
         illumination(45) < illumination(90) < illumination(135))

    r.check("twelve traditional names", len(MOON_NAMES), 12)
    r.check("january", moon_name(datetime(2026, 1, 3, tzinfo=timezone.utc)),
            "Wolf Moon")
    r.check("september",
            moon_name(datetime(2026, 9, 26, tzinfo=timezone.utc)),
            "Harvest Moon")


def suite_calculator(r: Results) -> None:
    # A missing ephemeris must degrade, not crash: the bot should still
    # start and every other command keep working.
    missing = MoonCalculator("/nonexistent/de421.bsp")
    r.ok("missing ephemeris is unavailable", not missing.available)
    r.check("now() returns None", missing.now(), None)
    r.check("upcoming() is empty", missing.upcoming(), [])
    r.check("next_full_moon() is None", missing.next_full_moon(), None)
    r.ok("is_full_today() is False", not missing.is_full_today())
    r.check("no ephemeris anywhere", find_ephemeris("/nonexistent"), None)

    path = find_ephemeris(REPO)
    if path is None:
        r.ok("ephemeris not present here (skipped)", True)
        return
    m = MoonCalculator(path)
    if not m.available:
        r.ok("skyfield unavailable (skipped)", True)
        return

    now = m.now()
    r.ok("phase computed", now is not None)
    r.ok("angle in range", 0 <= now.angle <= 360)
    r.ok("illumination in range", 0 <= now.illuminated <= 1)
    r.ok("percent is an int", isinstance(now.percent, int))
    r.ok("waxing matches the angle", now.waxing == (now.angle < 180))
    r.ok("next full moon found", now.next_full is not None)
    r.ok("it is in the future", now.next_full > datetime.now(timezone.utc))

    events = m.upcoming(days=90)
    r.ok("about three months of events", 8 <= len(events) <= 16)
    r.ok("chronological", all(events[i][0] <= events[i + 1][0]
                              for i in range(len(events) - 1)))
    kinds = {k for _t, k in events}
    r.check("all four phase kinds appear", len(kinds), 4)

    # A known full moon should be detected on its own date.
    known = datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)
    r.ok("detects a full moon on the day", m.is_full_today(known))
    r.ok("and not the day before",
         not m.is_full_today(known - timedelta(days=2)))


def suite_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = ChatSettings(Path(tmp) / "c.json")
        r.check("off by default", s.fullmoon("g"), None)
        r.check("no guilds", s.guilds_with_fullmoon(), [])
        s.set_fullmoon("g", "c1")
        r.check("stored", s.fullmoon("g"), "c1")
        r.check("listed", s.guilds_with_fullmoon(), ["g"])
        r.ok("persists",
             ChatSettings(Path(tmp) / "c.json").fullmoon("g") == "c1")
        s.set_fullmoon("g", None)
        r.check("cleared", s.fullmoon("g"), None)
        r.check("delisted", s.guilds_with_fullmoon(), [])
        r.check("no guild is safe", s.fullmoon(None), None)


SUITES = [
    ("moon phase maths", suite_pure),
    ("moon calculator", suite_calculator),
    ("full moon settings", suite_settings),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
