"""Streaming video into a voice channel, as a Fluxer screen share.

What Phase 0d proved on live Fluxer (``VOICE_ROADMAP.md``,
``tools/check_video_publish.py``), and what this module carries over:

* **Publish as a screen share**: ``SOURCE_SCREENSHARE``, ``is_screencast``,
  simulcast off, an explicit bitrate. Viewers opt in by clicking his
  stream tile; until they do it is grey. Nobody gets video they didn't
  ask for, and it isn't bound by the per-channel camera limit.
* **The apps only show a stream for the voice-state flag.** fluxer.py's
  ``join_voice`` never sets ``self_stream``, so it is sent as its own op 4
  -- and that op 4 **must carry the connection's ``connection_id``**.
  Without it Fluxer's gateway routes the update as a brand-new join
  (``route_by_connection_id`` in ``guild_voice_connection.erl``) and the
  flag never lands. The id arrives in VOICE_SERVER_UPDATE, which
  fluxer.py's models drop; :class:`ConnectionIds` catches it from the raw
  dispatch.
* **720p30 by default** (owner, 2026-10-17). The host is one CPU core: a
  1080p encode alone took half of it before any real video was decoded.
  A server can choose 1080p30 (T4), but only where the host allows it
  (``CORBIN_ALLOW_1080P``); otherwise the choice is refused with that
  reason, never silently downgraded. See :data:`QUALITIES`.

The owner's rules (2026-10-17), enforced by :class:`StreamManager`:

* **One stream at a time, across every server.** A second server asking
  is told where he's busy (:class:`StreamBusy`), not queued.
* **No voice chat while streaming.** Starting a stream ends voice chat
  wherever it runs, and ``VoiceChat.start`` refuses while a stream does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from ..identity import NAME

logger = logging.getLogger("corbin.video")

WIDTH, HEIGHT, FPS = 1280, 720, 30
#: What 720p30 VP8 needed in 0d (2.6-3.0 Mbit/s sent, no quality limit).
BITRATE = 3_000_000
#: The SFU never answering is a real failure mode on Fluxer (it did so for
#: an unknown audio source); don't wait forever on it.
PUBLISH_TIMEOUT = 15.0
#: Teardown steps are bounded for the same reason as voice.py's: a wedged
#: LiveKit call must not keep a stream "running" that nobody can end.
TEARDOWN_TIMEOUT = 5.0
#: How long to wait for Fluxer to echo the flag before calling it lost.
FLAG_ECHO_TIMEOUT = 5.0
#: After a fresh join: how long to wait for Fluxer to confirm the voice
#: connection (our own VOICE_STATE_UPDATE for it), then a short settle.
#: Publishing 24 ms after joining gave viewers a grey tile that never
#: filled; the same stream started in an already-settled connection
#: showed the picture (live, 2026-10-17). 0d's check script always
#: waited 5 s+ first, which is why it never saw this.
CONFIRM_TIMEOUT = 10.0
SETTLE = 2.0
#: Outbound stats are logged this long after starting, then this often.
STATS_FIRST, STATS_EVERY = 10.0, 30.0
#: When the one-core host is squeezed, give up frame rate, not
#: resolution. (Fluxer's overlay showing 240p in a tile is the viewer
#: asking for less; fullscreen shows 720p30. Not this.)
DEGRADATION = "DEGRADATION_PREFERENCE_MAINTAIN_RESOLUTION"
#: Bytes in one I420 frame at the stream size.
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2
#: Stall watchdog for the ffmpeg feeding a stream (TV, music video). A
#: live HLS source that stalls can leave ffmpeg waiting forever without
#: exiting, and every recovery path (TV's on-air retry, music video's
#: restart) is triggered by ffmpeg *ending*: live 2026-10-20 the picture
#: froze for good, stats at "0x0 @ 0 fps", nothing logged. A read that
#: waits longer than this kills ffmpeg, turning the hang into an ending.
#: Only a read actually in progress is timed, so a paused song or a full
#: read-ahead queue never trips it.
FRAME_STALL_TIMEOUT = 12.0
#: The first frame gets longer: opening and seeking a stream measured up
#: to ~7 s here (music video plans for up to 12).
FIRST_FRAME_TIMEOUT = 30.0


@dataclass(frozen=True)
class Quality:
    """One stream size. Every producer of frames (TV, music video, title
    cards) sizes its output from this, and the stream is published at
    it, so a frame can never be the wrong number of bytes."""
    name: str
    width: int
    height: int
    bitrate: int

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 3 // 2


#: The two sizes the owner allows (2026-10-17): no 480p.
QUALITIES = {
    "720p": Quality("720p", WIDTH, HEIGHT, BITRATE),
    # Untested live: 0d only measured 720p. VP8 at 1080p30 wants about
    # twice the bits.
    "1080p": Quality("1080p", 1920, 1080, 6_000_000),
}
DEFAULT_QUALITY = QUALITIES["720p"]


def quality(name: Optional[str]) -> Quality:
    """``QUALITIES[name]``, 720p for anything unknown or None."""
    return QUALITIES.get(str(name or "").lower(), DEFAULT_QUALITY)


# -- the voice connection id ---------------------------------------------------

class ConnectionIds:
    """Each guild's current voice ``connection_id``, from raw dispatches.

    Fed every gateway dispatch by ``bot.py``'s ``_dispatch`` wrapper.
    VOICE_SERVER_UPDATE carries it (0d: fields ``channel_id,
    connection_id, endpoint, guild_id``); our own VOICE_STATE_UPDATE does
    too, and also says whether the flag landed.
    """

    def __init__(self, me: Callable[[], str]) -> None:
        self._me = me
        self._ids: dict[str, str] = {}
        #: guild -> our latest raw voice state
        self._states: dict[str, dict] = {}
        self._changed: dict[str, asyncio.Event] = {}

    def observe(self, event_name: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        gid = str(data.get("guild_id") or "")
        if not gid:
            return
        if event_name == "VOICE_SERVER_UPDATE":
            if data.get("connection_id"):
                self._ids[gid] = str(data["connection_id"])
        elif event_name == "VOICE_STATE_UPDATE":
            if str(data.get("user_id") or "") != str(self._me() or ""):
                return
            if data.get("channel_id") is None:
                # He left: the id is dead with the connection.
                self._ids.pop(gid, None)
            elif data.get("connection_id"):
                self._ids[gid] = str(data["connection_id"])
            self._states[gid] = data
            self._event(gid).set()

    def _event(self, gid: str) -> asyncio.Event:
        if gid not in self._changed:
            self._changed[gid] = asyncio.Event()
        return self._changed[gid]

    def get(self, guild_id: Any) -> Optional[str]:
        return self._ids.get(str(guild_id))

    def state(self, guild_id: Any) -> dict:
        return dict(self._states.get(str(guild_id)) or {})

    async def wait_for_state(self, guild_id: Any, timeout: float) -> bool:
        """True when a new voice state for us arrived within ``timeout``."""
        event = self._event(str(guild_id))
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def confirmed(self, guild_id: Any, channel_id: Any) -> bool:
        """Has Fluxer confirmed our connection in this channel? Its own
        voice state for us, naming the channel and our current id."""
        gid = str(guild_id)
        state = self._states.get(gid) or {}
        return (str(state.get("channel_id") or "") == str(channel_id)
                and bool(self._ids.get(gid))
                and str(state.get("connection_id") or self._ids[gid])
                == self._ids[gid])

    async def wait_confirmed(self, guild_id: Any, channel_id: Any,
                             timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.confirmed(guild_id, channel_id):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.expect_state(guild_id)
            if self.confirmed(guild_id, channel_id):
                break
            await self.wait_for_state(guild_id, remaining)
        return True

    def expect_state(self, guild_id: Any) -> None:
        """Call before sending an op 4 whose echo you'll wait for."""
        self._event(str(guild_id)).clear()


