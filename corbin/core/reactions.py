"""Reaction controls: emoji reactions standing in for buttons.

Fluxer has no message components yet (buttons are on its 2026 roadmap), so
an embed that needs controls -- switch translation, pause, skip -- gets
emoji reactions instead. Pressing one runs an action; the bot then removes
the presser's reaction so the same emoji can be pressed again.

Any embed opts in by carrying ``Embed.controls`` (a :class:`Controls`).
``adapter.send`` registers the posted message here and adds the reactions,
so a command never has to know its message id. ``bot.py`` routes reaction
events to :meth:`ReactionPanels.press`, which calls the handler registered
for the panel's ``kind``.

Platform-neutral: no SDK imports. The adapter is reached through the small
``ReactionPort`` protocol, so this is testable offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, Optional, Protocol, Sequence)

logger = logging.getLogger("corbin.reactions")

#: How long a panel answers presses. Long enough for a verse someone
#: scrolls back to the next day; the music panel is replaced per track.
PANEL_TTL = 7 * 24 * 3600
#: Panels kept in memory, oldest dropped first.
MAX_PANELS = 1000
#: Presses closer together than this from one person on one message are
#: the add/remove echo of a single press, not two presses.
PRESS_DEBOUNCE = 1.0
#: Longest one press may run. Presses on a message are handled one at a
#: time, so a handler that hung (a wedged LiveKit call under ⏮️, live
#: 2026-10-17) left every later button on that card dead.
PRESS_TIMEOUT = 45.0


def normalise(emoji: str) -> str:
    """Compare emoji without the variation selector.

    ⏯️ arrives as U+23EF alone or U+23EF U+FE0F depending on the client;
    both are the same button.
    """
    return (emoji or "").replace("️", "").strip()


def reaction_emoji(partial: Any) -> Optional[str]:
    """The emoji character from a gateway reaction, or None.

    fluxer.py's ``PartialEmoji`` *demojizes* ``name`` for unicode emoji --
    🔍 arrives as ``":magnifying_glass_tilted_left:"`` -- and keeps the
    character in ``unicode``. Reading ``name`` silently matched nothing.
    Custom emoji (they have an id) are never controls: None.
    """
    if partial is None or getattr(partial, "id", None):
        return None
    return (getattr(partial, "unicode", None)
            or getattr(partial, "name", None) or None)


@dataclass
class Controls:
    """What an embed asks for: a kind, its state, and the emoji to add.

    ``buttons`` is ``((emoji, label), ...)`` in display order. The label is
    for the footer legend; :func:`legend` renders it.
    """

    kind: str
    buttons: Sequence[tuple[str, str]]
    state: dict = field(default_factory=dict)
    #: Only the newest panel of this kind per guild answers. The Now
    #: Playing card: an old card's buttons acting on the new track, with
    #: a stale title above them, would be confusing.
    exclusive: bool = False

    def emojis(self) -> list[str]:
        return [e for e, _ in self.buttons]


def legend(buttons: Sequence[tuple[str, str]]) -> str:
    """``📜 KJV · 📘 NASB95`` for a footer."""
    return " · ".join(f"{e} {label}" for e, label in buttons)


@dataclass
class Panel:
    kind: str
    channel_id: str
    guild_id: Optional[str]
    emojis: tuple[str, ...]
    state: dict
    created: float = field(default_factory=time.time)

    def offers(self, emoji: str) -> bool:
        want = normalise(emoji)
        return any(normalise(e) == want for e in self.emojis)

    def canonical(self, emoji: str) -> Optional[str]:
        """The panel's own spelling of ``emoji``, so handlers can compare."""
        want = normalise(emoji)
        for e in self.emojis:
            if normalise(e) == want:
                return e
        return None


class ReactionPort(Protocol):
    async def add_reaction(self, channel_id: str, message_id: str,
                           emoji: str) -> bool: ...

    async def remove_user_reaction(self, channel_id: str, message_id: str,
                                   emoji: str, user_id: str) -> bool: ...


#: ``handler(panel, message_id, emoji, user_id) -> None``
Handler = Callable[[Panel, str, str, str], Awaitable[None]]
#: ``resolver(channel_id, message_id, emoji) -> Panel or None`` -- rebuilds
#: a panel the registry has forgotten (a restart), if the kind allows it.
Resolver = Callable[[str, str, str], Awaitable[Optional[Panel]]]


