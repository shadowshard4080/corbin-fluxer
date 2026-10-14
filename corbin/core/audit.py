"""Server event logging.

Scope is dictated by what Fluxer actually emits. Phase 0 confirmed there are
**no audit-log events**, so nothing here can report *who* performed an
action -- the Discord build's `on_audit_log_entry_create` half (kicks, bans,
role edits, with an actor) has no equivalent and is deliberately absent
rather than faked from guesswork.

What the gateway does carry: message edits and deletes, joins and leaves,
channel create/update/delete, and voice state. Those are covered.

``MESSAGE_DELETE`` arrives as a bare dict of ids with no content, and
``MESSAGE_UPDATE`` carries only the *new* text, so :class:`MessageCache`
keeps recent messages in memory. Without it a delete log can only say "a
message was deleted", which is worth very little.

Logged content is rendered as posted (not code-blocked) so markdown and
emoji survive; mention tokens are flattened by the caller first so nothing
in an embed can ping.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger("corbin.audit")

#: Categories Fluxer can actually source. Keys are stable; renaming one
#: silently disables it for every guild that had it on.
CATEGORIES: dict[str, str] = {
    "message_edit": "Message edited",
    "message_delete": "Message deleted",
    "member_join": "Member joined",
    "member_leave": "Member left",
    "channel_create": "Channel created",
    "channel_update": "Channel updated",
    "channel_delete": "Channel deleted",
    "voice": "Voice activity",
}
#: Noisy ones stay off until asked for.
DEFAULT_OFF = frozenset({"voice"})

COLOURS = {
    "message_edit": 0xE8A33D, "message_delete": 0xD9534F,
    "member_join": 0x5CB85C, "member_leave": 0xD9534F,
    "channel_create": 0x5BC0DE, "channel_update": 0x5BC0DE,
    "channel_delete": 0xD9534F, "voice": 0x9B59B6,
}

CACHE_PER_CHANNEL = 200
CONTENT_LIMIT = 1000


@dataclass
class CachedMessage:
    id: str
    channel_id: str
    author_id: str
    author_name: str
    content: str
    attachments: tuple[str, ...] = ()
    created_at: Optional[dt.datetime] = None


class MessageCache:
    """Bounded per-channel cache, so deletes and edits can show content.

    Memory-only and intentionally small: this exists to make a log entry
    readable, not to be a message archive.
    """

    def __init__(self, per_channel: int = CACHE_PER_CHANNEL) -> None:
        self.per_channel = per_channel
        self._by_channel: dict[str, "OrderedDict[str, CachedMessage]"] = {}

    def put(self, message: CachedMessage) -> None:
        if not message.id:
            return
        bucket = self._by_channel.setdefault(str(message.channel_id),
                                             OrderedDict())
        bucket[str(message.id)] = message
        while len(bucket) > self.per_channel:
            bucket.popitem(last=False)

    def get(self, channel_id: str,
            message_id: str) -> Optional[CachedMessage]:
        return self._by_channel.get(str(channel_id), {}).get(str(message_id))

    def pop(self, channel_id: str,
            message_id: str) -> Optional[CachedMessage]:
        bucket = self._by_channel.get(str(channel_id))
        return bucket.pop(str(message_id), None) if bucket else None

    def __len__(self) -> int:
        return sum(len(b) for b in self._by_channel.values())


def normalise_categories(raw: Any) -> dict[str, bool]:
    """Coerce stored settings into a full category map."""
    stored = raw if isinstance(raw, dict) else {}
    return {
        key: bool(stored.get(key, key not in DEFAULT_OFF))
        for key in CATEGORIES
    }


def truncate(text: str, limit: int = CONTENT_LIMIT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"
