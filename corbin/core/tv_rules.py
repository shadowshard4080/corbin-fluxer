"""A server's own live TV rules: T3 and T4 of ``VOICE_ROADMAP.md``.

The owner's ``streaming`` allowlist (:mod:`corbin.core.features`) decides
*which servers* may stream at all. Inside an allowed server, its admins
decide the rest, here:

* **Live TV on or off** (default on). Off hides the listings and refuses
  "put on X"; music video is unaffected, it isn't TV.
* **Categories**: an allow list ("news and documentaries only") or a
  block list ("no sports"), never both. Uncategorised channels count as
  :data:`~corbin.core.iptv.OTHER_CATEGORY`, so "Other" can be allowed or
  blocked like any category.
* **A maximum watch time**, in minutes, or none. ``CORBIN_TV_MAX_MINUTES``
  is the default for servers that haven't chosen.
* **Stream quality**, 720p or 1080p, for live TV and music video alike.
  1080p only where the host allows it (``CORBIN_ALLOW_1080P``): asking
  for it anywhere else is refused with the reason, not stored and then
  silently ignored.
* **Favourites** and **the usual** (T4).

Stored in the chat settings under each guild's ``live_tv`` key. Platform
neutral: no SDK, no network.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from . import iptv as I
from .video import QUALITIES, Quality, quality

logger = logging.getLogger("corbin.tv_rules")

KEY = "live_tv"
#: Favourites kept per server. A list longer than this is no longer a
#: list of favourites.
MAX_FAVOURITES = 25
#: The longest watch limit a server can set: a week.
MAX_LIMIT_MINUTES = 7 * 24 * 60


class RuleError(ValueError):
    """A setting that can't be made, worded for the admin who asked."""