class ReactionPanels:
    """Message id -> panel, plus the press pipeline."""

    def __init__(self, port: Optional[ReactionPort] = None, *,
                 ttl: float = PANEL_TTL, max_panels: int = MAX_PANELS,
                 clock: Callable[[], float] = time.time) -> None:
        self.port = port
        self.ttl = ttl
        self.max_panels = max_panels
        self.clock = clock
        self._panels: "OrderedDict[str, Panel]" = OrderedDict()
        self._handlers: dict[str, Handler] = {}
        self._resolvers: list[Resolver] = []
        #: (message, user, emoji) we removed ourselves; their REMOVE event
        #: is our own echo, not a press.
        self._self_removed: dict[tuple[str, str, str], float] = {}
        #: (message, user) -> last press time, for debouncing.
        self._last_press: dict[tuple[str, str], float] = {}
        #: One press at a time per message, so two quick presses can't
        #: interleave their edits.
        self._locks: dict[str, asyncio.Lock] = {}
        #: Messages where removing a reaction failed (no Manage Messages):
        #: there, un-reacting is also a press.
        self._toggle_mode: set[str] = set()

    # -- registry ----------------------------------------------------------

    def on(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def resolver(self, fn: Resolver) -> None:
        self._resolvers.append(fn)

    def register(self, message_id: str, panel: Panel) -> None:
        self._panels[str(message_id)] = panel
        self._panels.move_to_end(str(message_id))
        while len(self._panels) > self.max_panels:
            self._panels.popitem(last=False)

    def get(self, message_id: str) -> Optional[Panel]:
        panel = self._panels.get(str(message_id))
        if panel is None:
            return None
        if self.clock() - panel.created > self.ttl:
            self._panels.pop(str(message_id), None)
            return None
        return panel

    def drop(self, message_id: str) -> None:
        self._panels.pop(str(message_id), None)

    def latest(self, kind: str, guild_id: Optional[str]
               ) -> Optional[tuple[str, Panel]]:
        """The newest live panel of ``kind`` in a guild, as (id, panel).
        The Now Playing card's footer is kept current through this."""
        gid = str(guild_id) if guild_id else None
        for mid in reversed(list(self._panels)):
            panel = self.get(mid)
            if panel is not None and panel.kind == kind \
                    and panel.guild_id == gid:
                return mid, panel
        return None

    def drop_kind(self, kind: str, guild_id: Optional[str]) -> list[str]:
        """Forget every panel of ``kind`` in a guild. Returns their ids."""
        gone = [mid for mid, p in self._panels.items()
                if p.kind == kind and p.guild_id == guild_id]
        for mid in gone:
            self._panels.pop(mid, None)
        return gone

    def __len__(self) -> int:
        return len(self._panels)

    # -- attaching ---------------------------------------------------------

    async def attach(self, message_id: str, channel_id: str,
                     guild_id: Optional[str], controls: Controls) -> Panel:
        """Register a freshly sent message and add its reactions."""
        panel = Panel(kind=controls.kind, channel_id=str(channel_id),
                      guild_id=str(guild_id) if guild_id else None,
                      emojis=tuple(controls.emojis()),
                      state=dict(controls.state), created=self.clock())
        if controls.exclusive:
            self.drop_kind(panel.kind, panel.guild_id)
        self.register(message_id, panel)
        if self.port is not None:
            for emoji in panel.emojis:
                try:
                    if not await self.port.add_reaction(
                            str(channel_id), str(message_id), emoji):
                        logger.info("could not add %s to %s; the bot may "
                                    "lack Add Reactions", emoji, message_id)
                        break
                except Exception:
                    logger.debug("add_reaction failed", exc_info=True)
                    break
        return panel

    # -- pressing ----------------------------------------------------------

    async def press(self, *, message_id: str, channel_id: str,
                    user_id: str, emoji: str, added: bool,
                    bot_user_id: Optional[str] = None) -> bool:
        """Handle one reaction event. Returns True if it ran an action."""
        message_id, user_id = str(message_id), str(user_id)
        if bot_user_id and user_id == str(bot_user_id):
            return False

        key = (message_id, user_id, normalise(emoji))
        if not added:
            # Our own clean-up echoing back, or a real un-react. Only the
            # latter counts, and only where we can't remove reactions.
            if self._self_removed.pop(key, None) is not None:
                return False
            if message_id not in self._toggle_mode:
                return False

        panel = self.get(message_id)
        if panel is None:
            panel = await self._resolve(str(channel_id), message_id, emoji)
            if panel is None:
                return False
            panel.created = self.clock()
            self.register(message_id, panel)
        if not panel.offers(emoji):
            return False
        handler = self._handlers.get(panel.kind)
        if handler is None:
            return False

        now = self.clock()
        last = self._last_press.get((message_id, user_id), 0.0)
        if now - last < PRESS_DEBOUNCE:
            return False
        self._last_press[(message_id, user_id)] = now
        if len(self._last_press) > 4 * self.max_panels:
            self._last_press.clear()

        canonical = panel.canonical(emoji) or emoji
        lock = self._locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            try:
                await asyncio.wait_for(
                    handler(panel, message_id, canonical, user_id),
                    PRESS_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("reaction handler for %s %s on %s gave up "
                             "after %.0fs; the card's buttons are free "
                             "again", panel.kind, canonical, message_id,
                             PRESS_TIMEOUT)
            except Exception:
                logger.exception("reaction handler for %s failed",
                                 panel.kind)
        if added:
            await self._clear_press(panel.channel_id, message_id, emoji,
                                    user_id)
        return True

    async def _resolve(self, channel_id: str, message_id: str,
                       emoji: str) -> Optional[Panel]:
        for fn in self._resolvers:
            try:
                panel = await fn(channel_id, message_id, emoji)
            except Exception:
                logger.debug("panel resolver failed", exc_info=True)
                continue
            if panel is not None:
                return panel
        return None

    async def _clear_press(self, channel_id: str, message_id: str,
                           emoji: str, user_id: str) -> None:
        """Take the presser's reaction off so the button can be pressed again."""
        if self.port is None:
            return
        key = (message_id, user_id, normalise(emoji))
        self._self_removed[key] = self.clock()
        try:
            ok = await self.port.remove_user_reaction(
                channel_id, message_id, emoji, user_id)
        except Exception:
            ok = False
        if not ok:
            self._self_removed.pop(key, None)
            if message_id not in self._toggle_mode:
                logger.info("can't remove reactions in %s (Manage Messages?);"
                            " un-reacting will also press", channel_id)
            self._toggle_mode.add(message_id)
        if len(self._self_removed) > 4 * self.max_panels:
            self._self_removed.clear()
