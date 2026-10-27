"""Playing videos from the local library into voice (owner, 2026-10-20).

Built from live TV's pipeline (``corbin.core.tv``), which live testing
hardened: **one ffmpeg** writes the sound (PCM into a named pipe the
player plays) and the picture (raw frames shown against the sound's
clock), so they can't drift; the sound is padded across timestamp gaps;
frames are read ahead with the stall watchdog; a title card covers the
start. What a file adds over a live channel:

* **Seeking and resuming.** ffmpeg starts at a position (``-ss`` before
  ``-i``: fast). Where you are is the start position plus the sound's
  clock; progress is saved per server and show every 30 s and on stop,
  so "continue Bob's Burgers" picks up there.
* **Pause.** The player's pause stops the sound, the clock stops with it,
  the picture waits, and ffmpeg blocks on its pipes: everything holds
  where it is, no restart. The freeze monitor knows a pause isn't a
  freeze.
* **Audio and subtitle tracks** (``ffprobe``). English audio when there
  is one; otherwise the default track with English subtitles burned in.
  "In Japanese with subtitles" overrides, and a change mid-episode
  restarts at the same spot. Text subtitles (ASS/SRT) go through libass;
  picture subtitles (PGS/DVD) are overlaid.
* **The end of an episode** is the sound finishing within a minute of
  the end: the next one starts if autoplay is on. Ending earlier is a
  failure, and it restarts where it was (up to MAX_RESTARTS).
* **No upscaling**: a 720p file streams at 720p on a 1080p server.

Stream kind ``video``. Starting a song, the radio or live TV replaces
it (the sound is replaced, and that's noticed as with TV).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import library as L
from . import video as _video
from .tv import (AUDIO_FILTER, RAW_PCM_IN, TV_AUDIO_QUEUE_MS, TV_MAX_LEAD,
                 _ffmpeg_pcm_audio, _reap, _spawn, clocked_frames,
                 read_ahead_for)
from .video import DEFAULT_QUALITY, FPS, QUALITIES, Quality, card_for, \
    with_card

logger = logging.getLogger("corbin.video_player")

KIND = "video"
SAMPLE_RATE, CHANNELS = 48000, 2
#: Within this of the end, the sound finishing means the episode ended.
END_SLACK = 60.0
#: Restarts after an early end, per episode, before giving up.
MAX_RESTARTS = 3
#: Progress is saved this often while playing.
SAVE_EVERY = 30.0
#: The freeze monitor (as TV's): no new frame for this long, not paused,
#: restarts at the current position.
FROZEN_AFTER = 20.0
MONITOR_EVERY = 3.0
MONITOR_GRACE = 15.0
ENDED_SETTLE = 1.0
PROBE_TIMEOUT = 20.0
#: Preferred audio/subtitle language, as ISO 639-2 codes ffprobe reports.
LANG_CODES = {"english": ("eng", "en"), "japanese": ("jpn", "ja"),
              "spanish": ("spa", "es"), "french": ("fre", "fra", "fr"),
              "german": ("ger", "deu", "de"), "italian": ("ita", "it"),
              "portuguese": ("por", "pt"), "korean": ("kor", "ko"),
              "chinese": ("chi", "zho", "zh")}
TEXT_SUBS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
              "dvb_subtitle"}


class VideoError(RuntimeError):
    """Why a video can't play, worded for the person who asked."""


# ------------------------------------------------------------------ probe

@dataclass
class Track:
    index: int          # position among streams of its type (0:a:N / 0:s:N)
    language: str = ""
    title: str = ""
    codec: str = ""
    default: bool = False

    @property
    def name(self) -> str:
        lang = next((k.title() for k, v in LANG_CODES.items()
                     if self.language in v), self.language or "")
        return " ".join(x for x in (lang, f"({self.title})" if self.title
                                    else "") if x) or f"track {self.index + 1}"


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    #: The video's frame rate (r_frame_rate), 0 if unknown.
    fps: float = 0.0
    audio: list = field(default_factory=list)
    subs: list = field(default_factory=list)


