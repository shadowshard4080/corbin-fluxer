"""Watching live TV: Track T2 of ``VOICE_ROADMAP.md``.

A channel is a *source*, like the radio: putting one on replaces whatever
is playing, and stopping the stream or leaving voice ends it.

**One ffmpeg decodes the channel; its audio and video can't drift.** A
live stream can't be seeked, so two processes (music video's design)
would join the broadcast seconds apart. The one process writes:

* **audio** as raw PCM into a named pipe, which the normal player path
  plays (``client.play`` on an ``FFmpegPCMAudio`` reading the pipe). So
  the frame-loop fixes, the audio clock and "something else replaced the
  sound" all come for free: starting music or the radio ends the TV
  audio, the pipe closes, ffmpeg exits and the picture ends with it.
* **video** as 720p30 I420 on stdout, shown against the audio clock
  (``voice.audio_position``), the same sync as music video.

**Why the read-ahead buffer.** A pipe holds less than one raw 720p frame
(1.4 MB), so with nothing buffering it ffmpeg could only run a frame or
two ahead on video, and being one process, on audio too. LiveKit plays
audio out of a queue that wants to stay full, so the audio would starve
and crackle. :func:`clocked_frames` reads frames ahead into a bounded
queue, and the TV audio source gets a short LiveKit queue
(``TV_AUDIO_QUEUE_MS``) so that buffer stays small (tens of frames).

**Probe before announcing** (:func:`probe_play`): decode a few seconds
and require that it keeps up. 0f found streams that open and then
crawl (1.3 s of video in 25 s); the background health check can't see
those. A stream that fails is marked failed in the catalogue at once and
the channel's next stream is tried.

**Sound-only** (owner, 2026-10-17): a channel with no video, or a
picture that can't be shown, plays its sound; when the screen share
itself works, it shows the channel's logo bouncing round the screen,
changing colour at every edge, like the old DVD screensaver.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import iptv as I
from . import video as _video
from .video import (DEFAULT_QUALITY, FPS, FRAME_BYTES, HEIGHT, Quality,
                    card_for, with_card)

logger = logging.getLogger("corbin.tv")

KIND = "tv"
#: LiveKit's audio queue for TV sound. The player's default is 1000 ms;
#: every millisecond here is video the read-ahead buffer must hold.
TV_AUDIO_QUEUE_MS = 300
#: Frames read ahead of the clock. Self-sizing in practice (ffmpeg can
#: only run ahead as far as the audio side lets it); this is the cap.
#: 45 frames is 1.5 s, ~62 MB at 720p.
READ_AHEAD = 45
#: ...and the cap in bytes, so 1080p (3.1 MB a frame) holds 32 frames
#: (~1.07 s, 96 MiB) rather than 45 (~140 MB). Still over TV_MAX_LEAD.
READ_AHEAD_BYTES = 96 * 1024 * 1024


def read_ahead_for(q: Quality) -> int:
    """Frames of read-ahead at this size: READ_AHEAD, capped by bytes."""
    return max(15, min(READ_AHEAD, READ_AHEAD_BYTES // q.frame_bytes))
#: The probe decodes this much and must get through it in time.
PROBE_SECONDS = 4.0
PROBE_OPEN_TIMEOUT = 10.0
#: Streams of one channel tried before giving up.
MAX_TRIES = 3
#: Other feeds of the same channel (same name, another iptv-org entry)
#: tried after its own streams fail.
MAX_SIBLINGS = 3
SAMPLE_RATE, CHANNELS = 48000, 2
RAW_PCM_IN = f"-f s16le -ar {SAMPLE_RATE} -ac {CHANNELS}"
RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "5",
             "-reconnect_on_network_error", "1",
             "-reconnect_on_http_error", "4xx,5xx"]
def video_filter(q: Quality = DEFAULT_QUALITY) -> str:
    """Scale and pad to the stream size, at the stream frame rate."""
    w, h = q.width, q.height
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease"
            f":flags=fast_bilinear,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps={FPS},format=yuv420p")


VIDEO_FILTER = video_filter()
#: The bouncing logo: its box, and speed in pixels per second. The two
#: speeds differ so it doesn't trace the same line forever.
LOGO_W, LOGO_H = 320, 160
LOGO_VX, LOGO_VY = 150, 110
#: Degrees the logo's hue turns at every bounce.
LOGO_HUE_STEP = 70
TEARDOWN_TIMEOUT = 5.0
#: After the sound or picture ends: long enough for a replacing play() to
#: start and for a dead ffmpeg's queued sound (TV_AUDIO_QUEUE_MS) to run
#: out, so "replaced" and "dropped" can be told apart.
ENDED_SETTLE = 1.0
#: The safety net for picture/sound drift (``ActiveStream.max_lead``): a
#: frame further than this ahead of the TV sound is shown now and the
#: picture re-anchored, rather than waited for. Waiting is what turns
#: drift into pauses: the read-ahead fills, the one ffmpeg blocks on its
#: video pipe, and the sound runs dry. Under READ_AHEAD's 1.5 s.
TV_MAX_LEAD = 0.5
#: Pad audio across timestamp gaps the way the ``fps`` filter pads video,
#: so both outputs stay true to the stream's clock. Pluto splices ads in
#: with gaps in the timestamps: video got frames for each gap, audio got
#: nothing, and the picture crept ahead of the sound at every break (the
#: likely cause of short pauses that came more often the longer a stream
#: ran; owner, 2026-10-20).
AUDIO_FILTER = "aresample=async=1"
#: A channel that drops on air is put back on (its next working stream,
#: or the same one if it recovered) this many times per watch before he
#: gives up and says so (T4). Each try probes first, so a dead channel
#: costs a few seconds, not a black screen.
ON_AIR_RETRIES = 2
#: A watch that has played this long since it last (re)started gets the
#: retry budget back: ON_AIR_RETRIES is for a channel failing again and
#: again, not a lifetime allowance. Pluto drops at programme boundaries
#: (live, 2026-10-20), and a film night crosses several. Was 120 s: one
#: Pluto channel dropped every ~100 s and was a drop from being given up
#: on ("2/2, after 103s on air"), though each reconnect took 2.4 s. The
#: budget is only for a stream that dies straight away, again and again.
STABLE_AFTER = 60.0
#: The picture's frame count not moving for this long, while the TV is
#: on, means it's frozen however it happened: ffmpeg is killed and the
#: channel recovered. The read watchdog (FRAME_STALL_TIMEOUT) can't see
#: TV's commonest freeze: frames wait on the sound clock, so the
#: read-ahead queue sits full and nothing is reading when the source
#: stalls; the sound stops, the clock freezes, and the picture waits on
#: it forever (live, 2026-10-20: frames stuck at 1,695, nothing logged).
FROZEN_AFTER = 15.0
#: How often the freeze monitor looks, and the grace after a (re)start.
MONITOR_EVERY = 3.0
MONITOR_GRACE = 10.0


class TVError(RuntimeError):
    """Why a channel can't be put on, worded for the person who asked."""


