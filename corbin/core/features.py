"""The owner's per-server allowlist for voice chat and streaming.

None of these work in a server unless the bot owner has added that
server, each feature separately. Music and the radio are not gated; they predate this
and work everywhere.

**Default deny.** Missing, unknown or unreadable means no. A settings read
error must never be read as "allowed" -- that is the plausible-fallback
failure this port keeps re-learning. DMs have no server to allow, so
nothing here applies in them.

Stored in the chat settings beside ``_verified``, as
``_feature_allowlist: {guild_id: {"voice": bool, "streaming": bool}}``.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger("corbin.features")

#: Feature key -> how it is named to people.
FEATURES = {
    "voice": "Voice chat",
    "streaming": "Streaming",
}

#: The settings bucket. A leading underscore keeps it out of the guild ids.
BUCKET = "_feature_allowlist"


def label(feature: str) -> str:
    return FEATURES.get(feature, feature)


def parse_features(text: str) -> tuple[str, ...]:
    """"voice", "streaming", "voice and streaming", "both" -> keys."""
    low = (text or "").lower()
    if "both" in low or "all" in low.split():
        return tuple(FEATURES)
    found = tuple(f for f in FEATURES if f in low or
                  (f == "streaming" and "stream" in low))
    return found


class FeatureAllowlist:
    """Read and change the allowlist. Every check goes through ``allowed``."""

    def __init__(self, settings) -> None:
        #: A :class:`~corbin.core.settings.ChatSettings`.
        self.settings = settings

    def _entry(self, guild_id: str) -> dict:
        try:
            entry = self.settings.store.get(BUCKET, str(guild_id), {})
        except Exception:
            logger.warning("allowlist: settings unreadable; treating %s as "
                           "not allowed", guild_id, exc_info=True)
            return {}
        return entry if isinstance(entry, dict) else {}

    def allowed(self, guild_id: Optional[str], feature: str) -> bool:
        if not guild_id or feature not in FEATURES:
            return False
        return self._entry(str(guild_id)).get(feature) is True

    def enabled(self, guild_id: Optional[str]) -> frozenset[str]:
        """The features on for a server. Empty in DMs."""
        if not guild_id:
            return frozenset()
        return frozenset(f for f in FEATURES if self.allowed(guild_id, f))

    def set(self, guild_id: str, features: Iterable[str], on: bool, *,
            by: str = "owner") -> None:
        entry = dict(self._entry(str(guild_id)))
        for feature in features:
            if feature not in FEATURES:
                raise ValueError(f"unknown feature {feature!r}")
            entry[feature] = bool(on)
        self.settings.store.set(BUCKET, str(guild_id), entry)
        logger.info("allowlist: %s %s (by %s)", guild_id,
                    " ".join(f"{f}={'on' if entry.get(f) else 'off'}"
                             for f in FEATURES), by)

    def entries(self) -> dict[str, frozenset[str]]:
        """Every server with at least one feature on."""
        try:
            raw = self.settings.store.guild(BUCKET)
        except Exception:
            logger.warning("allowlist: settings unreadable", exc_info=True)
            return {}
        out = {}
        for gid, entry in (raw or {}).items():
            if isinstance(entry, dict):
                on = frozenset(f for f in FEATURES if entry.get(f) is True)
                if on:
                    out[str(gid)] = on
        return out

    def seeded(self) -> bool:
        """Has the allowlist ever been written?"""
        try:
            return bool(self.settings.store.guild(BUCKET))
        except Exception:
            return True

    def seed(self, guild_ids: Iterable[str], feature: str) -> int:
        """First-boot import from ``.env`` (``VOICE_CHAT_GUILDS``).

        Only when the allowlist has never been written: after that the
        DM command is the source of truth, and a stale ``.env`` line must
        not re-add a server the owner removed.
        """
        if self.seeded():
            return 0
        ids = [str(g).strip() for g in guild_ids if str(g).strip()]
        for gid in ids:
            self.set(gid, [feature], True, by="VOICE_CHAT_GUILDS seed")
        return len(ids)