def parse_probe(data: dict) -> MediaInfo:
    """ffprobe's ``-show_streams -show_format`` JSON. Pure, for tests."""
    info = MediaInfo()
    try:
        info.duration = float((data.get("format") or {}).get("duration")
                              or 0)
    except (TypeError, ValueError):
        info.duration = 0.0
    a = s = 0
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        tags = st.get("tags") or {}
        default = bool((st.get("disposition") or {}).get("default"))
        if kind == "video" and not info.width and \
                not (st.get("disposition") or {}).get("attached_pic"):
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            try:
                num, _, den = str(st.get("r_frame_rate") or "0/1") \
                    .partition("/")
                info.fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                info.fps = 0.0
        elif kind == "audio":
            info.audio.append(Track(a, str(tags.get("language") or "").lower(),
                                    str(tags.get("title") or ""),
                                    str(st.get("codec_name") or ""), default))
            a += 1
        elif kind == "subtitle":
            info.subs.append(Track(s, str(tags.get("language") or "").lower(),
                                   str(tags.get("title") or ""),
                                   str(st.get("codec_name") or ""), default))
            s += 1
    return info


async def probe(path: str) -> MediaInfo:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", path, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise VideoError("I couldn't read that file in time")
    # The output decides, not the exit code (see iptv.ffprobe_stream: a
    # reaped child can be reported as exit 255).
    try:
        data = json.loads(out or b"{}")
    except json.JSONDecodeError:
        data = {}
    if not data.get("streams"):
        raise VideoError("I couldn't read that file: "
                         + ((err or b"").decode(errors="replace")[-160:]
                            .strip() or f"exit {proc.returncode}"))
    return parse_probe(data)


def _lang_matches(track: Track, language: Optional[str]) -> bool:
    if not language:
        return False
    codes = LANG_CODES.get(language.lower(), (language.lower()[:3],))
    return track.language in codes or language.lower() in \
        track.title.lower()


def choose_tracks(info: MediaInfo, *, language: Optional[str] = None,
                  subtitles: Optional[bool] = None) -> tuple:
    """``(audio Track or None, subtitle Track or None)``.

    Audio: the language asked for, else English, else the default track,
    else the first. Subtitles: asked for or not; unasked, on (English,
    else default, else first) when the audio isn't English, so a Japanese
    track is never played to people without subtitles."""
    audio = None
    if info.audio:
        audio = (next((t for t in info.audio if _lang_matches(t, language)),
                      None)
                 or (None if language else next(
                     (t for t in info.audio if _lang_matches(t, "english")),
                     None))
                 or next((t for t in info.audio if t.default), None)
                 or info.audio[0])
    want_subs = subtitles
    if want_subs is None:
        want_subs = audio is not None and audio.language not in ("", "eng",
                                                                  "en") \
            and bool(info.subs)
    sub = None
    if want_subs and info.subs:
        usable = [t for t in info.subs
                  if t.codec in TEXT_SUBS or t.codec in IMAGE_SUBS]
        english = [t for t in usable if _lang_matches(t, "english")]
        # Prefer full subtitles over "signs & songs" tracks.
        full = [t for t in english if not any(
            w in t.title.lower() for w in ("sign", "song", "forced"))]
        sub = (full or english or [t for t in usable if t.default]
               or usable or [None])[0]
    return audio, sub


# ----------------------------------------------------------------- ffmpeg

def out_fps(info: MediaInfo) -> float:
    """The frame rate to stream at: the file's own when it's 20-30 fps
    (anime and films are 23.976), else 30. At a fixed 30 a 23.976 file
    had every fourth frame repeated: 25% more encoding for nothing."""
    return info.fps if 20.0 <= info.fps <= 30.0 else float(FPS)


