"""Music video: the current track's YouTube video, streamed with the song.

Opt-in twice over: someone turns it on (📺 on the Now Playing card, or
"stream the video"), and each listener still chooses to watch by clicking
his stream tile. Audio keeps playing exactly as before either way.

How it fits together (VOICE_ROADMAP.md, V1/V2):

* **The audio path is untouched.** The player's ffmpeg keeps feeding the
  music track. Video is a second ffmpeg reading YouTube's video-only
  stream (720p is video-only on YouTube anyway), scaled and padded to
  1280x720 at 30 fps.
* **Synced to the audio clock, not the wall clock.** Each frame carries
  its position in the track, and ``StreamManager`` shows it when
  ``voice.audio_position`` (samples sent to LiveKit, less what is still
  queued) reaches it. Late frames are dropped; a paused song freezes the
  picture. Turning video on mid-song starts the video at the song's
  current position, with no audio restart.
* **One publication across songs.** The next track's video is swapped in
  underneath (``StreamManager.swap``); republishing would drop every
  viewer back to the grey "click to watch" tile each song.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import video as _video
from .video import (DEFAULT_QUALITY, FPS, Quality, StreamBusy, card_for,
                    with_card)
from .voice import audio_position
from ..identity import NAME

logger = logging.getLogger("corbin.music_video")

KIND = "music"
#: A video that dies this long before the song ends is restarted where
#: the song is; closer than this, the song is ending anyway.
RESTART_IF_MORE_THAN = 5.0
#: Restarts per song before giving up on its video.
MAX_RESTARTS = 3
#: How long a looked-up video link is reused. YouTube's last hours; this
#: is well inside that. The next song's is looked up while the current
#: one plays, so a song change only waits for ffmpeg to start.
LINK_TTL = 30 * 60
#: Margin on top of ffmpeg's measured start-up time. The video starts this
#: far past where the song will be once ffmpeg is ready: early frames
#: wait, late ones are dropped. With a fixed 0.5 s, ffmpeg's real start-up
#: on this host (seeking a YouTube stream) put the video ~7 s behind and
#: every frame for 19 s was dropped (live, 2026-10-17).
START_LEAD = 1.0
#: First guess at ffmpeg's start-up before anything has been measured.
STARTUP_GUESS = 4.0
#: Never plan for more than this; a freak slow start shouldn't make every
#: later video start half a minute in.
STARTUP_CAP = 12.0
#: Times a video may be restarted further ahead for falling behind.
MAX_RESYNCS = 3
#: Reconnect flags for a long HTTP read; the audio path's probe limits are
#: left out on purpose, a video stream needs real probing.
_RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
              "-reconnect_delay_max", "5",
              "-reconnect_on_network_error", "1",
              "-reconnect_on_http_error", "4xx,5xx"]


def _timer(q: Quality, start: Optional[float],
           total: Optional[float]) -> str:
    """The on-screen "elapsed / total" (``video.timer_filter``), from
    where in the song the video starts."""
    from .video import timer_filter

    return timer_filter(q, start=start or 0.0, total=total)


class NoVideo(RuntimeError):
    """The track has no video to show (or it couldn't be fetched)."""


def is_video_page(url: str) -> bool:
    """Tracks whose page can have a video. Spotify resolves to YouTube."""
    low = (url or "").lower()
    return "youtube.com/" in low or "youtu.be/" in low


def stream_fps(source_fps: float) -> float:
    """The source's own rate when it's 20-30 fps (Thriller: 24), else 30:
    padding 24 to 30 repeats every fourth frame, 25% more to encode."""
    return source_fps if 20.0 <= (source_fps or 0) <= 30.0 else float(FPS)


def _lower_priority(pid: int) -> None:
    """Nice the video decoder once it's running: under CPU pressure the
    picture drops frames, not the song's own audio, which a separate
    ffmpeg at normal priority feeds. A gap in the sound was what people
    noticed (live 2026-10-20: "clock 0.71x, sound ran dry 13x" at
    1080p). Set from outside after the spawn, not with ``preexec_fn``:
    Python code between fork and exec can deadlock a process full of
    threads, and LiveKit's are."""
    try:
        import os

        os.setpriority(os.PRIO_PROCESS, pid, 10)
    except Exception:
        logger.debug("couldn't lower the video decoder's priority",
                     exc_info=True)


def video_args(url: str, headers: dict, start: Optional[float],
               q: Quality = DEFAULT_QUALITY,
               total: Optional[float] = None,
               fps: float = FPS) -> list[str]:
    """The ffmpeg command for raw I420 frames at the stream size, 30 fps,
    from a video URL, with the on-screen timer (``total``: the song's
    length; without it, just the time)."""
    args = ["ffmpeg", "-loglevel", "error", "-nostdin", *_RECONNECT]
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n"
                                     for k, v in headers.items())]
    if start and start > 0:
        # Before -i: a fast seek, then exact (ffmpeg decodes from the
        # keyframe to the position), so the first frame is at ``start``.
        args += ["-ss", f"{start:.3f}"]
    timer = _timer(q, start, total)
    args += ["-i", url, "-an", "-sn",
             "-vf", (f"scale={q.width}:{q.height}"
                     ":force_original_aspect_ratio=decrease"
                     ":flags=fast_bilinear,"
                     f"pad={q.width}:{q.height}:(ow-iw)/2:(oh-ih)/2:black,"
                     f"fps={fps:.6g},"
                     + (timer + "," if timer else "")
                     + "format=yuv420p"),
             "-f", "rawvideo", "pipe:1"]
    return args


