"""Timed bans and chatbans.

Two separate things that both live here because they are both "people
Corbin treats differently":

* **Timed bans** -- a real guild ban with an expiry, persisted so it still
  lifts after a restart. The scheduler sweeps for due unbans.
* **Chatbans** -- a global list of people Corbin will not *converse* with.
  They keep full use of commands; he just stops replying in chat. Matches
  the Discord build, where the guard sits in ``on_message``.

Durations are written the way people write them: ``30m``, ``2h``, ``7d``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("corbin.bans")

#: Fluxer-specific: Discord timed bans reference Discord guilds.
TIMED_BANS_FILE = "fluxer_timed_bans.json"
#: SHARED with the Discord bot on purpose: a chatban is about a
#: person, not a platform, and the file holds bare user ids.
CHATBAN_FILE = "banned_users.json"

_DURATION = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours",
          "d": "days", "w": "weeks"}
MAX_BAN_DAYS = 365


def parse_duration(text: str) -> Optional[timedelta]:
    """``"2h"``, ``"30m"``, ``"1d 12h"`` -> timedelta. None if unparseable.

    Deliberately strict about units: a bare number is ambiguous (minutes?
    days?) and guessing wrong on a ban length is not recoverable.
    """
    if not text:
        return None
    parts = _DURATION.findall(str(text))
    if not parts:
        return None
    total = timedelta()
    for amount, unit in parts:
        total += timedelta(**{_UNITS[unit.lower()]: int(amount)})
    if total <= timedelta(0):
        return None
    return min(total, timedelta(days=MAX_BAN_DAYS))


def humanise(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "expired"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    bits = [f"{days}d" if days else "", f"{hours}h" if hours else "",
            f"{minutes}m" if minutes and not days else ""]
    return " ".join(b for b in bits if b) or "under a minute"


@dataclass
class TimedBan:
    user_id: str
    guild_id: str
    unban_at: datetime
    reason: Optional[str] = None
    banner_id: Optional[str] = None
    channel_id: Optional[str] = None

    @property
    def remaining(self) -> timedelta:
        return self.unban_at - datetime.now(timezone.utc)

    @property
    def due(self) -> bool:
        return self.remaining.total_seconds() <= 0


class BanStore:
    """Persisted timed bans, keyed ``guild:user``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / TIMED_BANS_FILE
        self._bans: dict[str, TimedBan] = {}
        self._load()

    @staticmethod
    def _key(guild_id: str, user_id: str) -> str:
        return f"{guild_id}:{user_id}"

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError):
            logger.warning("unreadable timed bans file; starting empty")
            return
        for key, info in (data or {}).items():
            if not isinstance(info, dict):
                continue
            try:
                when = datetime.fromisoformat(str(info["unban_time"]))
            except (KeyError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            guild_id = str(info.get("guild_id") or "")
            # Legacy records key on user id alone.
            user_id = key.split(":")[-1]
            self._bans[self._key(guild_id, user_id)] = TimedBan(
                user_id=user_id, guild_id=guild_id, unban_at=when,
                reason=info.get("reason"),
                banner_id=info.get("banner_id"),
                channel_id=info.get("channel_id"))

    def _flush(self) -> None:
        payload = {
            key: {"guild_id": b.guild_id,
                  "unban_time": b.unban_at.isoformat(),
                  "reason": b.reason, "banner_id": b.banner_id,
                  "channel_id": b.channel_id}
            for key, b in self._bans.items()
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("could not persist timed bans")

    def add(self, guild_id: str, user_id: str, duration: timedelta, *,
            reason: Optional[str] = None, banner_id: Optional[str] = None,
            channel_id: Optional[str] = None) -> TimedBan:
        ban = TimedBan(user_id=str(user_id), guild_id=str(guild_id),
                       unban_at=datetime.now(timezone.utc) + duration,
                       reason=reason, banner_id=banner_id,
                       channel_id=channel_id)
        self._bans[self._key(str(guild_id), str(user_id))] = ban
        self._flush()
        return ban

    def remove(self, guild_id: str, user_id: str) -> bool:
        if self._bans.pop(self._key(str(guild_id), str(user_id)), None):
            self._flush()
            return True
        return False

    def get(self, guild_id: str, user_id: str) -> Optional[TimedBan]:
        return self._bans.get(self._key(str(guild_id), str(user_id)))

    def for_guild(self, guild_id: str) -> list[TimedBan]:
        return [b for b in self._bans.values()
                if b.guild_id == str(guild_id)]

    def due(self) -> list[TimedBan]:
        return [b for b in self._bans.values() if b.due]

    def __len__(self) -> int:
        return len(self._bans)


class ChatbanStore:
    """People Corbin will not converse with. Commands still work for them."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / CHATBAN_FILE
        self._ids: set[str] = set()
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                self._ids = {str(x) for x in data}
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            logger.warning("unreadable chatban file; starting empty")

    def __contains__(self, user_id: object) -> bool:
        return str(user_id) in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def all(self) -> list[str]:
        return sorted(self._ids)

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(sorted(self._ids), handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("could not persist chatbans")

    def toggle(self, user_id: str) -> bool:
        """Returns the new state: True = now chatbanned."""
        uid = str(user_id)
        if uid in self._ids:
            self._ids.discard(uid)
            self._flush()
            return False
        self._ids.add(uid)
        self._flush()
        return True