def _escape_filter_path(path: str) -> str:
    """A path inside a filtergraph option: ``\\``, ``:``, ``'``, ``,``,
    ``[``, ``]`` escaped as libavfilter wants."""
    out = path.replace("\\", "\\\\\\\\").replace("'", "\\\\\\'")
    for ch in ":,[]":
        out = out.replace(ch, "\\" + ch)
    return out


def av_args(path: str, *, start: float, fifo: Optional[str], q: Quality,
            audio: Optional[Track], sub: Optional[Track],
            sub_path: Optional[str] = None, fps: float = FPS,
            total: Optional[float] = None) -> list:
    """One ffmpeg for a file: sound into ``fifo``, frames on stdout.

    Subtitles burned in: text ones through ``subtitles`` (libass) with the
    timestamps put back to the file's own for the seek (``-ss`` before
    ``-i`` restarts them at 0, which would show the first line's
    subtitles at the seek point), picture ones overlaid.

    ``sub_path``: the same file under a plain name (a symlink in the
    run's temp folder) for the ``subtitles`` filter. Its path sits inside
    a filtergraph, where an apostrophe, colon or bracket needs two layers
    of escaping, and anime file names are full of them: "Bob's Burgers"
    broke the first version."""
    w, h = q.width, q.height
    scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease"
             f":flags=bicubic,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
             f"fps={fps:.6g},")
    timer = _video.timer_filter(q, start=start, total=total)
    scale += (timer + "," if timer else "") + "format=yuv420p"
    args = ["ffmpeg", "-loglevel", "error", "-nostdin"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", path]
    if fifo and audio is not None:
        args += ["-map", f"0:a:{audio.index}", "-vn", "-sn",
                 "-af", AUDIO_FILTER, "-f", "s16le", "-ar", str(SAMPLE_RATE),
                 "-ac", str(CHANNELS), "-y", fifo]
    if sub is not None and sub.codec in IMAGE_SUBS:
        args += ["-filter_complex",
                 f"[0:v:0][0:s:{sub.index}]overlay=(W-w)/2:H-h,{scale}[v]",
                 "-map", "[v]"]
    else:
        vf = scale
        if sub is not None:
            shift = f"setpts=PTS+{start:.3f}/TB," if start > 0 else ""
            back = ",setpts=PTS-STARTPTS" if start > 0 else ""
            vf = (f"{shift}subtitles="
                  f"{_escape_filter_path(sub_path or path)}"
                  f":si={sub.index}{back},{scale}")
        args += ["-map", "0:v:0", "-vf", vf]
    args += ["-an", "-sn", "-f", "rawvideo", "pipe:1"]
    return args


# --------------------------------------------------------------- playing

@dataclass
class Playing:
    guild_id: str
    voice_channel_id: str
    show: L.Show
    episode: L.Episode
    path: str
    info: MediaInfo
    audio: Optional[Track]
    sub: Optional[Track]
    quality: Quality
    #: Where in the episode this run of ffmpeg started.
    start: float = 0.0
    notify_to: Optional[str] = None
    folder: Optional[str] = None
    proc: Any = None
    client: Any = None
    sound_done: bool = False
    ended: bool = False
    paused: bool = False
    paused_at: float = 0.0
    #: What the person asked for, kept for restarts.
    language: Optional[str] = None
    subtitles: Optional[bool] = None
    restarts: int = 0
    tasks: list = field(default_factory=list)
    recovering: bool = False

    def position(self) -> float:
        """Seconds into the episode, from the sound's clock."""
        from .voice import audio_position

        heard = audio_position(self.client) if self.client is not None \
            else None
        return self.start + (heard or 0.0)


Notify = Callable[[str, str], Awaitable[Any]]


class VideoPlayer:
    """Plays one episode per server, through ``streams`` (the
    StreamManager) and ``voice`` (the VoiceManager). ``settings`` holds
    progress and autoplay; ``rules`` the stream quality."""

    PROGRESS = "video_progress"
    AUTOPLAY = "video_autoplay"

    def __init__(self, root: str | os.PathLike, streams: Any, voice: Any, *,
                 settings: Any = None, rules: Any = None,
                 player: Any = None, tv: Any = None,
                 notify: Optional[Notify] = None,
                 probe_fn: Callable[[str], Awaitable[MediaInfo]] = probe,
                 spawn: Callable[..., Awaitable[Any]] = _spawn,
                 audio_source: Optional[Callable[..., Any]] = None,
                 card: Callable[..., Awaitable[Optional[bytes]]] = card_for
                 ) -> None:
        self.root = Path(root)
        self.streams, self.voice = streams, voice
        self.settings, self.rules = settings, rules
        self.player, self.tv = player, tv
        self.notify = notify
        self._probe, self._spawn, self._card = probe_fn, spawn, card
        self._audio_source = audio_source or _ffmpeg_pcm_audio
        self._playing: dict[str, Playing] = {}
        self._lock = asyncio.Lock()
        #: ``hook(guild_id)`` when what's playing changes (the card).
        self.on_change: Optional[Callable[[str], Awaitable[Any]]] = None

    # -- the library ------------------------------------------------------

    def shows(self) -> list:
        return L.scan(self.root)

    def playing(self, guild_id: Optional[str]) -> Optional[Playing]:
        return self._playing.get(str(guild_id)) if guild_id else None

    # -- progress and autoplay --------------------------------------------

    def _store(self) -> Any:
        return getattr(self.settings, "store", None)

    def progress(self, guild_id: str, show: str) -> Optional[dict]:
        store = self._store()
        if store is None:
            return None
        entry = (store.get(str(guild_id), self.PROGRESS, {}) or {}).get(show)
        return entry if isinstance(entry, dict) else None

    def _save(self, p: Playing, position: Optional[float] = None,
              finished: bool = False) -> None:
        store = self._store()
        if store is None:
            return
        allp = dict(store.get(p.guild_id, self.PROGRESS, {}) or {})
        allp[p.show.name] = {"episode": p.episode.rel,
                             "position": 0.0 if finished else round(
                                 position if position is not None
                                 else p.position(), 1),
                             "finished": bool(finished),
                             "at": int(time.time())}
        store.set(p.guild_id, self.PROGRESS, allp)

    def autoplay(self, guild_id: str) -> bool:
        store = self._store()
        if store is None:
            return True
        return store.get(str(guild_id), self.AUTOPLAY, True) is not False

    def set_autoplay(self, guild_id: str, on: bool) -> None:
        store = self._store()
        if store is not None:
            store.set(str(guild_id), self.AUTOPLAY, bool(on))

    def resume_point(self, guild_id: str, show: L.Show) -> tuple:
        """``(episode, position)`` to continue a show from: where it was
        left, the next episode if that one was finished, else the first."""
        prog = self.progress(guild_id, show.name)
        if prog:
            found = L.find_episode([show], prog.get("episode", ""))
            if found:
                ep = found[1]
                if prog.get("finished"):
                    nxt = L.neighbour(show, ep.rel)
                    if nxt is not None:
                        return nxt, 0.0
                    return ep, 0.0
                return ep, float(prog.get("position") or 0.0)
        first = L.pick(show)
        return first, 0.0

    # -- playing ----------------------------------------------------------

    def _quality(self, guild_id: str, info: MediaInfo) -> Quality:
        q = DEFAULT_QUALITY
        if self.rules is not None:
            try:
                q = self.rules.quality(guild_id)
            except Exception:
                q = DEFAULT_QUALITY
        if q.height > 720 and info.height and info.height <= 720:
            q = QUALITIES["720p"]      # no upscaling
        return q

    async def play(self, guild_id: str, voice_channel_id: str,
                   show: L.Show, episode: L.Episode, *, start: float = 0.0,
                   notify_to: Optional[str] = None,
                   language: Optional[str] = None,
                   subtitles: Optional[bool] = None,
                   restarts: int = 0) -> Playing:
        """Put ``episode`` on at ``start`` seconds. Raises VideoError."""
        guild_id = str(guild_id)
        try:
            path = str(L.resolve(self.root, episode.rel))
        except ValueError as exc:
            raise VideoError(str(exc)) from exc
        if not os.path.isfile(path):
            raise VideoError(f"{episode.title} isn't in the video folder "
                             f"any more.")
        info = await self._probe(path)
        if info.duration and start >= info.duration - 5:
            start = 0.0
        audio, sub = choose_tracks(info, language=language,
                                   subtitles=subtitles)
        q = self._quality(guild_id, info)
        async with self._lock:
            old = self._playing.get(guild_id)
            if old is not None:
                await self._stop_locked(guild_id, "next episode",
                                        keep_picture=True, save=old.episode
                                        .rel != episode.rel)
            await self._quiet_others(guild_id)
            p = Playing(guild_id=guild_id, voice_channel_id=str(
                voice_channel_id), show=show, episode=episode, path=path,
                info=info, audio=audio, sub=sub, quality=q, start=start,
                notify_to=notify_to, language=language,
                subtitles=subtitles, restarts=restarts,
                folder=tempfile.mkdtemp(prefix="corbin-video-"))
            try:
                await self._start(p)
            except BaseException:
                self._cleanup(p)
                await self._drop_picture(guild_id, "the video didn't start")
                raise
            self._playing[guild_id] = p
            p.tasks.append(asyncio.get_running_loop().create_task(
                self._saver(p)))
            p.tasks.append(asyncio.get_running_loop().create_task(
                self._monitor(p)))
        self._save(p, start)
        logger.info("video: %s %s (%s) in %s at %.0fs, %s, audio %s, "
                    "subs %s", show.name, episode.label, episode.title,
                    guild_id, start, q.name,
                    audio.name if audio else "none",
                    sub.name if sub else "off")
        await self._changed(guild_id)
        return p

    async def _quiet_others(self, guild_id: str) -> None:
        """A video replaces live TV and the music, like TV replaces the
        music."""
        tv = self.tv
        if tv is not None and tv.watching(guild_id) is not None:
            await tv.stop(guild_id, "a video went on")
        pm = self.player
        if pm is None:
            return
        try:
            pm.cancel_import(guild_id)
        except Exception:
            pass
        pl = pm.get(guild_id) if hasattr(pm, "get") else None
        if pl is not None:
            pl.clear()
            pl.current = None
            pl.loop_track = pl.loop_queue = False
            pl.generation += 1

    async def _start(self, p: Playing) -> None:
        fifo = None
        if p.audio is not None:
            fifo = os.path.join(p.folder, "audio.pcm")
            os.mkfifo(fifo)
        title = p.show.name if p.episode.film else \
            f"{p.show.name}: {p.episode.label}"
        sub = "Resuming…" if p.start > 1 else (p.episode.nice_title
                                                or p.episode.label)
        sub_path = None
        if p.sub is not None and p.sub.codec in TEXT_SUBS:
            sub_path = os.path.join(p.folder, "input" + Path(p.path).suffix)
            os.symlink(p.path, sub_path)
        card = await self._card(title, sub, p.quality)
        rate = out_fps(p.info)
        proc = await self._spawn(av_args(p.path, start=p.start, fifo=fifo,
                                         q=p.quality, audio=p.audio,
                                         sub=p.sub, sub_path=sub_path,
                                         fps=rate,
                                         total=p.info.duration or None))
        p.proc = proc
        frames = with_card(clocked_frames(
            proc, clocked=fifo is not None,
            frame_bytes=p.quality.frame_bytes,
            read_ahead=read_ahead_for(p.quality), fps=rate,
            on_end=lambda n, err: logger.info(
                "video ffmpeg for %s %s ended after %d frames: %s",
                p.show.name, p.episode.label, n, err or "no error")), card)
        client_ref: dict = {}

        def clock() -> Optional[float]:
            from .voice import audio_position

            client = client_ref.get("client")
            return audio_position(client) if client is not None else None

        label = f"video: {title}"
        ended = lambda st: self._frames_ended(p)  # noqa: E731
        use_clock = clock if fifo else None
        lead = TV_MAX_LEAD if fifo else None
        try:
            active = self.streams.active
            if active is not None and active.kind == KIND \
                    and active.guild_id == p.guild_id \
                    and str(active.channel_id) == p.voice_channel_id \
                    and getattr(active, "quality", None) == p.quality \
                    and await self.streams.swap(
                        p.guild_id, frames, label=label, clock=use_clock,
                        on_frames_end=ended, max_lead=lead):
                pass
            else:
                await self.streams.start(
                    p.guild_id, p.voice_channel_id, frames, label=label,
                    kind=KIND, clock=use_clock, hold=True,
                    on_frames_end=ended, quality=p.quality, max_lead=lead)
        except Exception as exc:
            await frames.aclose()
            await _reap(proc, "video")
            raise VideoError(f"I couldn't start the stream: {exc}") from exc
        if fifo:
            session = await self.voice.connect(p.guild_id,
                                               p.voice_channel_id)
            source = self._audio_source(fifo, before_options=RAW_PCM_IN,
                                        options="")
            source.corbin_queue_ms = TV_AUDIO_QUEUE_MS
            loop = asyncio.get_running_loop()

            def finished(_err: Any = None) -> None:
                p.sound_done = True
                loop.create_task(self._ended(p))

            await session.client.play(source, after=finished)
            session.source_url = fifo
            session.label = f"🎬 {title}"
            session.cancel_watcher()
            client_ref["client"] = session.client
            p.client = session.client
            closers = getattr(session, "closers", None)
            if closers is not None:
                closers.append(lambda: self._closed(p))

    # -- controls ---------------------------------------------------------

    def pause(self, guild_id: str) -> Optional[bool]:
        """Toggle pause. The new state, or None if nothing's playing."""
        p = self.playing(guild_id)
        if p is None or p.client is None:
            return None
        if p.paused:
            p.client.resume()
            p.paused = False
        else:
            p.client.pause()
            p.paused, p.paused_at = True, time.monotonic()
        return p.paused

    async def seek(self, guild_id: str, position: float) -> Playing:
        p = self.playing(guild_id)
        if p is None:
            raise VideoError("Nothing's playing.")
        top = (p.info.duration or position + 1) - 2
        position = max(0.0, min(position, top))
        return await self.play(p.guild_id, p.voice_channel_id, p.show,
                               p.episode, start=position,
                               notify_to=p.notify_to, language=p.language,
                               subtitles=p.subtitles)

    async def change_tracks(self, guild_id: str, *,
                            language: Optional[str] = None,
                            subtitles: Optional[bool] = None) -> Playing:
        p = self.playing(guild_id)
        if p is None:
            raise VideoError("Nothing's playing.")
        return await self.play(
            p.guild_id, p.voice_channel_id, p.show, p.episode,
            start=max(0.0, p.position() - 2), notify_to=p.notify_to,
            language=language if language is not None else p.language,
            subtitles=subtitles if subtitles is not None else p.subtitles)

    async def step(self, guild_id: str, direction: int = 1
                   ) -> Optional[Playing]:
        """Next (or previous) episode; None at the end of the show."""
        p = self.playing(guild_id)
        if p is None:
            raise VideoError("Nothing's playing.")
        nxt = L.neighbour(p.show, p.episode.rel, direction)
        if nxt is None:
            return None
        self._save(p, finished=direction > 0)
        return await self.play(p.guild_id, p.voice_channel_id, p.show, nxt,
                               notify_to=p.notify_to, language=p.language,
                               subtitles=p.subtitles)

    async def stop(self, guild_id: str, reason: str = "stopped") -> bool:
        async with self._lock:
            done = await self._stop_locked(str(guild_id), reason)
        if done:
            await self._changed(str(guild_id))
        return done

    async def _stop_locked(self, guild_id: str, reason: str, *,
                           keep_picture: bool = False,
                           save: bool = True) -> bool:
        p = self._playing.pop(guild_id, None)
        if p is None:
            return False
        p.ended = True
        if save:
            self._save(p)
        for t in p.tasks:
            if t is not asyncio.current_task():
                t.cancel()
        if not keep_picture:
            await self._drop_picture(guild_id, reason)
        if p.client is not None and not p.sound_done and \
                self.voice.get(guild_id) is not None:
            if p.paused:
                try:
                    p.client.resume()      # a stopped pause must not stick
                except Exception:
                    pass
            await self.voice.stop(guild_id)
        # Kill, don't reap: the frame reader still holds ffmpeg's stdout,
        # and reaping here raced it ("video ffmpeg didn't exit"). The
        # reader ends with the kill and reaps it itself, as with TV.
        if p.proc is not None and getattr(p.proc, "returncode", 0) is None:
            try:
                p.proc.kill()
            except ProcessLookupError:
                pass
        self._cleanup(p)
        logger.info("video: %s %s off in %s: %s", p.show.name,
                    p.episode.label, guild_id, reason)
        return True

    async def _drop_picture(self, guild_id: str, reason: str) -> None:
        active = self.streams.active
        if active is not None and active.kind == KIND \
                and active.guild_id == guild_id \
                and guild_id not in self._playing:
            await self.streams.stop(reason)

    # -- endings ----------------------------------------------------------

    async def _closed(self, p: Playing) -> None:
        if self._playing.get(p.guild_id) is p:
            await self.stop(p.guild_id, "left voice")

    async def _frames_ended(self, p: Playing) -> None:
        if p.audio is None:          # no sound: the picture is the clock
            await self._ended(p)

    async def _ended(self, p: Playing) -> None:
        await asyncio.sleep(ENDED_SETTLE)
        if p.ended or p.recovering or self._playing.get(p.guild_id) is not p:
            return
        p.recovering = True
        session = self.voice.get(p.guild_id)
        if p.sound_done and session is not None and session.playing:
            await self.stop(p.guild_id, "replaced by other audio")
            return
        where = p.position()
        near_end = not p.info.duration or \
            where >= p.info.duration - END_SLACK
        if near_end:
            await self._finished(p)
            return
        if p.restarts < MAX_RESTARTS:
            logger.warning("video: %s %s stopped at %.0fs of %.0fs; "
                           "restarting there (%d/%d)", p.show.name,
                           p.episode.label, where, p.info.duration,
                           p.restarts + 1, MAX_RESTARTS)
            try:
                await self.play(p.guild_id, p.voice_channel_id, p.show,
                                p.episode, start=max(0.0, where - 2),
                                notify_to=p.notify_to, language=p.language,
                                subtitles=p.subtitles,
                                restarts=p.restarts + 1)
                return
            except VideoError as exc:
                logger.warning("video: restart failed: %s", exc)
        await self.stop(p.guild_id, "the video kept stopping")
        await self._tell(p, f"🎬 {p.show.name} {p.episode.label} kept "
                            f"stopping, so it's off. Say “continue "
                            f"{p.show.name}” to pick up where it was.")

    async def _finished(self, p: Playing) -> None:
        self._save(p, finished=True)
        nxt = L.neighbour(p.show, p.episode.rel)
        if nxt is not None and self.autoplay(p.guild_id):
            try:
                await self.play(p.guild_id, p.voice_channel_id, p.show, nxt,
                                notify_to=p.notify_to, language=p.language,
                                subtitles=p.subtitles)
                await self._tell(p, f"🎬 Up next: {p.show.name} {nxt.label}"
                                    + (f", {nxt.nice_title}" if
                                       nxt.nice_title else "") + ".")
                return
            except VideoError as exc:
                logger.warning("video: next episode failed: %s", exc)
        await self.stop(p.guild_id, "the episode ended")
        if nxt is None:
            # A film (a loose file in the library folder) has no episodes.
            await self._tell(p, f"🎬 That's the end of {p.show.name}."
                             if p.episode.film else
                             f"🎬 That's the last episode of "
                             f"{p.show.name}.")
        else:
            await self._tell(p, f"🎬 That's {p.episode.label}. Say “next "
                                f"episode” for {nxt.label}.")

    # -- background -------------------------------------------------------

    async def _saver(self, p: Playing) -> None:
        try:
            while not p.ended:
                await asyncio.sleep(SAVE_EVERY)
                if not p.ended and not p.paused:
                    self._save(p)
        except asyncio.CancelledError:
            return

    async def _monitor(self, p: Playing) -> None:
        """Frames not reaching viewers while not paused: restart where it
        was. (A pause holds the frames too, and is left alone.)"""
        try:
            await asyncio.sleep(MONITOR_GRACE)
            last, since = -1, time.monotonic()
            while not p.ended and not p.recovering:
                active = self.streams.active
                if active is None or active.kind != KIND \
                        or active.guild_id != p.guild_id:
                    return
                frames = getattr(active, "frames", 0)
                now = time.monotonic()
                if p.paused or frames != last:
                    last, since = frames, now
                elif now - since >= FROZEN_AFTER:
                    logger.warning("video: %s %s picture frozen for %.0fs; "
                                   "restarting where it was", p.show.name,
                                   p.episode.label, now - since)
                    if p.proc is not None and p.proc.returncode is None:
                        try:
                            p.proc.kill()
                        except ProcessLookupError:
                            pass
                    asyncio.get_running_loop().create_task(self._ended(p))
                    return
                await asyncio.sleep(MONITOR_EVERY)
        except asyncio.CancelledError:
            return

    async def _changed(self, guild_id: str) -> None:
        if self.on_change is not None:
            try:
                await self.on_change(guild_id)
            except Exception:
                logger.debug("video card refresh failed", exc_info=True)

    async def _tell(self, p: Playing, text: str) -> None:
        if self.notify is None or not p.notify_to:
            return
        try:
            await self.notify(p.notify_to, text)
        except Exception:
            logger.debug("video notice failed", exc_info=True)

    @staticmethod
    def _cleanup(p: Playing) -> None:
        if p.folder:
            shutil.rmtree(p.folder, ignore_errors=True)
            p.folder = None


def fmt_time(seconds: float) -> str:
    """"1:02:03" / "12:34"."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str, now: float = 0.0) -> Optional[float]:
    """"12:30", "1:02:03", "12 minutes", "90 seconds", "1h 5m", and
    relative "+30s" / "back 30 seconds" / "forward 2 minutes" (from
    ``now``). None if unreadable."""
    import re

    t = (text or "").strip().lower()
    if not t:
        return None
    sign = 0
    if re.match(r"^(back|rewind|-)", t) or t.endswith(" back"):
        sign = -1
    elif re.match(r"^(forward|ahead|skip|\+)", t) or t.endswith(
            (" forward", " ahead")):
        sign = 1
    t = re.sub(r"^(back|rewind|forward|ahead|skip|to|by|\+|-)\s*", "", t)
    t = re.sub(r"\s*(back|forward|ahead)$", "", t)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", t)
    if m:
        val = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + \
            int(m.group(3))
    else:
        parts = re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|"
                           r"minutes?|s|sec|secs|seconds?)\b", t)
        if not parts:
            if re.fullmatch(r"\d+(?:\.\d+)?", t):
                parts = [(t, "s")]
            else:
                return None
        val = 0.0
        for n, unit in parts:
            mult = 3600 if unit.startswith("h") else 60 if unit.startswith(
                "m") else 1
            val += float(n) * mult
    return max(0.0, now + sign * val) if sign else float(val)