async def video_frames(url: str, headers: dict, start: Optional[float],
                       on_ready: Optional[Callable[[float], Any]] = None,
                       q: Quality = DEFAULT_QUALITY,
                       total: Optional[float] = None,
                       fps: float = FPS
                       ) -> AsyncIterator[tuple]:
    """``(pts, frame)`` from ffmpeg until the video ends. pts is the
    frame's position in the track, which the audio clock is compared to.
    ``on_ready(seconds)`` is told how long ffmpeg took to the first frame."""
    import time

    from .relay import RELAY

    # Range-fetched, like the song's audio (corbin/core/relay.py).
    url = RELAY.wrap(url, headers)
    size = q.frame_bytes
    spawned = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *video_args(url, headers, start, q, total, fps),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if getattr(proc, "pid", None):
        _lower_priority(proc.pid)
    n = 0
    base = start or 0.0
    ended = False
    stalled = None
    try:
        while True:
            # Timed only while waiting on ffmpeg: a paused song stops
            # asking for frames, it doesn't leave a read hanging.
            limit = (_video.FIRST_FRAME_TIMEOUT if n == 0
                     else _video.FRAME_STALL_TIMEOUT)
            try:
                data = await asyncio.wait_for(proc.stdout.readexactly(size),
                                              limit)
            except asyncio.IncompleteReadError:
                ended = True
                break
            except asyncio.TimeoutError:
                # A hung ffmpeg never ends on its own, and the restart in
                # MusicVideo._frames_ended only runs when it does.
                stalled = limit
                ended = True
                logger.warning("music video ffmpeg stalled: no frame for "
                               "%.0fs at %.1fs; killing it so the video can "
                               "restart", limit, base + n / fps)
                break
            if n == 0 and on_ready is not None:
                try:
                    on_ready(time.monotonic() - spawned)
                except Exception:
                    pass
            yield base + n / fps, data
            n += 1
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), 5.0)
        except Exception:
            err = b""
        if ended:
            # ffmpeg stopped on its own. Logged with its own words: a
            # dropped connection mid-song looked exactly like the video
            # finishing, and the picture froze with no trace why.
            logger.info("music video ffmpeg ended at %.1fs after %d "
                        "frames (exit %s)%s%s", base + n / fps, n,
                        proc.returncode,
                        f" [stalled {stalled:.0f}s]" if stalled else "",
                        (": " + err.decode(errors="replace").strip()[-300:])
                        if err else "")