class TVRules:
    """Read and change one server's TV rules. ``settings`` is a
    :class:`~corbin.core.settings.ChatSettings`."""

    def __init__(self, settings: Any, *, allow_1080p: bool = False,
                 default_max_minutes: int = 0,
                 default_quality: str = "720p") -> None:
        self.settings = settings
        self.allow_1080p = bool(allow_1080p)
        #: For servers that haven't chosen (``CORBIN_STREAM_QUALITY``).
        self.default_quality = quality(default_quality).name
        self.default_max_minutes = max(0, int(default_max_minutes or 0))

    # -- storage ----------------------------------------------------------

    def _get(self, guild_id: Optional[str]) -> dict:
        if not guild_id:
            return {}
        try:
            value = self.settings.store.get(str(guild_id), KEY, {})
        except Exception:
            logger.warning("tv rules unreadable for %s", guild_id,
                           exc_info=True)
            return {}
        return value if isinstance(value, dict) else {}

    def _put(self, guild_id: str, **changes: Any) -> None:
        entry = dict(self._get(guild_id))
        for key, value in changes.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        self.settings.store.set(str(guild_id), KEY, entry)

    # -- on / off ---------------------------------------------------------

    def enabled(self, guild_id: Optional[str]) -> bool:
        """On unless an admin turned it off. (The owner's allowlist is
        checked separately, by the command's ``feature=`` gate.)"""
        return self._get(guild_id).get("enabled", True) is not False

    def set_enabled(self, guild_id: str, on: bool) -> None:
        self._put(guild_id, enabled=None if on else False)

    # -- categories -------------------------------------------------------

    def allowed_categories(self, guild_id: Optional[str]) -> list:
        return [str(c) for c in self._get(guild_id).get("allow") or []]

    def blocked_categories(self, guild_id: Optional[str]) -> list:
        return [str(c) for c in self._get(guild_id).get("block") or []]

    def set_categories(self, guild_id: str, mode: str,
                       ids: Iterable[str] = ()) -> None:
        """``mode``: "only" (allow list), "block", or "clear". Setting one
        list clears the other: "news only" and "no sports" together would
        leave an admin guessing which wins."""
        ids = sorted({str(i) for i in ids if i})
        if mode == "clear":
            self._put(guild_id, allow=None, block=None)
        elif mode == "only":
            if not ids:
                raise RuleError("Tell me which categories to allow.")
            self._put(guild_id, allow=ids, block=None)
        elif mode == "block":
            if not ids:
                raise RuleError("Tell me which categories to block.")
            self._put(guild_id, block=ids, allow=None)
        else:
            raise RuleError(f"I don't know how to {mode!r} categories.")

    @staticmethod
    def _cats(channel: I.Channel) -> list:
        return list(channel.categories) or [I.OTHER_CATEGORY]

    def permits(self, guild_id: Optional[str], channel: I.Channel) -> bool:
        """May this channel be listed or put on here? Allowed if any of
        its categories is on the allow list; blocked if any is on the
        block list (a news channel also filed under sports is blocked by
        "no sports": blocking means not seeing it)."""
        cats = self._cats(channel)
        allow = self.allowed_categories(guild_id)
        if allow and not any(c in allow for c in cats):
            return False
        block = self.blocked_categories(guild_id)
        return not (block and any(c in block for c in cats))

    def has_category_rules(self, guild_id: Optional[str]) -> bool:
        return bool(self.allowed_categories(guild_id)
                    or self.blocked_categories(guild_id))

    # -- watch time -------------------------------------------------------

    def max_minutes(self, guild_id: Optional[str]) -> int:
        """0 means no limit."""
        value = self._get(guild_id).get("max_minutes")
        if value is None:
            return self.default_max_minutes
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return self.default_max_minutes

    def max_seconds(self, guild_id: Optional[str]) -> Optional[float]:
        minutes = self.max_minutes(guild_id)
        return minutes * 60.0 if minutes else None

    def set_max_minutes(self, guild_id: str, minutes: Optional[int]) -> None:
        """None or 0 removes the limit (stored as 0, so it also
        overrides a host default)."""
        minutes = int(minutes or 0)
        if minutes < 0 or minutes > MAX_LIMIT_MINUTES:
            raise RuleError("A limit has to be between 1 minute and a "
                            "week.")
        self._put(guild_id, max_minutes=minutes)

    # -- quality ----------------------------------------------------------

    def quality(self, guild_id: Optional[str]) -> Quality:
        """The server's stream size. A stored 1080p on a host that has
        since turned 1080p off reads as 720p, and :meth:`quality_note`
        says so."""
        q = quality(self._get(guild_id).get("quality")
                    or self.default_quality)
        if q.name == "1080p" and not self.allow_1080p:
            return QUALITIES["720p"]
        return q

    def quality_note(self, guild_id: Optional[str]) -> str:
        """Why the stored choice isn't what streams, or ""."""
        stored = str(self._get(guild_id).get("quality")
                     or self.default_quality)
        if stored == "1080p" and not self.allow_1080p:
            return ("1080p was chosen, but this host has 1080p turned off, "
                    "so it streams at 720p.")
        return ""

    def set_quality(self, guild_id: str, name: str) -> Quality:
        key = str(name or "").lower().replace(" ", "")
        key = {"1080": "1080p", "fullhd": "1080p", "hd": "720p",
               "720": "720p"}.get(key, key)
        if key not in QUALITIES:
            raise RuleError("I can stream at 720p or 1080p.")
        if key == "1080p" and not self.allow_1080p:
            # Owner, V2: refused with the reason, never silently
            # downgraded.
            raise RuleError("I can't stream 1080p from this host: it has "
                            "one CPU core, and a 1080p encode alone takes "
                            "half of it. 720p it is.")
        # Stored either way: with a 1080p default, "720p" has to be
        # remembered rather than read as "unset".
        self._put(guild_id, quality=key)
        return QUALITIES[key]

    # -- favourites -------------------------------------------------------

    def favourites(self, guild_id: Optional[str]) -> list:
        """Channel ids, oldest first."""
        return [str(c) for c in self._get(guild_id).get("favourites") or []]

    def add_favourite(self, guild_id: str, channel_id: str) -> bool:
        """False if it was already one."""
        favs = self.favourites(guild_id)
        if channel_id in favs:
            return False
        if len(favs) >= MAX_FAVOURITES:
            raise RuleError(f"That's {MAX_FAVOURITES} favourites already. "
                            f"Take one off first.")
        self._put(guild_id, favourites=favs + [channel_id])
        return True

    def remove_favourite(self, guild_id: str, channel_id: str) -> bool:
        favs = self.favourites(guild_id)
        if channel_id not in favs:
            return False
        favs.remove(channel_id)
        self._put(guild_id, favourites=favs or None)
        return True

    def last_watched(self, guild_id: Optional[str]) -> Optional[str]:
        value = self._get(guild_id).get("last")
        return str(value) if value else None

    def note_watched(self, guild_id: str, channel_id: str) -> None:
        if self.last_watched(guild_id) != channel_id:
            self._put(guild_id, last=channel_id)

    def the_usual(self, guild_id: Optional[str]) -> Optional[str]:
        """"Put on the usual": the last channel watched here if it's a
        favourite, else the first favourite, else the last channel
        watched. None when there's nothing to go on."""
        favs = self.favourites(guild_id)
        last = self.last_watched(guild_id)
        if last and last in favs:
            return last
        if favs:
            return favs[0]
        return last
