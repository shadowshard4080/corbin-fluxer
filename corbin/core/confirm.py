"""Pending confirmations and the post-execution undo window.

Fluxer has no buttons, so a confirmation is a follow-up *message* and an undo
is a follow-up *word*.  Both are therefore scoped to (channel, user) and
expire on a timer, and both verify the responder is the original invoker --
otherwise a bystander could confirm somebody else's ban.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

#: How long a "yes/no?" stays answerable.
CONFIRM_TTL = 45.0
#: How long after a destructive action the invoker may say "undo".
UNDO_TTL = 60.0

_YES = re.compile(
    r"^\s*(y|ye|yes|yep|yeah|yup|sure|ok|okay|do it|go|go ahead|confirm|"
    r"affirmative|please do|send it)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(n|no|nope|nah|cancel|stop|abort|nevermind|never mind|don'?t|"
    r"negative|wait)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_UNDO = re.compile(r"^\s*(undo|revert|unban|undo that|take it back)\s*[.!]*\s*$",
                   re.IGNORECASE)


def is_affirmative(text: str) -> bool:
    return bool(_YES.match(text or ""))


def is_negative(text: str) -> bool:
    return bool(_NO.match(text or ""))


def is_undo(text: str) -> bool:
    return bool(_UNDO.match(text or ""))


@dataclass
class Pending:
    """An action awaiting a yes/no from one specific user in one channel."""

    id: str
    guild_id: Optional[str]
    channel_id: str
    user_id: str
    command: str
    args: Mapping[str, Any]
    summary: str
    created_at: float = field(default_factory=time.monotonic)
    ttl: float = CONFIRM_TTL

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.ttl - (time.monotonic() - self.created_at))


@dataclass
class Undoable:
    """A completed action that can still be reversed."""

    id: str
    guild_id: Optional[str]
    channel_id: str
    user_id: str
    command: str
    summary: str
    undo: Callable[[], Awaitable[None]]
    created_at: float = field(default_factory=time.monotonic)
    ttl: float = UNDO_TTL

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl


class PendingStore:
    """In-memory, per-(channel,user) store for confirmations and undos.

    Deliberately not persisted: a confirmation that survives a restart is a
    confirmation the user has forgotten agreeing to.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], Pending] = {}
        self._undoable: dict[tuple[str, str], Undoable] = {}

    @staticmethod
    def _key(channel_id: str, user_id: str) -> tuple[str, str]:
        return (str(channel_id), str(user_id))

    # -- confirmations ----------------------------------------------------

    def put(
        self,
        *,
        guild_id: Optional[str],
        channel_id: str,
        user_id: str,
        command: str,
        args: Mapping[str, Any],
        summary: str,
        ttl: float = CONFIRM_TTL,
    ) -> Pending:
        pending = Pending(
            id=uuid.uuid4().hex[:8], guild_id=guild_id, channel_id=channel_id,
            user_id=user_id, command=command, args=dict(args),
            summary=summary, ttl=ttl,
        )
        self._pending[self._key(channel_id, user_id)] = pending
        return pending

    def take(self, channel_id: str, user_id: str) -> Optional[Pending]:
        """Pop a live confirmation, if one is waiting for this exact user."""
        key = self._key(channel_id, user_id)
        pending = self._pending.get(key)
        if pending is None:
            return None
        del self._pending[key]
        return None if pending.expired else pending

    def peek(self, channel_id: str, user_id: str) -> Optional[Pending]:
        pending = self._pending.get(self._key(channel_id, user_id))
        if pending is None or pending.expired:
            return None
        return pending

    def drop(self, channel_id: str, user_id: str) -> None:
        self._pending.pop(self._key(channel_id, user_id), None)

    # -- undo -------------------------------------------------------------

    def put_undo(
        self,
        *,
        guild_id: Optional[str],
        channel_id: str,
        user_id: str,
        command: str,
        summary: str,
        undo: Callable[[], Awaitable[None]],
        ttl: float = UNDO_TTL,
    ) -> Undoable:
        entry = Undoable(
            id=uuid.uuid4().hex[:8], guild_id=guild_id, channel_id=channel_id,
            user_id=user_id, command=command, summary=summary,
            undo=undo, ttl=ttl,
        )
        self._undoable[self._key(channel_id, user_id)] = entry
        return entry

    def take_undo(self, channel_id: str, user_id: str) -> Optional[Undoable]:
        key = self._key(channel_id, user_id)
        entry = self._undoable.get(key)
        if entry is None:
            return None
        del self._undoable[key]
        return None if entry.expired else entry

    # -- housekeeping -----------------------------------------------------

    def sweep(self) -> int:
        """Drop expired entries. Returns how many were removed."""
        removed = 0
        for store in (self._pending, self._undoable):
            for key in [k for k, v in store.items() if v.expired]:
                del store[key]
                removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._pending) + len(self._undoable)