class MusicVideo:
    """Turns the music video on and off, and follows the queue."""

    def __init__(self, streams: Any, player: Any, voice: Any, extractor: Any,
                 *, notify: Optional[Callable[[str, str],
                                              Awaitable[Any]]] = None,
                 quality: Optional[Callable[[str], Quality]] = None,
                 card: Callable[..., Awaitable[Optional[bytes]]] = card_for
                 ) -> None:
        self.streams = streams
        self.player = player
        self.voice = voice
        self.extractor = extractor
        #: (channel_id, text): tell the voice channel's chat something.
        self.notify = notify
        #: guild -> (track id, restarts so far) for the current song.
        self._restarts: dict[str, tuple[int, int]] = {}
        #: page url -> (monotonic time, (video url, headers))
        self._links: dict[str, tuple[float, tuple[str, dict]]] = {}
        #: guild -> "starting" (looking up / opening the video) or "on"
        #: (frames are going out). Shown in the Now Playing footer.
        self._state: dict[str, str] = {}
        #: ``hook(guild_id)`` when that changes: bot.py re-renders the card.
        self.on_status: Optional[Callable[[str], Awaitable[Any]]] = None
        #: guild -> when "starting" began, for the "video on in" log.
        self._asked: dict[str, float] = {}
        #: How long ffmpeg takes to its first frame here, measured.
        self._startup = STARTUP_GUESS
        #: guild -> (track id, resyncs so far)
        self._resyncs: dict[str, tuple[int, int]] = {}
        #: guild -> the server's stream size (T4); 720p without it.
        self.quality = quality
        self._card = card

    def _size(self, guild_id: str) -> Quality:
        """The running stream's size (swapped frames must match it), else
        the server's choice."""
        stream = self.streams.active
        if stream is not None and stream.guild_id == str(guild_id):
            return getattr(stream, "quality", DEFAULT_QUALITY)
        if self.quality is None:
            return DEFAULT_QUALITY
        try:
            return self.quality(str(guild_id))
        except Exception:
            logger.warning("stream quality unreadable for %s", guild_id,
                           exc_info=True)
            return DEFAULT_QUALITY

    async def _carded(self, frames: AsyncIterator, title: str,
                      subtitle: str, q: Quality) -> AsyncIterator:
        """``frames`` behind a title card (V3), shown while ffmpeg opens
        and seeks the video: 4-7 s of grey tile otherwise."""
        return with_card(frames, await self._card(title, subtitle, q))

    def _measured(self, seconds: float) -> None:
        old = self._startup
        self._startup = min(STARTUP_CAP, 0.5 * old + 0.5 * seconds)
        logger.info("music video ffmpeg ready in %.1fs (planning for %.1fs "
                    "now)", seconds, self._startup)

    def _ahead(self) -> float:
        """How far past the song's position a new video should start."""
        return self._startup + START_LEAD

    async def _behind(self, stream: Any, behind: float) -> None:
        """Frames kept arriving too late: restart further ahead."""
        guild_id = stream.guild_id
        if not self.active(guild_id):
            return
        player = self.player.get(guild_id)
        track = getattr(player, "current", None)
        if track is None:
            return
        key, count = self._resyncs.get(guild_id, (0, 0))
        count = count + 1 if key == id(track) else 1
        self._resyncs[guild_id] = (id(track), count)
        if count > MAX_RESYNCS:
            return  # it'll catch up by dropping; don't thrash
        self._startup = min(STARTUP_CAP, self._startup + max(behind, 1.0))
        logger.warning("music video %.1fs behind the song; restarting it "
                       "%.1fs ahead (%d/%d)", behind, self._ahead(), count,
                       MAX_RESYNCS)
        try:
            frames = await self._frames_for(guild_id, track)
        except NoVideo:
            return
        await self.streams.swap(guild_id, frames)

    def _sound_stats(self, guild_id: str) -> str:
        """", sound ran dry Nx" for the stats line: gaps in the song's
        own audio, which the picture follows."""
        from .voice import sound_stats

        session = self.voice.get(guild_id)
        return sound_stats(session.client) if session is not None else ""

    def status_line(self, guild_id: Optional[str]) -> str:
        """The card's video line, or "" when the video's off."""
        state = self._state.get(str(guild_id))
        if state == "starting":
            return "📺 Starting the video…"
        if state == "on" and self.active(guild_id):
            return f"📺 Video on: click {NAME}'s stream tile to watch"
        return ""

    async def _set_state(self, guild_id: str, state: Optional[str]) -> None:
        import time

        gid = str(guild_id)
        if state == "starting" and self._state.get(gid) != "starting":
            self._asked[gid] = time.monotonic()
        elif state == "on" and gid in self._asked:
            logger.info("music video on in %.1fs (from asking to the first "
                        "frame sent)", time.monotonic() - self._asked.pop(gid))
        if state is None:
            changed = self._state.pop(gid, None) is not None
        else:
            changed = self._state.get(gid) != state
            self._state[gid] = state
        if changed and self.on_status is not None:
            try:
                await self.on_status(gid)
            except Exception:
                logger.debug("video status refresh failed", exc_info=True)

    async def _went_live(self, stream: Any) -> None:
        if stream.kind == KIND:
            await self._set_state(stream.guild_id, "on")

    async def _link(self, page_url: str, *, fresh: bool = False
                    ) -> Optional[tuple[str, dict]]:
        import time

        cached = self._links.get(page_url)
        if cached and not fresh and time.monotonic() - cached[0] < LINK_TTL:
            return cached[1]
        began = time.monotonic()
        found = await self.extractor.video_stream(page_url)
        # The first live start "took a long time to populate"; this and
        # "video on in" below say where the time went.
        logger.info("music video lookup took %.1fs for %s",
                    time.monotonic() - began, page_url)
        if found is not None:
            self._links[page_url] = (time.monotonic(), found)
            for stale in [k for k, (t, _) in self._links.items()
                          if time.monotonic() - t >= LINK_TTL]:
                self._links.pop(stale, None)
        return found

    @staticmethod
    def _own_link(track: Any) -> Optional[tuple[str, dict]]:
        """The video stream picked from the track's own extraction, if
        it's recent enough to still be good. No lookup at all."""
        import time

        url = getattr(track, "video_url", "")
        made = getattr(track, "extracted_at", 0.0) or 0.0
        if url and time.monotonic() - made < LINK_TTL:
            return url, dict(getattr(track, "video_headers", None) or {})
        return None

    async def _prefetch_next(self, guild_id: str) -> None:
        """Look up the next queued song's video now, while this one
        plays, so it can start the moment its audio does."""
        player = self.player.get(guild_id)
        queue = getattr(player, "queue", None) or []
        if not queue or not self.active(guild_id):
            return
        nxt = queue[0]
        page = getattr(nxt, "webpage_url", "")
        if not is_video_page(page) or self._own_link(nxt) is not None:
            return
        try:
            await self._link(page)
        except Exception:
            logger.debug("prefetch of the next video failed", exc_info=True)

    def active(self, guild_id: Optional[str]) -> bool:
        stream = self.streams.active
        return (stream is not None and stream.kind == KIND
                and stream.guild_id == str(guild_id))

    def _clock(self, guild_id: str) -> Callable[[], Optional[float]]:
        def clock() -> Optional[float]:
            session = self.voice.get(guild_id)
            if session is None:
                return None
            return audio_position(session.client)
        return clock

    async def _frames_for(self, guild_id: str, track: Any, *,
                          fresh: bool = False,
                          since: Optional[float] = None) -> AsyncIterator:
        """The track's video, starting where its audio will be.

        ``since``: the monotonic time this track's audio started. Given
        for a track change, where the clock can't be trusted yet: the
        hook fires the moment the new audio starts, and the old song's
        clock may still be there, which would start the video minutes
        in and freeze it waiting for the song to get there.
        """
        if not is_video_page(getattr(track, "webpage_url", "")):
            raise NoVideo("this track has no video")
        found = None if fresh else self._own_link(track)
        try:
            if found is None:
                found = await self._link(track.webpage_url, fresh=fresh)
        except Exception as exc:
            logger.warning("music video extraction failed for %r: %s",
                           track.title, exc)
            raise NoVideo("I couldn't get the video for this one") from exc
        if found is None:
            raise NoVideo("this track has no video")
        url, headers = found
        # Measured after extraction (it takes seconds), so the video
        # starts where the song is now, not where it was.
        start = None
        if not getattr(track, "is_live", False):
            import time

            if since is not None:
                now = time.monotonic() - since
            else:
                now = self._clock(guild_id)() or 0.0
            start = now + self._ahead()
        # The track's length for the on-screen timer; none for a live one.
        total = None if getattr(track, "is_live", False) else (
            float(getattr(track, "duration", 0) or 0) or None)
        return video_frames(url, headers, start, on_ready=self._measured,
                            q=self._size(guild_id), total=total,
                            fps=stream_fps(getattr(track, "video_fps", 0)
                                           or 0))

    async def start(self, guild_id: str) -> tuple[Any, list[str]]:
        """Turn the video on for what's playing. Raises NoVideo,
        StreamBusy, PermissionError or RuntimeError with the reason."""
        player = self.player.get(guild_id)
        session = self.voice.get(guild_id)
        if player is None or player.current is None or session is None:
            raise NoVideo("nothing's playing")
        track = player.current
        if self.active(guild_id):
            raise RuntimeError("the video's already on")
        busy = self.streams.active
        if busy is not None and busy.guild_id != str(guild_id):
            # Checked before the extraction, so the refusal is instant.
            raise StreamBusy(busy)
        await self._set_state(guild_id, "starting")
        try:
            q = self._size(guild_id)
            frames = await self._carded(
                await self._frames_for(guild_id, track),
                track.title, "Starting the video…", q)
            result = await self.streams.start(
                guild_id, session.channel_id, frames,
                label=f"music video: {track.title}", kind=KIND,
                clock=self._clock(guild_id), hold=True,
                on_frames_end=lambda s, g=str(guild_id): self._frames_ended(g),
                on_live=self._went_live, on_behind=self._behind, quality=q,
                extra_stats=lambda g=str(guild_id): self._sound_stats(g))
        except BaseException:
            await self._set_state(guild_id, None)
            raise
        asyncio.get_running_loop().create_task(self._prefetch_next(guild_id))
        return result

    async def _frames_ended(self, guild_id: str) -> None:
        """The video stopped. Between songs that's expected; mid-song it
        means its connection died, so restart it where the song is."""
        if not self.active(guild_id):
            return
        player = self.player.get(guild_id)
        track = getattr(player, "current", None)
        now = self._clock(guild_id)()
        if track is None or now is None:
            return  # between tracks: track_started will swap the next in
        duration = getattr(track, "duration", None) or 0
        if duration and now >= duration - RESTART_IF_MORE_THAN:
            return  # the song is ending anyway
        key, count = self._restarts.get(guild_id, (0, 0))
        count = count + 1 if key == id(track) else 1
        self._restarts[guild_id] = (id(track), count)
        channel = self.streams.active.channel_id
        if count > MAX_RESTARTS:
            await self.stop(guild_id, "the video kept dropping")
            await self._tell(channel, "📺 The video kept dropping out for "
                                      "this song, so it's off. The music "
                                      "carries on.")
            return
        logger.warning("music video for %r stopped at %.0fs while the song "
                       "plays on; restarting it there (%d/%d)", track.title,
                       now, count, MAX_RESTARTS)
        await self._set_state(guild_id, "starting")
        try:
            # A fresh link: the old one may be what died.
            frames = await self._frames_for(guild_id, track, fresh=True)
        except NoVideo as exc:
            await self.stop(guild_id, f"video lost: {exc}")
            await self._tell(channel, f"📺 Video off: {exc}.")
            return
        await self.streams.swap(guild_id, frames)

    async def stop(self, guild_id: str, reason: str = "video off") -> bool:
        if not self.active(guild_id):
            await self._set_state(guild_id, None)
            return False
        await self.streams.stop(reason)
        await self._set_state(guild_id, None)
        return True

    async def track_started(self, guild_id: str, track: Any) -> None:
        """The player started a track: swap its video in, or end the
        video when it has none."""
        if not self.active(guild_id):
            return
        import time

        began = time.monotonic()
        await self._set_state(guild_id, "starting")
        try:
            frames = await self._frames_for(guild_id, track, since=began)
        except NoVideo as exc:
            channel = self.streams.active.channel_id
            await self.stop(guild_id, f"no video: {track.title}")
            await self._tell(channel, f"📺 Video off: {exc}.")
            return
        # The next song's title while its video opens, instead of the
        # last song's final frame frozen on screen.
        frames = await self._carded(frames, track.title, "Up next",
                                    self._size(guild_id))
        if not await self.streams.swap(guild_id, frames,
                                       label=f"music video: {track.title}"):
            return
        asyncio.get_running_loop().create_task(self._prefetch_next(guild_id))

    async def queue_ended(self, guild_id: str) -> None:
        """The music ran out: there's nothing left to show."""
        if self.active(guild_id):
            channel = self.streams.active.channel_id
            await self.stop(guild_id, "the music ended")
            await self._tell(channel, "📺 The music's over, so the video's "
                                      "off too.")

    async def _tell(self, channel_id: str, text: str) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(channel_id, text)
        except Exception:
            logger.debug("music video notice failed", exc_info=True)
