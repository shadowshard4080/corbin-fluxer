"""Warnings, and the appeal flow.

Uses the Discord build's ``spamblock_warnings.json`` verbatim --
``{guild_id: {user_id: [warning, ...]}}`` -- because the risk model already
reads it for the "target has prior warnings" prior. Writing a different
shape here would quietly disable that.

Two behaviours are ported deliberately, both of which were bugs the Discord
build had to fix:

* **Timeouts are clamped** to 1 minute .. 28 days. Discord 400s above that
  and Fluxer mirrors its limits everywhere else measured.
* **The appeal cooldown is spent on delivery, not on attempt.** Stamping it
  up front told a user "sent to the moderators", notified nobody (no mod
  channel, closed DMs), and locked them out for a week.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("corbin.warnings")

WARN_FILE = "spamblock_warnings.json"
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
APPEAL_COOLDOWN_DAYS = 7
MAX_REASON_CHARS = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clamp_timeout(minutes: Any) -> int:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, MAX_TIMEOUT_MINUTES))


def describe_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if minutes < 1440:
        hours = minutes / 60
        return f"{hours:.0f} hour{'s' if hours != 1 else ''}"
    days = minutes / 1440
    return f"{days:.0f} day{'s' if days != 1 else ''}"


@dataclass
class Warning:
    id: str
    user_id: str
    moderator_id: Optional[str]
    reason: str
    action: Optional[str]
    timestamp: Optional[datetime]
    last_appeal_at: Optional[datetime] = None
    raw: dict = None  # type: ignore[assignment]

    @classmethod
    def from_dict(cls, data: dict, user_id: str) -> "Warning":
        return cls(
            id=str(data.get("id") or ""),
            user_id=str(user_id),
            moderator_id=(str(data["moderator_id"])
                          if data.get("moderator_id") else None),
            reason=str(data.get("reason") or "(no reason given)"),
            action=data.get("action"),
            timestamp=parse_time(data.get("timestamp")),
            last_appeal_at=parse_time(data.get("last_appeal_at")),
            raw=data,
        )

    def appeal_available_at(self) -> Optional[datetime]:
        if not self.last_appeal_at:
            return None
        return self.last_appeal_at + timedelta(days=APPEAL_COOLDOWN_DAYS)

    def can_appeal(self) -> tuple[bool, Optional[datetime]]:
        nxt = self.appeal_available_at()
        if nxt and nxt > _now():
            return False, nxt
        return True, None


class WarningStore:
    def __init__(self, root: str | os.PathLike[str],
                 filename: str = WARN_FILE) -> None:
        self.path = Path(root) / filename
        self._data: dict = self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            logger.warning("unreadable warnings file %s; starting empty",
                           self.path)
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, default=str)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("could not persist warnings")

    # -- reads ------------------------------------------------------------

    def for_user(self, guild_id: str, user_id: str) -> list[Warning]:
        raw = self._data.get(str(guild_id), {}).get(str(user_id), [])
        return [Warning.from_dict(w, str(user_id))
                for w in raw if isinstance(w, dict)]

    def count(self, guild_id: Optional[str], user_id: str) -> int:
        if not guild_id:
            return 0
        return len(self.for_user(guild_id, user_id))

    def find(self, guild_id: str,
             warn_id: str) -> tuple[Optional[Warning], Optional[str]]:
        """Locate a warning by id anywhere in the guild."""
        for user_id, entries in self._data.get(str(guild_id), {}).items():
            for entry in entries or []:
                if isinstance(entry, dict) and \
                        str(entry.get("id")) == str(warn_id):
                    return Warning.from_dict(entry, user_id), user_id
        return None, None

    def guild_total(self, guild_id: str) -> int:
        return sum(len(v or [])
                   for v in self._data.get(str(guild_id), {}).values())

    # -- writes -----------------------------------------------------------

    def add(self, guild_id: str, user_id: str, *,
            moderator_id: Optional[str], reason: str,
            action: Optional[str] = None) -> Warning:
        # Short ids: a user has to type this to appeal, and a full uuid4 is
        # hostile for that. Still unique enough within one guild.
        entry = {
            "id": uuid.uuid4().hex[:8],
            "moderator_id": str(moderator_id) if moderator_id else None,
            "reason": str(reason or "(no reason given)")[:MAX_REASON_CHARS],
            "action": action,
            "timestamp": _now().isoformat(),
        }
        self._data.setdefault(str(guild_id), {}) \
                  .setdefault(str(user_id), []).append(entry)
        self._write()
        return Warning.from_dict(entry, str(user_id))

    def remove(self, guild_id: str, warn_id: str) -> bool:
        guild = self._data.get(str(guild_id), {})
        for user_id, entries in guild.items():
            for index, entry in enumerate(entries or []):
                if isinstance(entry, dict) and \
                        str(entry.get("id")) == str(warn_id):
                    del entries[index]
                    if not entries:
                        del guild[user_id]
                    self._write()
                    return True
        return False

    def edit(self, guild_id: str, warn_id: str, reason: str) -> bool:
        for entries in self._data.get(str(guild_id), {}).values():
            for entry in entries or []:
                if isinstance(entry, dict) and \
                        str(entry.get("id")) == str(warn_id):
                    entry["reason"] = str(reason)[:MAX_REASON_CHARS]
                    self._write()
                    return True
        return False

    def stamp_appeal(self, guild_id: str, warn_id: str) -> bool:
        """Record an appeal. Call ONLY after delivery actually succeeded."""
        for entries in self._data.get(str(guild_id), {}).values():
            for entry in entries or []:
                if isinstance(entry, dict) and \
                        str(entry.get("id")) == str(warn_id):
                    entry["last_appeal_at"] = _now().isoformat()
                    self._write()
                    return True
        return False