# ----------------------------------------------------------------- ffmpeg

def header_args(s: I.Stream) -> list:
    """The same agent and referrer the health check sent: a stream that
    passed the check has to be asked for the same way to play."""
    args = ["-user_agent", s.user_agent or I.DEFAULT_USER_AGENT]
    if s.referrer:
        args += ["-headers", f"Referer: {s.referrer}\r\n"]
    return args


def av_args(url: str, s: I.Stream, *, fifo: Optional[str],
            video: bool = True, q: Quality = DEFAULT_QUALITY) -> list:
    """One ffmpeg for the channel: PCM into ``fifo`` (if given) and raw
    frames at the stream size on stdout (if ``video``). ``-vn``/``-an``
    rather than ``-map``, so ffmpeg picks the stream of each kind
    itself."""
    args = ["ffmpeg", "-loglevel", "error", "-nostdin", *RECONNECT,
            *header_args(s), "-i", url]
    if fifo:
        args += ["-vn", "-sn", "-af", AUDIO_FILTER, "-f", "s16le",
                 "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-y", fifo]
    if video:
        args += ["-an", "-sn", "-vf", video_filter(q), "-f", "rawvideo",
                 "pipe:1"]
    return args


def sound_options(url: str, s: I.Stream) -> tuple:
    """``(before_options, options)`` for an ``FFmpegPCMAudio`` playing the
    channel's sound straight from its URL (the radio's way)."""
    from .voice import STREAM_BEFORE_OPTIONS

    return (f"{STREAM_BEFORE_OPTIONS} {shlex.join(header_args(s))}", "-vn")


def logo_args(png: str, q: Quality = DEFAULT_QUALITY) -> list:
    """The bouncing logo: a still PNG overlaid on black, moving on a
    triangle wave in x and y, its hue turned at every bounce."""
    rx, ry = q.width - LOGO_W, q.height - LOGO_H
    bounces = (f"floor(t*{LOGO_VX}/{rx})+floor(t*{LOGO_VY}/{ry})")
    return ["ffmpeg", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i",
            f"color=c=black:s={q.width}x{q.height}:r={FPS}",
            "-loop", "1", "-framerate", str(FPS), "-i", png,
            "-filter_complex",
            f"[1:v]format=yuva420p,hue=h='{LOGO_HUE_STEP}*({bounces})'[l];"
            f"[0:v][l]overlay=x='{rx}-abs({rx}-mod(t*{LOGO_VX}\\,{2 * rx}))'"
            f":y='{ry}-abs({ry}-mod(t*{LOGO_VY}\\,{2 * ry}))',"
            f"format=yuv420p",
            "-f", "rawvideo", "pipe:1"]


# ------------------------------------------------------------------ probe

@dataclass
class PlayProbe:
    ok: bool
    why: str = ""
    has_video: bool = False
    has_audio: bool = False
    height: Optional[int] = None
    decoded: float = 0.0


_STREAM_LINE = re.compile(r"Stream #\d+:\d+.*?: (Video|Audio): (.*)")
_SIZE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_OUT_TIME = re.compile(r"^out_time_us=(\d+)", re.M)


def parse_probe(code: Optional[int], progress: str, stderr: str,
                seconds: float = PROBE_SECONDS) -> PlayProbe:
    """What a probe run says. Pure, so the tests feed it ffmpeg's text."""
    p = PlayProbe(ok=False)
    for kind, rest in _STREAM_LINE.findall(stderr):
        if kind == "Video":
            p.has_video = True
            m = _SIZE.search(rest)
            if m:
                p.height = max(p.height or 0, int(m.group(2)))
        else:
            p.has_audio = True
    times = _OUT_TIME.findall(progress)
    p.decoded = round(int(times[-1]) / 1e6, 2) if times else 0.0
    if not (p.has_video or p.has_audio):
        lines = [ln for ln in stderr.splitlines() if ln.strip()]
        p.why = (lines[-1][-120:] if lines else
                 "timeout" if code is None else "no audio or video")
        return p
    if p.decoded < seconds * 0.85:
        # Opened, but couldn't deliver: the crawl 0f found.
        p.why = (f"too slow: {p.decoded:.1f}s of {seconds:.0f}s"
                 if code is None or p.decoded else
                 (stderr.strip().splitlines() or ["ended at once"])[-1][-120:])
        return p
    p.ok = True
    return p


