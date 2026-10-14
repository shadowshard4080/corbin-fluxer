"""XP and levelling.

State lives in ``user_data/<guild_id>/<user_id>.json`` under DATA_ROOT.
Voice XP is not counted yet (it needs VOICE_STATE_UPDATE session
tracking); the stored shape already has room for it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("corbin.ranking")

#: word_count -> xp, linearly interpolated between points and capped.
XP_BREAKPOINTS: tuple[tuple[int, int], ...] = ((0, 3), (10, 6), (20, 12), (40, 20))
#: Messages are batched into one award per window, as in the Discord build.
XP_WINDOW_SECONDS = 60.0

#: Level -> (role name, colour). Created in the server when ranking is set
#: up. Override with a JSON file named by RANK_ROLES_FILE, shaped like
#: ``{"1": ["Newcomer", "#FFFFFF"], "25": ["Regular", "#808080"]}``.
DEFAULT_ROLE_TIERS: dict[int, tuple[str, int]] = {
    1: ("One of No Renown", 0xFFFFFF),
    25: ("Moongazer", 0x808080),
    50: ("Silver Warrior", 0xC0C0C0),
    100: ("Starfallen Warden", 0xCD7F32),
    175: ("Godlike", 0xFFD700),
    250: ("Eternal Legend", 0xDC143C),
    500: ("Lord of Eternal Night", 0x1A0033),
}


def load_role_tiers(path: Optional[str]) -> dict[int, tuple[str, int]]:
    """The tiers from ``path``, or the defaults if it is unset or broken."""
    if not path:
        return dict(DEFAULT_ROLE_TIERS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        tiers = {int(level): (str(name), int(str(colour).lstrip("#"), 16))
                 for level, (name, colour) in raw.items()}
        if tiers:
            return tiers
    except Exception:
        logger.exception("could not read RANK_ROLES_FILE %s; using the "
                         "default rank roles", path)
    return dict(DEFAULT_ROLE_TIERS)


ROLE_TIERS = load_role_tiers(os.environ.get("RANK_ROLES_FILE"))

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_id(value: str) -> str:
    """Filesystem-safe id. Ids come from the network and become paths."""
    return _UNSAFE.sub("", str(value or ""))[:64]


def calculate_level(xp: int) -> int:
    """Identical to the Discord build's ``calculate_level``."""
    if xp < 100:
        return 0
    return int((xp - 100) / 999.6 + 1)


def xp_for_level(level: int) -> int:
    """Least XP that reaches ``level``. Inverse of :func:`calculate_level`.

    Must use ceil, not round: ``calculate_level`` truncates, so rounding the
    threshold down lands one level short (level 25 -> 24090 -> level 24) and
    every progress bar would read wrong near a boundary.
    """
    if level <= 0:
        return 0
    return math.ceil((level - 1) * 999.6 + 100)


def xp_for_message(content: str) -> int:
    """XP a single message is worth, by word count."""
    words = len((content or "").split())
    points = XP_BREAKPOINTS
    if words >= points[-1][0]:
        return points[-1][1]
    lower, upper = points[0], None
    for i in range(1, len(points)):
        if words < points[i][0]:
            lower, upper = points[i - 1], points[i]
            break
    if upper is None:
        return points[-1][1]
    (lx, ly), (ux, uy) = lower, upper
    if ux == lx:
        return ly
    frac = (words - lx) / (ux - lx)
    return max(int(round(ly + frac * (uy - ly))), points[0][1])


def role_for_level(level: int) -> Optional[tuple[str, int]]:
    """Highest milestone role at or below ``level``."""
    for threshold in sorted(ROLE_TIERS, reverse=True):
        if level >= threshold:
            return ROLE_TIERS[threshold]
    return None


def next_role(level: int) -> Optional[tuple[int, str]]:
    """The next milestone above ``level``, as (level, name)."""
    for threshold in sorted(ROLE_TIERS):
        if level < threshold:
            return threshold, ROLE_TIERS[threshold][0]
    return None


@dataclass
class UserRank:
    user_id: str
    chat_xp: int = 0
    total_messages: int = 0
    vc_seconds: int = 0

    @property
    def level(self) -> int:
        return calculate_level(self.chat_xp)

    @property
    def role_name(self) -> Optional[str]:
        role = role_for_level(self.level)
        return role[0] if role else None

    @property
    def progress(self) -> tuple[int, int]:
        """(xp into current level, xp needed for the next)."""
        current = xp_for_level(self.level)
        nxt = xp_for_level(self.level + 1)
        span = max(1, nxt - current)
        return max(0, self.chat_xp - current), span

    def bar(self, width: int = 16) -> str:
        into, span = self.progress
        filled = min(width, max(0, round(width * into / span)))
        return "█" * filled + "░" * (width - filled)


