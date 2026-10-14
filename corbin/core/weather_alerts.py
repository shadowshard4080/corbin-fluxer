"""Daily weather alerts: when each one is due, in its own timezone.

The port shipped the ``weatheralerts`` command without a job to deliver
them, so alerts were saved and never posted. This is the pure half of the
fix -- parsing and due-checks, no I/O -- so it can be tested offline.
``bot.py`` runs ``_weather_alerts_job`` on the scheduler tick.

Stored per guild under ``weather_alerts`` as a list of::

    {"id", "location", "hour", "minute", "tz", "channel_id", "units",
     "last_sent"}

``last_sent`` is the **local** date the alert last fired, so an alert set
for 07:00 in Los Angeles fires once per Los Angeles day. Alerts written
before this module (``location``/``hour``/``channel_id`` only) were UTC,
and ``normalise`` reads them that way.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Optional

#: How late an alert may fire after its time. Wide enough to survive a
#: restart; narrow enough that a bot down all morning doesn't post the
#: "morning" weather at 4 pm.
LATE_TOLERANCE = timedelta(hours=3)
MAX_PER_GUILD = 10

#: Common names people actually type. Anything else must be an IANA name.
#: Deliberately no bare "CST"/"IST": each is two unrelated zones.
_TZ_ALIASES = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pt": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "mst": "America/Denver", "mdt": "America/Denver",
    "mt": "America/Denver", "mountain": "America/Denver",
    "cdt": "America/Chicago", "ct": "America/Chicago",
    "central": "America/Chicago",
    "est": "America/New_York", "edt": "America/New_York",
    "et": "America/New_York", "eastern": "America/New_York",
    "akst": "America/Anchorage", "alaska": "America/Anchorage",
    "hst": "Pacific/Honolulu", "hawaii": "Pacific/Honolulu",
    "bst": "Europe/London", "uk": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris",
    "aest": "Australia/Sydney", "jst": "Asia/Tokyo",
}

_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$",
                    re.IGNORECASE)


def resolve_tz(name: Optional[str]) -> Optional[tzinfo]:
    """A tzinfo for ``name``, or None when it isn't a real zone.

    Returns None rather than falling back to UTC: an alert that silently
    fires eight hours off looks like it works and is wrong every day.
    """
    if not name or not str(name).strip():
        return None
    text = str(name).strip()
    key = _TZ_ALIASES.get(text.lower(), text)
    if key.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(key)
    except Exception:
        return None


def tz_name(tz: tzinfo) -> str:
    return "UTC" if tz is timezone.utc else str(getattr(tz, "key", tz))


def parse_clock(text: Optional[str]) -> Optional[tuple[int, int]]:
    """"7am" / "7:30 pm" / "19:05" / "7" -> (hour, minute), or None."""
    match = _CLOCK.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower().replace(".", "")
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    return hour, minute


def new_id() -> str:
    return secrets.token_hex(3)


def normalise(alert: dict) -> dict:
    """Fill the fields older alerts don't have, without changing meaning."""
    out = dict(alert)
    out.setdefault("id", new_id())
    out["hour"] = int(out.get("hour", 7))
    out["minute"] = int(out.get("minute", 0))
    out.setdefault("tz", "UTC")
    out.setdefault("units", "imperial")
    return out


def due(alert: dict, now: datetime) -> Optional[str]:
    """The local date to mark as sent if ``alert`` should fire now, else None."""
    tz = resolve_tz(alert.get("tz")) or timezone.utc
    local = now.astimezone(tz)
    today = local.date().isoformat()
    if alert.get("last_sent") == today:
        return None
    scheduled = local.replace(hour=int(alert.get("hour", 7)),
                              minute=int(alert.get("minute", 0)),
                              second=0, microsecond=0)
    if local < scheduled or local - scheduled > LATE_TOLERANCE:
        return None
    return today


def next_fire(alert: dict, now: datetime) -> datetime:
    """When it will next post, in its own timezone (for confirmations)."""
    tz = resolve_tz(alert.get("tz")) or timezone.utc
    local = now.astimezone(tz)
    at = local.replace(hour=int(alert.get("hour", 7)),
                       minute=int(alert.get("minute", 0)),
                       second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)
    return at


def describe(alert: dict) -> str:
    return (f"`{alert.get('id', '?')}` **{alert.get('location')}** at "
            f"{int(alert.get('hour', 7)):02d}:{int(alert.get('minute', 0)):02d}"
            f" {alert.get('tz', 'UTC')} in <#{alert.get('channel_id')}>")


def matches(alert: dict, ref: Any) -> bool:
    """Does ``ref`` (an alert id or a location) name this alert?"""
    text = str(ref or "").strip().lower()
    return bool(text) and text in (str(alert.get("id", "")).lower(),
                                   str(alert.get("location", "")).lower())
