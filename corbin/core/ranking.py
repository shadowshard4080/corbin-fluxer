"""XP and levelling.

State lives in ``user_data/<guild_id>/<user_id>.json`` under DATA_ROOT.

Milestone roles are per server (:class:`RankRoles`); a server that never
changed them gets :data:`ROLE_TIERS` (the defaults, or RANK_ROLES_FILE).
Voice XP is not counted yet.
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


def role_for_level(level: int, tiers: Optional[dict[int, tuple[str, int]]]
                   = None) -> Optional[tuple[str, int]]:
    """Highest milestone role at or below ``level``. ``tiers``: a
    server's own (:meth:`RankRoles.tiers`); the defaults otherwise."""
    tiers = ROLE_TIERS if tiers is None else tiers
    for threshold in sorted(tiers, reverse=True):
        if level >= threshold:
            return tiers[threshold]
    return None


def next_role(level: int, tiers: Optional[dict[int, tuple[str, int]]]
              = None) -> Optional[tuple[int, str]]:
    """The next milestone above ``level``, as (level, name)."""
    tiers = ROLE_TIERS if tiers is None else tiers
    for threshold in sorted(tiers):
        if level < threshold:
            return threshold, tiers[threshold][0]
    return None


#: Per-server rank roles (owner, 2026-10-20): ``rank_roles`` in that
#: guild's chat settings, ``{"tiers": {"<level>": [name, colour]},
#: "retired": [names]}``. Absent = :data:`ROLE_TIERS`.
MAX_TIERS = 25
MAX_TIER_LEVEL = 1000
MAX_RETIRED = 50
DEFAULT_COLOUR = 0x99AAB5


class RankRoles:
    """A server's milestone roles: level -> (role name, colour).

    A server starts on :data:`ROLE_TIERS` and gets its own copy on the
    first change. Names a tier used to have are kept as ``retired`` so
    the role can still be taken off members (``all_names``): renaming
    "Moongazer" to "Moon Child" must not leave everyone holding both.
    Unreadable or malformed settings fall back to the defaults with a
    warning, since a server losing its roles silently would strip them.
    """

    KEY = "rank_roles"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def _raw(self, guild_id: str) -> dict:
        try:
            raw = self.settings.store.get(str(guild_id), self.KEY)
        except Exception:
            logger.warning("rank roles for %s unreadable; using the "
                           "defaults", guild_id, exc_info=True)
            return {}
        return raw if isinstance(raw, dict) else {}

    def custom(self, guild_id: Optional[str]) -> bool:
        return bool(guild_id) and isinstance(
            self._raw(str(guild_id)).get("tiers"), dict)

    def tiers(self, guild_id: Optional[str]) -> dict[int, tuple[str, int]]:
        if not guild_id:
            return dict(ROLE_TIERS)
        stored = self._raw(str(guild_id)).get("tiers")
        if not isinstance(stored, dict):
            return dict(ROLE_TIERS)
        out: dict[int, tuple[str, int]] = {}
        for level, value in stored.items():
            try:
                name, colour = value
                out[int(level)] = (str(name), int(colour))
            except (TypeError, ValueError):
                logger.warning("rank roles for %s: bad tier %r=%r skipped",
                               guild_id, level, value)
        return out

    def all_names(self, guild_id: Optional[str]) -> list[str]:
        """Every name to strip when giving someone their tier: current
        tiers plus retired ones."""
        names = [n for n, _c in self.tiers(guild_id).values()]
        retired = self._raw(str(guild_id)).get("retired") if guild_id \
            else None
        for name in retired if isinstance(retired, list) else ():
            if str(name).lower() not in {n.lower() for n in names}:
                names.append(str(name))
        return names

    def role_for(self, guild_id: Optional[str],
                 level: int) -> Optional[str]:
        role = role_for_level(level, self.tiers(guild_id))
        return role[0] if role else None

    def _save(self, guild_id: str, tiers: dict[int, tuple[str, int]],
              retire: tuple[str, ...] = ()) -> None:
        raw = self._raw(guild_id)
        retired = [str(n) for n in raw.get("retired") or []
                   if isinstance(n, str)]
        current = {n.lower() for n, _c in tiers.values()}
        for name in retire:
            if name.lower() not in current and name not in retired:
                retired.append(name)
        retired = [n for n in retired if n.lower() not in current]
        self.settings.store.set(str(guild_id), self.KEY, {
            "tiers": {str(k): [v[0], v[1]] for k, v in sorted(tiers.items())},
            "retired": retired[-MAX_RETIRED:],
        })

    def set(self, guild_id: str, level: int, name: str,
            colour: Optional[int] = None) -> Optional[str]:
        """Add or replace the tier at ``level``. Returns the name it
        replaced, if any. Raises ValueError on a bad tier."""
        level = int(level)
        name = " ".join(str(name or "").split())[:100]
        if not 1 <= level <= MAX_TIER_LEVEL:
            raise ValueError(f"the level has to be 1 to {MAX_TIER_LEVEL}")
        if not name:
            raise ValueError("the role needs a name")
        if name.lower() == "@everyone":
            raise ValueError("that's not a role I can hand out")
        tiers = self.tiers(guild_id)
        for other, (other_name, _c) in tiers.items():
            if other != level and other_name.lower() == name.lower():
                raise ValueError(f"**{other_name}** is already the role "
                                 f"for level {other}")
        if level not in tiers and len(tiers) >= MAX_TIERS:
            raise ValueError(f"a server can have up to {MAX_TIERS} rank "
                             "roles")
        old = tiers.get(level)
        if colour is None:
            colour = old[1] if old else DEFAULT_COLOUR
        tiers[level] = (name, int(colour) & 0xFFFFFF)
        replaced = old[0] if old and old[0].lower() != name.lower() else None
        self._save(guild_id, tiers, (replaced,) if replaced else ())
        logger.info("rank roles for %s: level %d = %s%s", guild_id, level,
                    name, f" (was {replaced})" if replaced else "")
        return replaced

    def remove(self, guild_id: str, level: int) -> Optional[str]:
        """Drop the tier at ``level``; its name, or None if none."""
        tiers = self.tiers(guild_id)
        old = tiers.pop(int(level), None)
        if old is None:
            return None
        self._save(guild_id, tiers, (old[0],))
        logger.info("rank roles for %s: level %d (%s) removed", guild_id,
                    level, old[0])
        return old[0]

    def reset(self, guild_id: str) -> None:
        """Back to the defaults; any custom names are retired."""
        mine = [n for n, _c in self.tiers(guild_id).values()]
        self._save(guild_id, dict(ROLE_TIERS), tuple(mine))
        raw = self._raw(guild_id)
        # The defaults, stored as "no custom tiers", keeping the retired
        # list so the old custom roles are still stripped on sync.
        self.settings.store.set(str(guild_id), self.KEY,
                                {"retired": raw.get("retired") or []})
        logger.info("rank roles for %s back to the defaults", guild_id)


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
        #: ((guild, user), level_before, level_after), oldest first
        self._level_ups: list[tuple[tuple[str, str], int, int]] = []

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
            # A queue, not one slot: flush_due can level several people
            # at once, and a slot kept only the last of them.
            self._level_ups.append((key, before, rank.level))

    def pop_level_up(self) -> Optional[tuple[tuple[str, str], int, int]]:
        """The oldest unannounced level-up, or None."""
        return self._level_ups.pop(0) if self._level_ups else None
