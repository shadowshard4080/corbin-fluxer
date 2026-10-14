"""Moon phases.

Phases are **computed** from the JPL ephemeris rather than read from a
table. The Discord build carried a hardcoded ``FULL_MOONS_2026`` list,
which silently stops working on 1 January 2027; skyfield gives exact times
for any year.

Traditional month names (Wolf Moon, Snow Moon, ...) are kept, since those
are cultural rather than astronomical and there is nothing to compute.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("corbin.moon")

#: Phase angle (degrees) -> name and emoji. 0 is new, 180 is full.
PHASES: tuple[tuple[float, str, str], ...] = (
    (0, "New moon", "🌑"),
    (45, "Waxing crescent", "🌒"),
    (90, "First quarter", "🌓"),
    (135, "Waxing gibbous", "🌔"),
    (180, "Full moon", "🌕"),
    (225, "Waning gibbous", "🌖"),
    (270, "Last quarter", "🌗"),
    (315, "Waning crescent", "🌘"),
)

#: Traditional northern-hemisphere names, by month of the full moon.
MOON_NAMES = {
    1: "Wolf Moon", 2: "Snow Moon", 3: "Worm Moon", 4: "Pink Moon",
    5: "Flower Moon", 6: "Strawberry Moon", 7: "Buck Moon",
    8: "Sturgeon Moon", 9: "Harvest Moon", 10: "Hunter's Moon",
    11: "Beaver Moon", 12: "Cold Moon",
}

#: Candidate ephemeris locations, most specific first.
EPHEMERIS_NAMES = ("de421.bsp",)


def find_ephemeris(*roots: str | os.PathLike[str]) -> Optional[Path]:
    for root in roots:
        if not root:
            continue
        base = Path(root)
        for name in EPHEMERIS_NAMES:
            for candidate in (base / name,):
                if candidate.is_file():
                    return candidate
    return None


def phase_name(degrees: float) -> tuple[str, str]:
    """Closest named phase for an angle. Returns (name, emoji)."""
    best = min(PHASES, key=lambda p: min(abs(degrees - p[0]),
                                         360 - abs(degrees - p[0])))
    return best[1], best[2]


def illumination(degrees: float) -> float:
    """Fraction of the disc lit, 0.0 to 1.0."""
    return (1 - math.cos(math.radians(degrees))) / 2


def moon_name(when: datetime) -> str:
    return MOON_NAMES.get(when.month, "Full Moon")


@dataclass
class MoonNow:
    angle: float
    name: str
    emoji: str
    illuminated: float
    next_full: Optional[datetime] = None
    next_new: Optional[datetime] = None

    @property
    def percent(self) -> int:
        return round(self.illuminated * 100)

    @property
    def waxing(self) -> bool:
        return self.angle < 180


class MoonCalculator:
    """Wraps skyfield. Degrades to None if the ephemeris is missing."""

    def __init__(self, ephemeris_path: Optional[str | os.PathLike[str]]
                 = None) -> None:
        self._eph: Any = None
        self._ts: Any = None
        self._almanac: Any = None
        if not ephemeris_path:
            logger.info("no ephemeris found; moon commands are off")
            return
        try:
            from skyfield import almanac
            from skyfield.api import load, load_file

            self._ts = load.timescale()
            # load_file, not load: load() treats an absolute path as a URL
            # to download and fails with a confusing 404.
            self._eph = load_file(str(ephemeris_path))
            self._almanac = almanac
            logger.info("moon ephemeris loaded from %s", ephemeris_path)
        except Exception:
            logger.warning("could not load the ephemeris", exc_info=True)
            self._eph = None

    @property
    def available(self) -> bool:
        return self._eph is not None

    def now(self, when: Optional[datetime] = None) -> Optional[MoonNow]:
        if not self.available:
            return None
        when = when or datetime.now(timezone.utc)
        try:
            degrees = float(self._almanac.moon_phase(
                self._eph, self._ts.from_datetime(when)).degrees)
        except Exception:
            logger.warning("phase calculation failed", exc_info=True)
            return None
        name, emoji = phase_name(degrees)
        upcoming = self.upcoming(when, days=45)
        return MoonNow(
            angle=degrees, name=name, emoji=emoji,
            illuminated=illumination(degrees),
            next_full=next((t for t, k in upcoming if k == "Full moon"), None),
            next_new=next((t for t, k in upcoming if k == "New moon"), None),
        )

    def upcoming(self, when: Optional[datetime] = None, *,
                 days: int = 90) -> list[tuple[datetime, str]]:
        """Named phase events in the next ``days``."""
        if not self.available:
            return []
        when = when or datetime.now(timezone.utc)
        labels = {0: "New moon", 1: "First quarter",
                  2: "Full moon", 3: "Last quarter"}
        try:
            times, kinds = self._almanac.find_discrete(
                self._ts.from_datetime(when),
                self._ts.from_datetime(when + timedelta(days=days)),
                self._almanac.moon_phases(self._eph))
        except Exception:
            logger.warning("phase search failed", exc_info=True)
            return []
        return [(t.utc_datetime(), labels[int(k)])
                for t, k in zip(times, kinds)]

    def next_full_moon(self, when: Optional[datetime] = None
                       ) -> Optional[datetime]:
        for moment, kind in self.upcoming(when, days=45):
            if kind == "Full moon":
                return moment
        return None

    def is_full_today(self, when: Optional[datetime] = None) -> bool:
        """True when a full moon falls on this UTC date."""
        when = when or datetime.now(timezone.utc)
        start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        for moment, kind in self.upcoming(start, days=2):
            if kind == "Full moon" and moment.date() == when.date():
                return True
        return False