def stream_flags(guild_id: Any, channel_id: Any, connection_id: str, *,
                 stream: bool, video: bool = False) -> dict:
    """The op 4 body that sets the flags on an existing connection."""
    return {"guild_id": str(guild_id), "channel_id": str(channel_id),
            "connection_id": str(connection_id),
            "self_mute": False, "self_deaf": False,
            "self_stream": bool(stream), "self_video": bool(video)}


# -- frames --------------------------------------------------------------------

#: An async iterator of I420 frames at WIDTH x HEIGHT: bare ``bytes``
#: (paced by the wall clock at FPS) or ``(pts_seconds, bytes)`` (paced by
#: the stream's ``clock``, e.g. the music it accompanies).
Frames = AsyncIterator[Any]

#: A clock-paced frame this far behind the clock is dropped, not shown
#: late: catching up beats a picture that lags the song.
LATE_DROP = 0.25
#: Longest a clock-paced frame waits for a clock that isn't running
#: (between tracks) before it's shown anyway.
CLOCK_STALL = 15.0
#: This many clock-paced frames dropped in a row (2 s of video) means the
#: video is running behind the music, not jittering: ``on_behind`` is told
#: once per feed so the owner can restart it further ahead. Dropping until
#: it caught up took ~10 s after a slow start (live, 2026-10-17).
RESYNC_AFTER = 60


# -- the on-screen timer ---------------------------------------------------