class RankingStore:
    """Reads and writes ``user_data/<guild_id>/<user_id>.json``."""

    def __init__(self, root: str | os.PathLike[str],
                 dirname: str = "user_data") -> None:
        self.root = Path(root) / dirname

    def _path(self, guild_id: str, user_id: str) -> Optional[Path]:
        gid, uid = sanitize_id(guild_id), sanitize_id(user_id)
        if not gid or not uid:
            return None
        return self.root / gid / f"{uid}.json"

    def get(self, guild_id: str, user_id: str) -> UserRank:
        path = self._path(guild_id, user_id)
        data: dict = {}
        if path and path.exists():
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                logger.warning("unreadable rank file %s", path)
        return UserRank(
            user_id=str(user_id),
            chat_xp=int(data.get("chat_xp") or 0),
            total_messages=int(data.get("total_messages") or 0),
            vc_seconds=int(data.get("vc_seconds") or 0),
        )

    def save(self, guild_id: str, rank: UserRank) -> None:
        path = self._path(guild_id, rank.user_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Merge rather than overwrite: the Discord bot may store extra keys
        # (voice sessions, role state) that we must not clobber.
        existing: dict = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                pass
        existing.update({
            "chat_xp": rank.chat_xp,
            "total_messages": rank.total_messages,
            "vc_seconds": rank.vc_seconds,
        })
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(existing, handle, indent=2, default=str)
            os.replace(tmp, path)
        except OSError:
            logger.exception("could not persist rank for %s", rank.user_id)

    def leaderboard(self, guild_id: str, limit: int = 10,
                    offset: int = 0) -> tuple[list[UserRank], int]:
        """Top users by XP, plus the total number of ranked users."""
        gid = sanitize_id(guild_id)
        folder = self.root / gid
        if not folder.is_dir():
            return [], 0
        ranks: list[UserRank] = []
        for entry in folder.glob("*.json"):
            rank = self.get(guild_id, entry.stem)
            if rank.chat_xp > 0 or rank.total_messages > 0:
                ranks.append(rank)
        ranks.sort(key=lambda r: (-r.chat_xp, -r.total_messages, r.user_id))
        return ranks[offset:offset + limit], len(ranks)

    def position(self, guild_id: str, user_id: str) -> Optional[int]:
        """1-based rank of a user, or None if unranked."""
        ranks, _ = self.leaderboard(guild_id, limit=10_000)
        for index, rank in enumerate(ranks, start=1):
            if rank.user_id == str(user_id):
                return index
        return None


class XPAccumulator:
    """Batches message XP into one write per user per window.

    The Discord build waits 60s and awards the sum, which keeps a chatty user
    from causing a disk write per message. This is the same idea without the
    per-user asyncio task: the pending total is flushed on the next message
    after the window closes, or by :meth:`flush_due`.
    """

    def __init__(self, store: RankingStore,
                 window: float = XP_WINDOW_SECONDS) -> None:
        self.store = store
        self.window = window
        #: (guild, user) -> [pending_xp, pending_messages, window_started]
        self._pending: dict[tuple[str, str], list] = {}

    def record(self, guild_id: str, user_id: str, content: str) -> None:
        key = (str(guild_id), str(user_id))
        slot = self._pending.get(key)
        now = time.monotonic()
        if slot is None:
            self._pending[key] = [xp_for_message(content), 1, now]
            return
        slot[0] += xp_for_message(content)
        slot[1] += 1
        if now - slot[2] >= self.window:
            self._flush(key)

    def flush_due(self) -> int:
        """Flush every window that has closed. Returns how many were written."""
        now = time.monotonic()
        due = [k for k, v in self._pending.items() if now - v[2] >= self.window]
        for key in due:
            self._flush(key)
        return len(due)

    def flush_all(self) -> None:
        for key in list(self._pending):
            self._flush(key)

    def _flush(self, key: tuple[str, str]) -> None:
        slot = self._pending.pop(key, None)
        if not slot:
            return
        guild_id, user_id = key
        xp, messages, _ = slot
        rank = self.store.get(guild_id, user_id)
        before = rank.level
        rank.chat_xp += int(xp)
        rank.total_messages += int(messages)
        self.store.save(guild_id, rank)
        if rank.level > before:
            logger.info("level up: %s in %s -> %d (%s)", user_id, guild_id,
                        rank.level, rank.role_name)
        self.last_level_up = (key, before, rank.level) if rank.level > before \
            else getattr(self, "last_level_up", None)

    def pop_level_up(self) -> Optional[tuple[tuple[str, str], int, int]]:
        value = getattr(self, "last_level_up", None)
        self.last_level_up = None
        return value
