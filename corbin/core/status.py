"""Corbin's profile status: "Listening to …", "Watching …".

Every 10 seconds:

1. a queued track playing in voice -> "Playing Title by Artist", always
2. otherwise, alternating each tick between
   "Watching N servers" and "Listening to <what's on 975 KZLF>"

The radio half is the station's own now-playing, whether or not Corbin
is streaming it, and it is skipped (the tick stays on Watching) when the
station can't be reached.

Fluxer has **no activities**. Its op 3 reads ``status``, ``afk``,
``mobile`` and ``custom_status`` (``{text, emoji_id, emoji_name,
expires_at}``) and silently drops everything else -- verified in
``fluxer_gateway/src/gateway/gateway_handler_dispatch.erl``. The SDK's
``Gateway.update_presence`` only sends ``activities``, which is why the
status never rendered: every frame was accepted and then had nothing in
it Fluxer keeps. So the line goes out as a custom status, with the verb
("Listening to") written into the text, since there is no activity type
to imply it. ``activities`` is still sent alongside, harmlessly, in case
Fluxer ever adds them.

The loop never raises into the caller: a status line is cosmetic and
must not be able to take the bot down.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("corbin.status")

#: Discord-style activity types. Fluxer ignores them, but they still pick
#: the verb for the custom-status text.
PLAYING, STREAMING, LISTENING, WATCHING, CUSTOM, COMPETING = 0, 1, 2, 3, 4, 5

REFRESH_SECONDS = 10.0
#: The station's now-playing is one HTTP call. At a 10s cadence that is
#: 360 calls an hour for a line that changes every few minutes.
RADIO_CACHE_SECONDS = 30.0
#: Fluxer's CUSTOM_STATUS_TEXT_LIMIT (fluxer_app CustomStatus.ts).
MAX_ACTIVITY_CHARS = 128
#: Gateway opcode for PRESENCE_UPDATE.
OP_PRESENCE_UPDATE = 3

VERBS = {PLAYING: "Playing", STREAMING: "Streaming", LISTENING: "Listening to",
         WATCHING: "Watching", COMPETING: "Competing in"}
#: Below this and we say "online" rather than "idle".
ACTIVE_WINDOW = 1800.0


def memory_percent() -> Optional[int]:
    try:
        import psutil

        return int(psutil.virtual_memory().percent)
    except Exception:
        return None


def clip(label: str, suffix: str = "") -> str:
    """Fit ``label + suffix`` into the activity limit, trimming the label."""
    if len(label) + len(suffix) <= MAX_ACTIVITY_CHARS:
        return f"{label}{suffix}"
    room = max(0, MAX_ACTIVITY_CHARS - len(suffix))
    return f"{label[:room]}{suffix}"


def track_label(track: Any) -> str:
    """"Artist - Title", or just the title when the uploader adds nothing.

    A YouTube "uploader" is often the artist's own topic channel, so
    "Rotting Christ - Rotting Christ - The Raven" is a real outcome
    worth avoiding.
    """
    title = str(getattr(track, "title", "") or "").strip()
    artist = str(getattr(track, "uploader", "") or "").strip()
    for noise in (" - Topic", " - Official", "VEVO"):
        if artist.endswith(noise):
            artist = artist[: -len(noise)].strip()
    if not artist or artist.lower() in title.lower():
        return title
    return f"{artist} - {title}"


def status_text(kind: int, text: str) -> str:
    """The custom-status line: the verb spelled out, within the limit.

    Clipping goes through ``clip`` so the " · N% load" suffix survives a
    long track title.
    """
    verb = VERBS.get(kind, "")
    if not verb or kind == CUSTOM:
        return text[:MAX_ACTIVITY_CHARS]
    label, sep, load = text.rpartition(" · ")
    if not sep:
        label, load = text, ""
    return clip(f"{verb} {label}", f" · {load}" if load else "")


def presence_frame(status: str, text: str, kind: int) -> dict:
    """The op 3 ``d`` payload Fluxer actually reads."""
    return {
        "since": None,
        "status": status,
        "afk": False,
        "custom_status": ({"text": status_text(kind, text)} if text
                          else None),
        "activities": ([{"name": text, "type": kind}] if text else []),
    }


async def send_presence(gateway: Any, *, status: str, text: str,
                        kind: int) -> None:
    """Send op 3 with a custom status.

    Written straight to the socket, the same way ``Gateway._send`` does,
    because ``update_presence`` has no ``custom_status`` parameter. A
    gateway without a socket (tests, or an SDK that moved) falls back to
    ``update_presence`` rather than sending nothing -- and logs, since
    that fallback is exactly the path that renders as no status at all.
    """
    ws = getattr(gateway, "_ws", None)
    if ws is not None and not getattr(ws, "closed", False) \
            and hasattr(ws, "send_str"):
        import json

        await ws.send_str(json.dumps({
            "op": OP_PRESENCE_UPDATE,
            "d": presence_frame(status, text, kind)}))
        return
    logger.warning("no gateway socket for a custom status; sending an "
                   "activity-only presence, which Fluxer will not show")
    await gateway.update_presence(status=status, activity_name=text,
                                  activity_type=kind)


def playing_label(track: Any) -> str:
    """"Title by Artist", or just the title when there's no artist."""
    title = str(getattr(track, "title", "") or "").strip()
    artist = str(getattr(track, "uploader", "") or "").strip()
    for noise in (" - Topic", " - Official", "VEVO"):
        if artist.endswith(noise):
            artist = artist[: -len(noise)].strip()
    if not artist or artist.lower() in title.lower():
        return title
    return f"{title} by {artist}"