#: The "12:34 / 24:02" in the bottom-right corner of music videos and local
#: videos (owner, 2026-10-20: "small, and in a corner"). Drawn by ffmpeg's
#: drawtext in the process that already decodes, from the frames' own
#: timestamps: right after a seek, frozen while paused. Not on live TV,
#: which has no total or position. Off with CORBIN_STREAM_TIMER=0.
TIMER = True
#: Fonts tried in order. matplotlib (a dependency) ships DejaVu, so the
#: first is normally there; monospace so the digits don't jiggle.
_FONT_NAMES = ("DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf")
_SYSTEM_FONTS = ("/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                 "/usr/share/fonts/TTF/DejaVuSansMono.ttf")
#: None until the self-test has run; then whether drawtext works here.
_TIMER_OK: Optional[bool] = None


def timer_font() -> Optional[str]:
    """A font file for drawtext, or None (it then asks fontconfig)."""
    import importlib.util
    import os

    try:
        spec = importlib.util.find_spec("matplotlib")
    except Exception:
        spec = None
    for base in (spec.submodule_search_locations or []) if spec else []:
        for name in _FONT_NAMES:
            path = os.path.join(base, "mpl-data", "fonts", "ttf", name)
            if os.path.isfile(path):
                return path
    return next((p for p in _SYSTEM_FONTS if os.path.isfile(p)), None)


def _clock(total: Optional[float]) -> tuple:
    """(strftime for drawtext, already escaped; Python formatter)."""
    if total and total >= 3600:
        return r"%H\\\:%M\\\:%S", lambda t: (
            f"{int(t) // 3600:02d}:{int(t) % 3600 // 60:02d}:"
            f"{int(t) % 60:02d}")
    return r"%M\\\:%S", lambda t: f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def timer_filter(q: "Quality", *, start: float = 0.0,
                 total: Optional[float] = None) -> str:
    r"""The drawtext filter for "elapsed / total" (or just elapsed with no
    total), small, bottom right (owner, 2026-10-20). "" when the timer
    is off or drawtext
    failed its self-test here: streaming must never depend on it.

    Escaping, checked against real ffmpeg (2026-10-20): inside the quoted
    ``text``, the ``%{pts}`` argument separators are ``\:`` and the colon
    inside its strftime format ``\\\:``; a literal colon is ``\:``."""
    if not TIMER or _TIMER_OK is False:
        return ""
    strf, fmt = _clock(total)
    # Unobtrusive (owner, 2026-10-20): 20 px text at 1080p, 14 at 720p,
    # about 1.9% of the picture's height, in a faint box.
    size = max(14, round(q.height / 54))
    pad = max(8, round(q.height / 72))
    elapsed = r"%{pts\:gmtime\:" + f"{max(0.0, start):.3f}" + r"\:" + \
        strf + "}"
    tail = (" / " + fmt(total).replace(":", r"\:")) if total else ""
    parts = []
    font = timer_font()
    if font:
        parts.append("fontfile=" + font.replace("\\", "/")
                     .replace(":", r"\:").replace("'", ""))
    parts += [f"text='{elapsed}{tail}'", f"fontsize={size}",
              "fontcolor=white@0.85", "box=1", "boxcolor=black@0.35",
              f"boxborderw={pad // 2}", f"x=w-tw-{pad}", f"y=h-th-{pad}"]
    return "drawtext=" + ":".join(parts)


async def check_timer() -> bool:
    """Run the timer filter once on a blank frame. If it fails (no
    drawtext in this ffmpeg, no font), the timer turns itself off with a
    warning instead of every stream failing to start."""
    global _TIMER_OK
    if _TIMER_OK is not None or not TIMER:
        return bool(_TIMER_OK)
    _TIMER_OK = True                    # so timer_filter returns a filter
    filt = timer_filter(DEFAULT_QUALITY, start=5.0, total=1442.0)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}", "-frames:v", "1",
            "-vf", filt, "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        _out, err = await asyncio.wait_for(proc.communicate(), 20)
        ok = proc.returncode == 0
    except Exception as exc:
        ok, err = False, str(exc).encode()
    _TIMER_OK = ok
    if ok:
        logger.info("stream timer: on (font %s)", timer_font() or
                    "from fontconfig")
    else:
        logger.warning("stream timer: off, drawtext failed here: %s",
                       (err or b"").decode(errors="replace")[-200:].strip())
    return ok


class CardFrame(bytes):
    """A title card frame (:func:`with_card`): shown while the real frames
    start, and not counted as the first of them. The first *real* frame
    still goes out at once as a still, and ``on_live`` still means "the
    video is on", not "the card is"."""


#: Title card colours: Corbin's dark red on near-black.
CARD_BG = (16, 18, 24)
CARD_ACCENT = (178, 34, 34)
CARD_TEXT = (240, 240, 240)
CARD_DIM = (150, 155, 165)


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:            # Pillow < 10.1: one fixed size
        return ImageFont.load_default()


def _fit(draw: Any, text: str, size: int, width: int) -> tuple[Any, str]:
    """The largest font (down to a floor) that fits ``text`` in ``width``;
    past the floor the text is cut with an ellipsis."""
    while size > 24:
        font = _font(size)
        if draw.textlength(text, font=font) <= width:
            return font, text
        size -= 6
    font = _font(size)
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return font, text.rstrip() + "…"


