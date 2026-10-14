"""Readers for Corbin's existing on-disk state.

The risk model's priors already exist in the Discord bot's data files, so the
Fluxer build reads the same formats rather than inventing new ones:

* ``spamblock_warnings.json`` -- ``{guild_id: {user_id: [warning, ...]}}``
* ``user_data/<guild_id>/<user_id>.json`` -- XP and message counts

Both are read defensively: a missing or corrupt file yields "no signal", never
an exception, because a bad read must not block moderation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("corbin.stores")

WARN_FILE = "spamblock_warnings.json"
USER_DATA_DIR = "user_data"


def _read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        logger.warning("unreadable store: %s", path)
        return None


class WarningStore:
    """Read-only view over the warnings file the Discord bot already writes."""

    def __init__(self, root: str | os.PathLike[str] = ".",
                 filename: str = WARN_FILE) -> None:
        self.path = Path(root) / filename

    def count(self, guild_id: Optional[str], user_id: str) -> int:
        if not guild_id:
            return 0
        data = _read_json(self.path)
        if not isinstance(data, dict):
            return 0
        guild = data.get(str(guild_id))
        if not isinstance(guild, dict):
            return 0
        entries = guild.get(str(user_id))
        return len(entries) if isinstance(entries, list) else 0


class ActivityStore:
    """Read-only view over ``user_data/<guild>/<user>.json``."""

    def __init__(self, root: str | os.PathLike[str] = ".",
                 dirname: str = USER_DATA_DIR) -> None:
        self.root = Path(root) / dirname

    def message_count(self, guild_id: Optional[str], user_id: str) -> int:
        if not guild_id:
            return 0
        data = _read_json(self.root / str(guild_id) / f"{user_id}.json")
        if not isinstance(data, dict):
            return 0
        for key in ("messages", "message_count", "msg_count", "total_messages"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    def xp(self, guild_id: Optional[str], user_id: str) -> int:
        if not guild_id:
            return 0
        data = _read_json(self.root / str(guild_id) / f"{user_id}.json")
        if not isinstance(data, dict):
            return 0
        value = data.get("xp") or data.get("experience") or 0
        return int(value) if isinstance(value, (int, float)) else 0
