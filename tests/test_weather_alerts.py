"""Offline suite for daily weather alerts.

The port shipped ``weatheralerts`` with no job to deliver them: alerts
were saved and never posted. These cover when an alert is due in its own
timezone, the command's parsing and safety, and the delivery job's
contract (fire once per local day, retry on a failed fetch).
"""
from __future__ import annotations
import asyncio, logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import weather_alerts as wa

UTC = timezone.utc


def suite_parsing(r: Results) -> None:
    r.check("7am", wa.parse_clock("7am"), (7, 0))
    r.check("7:30 pm", wa.parse_clock("7:30 pm"), (19, 30))
    r.check("12am is midnight", wa.parse_clock("12am"), (0, 0))
    r.check("12pm is noon", wa.parse_clock("12pm"), (12, 0))
    r.check("24h clock", wa.parse_clock("18:05"), (18, 5))
    for bad in ("25:00", "13pm", "7:75", "morning", ""):
        r.check(f"refuses {bad!r}", wa.parse_clock(bad), None)

    r.check("PST is Los Angeles", wa.tz_name(wa.resolve_tz("PST")),
            "America/Los_Angeles")
    r.check("IANA names pass through",
            wa.tz_name(wa.resolve_tz("Europe/London")), "Europe/London")
    r.check("UTC", wa.resolve_tz("utc"), UTC)
    # A bad zone must be refused, not silently read as UTC: the alert
    # would then fire hours off, every day, looking like it works.
    r.check("an unknown zone is refused", wa.resolve_tz("Not/AZone"), None)
    r.check("ambiguous CST is refused", wa.resolve_tz("CST"), None)


def suite_due(r: Results) -> None:
    alert = wa.normalise({"location": "Denver", "hour": 7,
                          "tz": "America/Los_Angeles", "channel_id": "c"})
    # 07:00 PDT on 2026-09-26 is 14:00 UTC.
    r.check("not before its local time",
            wa.due(alert, datetime(2026, 9, 26, 13, 59, tzinfo=UTC)), None)
    r.check("fires at its local time, marking the LOCAL date",
            wa.due(alert, datetime(2026, 9, 26, 14, 0, tzinfo=UTC)),
            "2026-09-26")
    sent = dict(alert, last_sent="2026-09-26")
    r.check("once per local day",
            wa.due(sent, datetime(2026, 9, 26, 15, 0, tzinfo=UTC)), None)
    r.check("...and again the next day",
            wa.due(sent, datetime(2026, 9, 27, 14, 1, tzinfo=UTC)),
            "2026-09-27")
    r.check("a bot down all morning doesn't post it at 4pm",
            wa.due(alert, datetime(2026, 9, 26, 23, 0, tzinfo=UTC)), None)
    r.check("...but a short restart still catches up",
            wa.due(alert, datetime(2026, 9, 26, 16, 30, tzinfo=UTC)),
            "2026-09-26")

    legacy = wa.normalise({"location": "Oslo", "hour": 9, "channel_id": "c"})
    r.ok("old alerts get an id", bool(legacy["id"]))
    r.check("...and are read as the UTC they always were", legacy["tz"], "UTC")
    r.check("...firing on the UTC hour",
            wa.due(legacy, datetime(2026, 9, 26, 9, 5, tzinfo=UTC)),
            "2026-09-26")

    nxt = wa.next_fire(alert, datetime(2026, 9, 26, 15, 0, tzinfo=UTC))
    r.check("next post rolls to tomorrow once today's has passed",
            (nxt.day, nxt.hour), (27, 7))


def _ctx(settings, memory_tz=None):
    memory = None
    if memory_tz:
        memory = SimpleNamespace(
            get_user=lambda uid: SimpleNamespace(timezone=memory_tz))
    return SimpleNamespace(
        extras={"settings": settings, "memory": memory,
                "channel_index": {"weather": "555"}},
        guild_id="g", guild=SimpleNamespace(id="g", name="Den"),
        channel=SimpleNamespace(id="111", name="general"),
        invoker=SimpleNamespace(id="u"))


def suite_command(r: Results) -> None:
    from corbin.commands import info
    from corbin.core.context import Embed
    from corbin.core.settings import ChatSettings

    looked_up = []

    async def fake_embed(location, units="imperial"):
        looked_up.append(location)
        if location == "Nowhere":
            return "Couldn't find weather for “Nowhere”."
        return Embed(title=f"Weather in {location}")

    real = info.weather_embed
    info.weather_embed = fake_embed
    try:
        with tempfile.TemporaryDirectory() as tmp:
            settings = ChatSettings(Path(tmp) / "s.json")
            call = lambda **kw: asyncio.run(
                info.weatheralerts(_ctx(settings, kw.pop("tz_mem", None)),
                                   **kw))

            out = call(action="add", location="Denver", time="7am",
                       timezone="PST", channel="#weather")
            saved = settings.weather_alerts("g")
            r.ok("add posts a preview", getattr(out, "embed", None)
                 is not None)
            r.check("...saved with its own time and zone",
                    (saved[0]["hour"], saved[0]["tz"], saved[0]["channel_id"]),
                    (7, "America/Los_Angeles", "555"))

            out = call(action="add", location="Nowhere", time="7am")
            r.ok("a place that doesn't resolve is refused now, not at 7am",
                 isinstance(out, str) and len(settings.weather_alerts("g"))
                 == 1)
            r.ok("a bad timezone is refused",
                 "isn't a timezone" in call(action="add", location="Oslo",
                                            timezone="Mars/Olympus"))
            r.ok("a bad time is refused",
                 "couldn't read" in call(action="add", location="Oslo",
                                         time="whenever"))

            call(action="add", location="Oslo", time="18:00",
                 tz_mem="Europe/Oslo")
            oslo = [a for a in settings.weather_alerts("g")
                    if a["location"] == "Oslo"][0]
            r.check("no zone given uses the asker's learned one",
                    oslo["tz"], "Europe/Oslo")
            r.check("no channel given uses this one", oslo["channel_id"],
                    "111")

            out = call(action="remove")
            r.ok("remove with nothing named no longer wipes everything",
                 len(settings.weather_alerts("g")) == 2 and "Which" in out)
            call(action="remove", location=oslo["id"])
            r.check("remove by id", [a["location"] for a in
                                     settings.weather_alerts("g")],
                    ["Denver"])
            call(action="remove", location="all")
            r.check("remove all, when asked for",
                    settings.weather_alerts("g"), [])
    finally:
        info.weather_embed = real


def suite_job_wiring(r: Results) -> None:
    """The bug was a missing job; make its absence fail loudly."""
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8-sig")
    r.ok("the delivery job exists", "async def _weather_alerts_job" in src)
    r.ok("...and is scheduled",
         'scheduler.add("weather_alerts", _weather_alerts_job)' in src)
    r.ok("...and reads the alerts back", "settings.guilds_with_weather()"
         in src)


SUITES = [
    ("weather alert parsing", suite_parsing),
    ("weather alert timing", suite_due),
    ("weather alert command", suite_command),
    ("weather alert delivery wiring", suite_job_wiring),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