def i420(image: Any) -> bytes:
    """An RGB Pillow image as one I420 frame: full Y plane, then U and V
    at half size each way. Pillow's YCbCr is BT.601 full range, close
    enough for a still card."""
    y, cb, cr = image.convert("YCbCr").split()
    half = (image.width // 2, image.height // 2)
    return (y.tobytes() + cb.resize(half).tobytes()
            + cr.resize(half).tobytes())


def title_card(title: str, subtitle: str = "",
               q: Quality = DEFAULT_QUALITY) -> bytes:
    """A still title card at the stream size, as I420. Blocking (Pillow,
    ~50 ms at 1080p): call through ``asyncio.to_thread``."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (q.width, q.height), CARD_BG)
    draw = ImageDraw.Draw(img)
    unit = q.height / 720
    margin = int(96 * unit)
    draw.rectangle((0, q.height - int(10 * unit), q.width, q.height),
                   fill=CARD_ACCENT)
    font, text = _fit(draw, title or NAME, int(72 * unit),
                      q.width - 2 * margin)
    draw.text((q.width // 2, q.height // 2 - int(24 * unit)), text,
              font=font, fill=CARD_TEXT, anchor="mm")
    if subtitle:
        font, text = _fit(draw, subtitle, int(36 * unit),
                          q.width - 2 * margin)
        draw.text((q.width // 2, q.height // 2 + int(48 * unit)), text,
                  font=font, fill=CARD_DIM, anchor="mm")
    draw.text((q.width - margin // 2, q.height - int(36 * unit)), "CORBIN",
              font=_font(int(22 * unit)), fill=CARD_ACCENT, anchor="rm")
    return i420(img)


async def card_for(title: str, subtitle: str,
                   q: Quality = DEFAULT_QUALITY) -> Optional[bytes]:
    """:func:`title_card` off the loop; None if Pillow can't draw it (a
    missing card must never stop the stream it decorates)."""
    try:
        return await asyncio.to_thread(title_card, title, subtitle, q)
    except Exception:
        logger.warning("title card failed for %r", title, exc_info=True)
        return None


async def with_card(frames: Frames, card: Optional[bytes]) -> Frames:
    """``frames``, with ``card`` shown until the first of them arrives.

    The gap this fills: ffmpeg opening and seeking a YouTube or IPTV
    stream took 4-7 s on this host, and the tile sat grey the whole time
    (V3). The card is yielded as a bare :class:`CardFrame`, so the stream
    paces it by the wall clock; the real frames carry on as they are.
    With no card, ``frames`` passes straight through.
    """
    if card is None:
        async for item in frames:
            yield item
        return
    it = frames.__aiter__()
    first = asyncio.ensure_future(it.__anext__())
    try:
        while not first.done():
            yield CardFrame(card)
            # Let the read make progress between cards; _feed's own
            # pacing does the waiting.
            await asyncio.sleep(0)
        try:
            item = first.result()
        except StopAsyncIteration:
            return
        yield item
        async for item in it:
            yield item
    finally:
        if not first.done():
            first.cancel()
            try:
                await first
            except BaseException:
                pass
        # Close the inner frames (it reaps its ffmpeg) only once nothing
        # is mid-read on it: aclose() on a running generator raises.
        closer = getattr(frames, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass


async def test_pattern(q: Optional[Quality] = None) -> Frames:
    """ffmpeg's moving ``testsrc2`` at the stream size, forever.

    What 0d streamed; the owner's live check that the bot itself can
    stream, before any real video exists. ``q``: the size; the default
    (720p) when None.
    """
    q = q or DEFAULT_QUALITY
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi",
        "-i", f"testsrc2=size={q.width}x{q.height}:rate={FPS}",
        "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        while True:
            try:
                yield await proc.stdout.readexactly(q.frame_bytes)
            except asyncio.IncompleteReadError:
                return
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            # communicate(): wait() blocks while unread frames hold the
            # pipe open (see the audio loop in voice.py).
            await asyncio.wait_for(proc.communicate(), TEARDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("test pattern ffmpeg didn't exit")


def _set_enum(message: Any, field_name: str, value_name: str) -> bool:
    """Set a protobuf enum field by its value's name, if this LiveKit has
    it. The enum types aren't re-exported on ``livekit.rtc``."""
    try:
        fd = message.DESCRIPTOR.fields_by_name[field_name]
        setattr(message, field_name,
                fd.enum_type.values_by_name[value_name].number)
        return True
    except Exception:
        logger.debug("can't set %s=%s on this LiveKit", field_name,
                     value_name)
        return False


def _flatten(msg: Any, out: dict) -> dict:
    """Every set leaf of a protobuf message, by field name; enums named."""
    for fd, value in msg.ListFields():
        if hasattr(value, "ListFields"):
            _flatten(value, out)
        elif fd.enum_type is not None:
            out[fd.name] = fd.enum_type.values_by_number[value].name
        else:
            out[fd.name] = value
    return out


async def outbound_layers(track: Any) -> list[dict]:
    """Every outbound-rtp report for the track, flattened.

    More than one means LiveKit is sending simulcast layers, which it
    shouldn't with ``simulcast=False``; the stats log warns if it does.
    """
    if track is None or not hasattr(track, "get_stats"):
        return []
    try:
        reports = await asyncio.wait_for(track.get_stats(), TEARDOWN_TIMEOUT)
    except Exception:
        return []
    layers = []
    for report in reports or ():
        try:
            if report.HasField("outbound_rtp"):
                layers.append(_flatten(report.outbound_rtp, {}))
        except Exception:
            continue
    return layers


async def outbound_stats(track: Any) -> dict:
    """The largest outbound layer's stats; {} if none."""
    layers = await outbound_layers(track)
    if not layers:
        return {}
    return max(layers, key=lambda x: int(x.get("frame_width") or 0))


# -- the stream ----------------------------------------------------------------

class StreamBusy(RuntimeError):
    """He's already streaming in another server."""

    def __init__(self, active: "ActiveStream") -> None:
        self.active = active
        super().__init__(f"I'm already streaming in {active.where}. One "
                         "stream at a time; stop that one first.")


@dataclass
class ActiveStream:
    guild_id: str
    channel_id: str
    label: str
    #: "<channel> (guild name)" for messages.
    where: str
    started: float = field(default_factory=time.monotonic)
    source: Any = None
    publication: Any = None
    room: Any = None
    feeder: Optional[asyncio.Task] = None
    track: Any = None
    stats_task: Optional[asyncio.Task] = None
    frames: int = 0
    late: int = 0
    #: What it is ("test", "music"), for whoever owns that kind.
    kind: str = "test"
    #: () -> seconds, the clock ``(pts, frame)`` items are shown against.
    clock: Optional[Callable[[], Optional[float]]] = None
    #: Keep the publication when the frames run out (a music video
    #: between songs): republishing would drop every viewer back to the
    #: grey "click to watch" tile.
    hold: bool = False
    dropped: int = 0
    #: ``hook(stream)`` when a held stream's frames run out. Not always
    #: "the song finished": the video's connection can die mid-song
    #: while the audio carries on (live, 2026-10-17: frozen picture,
    #: music still playing). The owner decides whether to restart.
    on_frames_end: Optional[Callable[["ActiveStream"],
                                     Awaitable[Any]]] = None
    #: ``hook(stream)`` when the first frame of a start or a swap is
    #: actually sent: the moment "starting the video" becomes "video on".
    on_live: Optional[Callable[["ActiveStream"], Awaitable[Any]]] = None
    #: (event, handler) registered on the room, removed on stop.
    room_handlers: list = field(default_factory=list)
    #: ``hook(stream, behind_seconds)`` when RESYNC_AFTER frames in a row
    #: were too late to show.
    on_behind: Optional[Callable[["ActiveStream", float],
                                 Awaitable[Any]]] = None
    #: The size it was published at. Swapped-in frames must match it.
    quality: Quality = DEFAULT_QUALITY
    #: Live TV's safety net (``tv.TV_MAX_LEAD``): a clock-paced frame
    #: more than this far ahead of the clock is shown now and the
    #: stream re-anchored (``pts_offset``) rather than waited for. With
    #: one ffmpeg feeding sound and picture, a picture that runs ahead
    #: fills the read-ahead, blocks ffmpeg, and starves the sound: a short
    #: pause that comes more often as the offset grows. None (music
    #: video): wait, since its video starts ahead on purpose.
    max_lead: Optional[float] = None
    #: Added to every pts before it's compared with the clock.
    pts_offset: float = 0.0
    #: Re-anchorings so far, and when the last one was logged.
    resyncs: int = 0
    resync_logged: float = 0.0
    #: ``() -> str`` appended to the stats line (TV: how often the sound
    #: ran dry), or None.
    extra_stats: Optional[Callable[[], str]] = None


