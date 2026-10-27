"""Reminders: natural time parsing and a persisted store.

Time input is whatever people actually type -- "in 20 minutes", "tomorrow
at 9pm", "2026-12-25 08:00", "in 3 days". Anything unparseable is
**rejected** rather than guessed: a reminder that fires at the wrong time
is worse than one that was never set, because the user believes it is
handled.

Fire times are stored in UTC. A clock time ("9pm", "tomorrow") needs a
zone: one written in the expression ("9pm PST") wins, then the zone the
memory extractor learned. With neither, the command asks rather than
assuming UTC -- that is what made "9pm" fire at 2 pm Pacific. Relative
times ("in 20 minutes") need no zone.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional

from . import crypto

logger = logging.getLogger("corbin.reminders")

#: Fluxer-specific. The Discord bot uses "reminders.json" in the same
#: directory, and its entries carry Discord channel ids -- loading them
#: here makes this bot try to fire reminders into channels it cannot
#: see, and worse, rewrite that file.
REMINDERS_FILE = "fluxer_reminders.json"
MAX_PER_USER = 25
MAX_TEXT = 500
MAX_AHEAD = timedelta(days=366)

_RELATIVE = re.compile(
    r"in\s+(\d+)\s*(second|sec|minute|min|hour|hr|day|week|month)s?\b",
    re.IGNORECASE)
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T\s]+(\d{1,2}):(\d{2}))?")
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
_UNITS = {"second": "seconds", "sec": "seconds", "minute": "minutes",
          "min": "minutes", "hour": "hours", "hr": "hours",
          "day": "days", "week": "weeks"}


_ZONE_TOKEN = re.compile(
    r"(?<![\w/])([A-Za-z]+/[A-Za-z_]+(?:/[A-Za-z_]+)?|[A-Za-z]{2,7})(?![\w/])")


def is_relative(text: Optional[str]) -> bool:
    """True for "in 20 minutes"-style input, which no timezone affects."""
    return bool(_RELATIVE.search(text or ""))


def split_zone(text: Optional[str]) -> tuple[Optional[tzinfo], str]:
    """Pull a written timezone out of a time expression.

    "tomorrow at 9pm PST" -> (America/Los_Angeles, "tomorrow at 9pm").
    Only known abbreviations (the weather-alert alias table) and IANA
    names count, so ordinary words are never read as zones.
    """
    from .weather_alerts import _TZ_ALIASES, resolve_tz

    raw = text or ""
    for match in _ZONE_TOKEN.finditer(raw):
        token = match.group(1)
        if "/" not in token and token.lower() not in _TZ_ALIASES:
            continue
        zone = resolve_tz(token)
        if zone is not None:
            rest = (raw[:match.start()] + raw[match.end():]).strip()
            return zone, re.sub(r"\s{2,}", " ", rest)
    return None, raw


def user_timezone(name: Optional[str]) -> timezone | Any:
    """Resolve an IANA name to a tzinfo, falling back to UTC."""
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(str(name))
    except Exception:
        return timezone.utc


def parse_when(text: str, *, tz: Any = timezone.utc,
               now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a time expression into an aware UTC datetime, or None."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    now = (now or datetime.now(timezone.utc)).astimezone(tz)

    relative = _RELATIVE.search(raw)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        if unit == "month":
            target = now + timedelta(days=30 * amount)
        else:
            target = now + timedelta(**{_UNITS[unit]: amount})
        return target.astimezone(timezone.utc)

    iso = _ISO.search(raw)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        hour = int(iso.group(4)) if iso.group(4) else 9
        minute = int(iso.group(5)) if iso.group(5) else 0
        try:
            return datetime(year, month, day, hour, minute,
                            tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            return None

    lower = raw.lower()
    day_offset = 0
    if "tomorrow" in lower:
        day_offset = 1
    elif "tonight" in lower:
        day_offset = 0
    elif "next week" in lower:
        day_offset = 7
    elif "today" not in lower and not _CLOCK.search(lower):
        return None

    clock = _CLOCK.search(lower)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        meridiem = (clock.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem and "tonight" in lower and hour < 12:
            hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
    else:
        hour, minute = 9, 0

    target = (now + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    # A bare time that has already passed means tomorrow.
    if target <= now and day_offset == 0:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


@dataclass
class Reminder:
    id: str
    user_id: str
    channel_id: str
    guild_id: Optional[str]
    text: str
    fire_at: datetime

    @property
    def due(self) -> bool:
        return self.fire_at <= datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {"id": self.id, "user_id": self.user_id,
                "channel_id": self.channel_id, "guild_id": self.guild_id,
                "text": self.text, "fire_at": self.fire_at.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> Optional["Reminder"]:
        try:
            when = datetime.fromisoformat(str(data["fire_at"]))
        except (KeyError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return cls(id=str(data.get("id") or uuid.uuid4().hex[:8]),
                   user_id=str(data.get("user_id") or ""),
                   channel_id=str(data.get("channel_id") or ""),
                   guild_id=data.get("guild_id"),
                   text=str(data.get("text") or ""), fire_at=when)


class ReminderStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / REMINDERS_FILE
        self._items: list[Reminder] = []
        try:
            data = crypto.read_json(self.path)
            for entry in data if isinstance(data, list) else []:
                item = Reminder.from_dict(entry) if isinstance(entry, dict) \
                    else None
                if item:
                    self._items.append(item)
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            logger.warning("unreadable reminders file; starting empty")

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            crypto.write_json(self.path, [r.to_dict() for r in self._items],
                              indent=2)
        except OSError:
            logger.exception("could not persist reminders")

    def add(self, *, user_id: str, channel_id: str,
            guild_id: Optional[str], text: str,
            fire_at: datetime) -> Optional[Reminder]:
        if len(self.for_user(user_id)) >= MAX_PER_USER:
            return None
        item = Reminder(id=uuid.uuid4().hex[:8], user_id=str(user_id),
                        channel_id=str(channel_id), guild_id=guild_id,
                        text=text[:MAX_TEXT], fire_at=fire_at)
        self._items.append(item)
        self._flush()
        return item

    def for_user(self, user_id: str) -> list[Reminder]:
        return sorted((r for r in self._items
                       if r.user_id == str(user_id)),
                      key=lambda r: r.fire_at)

    def remove(self, reminder_id: str, user_id: Optional[str] = None
               ) -> Optional[Reminder]:
        for index, item in enumerate(self._items):
            if item.id != str(reminder_id):
                continue
            # Scoped by user so nobody can cancel someone else's.
            if user_id is not None and item.user_id != str(user_id):
                return None
            self._items.pop(index)
            self._flush()
            return item
        return None

    def due(self) -> list[Reminder]:
        return [r for r in self._items if r.due]

    def pop_due(self) -> list[Reminder]:
        ready = self.due()
        if ready:
            ids = {r.id for r in ready}
            self._items = [r for r in self._items if r.id not in ids]
            self._flush()
        return ready

    def __len__(self) -> int:
        return len(self._items)
