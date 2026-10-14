"""Daily usage digest.

Counts what Corbin did, and posts a summary once a day. The Discord build
paged users/servers behind buttons; Fluxer has no components, so the
overview carries the top entries inline and ``digest`` takes a section
parameter.

The reset is a **subtraction of the posted snapshot**, not a wipe: activity
that happens during the send would otherwise be lost. Posting is guarded by
a persisted date marker so a restart cannot double-post or skip a day.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("corbin.digest")

DIGEST_TZ_OFFSET = -8  # America/Los_Angeles standard time
DIGEST_HOUR = 20       # 8 PM
#: Fluxer-specific: separate usage counters per platform.
STATS_FILE = "fluxer_digest_stats.json"
TOP_N = 10


def digest_now() -> datetime:
    """Current time in the digest's timezone.

    A fixed offset rather than a tz database lookup: the only thing it
    decides is when 8 PM falls, and an hour of DST drift once or twice a
    year does not matter for a daily summary.
    """
    return datetime.now(timezone(timedelta(hours=DIGEST_TZ_OFFSET)))


@dataclass
class Counters:
    messages_sent: int = 0
    commands_run: int = 0
    users: dict = field(default_factory=dict)
    guilds: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Counters":
        data = data if isinstance(data, dict) else {}
        return cls(
            messages_sent=int(data.get("messages_sent") or 0),
            commands_run=int(data.get("commands_run") or 0),
            users=dict(data.get("users") or {}),
            guilds=dict(data.get("guilds") or {}),
        )

    def to_dict(self) -> dict:
        return {"messages_sent": self.messages_sent,
                "commands_run": self.commands_run,
                "users": self.users, "guilds": self.guilds}

    @property
    def total(self) -> int:
        return self.messages_sent + self.commands_run

    def top_users(self, limit: int = TOP_N) -> list[tuple[str, dict]]:
        return sorted(self.users.items(),
                      key=lambda kv: -int(kv[1].get("count", 0)))[:limit]

    def top_guilds(self, limit: int = TOP_N) -> list[tuple[str, dict]]:
        return sorted(self.guilds.items(),
                      key=lambda kv: -int(kv[1].get("count", 0)))[:limit]

    def subtract(self, snapshot: "Counters") -> None:
        """Remove a posted snapshot, keeping anything counted since."""
        self.messages_sent = max(0, self.messages_sent -
                                 snapshot.messages_sent)
        self.commands_run = max(0, self.commands_run - snapshot.commands_run)
        for bucket, taken in (("users", snapshot.users),
                              ("guilds", snapshot.guilds)):
            live = getattr(self, bucket)
            for key, entry in taken.items():
                if key not in live:
                    continue
                remaining = int(live[key].get("count", 0)) - \
                    int(entry.get("count", 0))
                if remaining > 0:
                    live[key]["count"] = remaining
                else:
                    del live[key]


class DigestStore:
    def __init__(self, root: str | os.PathLike[str],
                 filename: str = STATS_FILE) -> None:
        self.path = Path(root) / filename
        raw = self._read()
        self.counters = Counters.from_dict(raw.get("window"))
        self.last_sent: Optional[str] = raw.get("last_sent")

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            logger.warning("unreadable digest stats; starting fresh")
            return {}

    def flush(self) -> None:
        payload = {"window": self.counters.to_dict(),
                   "last_sent": self.last_sent}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("could not persist digest stats")

    # -- recording --------------------------------------------------------

    def record(self, kind: str, *, user_id: str, user_name: str,
               guild_id: Optional[str], guild_name: str = "") -> None:
        if kind == "command":
            self.counters.commands_run += 1
        else:
            self.counters.messages_sent += 1

        user = self.counters.users.setdefault(
            str(user_id), {"name": user_name, "count": 0})
        user["count"] = int(user.get("count", 0)) + 1
        if user_name:
            user["name"] = user_name

        if guild_id:
            guild = self.counters.guilds.setdefault(
                str(guild_id), {"name": guild_name, "count": 0})
            guild["count"] = int(guild.get("count", 0)) + 1
            if guild_name:
                guild["name"] = guild_name
        self.flush()

    # -- posting ----------------------------------------------------------

    def is_due(self, now: Optional[datetime] = None) -> bool:
        now = now or digest_now()
        today = now.date().isoformat()
        if self.last_sent == today:
            return False
        return now.hour >= DIGEST_HOUR

    def snapshot_and_reset(self, marker: str) -> Counters:
        """Take what to post, then subtract it so nothing in flight is lost."""
        posted = Counters.from_dict(
            json.loads(json.dumps(self.counters.to_dict())))
        self.counters.subtract(posted)
        self.last_sent = marker
        self.flush()
        return posted