class StreamManager:
    """At most one stream, across every server.

    ``send_voice_state(body)`` sends an op 4 on the main gateway;
    ``voicechat`` (optional) is ended wherever it runs when a stream
    starts, and is told to refuse while one runs.
    """

    def __init__(self, voice: Any, connection_ids: ConnectionIds, *,
                 send_voice_state: Callable[[dict], Awaitable[Any]],
                 voicechat: Any = None,
                 allowed: Optional[Callable[[str], bool]] = None) -> None:
        self.voice = voice
        self.ids = connection_ids
        self._send_voice_state = send_voice_state
        self.voicechat = voicechat
        self._allowed = allowed
        self.active: Optional[ActiveStream] = None
        self._lock = asyncio.Lock()
        #: Called with the stream after it ends on its own (frames ran
        #: out, or the feed failed), so the channel can be told.
        self.on_ended: Optional[Callable[[ActiveStream, str],
                                         Awaitable[Any]]] = None

    def streaming_in(self) -> Optional[str]:
        """Where he's streaming, for the voice-chat refusal; or None."""
        return self.active.where if self.active is not None else None

    def allowed(self, guild_id: Optional[str]) -> bool:
        if self._allowed is None or not guild_id:
            return False
        try:
            return bool(self._allowed(str(guild_id)))
        except Exception:
            logger.warning("streaming allowlist check failed for %s",
                           guild_id, exc_info=True)
            return False

    async def start(self, guild_id: str, channel_id: str, frames: Frames, *,
                    label: str, where: Optional[str] = None,
                    kind: str = "test",
                    clock: Optional[Callable[[], Optional[float]]] = None,
                    hold: bool = False,
                    on_frames_end: Optional[Callable[[ActiveStream],
                                                     Awaitable[Any]]] = None,
                    on_live: Optional[Callable[[ActiveStream],
                                               Awaitable[Any]]] = None,
                    on_behind: Optional[Callable[[ActiveStream, float],
                                                 Awaitable[Any]]] = None,
                    quality: Optional[Quality] = None,
                    max_lead: Optional[float] = None,
                    extra_stats: Optional[Callable[[], str]] = None
                    ) -> tuple[ActiveStream, list[str]]:
        """Join (or stay in) the channel and stream ``frames``.

        Returns the stream and the guilds whose voice chat was ended for
        it. Raises :class:`StreamBusy` if another server has the stream,
        PermissionError if streaming isn't allowed here, RuntimeError
        naming the step that failed otherwise.
        """
        if not self.allowed(guild_id):
            logger.info("allowlist: refused streaming in %s", guild_id)
            raise PermissionError("Streaming isn't turned on for this "
                                  "server")
        async with self._lock:
            current = self.active
            if current is not None and current.guild_id != str(guild_id):
                raise StreamBusy(current)
            if current is not None:
                # Same server: the new one replaces it.
                await self._stop_locked("replaced")
            ended = await self._end_voice_chat()
            existing = self.voice.get(guild_id) \
                if hasattr(self.voice, "get") else None
            settled = (existing is not None
                       and str(getattr(existing, "channel_id", ""))
                       == str(channel_id))
            session = await self.voice.connect(guild_id, channel_id)
            room = getattr(session.client, "_room", None)
            if room is None:
                raise RuntimeError("connected, but there's no media room")
            if not settled:
                await self._settle(guild_id, channel_id)
            where = (where or getattr(session, "channel_name", "")
                     or "the voice channel")
            stream = ActiveStream(guild_id=str(guild_id),
                                  channel_id=str(channel_id), label=label,
                                  where=where, room=room, kind=kind,
                                  clock=clock, hold=hold,
                                  on_frames_end=on_frames_end,
                                  on_live=on_live, on_behind=on_behind,
                                  quality=quality or DEFAULT_QUALITY,
                                  max_lead=max_lead,
                                  extra_stats=extra_stats)
            # Frames flow before the publish, as in 0d's script: a viewer
            # gets a picture at once rather than waiting on the first one.
            stream.source = self._make_source(stream.quality)
            stream.feeder = asyncio.create_task(self._feed(stream, frames))
            try:
                await self._publish(stream)
            except Exception:
                stream.feeder.cancel()
                raise
            self.active = stream
            self._watch_viewers(stream)
            # Leaving voice by any route ends the stream with it.
            closers = getattr(session, "closers", None)
            if closers is not None:
                closers.append(
                    lambda s=stream: self._stop_if_current(s, "left voice"))
            try:
                await self._set_flag(stream, True)
            except Exception:
                await self._stop_locked("flag failed")
                raise
            stream.stats_task = asyncio.create_task(self._log_stats(stream))
            logger.info("stream started in %s (%s): %s", where, guild_id,
                        label)
            return stream, ended

    async def swap(self, guild_id: str, frames: Frames, *,
                   label: Optional[str] = None, **hooks: Any) -> bool:
        """Feed new frames into the running stream, keeping the
        publication (and so every viewer). False if no stream here.

        ``hooks`` replace the stream's ``clock`` / ``on_frames_end`` /
        ``on_behind`` / ``on_live`` for the new frames: live TV switching
        channels keeps the tile, but the new channel's sound is a new
        clock."""
        async with self._lock:
            stream = self.active
            if stream is None or stream.guild_id != str(guild_id):
                closer = getattr(frames, "aclose", None)
                if closer is not None:
                    await closer()
                return False
            old = stream.feeder
            if old is not None and not old.done():
                old.cancel()
                try:
                    await asyncio.wait_for(old, TEARDOWN_TIMEOUT)
                except (asyncio.CancelledError, asyncio.TimeoutError,
                        Exception):
                    pass
            if label:
                stream.label = label
            for name, value in hooks.items():
                if name not in ("clock", "on_frames_end", "on_behind",
                                "on_live", "max_lead", "extra_stats"):
                    raise TypeError(f"swap() got an unexpected hook {name!r}")
                setattr(stream, name, value)
            # New frames count from 0 against (possibly) a new clock: an
            # old re-anchoring would put them out of step from the start.
            stream.pts_offset = 0.0
            stream.feeder = asyncio.create_task(self._feed(stream, frames))
            logger.info("stream in %s now showing: %s", stream.where,
                        stream.label)
            return True

    async def _stop_if_current(self, stream: ActiveStream,
                               reason: str) -> None:
        """A session closer. A no-op for a stream already replaced:
        moving the stream to another channel disconnects the old one
        while ``start`` holds the lock, and waiting on it would deadlock."""
        if self.active is stream:
            await self.stop(reason)

    async def stop(self, reason: str = "stopped") -> Optional[ActiveStream]:
        async with self._lock:
            return await self._stop_locked(reason)

    async def _stop_locked(self, reason: str) -> Optional[ActiveStream]:
        stream, self.active = self.active, None
        if stream is None:
            return None
        if stream.stats_task is not None:
            stream.stats_task.cancel()
        for event, handler in stream.room_handlers:
            try:
                stream.room.off(event, handler)
            except Exception:
                pass
        stream.room_handlers.clear()
        if stream.feeder is not None and \
                stream.feeder is not asyncio.current_task():
            stream.feeder.cancel()
            try:
                await asyncio.wait_for(stream.feeder, TEARDOWN_TIMEOUT)
            except (asyncio.CancelledError, asyncio.TimeoutError,
                    Exception):
                pass
        try:
            await self._set_flag(stream, False)
        except Exception:
            logger.debug("couldn't clear self_stream", exc_info=True)
        if stream.publication is not None and stream.room is not None:
            try:
                await asyncio.wait_for(
                    stream.room.local_participant.unpublish_track(
                        stream.publication.sid), TEARDOWN_TIMEOUT)
            except Exception:
                logger.debug("video unpublish failed", exc_info=True)
        if stream.source is not None:
            try:
                await asyncio.wait_for(stream.source.aclose(),
                                       TEARDOWN_TIMEOUT)
            except Exception:
                logger.debug("video source close failed", exc_info=True)
        logger.info("stream ended in %s (%s) after %.0fs, %d frames "
                    "(%d late): %s", stream.where, stream.guild_id,
                    time.monotonic() - stream.started, stream.frames,
                    stream.late, reason)
        return stream

    async def _end_voice_chat(self) -> list[str]:
        """Owner's rule: no voice chat while streaming, anywhere."""
        chat = self.voicechat
        if chat is None:
            return []
        ended = []
        for gid in list(getattr(self.voice, "_sessions", {}) or {}):
            if not chat.active(gid):
                continue
            listener = chat.listener(gid)
            if listener is not None:
                try:
                    await asyncio.wait_for(listener.say(
                        "I'm starting a stream, so I'll stop listening "
                        "for now."), 10.0)
                except Exception:
                    logger.debug("stream notice not spoken", exc_info=True)
            await chat.stop(gid)
            ended.append(gid)
            logger.info("voice chat ended in %s for a stream", gid)
        return ended

    async def _settle(self, guild_id: str, channel_id: str) -> None:
        """Wait out a fresh join before publishing (see CONFIRM_TIMEOUT)."""
        if not await self.ids.wait_confirmed(guild_id, channel_id,
                                             CONFIRM_TIMEOUT):
            logger.warning("Fluxer didn't confirm the voice connection in "
                           "%s within %.0fs; streaming anyway", guild_id,
                           CONFIRM_TIMEOUT)
        await asyncio.sleep(SETTLE)

    @staticmethod
    def _make_source(q: Quality = DEFAULT_QUALITY) -> Any:
        from livekit import rtc

        return rtc.VideoSource(q.width, q.height, is_screencast=True)

    async def _publish(self, stream: ActiveStream) -> None:
        from livekit import rtc

        source = stream.source
        track = rtc.LocalVideoTrack.create_video_track("corbin-stream",
                                                       source)
        options = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_SCREENSHARE, simulcast=False)
        options.video_encoding.max_bitrate = stream.quality.bitrate
        options.video_encoding.max_framerate = FPS
        _set_enum(options, "degradation_preference", DEGRADATION)
        stream.track = track
        try:
            stream.publication = await asyncio.wait_for(
                stream.room.local_participant.publish_track(track, options),
                PUBLISH_TIMEOUT)
        except asyncio.TimeoutError:
            await self._close_source(source)
            raise RuntimeError("the voice server never answered the video "
                               "publish")
        except Exception as exc:
            await self._close_source(source)
            raise RuntimeError(f"the voice server refused the video: "
                               f"{exc}") from exc

    def _watch_viewers(self, stream: ActiveStream) -> None:
        """Log when someone actually starts receiving the video.

        After 📺, the picture stayed grey ~10 s past the first frame
        going out (live). This splits that wait: before this line it's
        Fluxer letting the viewer subscribe; after it, the viewer's
        decoder waiting on a keyframe or late frames being dropped.
        """
        def subscribed(publication: Any = None, participant: Any = None,
                       *_: Any) -> None:
            if publication is not None and stream.publication is not None \
                    and getattr(publication, "sid", None) != \
                    getattr(stream.publication, "sid", None):
                return
            logger.info("stream in %s: %s started watching %.1fs in "
                        "(%d frames sent, %d dropped so far)", stream.where,
                        getattr(participant, "identity", "a viewer"),
                        time.monotonic() - stream.started, stream.frames,
                        stream.dropped)

        try:
            stream.room.on("local_track_subscribed", subscribed)
            stream.room_handlers.append(("local_track_subscribed",
                                         subscribed))
        except Exception:
            logger.debug("can't watch for viewers", exc_info=True)

    async def _log_stats(self, stream: ActiveStream) -> None:
        """What actually goes out, now and then. The one number that
        explains a soft or juddery stream is quality_limitation_reason:
        CPU (the one-core host) or BANDWIDTH."""
        delay = STATS_FIRST
        last: Optional[tuple[float, int]] = None
        #: (wall time, clock reading) at the last stats line: how fast the
        #: clock a clock-paced picture follows really moved. 1.00x is
        #: healthy; well under it means the sound itself is stalling and
        #: the picture is following it (live 2026-10-20: 1080p music video
        #: "plays a second, pauses a second", dropped late 0).
        clock_mark: Optional[tuple[float, float]] = None
        while self.active is stream or stream.feeder is not None and \
                not stream.feeder.done():
            await asyncio.sleep(delay)
            delay = STATS_EVERY
            layers = await outbound_layers(stream.track)
            if not layers:
                continue
            stats = max(layers, key=lambda x: int(x.get("frame_width") or 0))
            if len(layers) > 1:
                logger.warning(
                    "stream in %s is sending %d layers (simulcast): %s",
                    stream.where, len(layers), "; ".join(
                        f"rid={x.get('rid') or '-'} "
                        f"{x.get('frame_width', '?')}x"
                        f"{x.get('frame_height', '?')}@"
                        f"{x.get('frames_per_second', '?')} "
                        f"active={x.get('active', '?')}" for x in layers))
            now, sent = time.monotonic(), stats.get("bytes_sent")
            rate = ""
            if last is not None and sent is not None and now > last[0]:
                rate = f", {(sent - last[1]) * 8 / (now - last[0]) / 1e6:.2f} Mbit/s"
            if sent is not None:
                last = (now, sent)
            extra = ""
            reading = None
            if stream.clock is not None:
                try:
                    reading = stream.clock()
                except Exception:
                    reading = None
            if reading is not None:
                if clock_mark is not None and now > clock_mark[0]:
                    speed = (reading - clock_mark[1]) / (now - clock_mark[0])
                    if speed >= 0:     # a new song's clock restarts at 0
                        extra += f", clock {speed:.2f}x"
                clock_mark = (now, reading)
            else:
                clock_mark = None
            if stream.extra_stats is not None:
                try:
                    extra += stream.extra_stats() or ""
                except Exception:
                    pass
            if stream.resyncs:
                extra += f", re-synced {stream.resyncs}x"
            from .loopwatch import WATCH

            lag = WATCH.worst()
            if WATCH._task is not None:
                # The bot's own loop: high here means something in the
                # process held it (CPU, or a blocking call; the watch
                # logs which), not the stream's source.
                extra += f", loop lag {lag * 1000:.0f}ms"
            logger.info(
                "stream stats in %s: %sx%s @ %s fps%s, limit %s, encoder %s"
                ", keyframes %s, frames sent %d, dropped late %d%s",
                stream.where, stats.get("frame_width", "?"),
                stats.get("frame_height", "?"),
                stats.get("frames_per_second", "?"), rate,
                stats.get("quality_limitation_reason", "?"),
                stats.get("encoder_implementation", "?"),
                stats.get("key_frames_encoded", "?"), stream.frames,
                stream.dropped, extra)

    @staticmethod
    async def _close_source(source: Any) -> None:
        try:
            await asyncio.wait_for(source.aclose(), TEARDOWN_TIMEOUT)
        except Exception:
            pass

    async def _set_flag(self, stream: ActiveStream, on: bool) -> None:
        """``self_stream`` on our connection, checked against the echo."""
        connection_id = self.ids.get(stream.guild_id)
        if not connection_id:
            if on:
                # Without it the op 4 is a new join, not a flag; nobody
                # would ever see a tile.
                raise RuntimeError("Fluxer never told me this voice "
                                   "connection's id, so I can't mark it as "
                                   "a stream")
            return
        self.ids.expect_state(stream.guild_id)
        await self._send_voice_state(stream_flags(
            stream.guild_id, stream.channel_id, connection_id, stream=on))
        if not on:
            return
        # Wait for a state that HAS the flag. The first echo after sending
        # isn't necessarily ours: a late update from the join (or from
        # clearing the last stream) still says self_stream=false, and
        # reading that as a refusal failed the first live start
        # ("Fluxer took the flag off") while the second worked.
        deadline = time.monotonic() + FLAG_ECHO_TIMEOUT
        seen = False
        while True:
            if self.ids.state(stream.guild_id).get("self_stream") and seen:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not await self.ids.wait_for_state(stream.guild_id,
                                                 remaining):
                break
            seen = True
            if self.ids.state(stream.guild_id).get("self_stream"):
                return
            self.ids.expect_state(stream.guild_id)
        if not seen:
            logger.warning("no voice state echoed after setting "
                           "self_stream in %s; the tile may not appear",
                           stream.guild_id)
            return
        raise RuntimeError("Fluxer kept the stream flag off for "
                           f"{FLAG_ECHO_TIMEOUT:.0f}s (a channel permission "
                           "denying video/stream?)")

    async def _feed(self, stream: ActiveStream, frames: Frames) -> None:
        """Frames into the source until they run out or we're cancelled.

        Bare frames are paced by the wall clock; ``(pts, frame)`` ones by
        ``stream.clock`` (:meth:`_until`). Timestamps given to LiveKit come
        from the stream's own monotonic time, so they never run backwards
        when the frames are swapped for the next song's.
        """
        from livekit import rtc

        interval = 1.0 / FPS
        started = time.monotonic()
        shown = 0
        real = 0         # shown, title cards excluded
        run = 0          # clock-paced frames dropped in a row
        told = False     # on_behind already fired for this feed
        reason = "the video ended"
        try:
            async for item in frames:
                pts, data = item if isinstance(item, tuple) else (None, item)
                paced = pts is not None and stream.clock is not None
                if paced:
                    pts = self._reanchor(stream, pts + stream.pts_offset)
                if paced and real == 0:
                    # The first frame goes out at once, as a still, early or
                    # late: a picture beats a grey tile while the song
                    # catches up (or on_behind restarts it further ahead).
                    pass
                elif paced and not await self._until(stream, pts):
                    stream.dropped += 1
                    run += 1
                    if run >= RESYNC_AFTER and not told \
                            and stream.on_behind is not None:
                        told = True
                        behind = -self._lead(stream, pts)
                        asyncio.get_running_loop().create_task(
                            stream.on_behind(stream, behind))
                    continue
                run = 0
                stream.source.capture_frame(
                    rtc.VideoFrame(stream.quality.width,
                                   stream.quality.height,
                                   rtc.VideoBufferType.I420, data),
                    timestamp_us=int((time.monotonic() - stream.started)
                                     * 1_000_000))
                stream.frames += 1
                shown += 1
                if not isinstance(data, CardFrame):
                    real += 1
                if real == 1 and not isinstance(data, CardFrame) \
                        and stream.on_live is not None:
                    asyncio.get_running_loop().create_task(
                        stream.on_live(stream))
                if pts is not None and stream.clock is not None:
                    await asyncio.sleep(0)
                    continue
                delay = started + shown * interval - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    stream.late += 1
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"the video feed failed: {exc}"
            logger.warning("stream feed failed in %s", stream.where,
                           exc_info=True)
        finally:
            closer = getattr(frames, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        if stream.hold and reason == "the video ended":
            # Between songs: stay published, frozen on the last frame,
            # until swap() brings the next one.
            logger.info("stream in %s: %s finished; holding for the next",
                        stream.where, stream.label)
            if stream.on_frames_end is not None and self.active is stream:
                # A task: the hook may swap(), which cancels this feeder.
                asyncio.get_running_loop().create_task(
                    stream.on_frames_end(stream))
            return
        # Ran out on its own: end the stream, then say so.
        if self.active is stream:
            await self.stop(reason)
            if self.on_ended is not None:
                try:
                    await self.on_ended(stream, reason)
                except Exception:
                    logger.debug("stream-ended notice failed",
                                 exc_info=True)

    @staticmethod
    def _reanchor(stream: ActiveStream, pts: float) -> float:
        """``pts``, moved back to now if it's more than ``max_lead`` ahead
        of the clock (the offset sticks for the frames after it). See
        ``ActiveStream.max_lead``."""
        if stream.max_lead is None or stream.clock is None:
            return pts
        now = stream.clock()
        if now is None:
            return pts
        lead = pts - now
        if lead <= stream.max_lead:
            return pts
        stream.pts_offset -= lead
        stream.resyncs += 1
        t = time.monotonic()
        if t - stream.resync_logged >= 30.0:
            stream.resync_logged = t
            logger.info("stream in %s: picture %.2fs ahead of the sound; "
                        "re-synced (%d so far)", stream.where, lead,
                        stream.resyncs)
        return now

    @staticmethod
    def _lead(stream: ActiveStream, pts: float) -> float:
        """How far ``pts`` is ahead of the clock now (negative: behind;
        0 when the clock isn't running)."""
        now = stream.clock() if stream.clock is not None else None
        return 0.0 if now is None else pts - now

    async def _until(self, stream: ActiveStream, pts: float) -> bool:
        """Wait until the clock reaches ``pts``. False: too late, drop it.

        A clock that isn't running (paused, or between tracks) freezes
        the picture with it; after ``CLOCK_STALL`` the frame goes anyway.
        """
        stalled = 0.0
        while True:
            now = stream.clock()
            if now is None:
                if stalled >= CLOCK_STALL:
                    return True
                await asyncio.sleep(0.05)
                stalled += 0.05
                continue
            lead = pts - now
            if lead < -LATE_DROP:
                return False
            if lead <= 0.005:
                return True
            await asyncio.sleep(min(lead, 0.05))