def install_identify_presence(client: Any, describe: Any) -> bool:
    """Put the initial presence into IDENTIFY as well as sending op 3.

    Some gateways only read a bot's presence from the IDENTIFY payload
    and ignore later PRESENCE_UPDATE frames. fluxer.py's IDENTIFY has no
    presence field at all, so if Fluxer is one of those, nothing we send
    afterwards is ever going to show. This adds it without touching
    anything else in the handshake -- an unknown key is ignored by a
    gateway that does not want it, so the cost of being wrong is zero.

    Returns False if the SDK has moved; the connection is untouched
    either way.
    """
    gateway = getattr(client, "_gateway", None)
    original = getattr(gateway, "_send_identify", None)
    if gateway is None or original is None:
        return False
    if getattr(gateway, "_corbin_identify_presence", False):
        return True

    async def identify_with_presence() -> None:
        # Stash the presence where _send picks it up, by sending the
        # normal IDENTIFY and immediately following with op 3. Doing it
        # here rather than in on_ready means it lands before the first
        # GUILD_CREATE, which is when a client would first render it.
        await original()
        try:
            kind, text, state = await describe()
            await send_presence(gateway, status=state, text=text, kind=kind)
        except Exception:
            logger.debug("presence-at-identify failed", exc_info=True)

    try:
        gateway._send_identify = identify_with_presence
        gateway._corbin_identify_presence = True
    except Exception:
        return False
    return True


class StatusUpdater:
    """Keeps the profile status in step with what Corbin is doing."""

    def __init__(self, client: Any, *, players: Any = None,
                 voice: Any = None,
                 radio_label: Optional[Callable[[], Any]] = None,
                 guild_count: Optional[Callable[[], int]] = None) -> None:
        self.client = client
        self.players = players
        self.voice = voice
        self.radio_label = radio_label
        self.guild_count = guild_count
        self._task: Optional[asyncio.Task] = None
        self._last: Optional[tuple] = None
        #: Which half of the rotation this tick shows.
        self.show_radio = False
        self._radio_cached: tuple[float, Optional[str]] = (0.0, None)
        #: Set on any real activity, to choose online vs idle.
        self.last_active = 0.0

    # -- what to say -------------------------------------------------------

    def _current_track(self) -> Optional[Any]:
        players = getattr(self.players, "_players", None) or {}
        for player in players.values():
            track = getattr(player, "current", None)
            if track is not None:
                return track
        return None

    def _radio_session(self) -> bool:
        """True when a voice session is playing the radio rather than a
        queued track."""
        sessions = getattr(self.voice, "_sessions", None) or {}
        for session in sessions.values():
            if getattr(session, "source_url", None) and \
                    getattr(session, "playing", False):
                return True
        return False

    async def _radio_now(self) -> Optional[str]:
        import time

        at, label = self._radio_cached
        if self.radio_label is None:
            return None
        if time.monotonic() - at < RADIO_CACHE_SECONDS:
            return label
        try:
            value = self.radio_label()
            if asyncio.iscoroutine(value):
                value = await value
            label = str(value).strip() if value else None
        except Exception:
            label = None
        self._radio_cached = (time.monotonic(), label)
        return label

    async def describe(self) -> tuple[int, str, str]:
        """``(activity_type, text, status)`` for right now."""
        import time

        load = memory_percent()
        suffix = f" · {load}% load" if load is not None else ""
        active = (time.monotonic() - self.last_active) < ACTIVE_WINDOW

        track = self._current_track()
        if track is not None:
            return PLAYING, clip(playing_label(track), suffix), "online"

        on_air = self._radio_session()
        if self.show_radio:
            label = await self._radio_now()
            if label:
                return (LISTENING, clip(label, suffix),
                        "online" if (active or on_air) else "idle")

        count = 0
        try:
            count = int(self.guild_count()) if self.guild_count else 0
        except Exception:
            count = 0
        plural = "" if count == 1 else "s"
        return (WATCHING, clip(f"{count} server{plural}", suffix),
                "online" if (active or on_air) else "idle")

    # -- pushing it --------------------------------------------------------

    async def push(self, force: bool = False) -> bool:
        """Send the current status. Returns True if anything was sent."""
        gateway = getattr(self.client, "_gateway", None)
        update = getattr(gateway, "update_presence", None)
        if update is None:
            return False

        kind, text, status = await self.describe()
        # Re-sending an unchanged presence every 20s is pure gateway
        # traffic, and Discord rate-limits presence updates; assume
        # Fluxer does too.
        if not force and (kind, text, status) == self._last:
            return False
        try:
            await send_presence(gateway, status=status, text=text, kind=kind)
        except Exception:
            # WARNING, not debug: a status that silently never sends is
            # how this stayed broken for so long.
            logger.warning("presence update failed", exc_info=True)
            return False
        # INFO only when it means something: the first push, and music
        # starting or stopping. The rotation itself changes the line every
        # 10s, which at INFO would be ~8,600 log lines a day.
        was_music = self._last is not None and self._last[0] == PLAYING
        first = self._last is None
        self._last = (kind, text, status)
        level = (logging.INFO if first or (kind == PLAYING) != was_music
                 else logging.DEBUG)
        logger.log(level, "status: %s (%s)", status_text(kind, text), status)
        return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.push()
                self.show_radio = not self.show_radio
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("status loop error", exc_info=True)
            await asyncio.sleep(REFRESH_SECONDS)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