async def probe_play(url: str, s: I.Stream,
                     seconds: float = PROBE_SECONDS) -> PlayProbe:
    """Decode ``seconds`` of the channel. ok only if it got through them
    in time, so a stream that opens and then crawls is caught here."""
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
            "-nostats", "-progress", "pipe:1", "-stats_period", "0.5",
            *RECONNECT, *header_args(s), "-i", url, "-t", str(seconds),
            "-f", "null", "-"]
    try:
        code, out, err = await I.run_process(
            args, PROBE_OPEN_TIMEOUT + seconds * 2)
    except FileNotFoundError:
        return PlayProbe(False, "ffmpeg not installed")
    return parse_probe(code, out, err, seconds)


async def pick_input(s: I.Stream, max_height: int = HEIGHT) -> str:
    """The URL to give ffmpeg. For an HLS master whose audio is muxed into
    each variant, the variant at the stream size (<=720p by default): the
    one-core host then never decodes 1080p to throw it away. A master
    with separate audio renditions stays the master, or the variant would
    play silent."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=PROBE_OPEN_TIMEOUT,
                                     follow_redirects=True,
                                     headers=I._headers(s)) as client:
            resp = await client.get(s.url)
            if resp.status_code >= 400:
                return s.url
            text = resp.text[:256_000]
            final = str(resp.url)
    except Exception:
        return s.url
    if "#EXT-X-STREAM-INF" not in text:
        return s.url
    if re.search(r"#EXT-X-MEDIA:[^\n]*TYPE=AUDIO[^\n]*URI=", text):
        return s.url
    variant = I.pick_variant(I.parse_master(text, final), max_height)
    return variant["url"] if variant else s.url


# ----------------------------------------------------------------- frames

async def _spawn(args: list, **kw: Any) -> Any:
    return await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)


async def _reap(proc: Any, label: str) -> str:
    """Kill and reap, bounded; returns what ffmpeg said on stderr."""
    if proc is None:
        return ""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        _out, err = await asyncio.wait_for(proc.communicate(),
                                           TEARDOWN_TIMEOUT)
        return (err or b"").decode(errors="replace").strip()[-300:]
    except Exception:
        logger.warning("%s ffmpeg didn't exit", label)
        return ""


async def clocked_frames(proc: Any, *, clocked: bool,
                         read_ahead: int = READ_AHEAD,
                         on_end: Optional[Callable[[int, str], Any]] = None,
                         frame_bytes: int = FRAME_BYTES,
                         fps: float = FPS
                         ) -> AsyncIterator[Any]:
    """Frames from ``proc``'s stdout, read ahead into a bounded queue.

    ``clocked``: yield ``(pts, frame)`` for the stream manager to show
    against the audio clock; otherwise bare frames, paced by the wall.
    ``on_end(frames, ffmpeg's stderr)`` when ffmpeg stops on its own, or
    stalls (no frame for ``FRAME_STALL_TIMEOUT``; it's killed, so a hung
    stream ends and recovers instead of freezing for good).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=read_ahead)
    done = object()
    stalled: list = []

    async def reader() -> None:
        first = True
        try:
            while True:
                limit = (_video.FIRST_FRAME_TIMEOUT if first
                         else _video.FRAME_STALL_TIMEOUT)
                try:
                    data = await asyncio.wait_for(
                        proc.stdout.readexactly(frame_bytes), limit)
                except asyncio.TimeoutError:
                    stalled.append(limit)
                    logger.warning("tv ffmpeg stalled: no frame for %.0fs; "
                                   "killing it so the channel can recover",
                                   limit)
                    return
                first = False
                await queue.put(data)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            await queue.put(done)

    task = asyncio.ensure_future(reader())
    n = 0
    ended = False
    try:
        while True:
            data = await queue.get()
            if data is done:
                ended = True
                return
            yield (n / fps, data) if clocked else data
            n += 1
    finally:
        task.cancel()
        err = await _reap(proc, "tv")
        # Wait for the cancelled reader, or asyncio later reports "Task
        # was destroyed but it is pending!" (live, 2026-10-20).
        try:
            await asyncio.wait_for(asyncio.gather(task,
                                                  return_exceptions=True),
                                   TEARDOWN_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        if stalled:
            err = (f"stalled (no frame for {stalled[0]:.0f}s)"
                   + (f"; {err}" if err else ""))
        if ended and on_end is not None:
            try:
                on_end(n, err)
            except Exception:
                pass


#: Logos bigger than this aren't downloaded.
LOGO_MAX_BYTES = 5_000_000
#: The uploaded "Now watching" logo's longest side.
CARD_LOGO_PX = 160


async def load_logo(channel: I.Channel) -> Any:
    """The channel's logo as an RGBA Pillow image, or None (no logo, it
    won't download, or Pillow can't read it: SVG logos exist)."""
    if not channel.logo:
        return None
    from PIL import Image

    try:
        import io

        import httpx

        async with httpx.AsyncClient(timeout=8.0,
                                     follow_redirects=True) as client:
            resp = await client.get(channel.logo)
        if resp.status_code >= 400 or len(resp.content) > LOGO_MAX_BYTES:
            return None
        return await asyncio.to_thread(
            lambda: Image.open(io.BytesIO(resp.content)).convert("RGBA"))
    except Exception:
        logger.debug("logo for %s didn't load", channel.id, exc_info=True)
        return None


async def fetch_logo_png(channel: I.Channel,
                         size: int = CARD_LOGO_PX) -> Optional[bytes]:
    """The logo as a small PNG to upload with "Now watching" (T4), or
    None, in which case the card links it as before."""
    art = await load_logo(channel)
    if art is None:
        return None

    def encode() -> bytes:
        import io

        art.thumbnail((size, size))
        out = io.BytesIO()
        art.save(out, format="PNG")
        return out.getvalue()

    try:
        return await asyncio.to_thread(encode)
    except Exception:
        return None


async def make_logo(channel: I.Channel, folder: str) -> str:
    """A PNG for the bouncing logo: the channel's own logo fitted into
    the box, or its name in a bright colour when it has none (or it won't
    download). The hue filter needs colour to turn, so text is never
    white."""
    from PIL import Image, ImageDraw, ImageFont

    path = os.path.join(folder, "logo.png")
    box = Image.new("RGBA", (LOGO_W, LOGO_H), (0, 0, 0, 0))
    art = await load_logo(channel)
    if art is not None:
        art.thumbnail((LOGO_W, LOGO_H))
        box.paste(art, ((LOGO_W - art.width) // 2,
                        (LOGO_H - art.height) // 2), art)
    else:
        draw = ImageDraw.Draw(box)
        text = channel.name if len(channel.name) <= 18 else "CORBIN TV"
        size = 56
        while size > 18:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:           # Pillow < 10.1: one fixed size
                font = ImageFont.load_default()
                break
            if draw.textlength(text, font=font) <= LOGO_W - 16:
                break
            size -= 4
        draw.text((LOGO_W // 2, LOGO_H // 2), text, font=font,
                  fill=(235, 45, 45, 255), anchor="mm")
    await asyncio.to_thread(box.save, path)
    return path


# -------------------------------------------------------------- watching

@dataclass
class Watching:
    guild_id: str
    voice_channel_id: str
    channel: I.Channel
    stream: I.Stream
    #: "picture" (sound and video), "silent" (video, no sound),
    #: "logo" (sound, bouncing logo), "sound" (sound, no picture at all)
    mode: str
    #: Where to say it dropped: the voice channel's own chat.
    notify_to: Optional[str] = None
    #: The pipe or URL the TV sound plays from. If the session is
    #: playing something else, the TV was replaced, not broken.
    audio_url: str = ""
    folder: Optional[str] = None
    started: float = field(default_factory=time.monotonic)
    note: str = ""
    ended: bool = False
    #: The TV sound's "finished" callback has fired: it's no longer what
    #: the session plays. (The player doesn't update ``source_url``, so
    #: that can't tell "replaced by a song" from "still ours".)
    sound_done: bool = False
    #: The size the picture goes out at (the server's choice, T4).
    quality: Quality = DEFAULT_QUALITY
    #: Monotonic time the server's watch limit runs out (T3), or None.
    #: Kept across an on-air retry: a drop doesn't reset the clock.
    deadline: Optional[float] = None
    #: On-air retries spent so far this watch (ON_AIR_RETRIES).
    retries: int = 0
    #: The limit task, cancelled when the TV goes off.
    limit_task: Optional[asyncio.Task] = None
    #: A dead stream ends the picture AND the sound, and both used to run
    #: the recovery: two probes, two swaps, the channel put on twice
    #: (live, 2026-10-20). The first to arrive sets this; the other leaves.
    recovering: bool = False
    #: The ffmpeg making the picture (and sound), for the freeze monitor
    #: to kill; and that monitor's task.
    proc: Any = None
    monitor_task: Optional[asyncio.Task] = None


Notify = Callable[[str, str], Awaitable[Any]]


class TV:
    """Puts a channel on, and takes it off again.

    ``streams`` is the :class:`~corbin.core.video.StreamManager`,
    ``voice`` the :class:`~corbin.core.voice.VoiceManager`. ``player``
    and ``music_video`` (optional) are quietened when a channel goes on.
    ``rules`` (optional, :class:`~corbin.core.tv_rules.TVRules`) gives
    each server's quality and watch limit, and remembers the last
    channel for "the usual". ``probe``, ``spawn`` and ``audio_source``
    are seams for the tests.
    """

    def __init__(self, catalogue: Any, streams: Any, voice: Any, *,
                 player: Any = None, music_video: Any = None,
                 notify: Optional[Notify] = None, rules: Any = None,
                 probe: Callable[..., Awaitable[PlayProbe]] = probe_play,
                 pick: Callable[..., Awaitable[str]] = pick_input,
                 spawn: Callable[..., Awaitable[Any]] = _spawn,
                 audio_source: Optional[Callable[..., Any]] = None,
                 logo: Callable[[I.Channel, str], Awaitable[str]] = make_logo,
                 card: Callable[..., Awaitable[Optional[bytes]]] = card_for
                 ) -> None:
        self.catalogue = catalogue
        self.streams = streams
        self.voice = voice
        self.player = player
        self.music_video = music_video
        self.notify = notify
        self.rules = rules
        self._probe, self._pick, self._spawn = probe, pick, spawn
        self._audio_source = audio_source or _ffmpeg_pcm_audio
        self._logo = logo
        self._card = card
        self._watching: dict[str, Watching] = {}
        self._lock = asyncio.Lock()

    def watching(self, guild_id: Optional[str]) -> Optional[Watching]:
        return self._watching.get(str(guild_id)) if guild_id else None

    def _quality(self, guild_id: str) -> Quality:
        if self.rules is None:
            return DEFAULT_QUALITY
        try:
            return self.rules.quality(guild_id)
        except Exception:
            logger.warning("tv quality unreadable for %s", guild_id,
                           exc_info=True)
            return DEFAULT_QUALITY

    def _limit(self, guild_id: str) -> Optional[float]:
        if self.rules is None:
            return None
        try:
            return self.rules.max_seconds(guild_id)
        except Exception:
            logger.warning("tv limit unreadable for %s", guild_id,
                           exc_info=True)
            return None

    # -- putting a channel on ---------------------------------------------

    def siblings(self, channel: I.Channel) -> list:
        """Other catalogue channels with the same name: iptv-org lists
        one "Pluto TV Westerns" per country feed, each with its own
        streams. Same country first, then the most streams. Live,
        2026-10-20: one feed's only stream stalled, the retry found
        nothing left on that entry and gave up, while another
        "Pluto TV Westerns" played fine when asked again by hand."""
        key = I.normalise(channel.name)
        cat = getattr(self.catalogue, "catalogue", None)
        chans = getattr(cat, "channels", {}) or {}
        playable = getattr(self.catalogue, "playable", lambda c: True)
        found = [c for c in chans.values()
                 if c.id != channel.id and I.normalise(c.name) == key
                 and c.streams and playable(c)]
        found.sort(key=lambda c: (c.country != channel.country,
                                  -len(c.streams), c.id))
        return found

    async def watch(self, guild_id: str, voice_channel_id: str,
                    channel: I.Channel, *,
                    notify_to: Optional[str] = None,
                    deadline: Optional[float] = None,
                    retries: int = 0,
                    prefer: Optional[str] = None) -> Watching:
        """Put ``channel`` on: its own streams first, then, if none of
        those plays, the same channel's other feeds (:meth:`siblings`,
        up to ``MAX_SIBLINGS``). Raises :class:`TVError` from the
        channel asked for when nothing plays.

        ``prefer``: a stream URL to try first even if it's marked
        failed: the on-air retry reconnecting to what just dropped."""
        try:
            return await self._watch_one(guild_id, voice_channel_id,
                                         channel, notify_to=notify_to,
                                         deadline=deadline, retries=retries,
                                         prefer=prefer)
        except TVError as first:
            for sib in self.siblings(channel)[:MAX_SIBLINGS]:
                try:
                    w = await self._watch_one(
                        guild_id, voice_channel_id, sib,
                        notify_to=notify_to, deadline=deadline,
                        retries=retries)
                except TVError:
                    continue
                logger.info("tv: %s played from its %s feed (%s)",
                            channel.name, sib.country or "other", sib.id)
                return w
            raise first

    async def _watch_one(self, guild_id: str, voice_channel_id: str,
                         channel: I.Channel, *,
                         notify_to: Optional[str] = None,
                         deadline: Optional[float] = None,
                         retries: int = 0,
                         prefer: Optional[str] = None) -> Watching:
        """Probe one channel's streams in order and put the first one that
        plays on. Raises :class:`TVError` saying why not.

        ``deadline`` and ``retries`` carry an on-air retry's state over;
        a fresh "put on X" starts the server's watch limit anew."""
        guild_id = str(guild_id)
        now = self.catalogue.clock()
        first = [s for s in channel.streams if prefer and s.url == prefer]
        rest = [s for s in channel.streams if s not in first]
        healthy = [s for s in rest if self.catalogue.health.status(
            s.url, now) != I.FAILED]
        # Marked-failed streams are tried last, never skipped: someone
        # asked for this channel by name, and a mark is a guess about
        # the past (live, 2026-10-20: "the usual" refused without trying,
        # minutes after the same channel had been playing). The probe
        # decides; the marks only order the attempts and the listings.
        marked = [s for s in rest if s not in healthy]
        candidates = (first + healthy + marked)[:MAX_TRIES]
        if not candidates:
            raise TVError(f"{channel.name} has no streams to try.")
        q = self._quality(guild_id)
        if deadline is None:
            limit = self._limit(guild_id)
            deadline = time.monotonic() + limit if limit else None
        reasons = []
        for s in candidates:
            url = await self._pick(s, q.height)
            probe = await self._probe(url, s)
            if not probe.ok:
                self.catalogue.mark(s.url, False, f"play probe: {probe.why}")
                reasons.append(probe.why)
                logger.info("tv: %s stream %s failed its probe: %s",
                            channel.name, s.url, probe.why)
                continue
            self.catalogue.mark(s.url, True)
            size = q
            if q.height > 720 and probe.height and probe.height <= 720:
                # A 720p (or smaller) channel isn't upscaled to 1080p: the
                # same picture, at 2.25x the encode work and bandwidth.
                from .video import QUALITIES

                size = QUALITIES["720p"]
            async with self._lock:
                # Switching channels keeps the published picture (and so
                # every viewer's tile) when it can: the new channel is
                # swapped in underneath.
                await self._stop_locked(guild_id, "switching channels",
                                        keep_picture=True)
                try:
                    w = await self._start(guild_id, str(voice_channel_id),
                                          channel, s, url, probe, notify_to,
                                          size)
                except BaseException:
                    await self._drop_picture(guild_id, "the channel didn't "
                                                       "start")
                    raise
                w.deadline, w.retries = deadline, retries
                self._watching[guild_id] = w
                self._arm_limit(w)
                self._arm_monitor(w)
            if self.rules is not None:
                try:
                    self.rules.note_watched(guild_id, channel.id)
                except Exception:
                    logger.debug("couldn't note the last channel",
                                 exc_info=True)
            logger.info("tv: watching %s in %s (%s, %s, %s)", channel.name,
                        guild_id, w.mode, q.name, s.url)
            return w
        logger.info("tv: %s didn't play: %s", channel.name,
                    "; ".join(reasons))
        raise TVError(f"{channel.name} isn't working right now "
                      f"({len(reasons)} stream{'s' if len(reasons) > 1 else ''}"
                      f" tried).")

    async def _start(self, guild_id: str, vc: str, channel: I.Channel,
                     s: I.Stream, url: str, probe: PlayProbe,
                     notify_to: Optional[str],
                     q: Quality = DEFAULT_QUALITY) -> Watching:
        await self._quiet_music(guild_id)
        folder = tempfile.mkdtemp(prefix="corbin-tv-")
        w = Watching(guild_id=guild_id, voice_channel_id=vc, channel=channel,
                     stream=s, mode="picture", notify_to=notify_to,
                     folder=folder, quality=q)
        label = f"live TV: {channel.name}"
        try:
            if probe.has_video:
                await self._start_video(w, url, probe.has_audio, label)
            else:
                await self._start_logo(w, url, label)
        except Exception:
            self._cleanup(w)
            raise
        return w

    def _can_swap(self, w: Watching) -> bool:
        """Is our own picture still up in this voice channel, at this
        size? Then a new channel goes in underneath it."""
        active = self.streams.active
        return (active is not None and active.kind == KIND
                and active.guild_id == w.guild_id
                and str(active.channel_id) == str(w.voice_channel_id)
                and getattr(active, "quality", DEFAULT_QUALITY) == w.quality)

    async def _present(self, w: Watching, frames: Any, label: str, *,
                       clock: Optional[Callable[[], Optional[float]]]
                       ) -> None:
        """Show ``frames``: swapped into the TV picture already up here,
        or a fresh stream."""
        ended = lambda st: self._frames_ended(w)  # noqa: E731
        lead = TV_MAX_LEAD if clock is not None else None
        stats = lambda: self._sound_stats(w)  # noqa: E731
        if self._can_swap(w) and await self.streams.swap(
                w.guild_id, frames, label=label, clock=clock,
                on_frames_end=ended, max_lead=lead, extra_stats=stats):
            return
        await self.streams.start(
            w.guild_id, w.voice_channel_id, frames, label=label, kind=KIND,
            clock=clock, hold=True, on_frames_end=ended, quality=w.quality,
            max_lead=lead, extra_stats=stats)

    def _sound_stats(self, w: Watching) -> str:
        """", sound ran dry Nx" for the stream stats line: the gaps you
        hear. 0 is shown too, so "no gaps" is visible, not assumed."""
        from .voice import sound_stats

        session = self.voice.get(w.guild_id)
        return sound_stats(session.client) if session is not None else ""

    async def _start_video(self, w: Watching, url: str, sound: bool,
                           label: str) -> None:
        fifo = None
        if sound:
            fifo = os.path.join(w.folder, "audio.pcm")
            os.mkfifo(fifo)
        # Drawn before ffmpeg starts: it's shown while ffmpeg opens the
        # channel, the seconds the tile used to sit grey (V3).
        card = await self._card(w.channel.name, "Tuning in…", w.quality)
        proc = await self._spawn(av_args(url, w.stream, fifo=fifo,
                                         q=w.quality))
        w.proc = proc
        frames = with_card(clocked_frames(
            proc, clocked=sound, frame_bytes=w.quality.frame_bytes,
            read_ahead=read_ahead_for(w.quality),
            on_end=lambda n, err: logger.info(
                "tv ffmpeg for %s ended after %d frames: %s",
                w.channel.name, n, err or "no error")), card)
        client_ref: dict = {}

        def clock() -> Optional[float]:
            from .voice import audio_position

            client = client_ref.get("client")
            return audio_position(client) if client is not None else None

        try:
            await self._present(w, frames, label,
                                clock=clock if sound else None)
        except Exception as exc:
            # Kill it here: a frame generator that never started doesn't
            # run its cleanup on aclose(), and this ffmpeg may be blocked
            # opening the pipe forever.
            await frames.aclose()
            await _reap(proc, "tv")
            if not sound:
                raise TVError(f"I couldn't show {w.channel.name}: "
                              f"{_reason(exc)}") from exc
            # No picture possible: the sound alone, the radio's way.
            await self._play_sound(w, url)
            w.mode = "sound"
            w.note = (f"I couldn't show the picture ({_reason(exc)}), so "
                      f"it's sound only.")
            return
        if sound:
            # After the stream is up: ffmpeg blocks opening the pipe until
            # this reader exists, so the picture waits for the sound and
            # both start from the same moment.
            session = await self._play(w, fifo, before=RAW_PCM_IN,
                                       options="", queue_ms=TV_AUDIO_QUEUE_MS)
            client_ref["client"] = session.client
        else:
            w.mode = "silent"
            w.note = "This channel has no sound right now."

    async def _start_logo(self, w: Watching, url: str, label: str) -> None:
        """Sound only, with the bouncing logo for a picture."""
        await self._play_sound(w, url)
        w.mode = "logo"
        proc = None
        try:
            png = await self._logo(w.channel, w.folder)
            proc = await self._spawn(logo_args(png, w.quality))
            w.proc = proc
            frames = clocked_frames(proc, clocked=False,
                                    frame_bytes=w.quality.frame_bytes)
            await self._present(w, frames, label, clock=None)
            w.note = "This channel is sound only, so here's its logo."
        except Exception as exc:
            await _reap(proc, "tv logo")
            await self._drop_picture(w.guild_id, "no logo picture")
            w.mode = "sound"
            w.note = (f"It's sound only, and I couldn't show a picture "
                      f"({_reason(exc)}).")

    async def _play_sound(self, w: Watching, url: str) -> None:
        before, options = sound_options(url, w.stream)
        await self._play(w, url, before=before, options=options)

    async def _play(self, w: Watching, path: str, *, before: str,
                    options: str, queue_ms: Optional[int] = None) -> Any:
        session = await self.voice.connect(w.guild_id, w.voice_channel_id)
        source = self._audio_source(path, before_options=before,
                                    options=options)
        if queue_ms:
            # Read by voice.py's frame loop (see TV_AUDIO_QUEUE_MS).
            source.corbin_queue_ms = queue_ms
        loop = asyncio.get_running_loop()

        def finished(_error: Any = None) -> None:
            # Called when the sound ends or another play() replaces it.
            w.sound_done = True
            loop.create_task(self._sound_ended(w))

        await session.client.play(source, after=finished)
        session.source_url = path
        session.label = f"📺 {w.channel.name}"
        session.cancel_watcher()
        w.audio_url = path
        closers = getattr(session, "closers", None)
        if closers is not None:
            closers.append(lambda: self._closed(w))
        return session

    async def _quiet_music(self, guild_id: str) -> None:
        """A channel replaces the music: clear the queue first, so the
        player doesn't take the TV sound ending as "next song"."""
        mv = self.music_video
        if mv is not None and mv.active(guild_id):
            await mv.stop(guild_id, "live TV")
        pm = self.player
        if pm is None:
            return
        try:
            pm.cancel_import(guild_id)
        except Exception:
            pass
        p = pm.get(guild_id) if hasattr(pm, "get") else None
        if p is not None:
            p.clear()
            p.current = None
            p.loop_track = p.loop_queue = False
            # A stale "track finished" callback must not advance the queue.
            p.generation += 1

    # -- the watch limit (T3) ---------------------------------------------

    def _arm_monitor(self, w: Watching) -> None:
        """Watch the picture's frame count (see FROZEN_AFTER). Only when
        there's a picture of ours to watch."""
        if w.mode not in ("picture", "silent", "logo"):
            return
        w.monitor_task = asyncio.get_running_loop().create_task(
            self._monitor(w))

    async def _monitor(self, w: Watching) -> None:
        try:
            await asyncio.sleep(MONITOR_GRACE)
            last, since = -1, time.monotonic()
            while not w.ended and not w.recovering:
                active = self.streams.active
                if active is None or active.kind != KIND \
                        or active.guild_id != w.guild_id:
                    return                 # not ours any more
                frames = getattr(active, "frames", 0)
                now = time.monotonic()
                if frames != last:
                    last, since = frames, now
                elif now - since >= FROZEN_AFTER:
                    logger.warning("tv: %s's picture frozen for %.0fs at %d "
                                   "frames; restarting the channel",
                                   w.channel.name, now - since, frames)
                    proc = w.proc
                    if proc is not None and \
                            getattr(proc, "returncode", 0) is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    # Straight to the recovery: with the frames stuck
                    # behind a frozen clock, killing ffmpeg alone may
                    # never surface as an ending. _ended's recovering
                    # flag stops the sound's own ending doubling it.
                    asyncio.get_running_loop().create_task(self._ended(w))
                    return
                await asyncio.sleep(MONITOR_EVERY)
        except asyncio.CancelledError:
            return

    def _arm_limit(self, w: Watching) -> None:
        if w.deadline is None:
            return
        w.limit_task = asyncio.get_running_loop().create_task(
            self._limit_reached(w))

    async def _limit_reached(self, w: Watching) -> None:
        try:
            await asyncio.sleep(max(0.0, w.deadline - time.monotonic()))
        except asyncio.CancelledError:
            return
        if w.ended or self._watching.get(w.guild_id) is not w:
            return
        await self.stop(w.guild_id, "the server's watch limit")
        minutes = 0
        if self.rules is not None:
            try:
                minutes = self.rules.max_minutes(w.guild_id)
            except Exception:
                pass
        await self._tell(w, f"📺 That's the server's TV limit"
                            f"{f' of {_duration(minutes)}' if minutes else ''}"
                            f", so {w.channel.name} is off. Say “put on "
                            f"{w.channel.name}” to watch more.")

    # -- taking it off ----------------------------------------------------

    async def stop(self, guild_id: str, reason: str = "stopped") -> bool:
        """Turn the TV off here: picture and sound. Stays in voice."""
        async with self._lock:
            return await self._stop_locked(str(guild_id), reason)

    async def _stop_locked(self, guild_id: str, reason: str, *,
                           keep_picture: bool = False) -> bool:
        """``keep_picture``: leave the TV's published stream up, frozen,
        for a new channel to be swapped into (``watch`` switching)."""
        w = self._watching.pop(guild_id, None)
        if w is None:
            return False
        w.ended = True
        if w.monitor_task is not None and \
                w.monitor_task is not asyncio.current_task():
            w.monitor_task.cancel()
        if w.limit_task is not None and \
                w.limit_task is not asyncio.current_task():
            w.limit_task.cancel()
        if not keep_picture:
            await self._drop_picture(guild_id, reason)
        if w.audio_url and not w.sound_done and \
                self.voice.get(guild_id) is not None:
            # Only while the sound is still ours: never stop a song that
            # replaced it.
            await self.voice.stop(guild_id)
        self._cleanup(w)
        logger.info("tv: %s off in %s after %.0fs: %s", w.channel.name,
                    guild_id, time.monotonic() - w.started, reason)
        return True

    async def _drop_picture(self, guild_id: str, reason: str) -> None:
        """End the TV's stream here, if it's the one up."""
        active = self.streams.active
        if active is not None and active.kind == KIND \
                and active.guild_id == guild_id \
                and guild_id not in self._watching:
            await self.streams.stop(reason)

    async def _closed(self, w: Watching) -> None:
        """Voice left by any route: forget the TV, quietly."""
        if self._watching.get(w.guild_id) is w:
            await self.stop(w.guild_id, "left voice")

    async def _sound_ended(self, w: Watching) -> None:
        await self._ended(w)

    async def _frames_ended(self, w: Watching) -> None:
        await self._ended(w)

    async def _ended(self, w: Watching) -> None:
        """The sound or the picture stopped on its own. Either something
        else replaced the sound (a song, the radio: ffmpeg loses its pipe
        and the picture ends too) or the channel dropped."""
        # Let a replacing play() land, and a dying ffmpeg's sound drain.
        await asyncio.sleep(ENDED_SETTLE)
        if w.ended or w.recovering or self._watching.get(w.guild_id) \
                is not w:
            return
        w.recovering = True
        session = self.voice.get(w.guild_id)
        replaced = (w.sound_done and session is not None
                    and session.playing)
        if replaced:
            await self.stop(w.guild_id, "replaced by other audio")
            return
        # It died on air. Not marked failed yet: a drop isn't a dead
        # stream. Pluto's links hand out a fresh session on every connect,
        # and its streams drop at programme boundaries (live, 2026-10-20:
        # marking it failed first left the retry nothing to try, so it
        # gave up on a channel that reconnected fine). The reconnect's
        # probe marks it either way.
        played = time.monotonic() - w.started
        spent = 0 if played >= STABLE_AFTER else w.retries
        if spent < ON_AIR_RETRIES and \
                (w.deadline is None or w.deadline > time.monotonic()):
            # T4: put it back on (the same stream first, reconnected) before
            # giving up. The picture stays up, frozen, and is swapped back in.
            try:
                await self.watch(w.guild_id, w.voice_channel_id, w.channel,
                                 notify_to=w.notify_to, deadline=w.deadline,
                                 retries=spent + 1, prefer=w.stream.url)
                logger.info("tv: %s back on after dropping (%d/%d, after "
                            "%.0fs on air)", w.channel.name, spent + 1,
                            ON_AIR_RETRIES, played)
                return
            except TVError as exc:
                logger.info("tv: %s couldn't come back: %s",
                            w.channel.name, exc)
            except Exception:
                logger.warning("tv: retry of %s failed", w.channel.name,
                               exc_info=True)
        # Given up: now hide it from the list (roadmap T2), not at the
        # next health check.
        self.catalogue.mark(w.stream.url, False, "dropped on air")
        if self._watching.get(w.guild_id) is w:
            await self.stop(w.guild_id, "the channel's stream dropped")
        else:
            # A failed retry already took this watch down.
            await self._drop_picture(w.guild_id, "the channel dropped")
        await self._tell(w, f"📺 {w.channel.name} dropped out. It may "
                            f"be down; say “put on {w.channel.name}” to "
                            f"try again.")

    async def _tell(self, w: Watching, text: str) -> None:
        if self.notify is None or not w.notify_to:
            return
        try:
            await self.notify(w.notify_to, text)
        except Exception:
            logger.debug("tv notice failed", exc_info=True)

    @staticmethod
    def _cleanup(w: Watching) -> None:
        if w.folder:
            shutil.rmtree(w.folder, ignore_errors=True)
            w.folder = None


def _duration(minutes: int) -> str:
    """"90 minutes", "2 hours", "1 hour 30 minutes"."""
    hours, mins = divmod(int(minutes), 60)
    if not hours:
        return f"{mins} minute{'s' if mins != 1 else ''}"
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    if mins:
        text += f" {mins} minute{'s' if mins != 1 else ''}"
    return text


def _reason(exc: BaseException) -> str:
    text = str(exc).strip().rstrip(".")
    return text[0].lower() + text[1:] if text else type(exc).__name__


def _ffmpeg_pcm_audio(path: str, **kw: Any) -> Any:
    from fluxer.voice import FFmpegPCMAudio

    return FFmpegPCMAudio(path, **kw)
