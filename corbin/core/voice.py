"""Voice session management.

Phase 0 verified the whole path against live Fluxer: LiveKit negotiates,
``FFmpegPCMAudio`` accepts a URL (its ``path`` argument is ffmpeg's ``-i``,
not a filename), and a live radio stream plays audibly.

Two things this layer exists to handle:

* **Explicit teardown.** The spike leaked an aiohttp session and tripped an
  ``AssertionError`` inside LiveKit's ``FfiHandle.__del__`` when objects
  were collected rather than closed. Sessions are therefore tracked and
  disconnected deliberately, including on shutdown.
* **One session per guild.** Fluxer, like Discord, allows a bot in one
  voice channel per guild; a second ``connect()`` without disconnecting
  leaks the first.

Fluxer is mid-replacement of LiveKit with a custom QUIC/DAVE stack, so this
is the subsystem most likely to break under us on their schedule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("corbin.voice")

#: Streaming reconnect flags. Without these a dropped packet ends playback
#: silently and the bot sits in the channel doing nothing.
#: ``-reconnect 1 -reconnect_streamed 1`` alone does not cover a
#: googlevideo connection that drops or 4xx's partway through: FFmpeg
#: hits EOF and exits cleanly, which looks like a song that simply
#: stopped early with no error anywhere. The two ``-reconnect_on_*``
#: flags make it retry mid-stream instead. Carried over from the Discord
#: bot, which learned this the hard way; the port had the short form.
STREAM_BEFORE_OPTIONS = ("-reconnect 1 -reconnect_streamed 1 "
                         "-reconnect_delay_max 5 "
                         "-reconnect_on_network_error 1 "
                         "-reconnect_on_http_error 4xx,5xx "
                         "-analyzeduration 0 -probesize 64000")
STREAM_OPTIONS = "-vn"
CONNECT_TIMEOUT = 40.0

#: Per-step ceiling on leaving voice. The SDK's ``disconnect()`` is
#: stop -> unpublish -> gateway leave -> ``room.disconnect()`` with no
#: timeout anywhere, and LiveKit can wedge on the unpublish or the room
#: close. Playback had already stopped, so the radio went quiet, and then
#: "stop the radio" never returned: he stayed in the channel with the
#: command's typing indicator running forever. Each step is bounded and
#: the gateway leave is sent even when the step before it hung.
TEARDOWN_STEP_TIMEOUT = 8.0
#: Ceilings on the audio track's publish and unpublish between songs.
#: fluxer.py unpublishes with no timeout when a track stops, and ⏮️ with a
#: music video also published wedged right there: the restart never
#: returned, held the player's lock, and every button went dead (live,
#: 2026-10-17). Publishing is bounded for the same reason.
AUDIO_TRACK_TIMEOUT = 5.0
#: Seconds of a song's audio decoded ahead of playback (music and radio;
#: not TV or the video library, whose one ffmpeg also feeds the picture).
#: Corbin used to read only as fast as he played, with LiveKit's 1 s
#: queue as the whole cushion, so any pause in YouTube's delivery longer
#: than that was a gap people heard. Live 2026-10-20, a 1080p music video:
#: "longest audio wait 4.94s, loop lag 3ms": the source, not the bot.
#: A song's audio is small (30 s is ~5.8 MB of PCM), so ffmpeg can run
#: well ahead whenever YouTube is fast and coast through when it isn't.
AUDIO_READ_AHEAD = 30.0
AUDIO_PUBLISH_TIMEOUT = 10.0

#: Opus bitrate we ask LiveKit to publish at, in bits per second.
#:
#: The SDK publishes every track as ``SOURCE_MICROPHONE`` with an empty
#: ``audio_encoding``, which is LiveKit's speech profile: mono, heavily
#: compressed, tuned for a talking head. Music through it sounds thin and
#: swirly no matter what the *channel's* bitrate is set to -- that number
#: is a ceiling on what the server will relay, not what we encode at.
#:
#: 96 kbps stereo Opus is transparent enough for music and sits at the
#: channel cap, so nothing downstream has to re-encode.
MUSIC_BITRATE = 96_000


@dataclass
class Session:
    guild_id: str
    channel_id: str
    client: Any
    #: The voice channel's name. Never overwritten by a source label --
    #: doing so produced "Playing in 975 KZLF" instead of the channel.
    channel_name: str = ""
    #: What is playing (station or track source).
    label: str = ""
    source_url: str = ""
    #: Background task announcing track changes, if one is running.
    watcher: Optional[asyncio.Task] = field(default=None, repr=False)
    #: Last announced track, so the same song is never posted twice.
    last_track: str = ""
    #: Voice chat listener (``core.voicechat.Listener``), if one is
    #: running. Owned by the session so leaving voice by any route --
    #: "stop the radio", a channel move, shutdown -- stops listening too.
    listener: Optional[Any] = field(default=None, repr=False)
    #: Async callables run when the session is left, by any route: the
    #: video stream registers its stop here (``core.video``).
    closers: list = field(default_factory=list, repr=False)
    #: Monotonic time from which this session's voice time is uncounted.
    counted_from: float = field(default_factory=time.monotonic, repr=False)

    def take_elapsed(self) -> float:
        """Seconds since last counted, and restart the count from now."""
        now = time.monotonic()
        elapsed, self.counted_from = now - self.counted_from, now
        return max(0.0, elapsed)

    @property
    def playing(self) -> bool:
        try:
            return bool(self.client.is_playing)
        except Exception:
            return False

    def cancel_watcher(self) -> None:
        if self.watcher is not None and not self.watcher.done():
            self.watcher.cancel()
        self.watcher = None

    async def close_listener(self) -> None:
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                await listener.close()
            except Exception:
                logger.debug("voice chat listener close failed",
                             exc_info=True)


def _note_dry(clock: dict, source: Any) -> None:
    """Count the times LiveKit's playout queue had run empty when the
    next frame arrived: an audible gap. Counted as transitions, after
    the first second (the queue starts empty), so one long gap is one.
    Read by ``times_dry`` for the stream stats line."""
    try:
        queued = float(source.queued_duration)
    except Exception:
        return
    dry = queued <= 0.005 and clock["captured"] > clock["rate"]
    if dry and not clock.get("was_dry"):
        clock["dry"] = clock.get("dry", 0) + 1
    clock["was_dry"] = dry


def times_dry(client: Any) -> Optional[int]:
    """How often this track's sound has run dry, or None (no track)."""
    clock = getattr(client, "_corbin_audio_clock", None)
    return clock.get("dry", 0) if clock else None


def sound_stats(client: Any) -> str:
    """", sound ran dry Nx, longest audio wait 1.4s" for a stream's stats
    line, the wait since the last call (then reset); "" with no track."""
    clock = getattr(client, "_corbin_audio_clock", None)
    if not clock:
        return ""
    wait = clock.get("wait_max", 0.0)
    clock["wait_max"] = 0.0
    text = (f", sound ran dry {clock.get('dry', 0)}x, longest audio wait "
            f"{wait:.2f}s")
    if "buffered" in clock:
        text += f", audio buffer {clock['buffered']:.1f}s"
    return text


def audio_position(client: Any) -> Optional[float]:
    """Seconds of the current track that listeners have heard, or None.

    Samples handed to LiveKit, less what is still in its queue (the frame
    loop runs up to ~1 s ahead of playout). Freezes while paused, since
    the loop stops capturing. The clock a music video syncs to.
    """
    clock = getattr(client, "_corbin_audio_clock", None)
    if not clock:
        return None
    source = clock.get("source")
    try:
        queued = float(source.queued_duration) if source is not None else 0.0
    except Exception:
        queued = 0.0
    return max(0.0, clock["captured"] / clock["rate"] - queued)


class VoiceManager:
    """Tracks one voice session per guild."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._sessions: dict[str, Session] = {}
        #: Guild -> teardown steps that failed on the last disconnect.
        self._teardown_failures: dict[str, tuple[str, ...]] = {}
        #: Called with seconds spent in voice when a session ends, for the
        #: daily digest.
        self.on_voice_time: Optional[Any] = None

    def collect_voice_time(self) -> float:
        """Uncounted seconds across live sessions, marking them counted.

        The digest calls this before posting, so a session running across
        8 PM is split between the two days rather than landing whole in
        whichever day it ends.
        """
        return sum(s.take_elapsed() for s in self._sessions.values())

    def _credit(self, session: "Session") -> None:
        if self.on_voice_time is None:
            return
        try:
            self.on_voice_time(session.take_elapsed())
        except Exception:
            logger.debug("voice time counter failed", exc_info=True)

    def get(self, guild_id: Optional[str]) -> Optional[Session]:
        return self._sessions.get(str(guild_id)) if guild_id else None

    def __len__(self) -> int:
        return len(self._sessions)

    @staticmethod
    def _apply_frame_loop(client: Any) -> bool:
        """Replace the SDK's ffmpeg read loop with a corrected copy.

        Two defects in ``VoiceClient._run_ffmpeg_loop`` make music
        crackle, and neither is about bitrate:

        1. It calls ``proc.stdout.read(chunk_bytes)``, which returns *up
           to* that many bytes. Short reads are routine on a pipe, and
           the loop **pads every short chunk with silence** to frame
           size. So zero-samples get injected throughout the stream, not
           just at the end -- that is the clicking. ``readexactly`` is
           what was meant; a genuine partial only happens at EOF.

        2. It sleeps a fixed 20 ms after every frame *on top of*
           ``capture_frame``, which already blocks until its 1000 ms
           queue has room. Double-pacing means each iteration costs 20 ms
           plus the read plus overhead, so frames are produced slower
           than real time and the queue steadily drains into underruns.

        A corrected copy rather than a wrapper, because both faults are
        in the middle of one method. Returns False if the SDK has
        changed shape, leaving its own loop in place.
        """
        if getattr(client, "_corbin_frame_loop", False):
            return True
        if not hasattr(client, "_run_ffmpeg_loop"):
            return False
        try:
            from livekit import rtc
        except Exception:
            return False

        async def _loop(source: Any) -> None:
            import shlex

            if client._room is None:
                raise RuntimeError("not connected to a voice channel")

            chunk_bytes = 960 * source.num_channels * 2
            args = [source.executable]
            if source.before_options:
                args += shlex.split(source.before_options)
            args += ["-i", source.path, "-vn", "-f", "s16le",
                     "-ar", str(source.sample_rate),
                     "-ac", str(source.num_channels),
                     "-loglevel", "warning"]
            if source.options:
                args += shlex.split(source.options)
            args.append("pipe:1")

            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            if proc.stdout is None:
                raise RuntimeError("ffmpeg did not open stdout")

            # Live TV asks for a shorter queue (core.tv.TV_AUDIO_QUEUE_MS)
            # so its picture's read-ahead stays small; music keeps 1 s.
            queue_ms = getattr(source, "corbin_queue_ms", None)
            rtc_source = rtc.AudioSource(
                source.sample_rate, source.num_channels,
                **({"queue_size_ms": int(queue_ms)} if queue_ms else {}))
            await client._publish_track(rtc_source)
            # The audio clock a music video syncs to (audio_position):
            # samples handed to LiveKit, less what is still queued there.
            clock = {"source": rtc_source, "rate": source.sample_rate,
                     "captured": 0, "dry": 0, "was_dry": False}
            client._corbin_audio_clock = clock
            # Read ahead into a queue (AUDIO_READ_AHEAD), unless the
            # source asks for a short LiveKit queue: those (TV, the video
            # library) share their ffmpeg with the picture and must stay
            # in step with it.
            ahead = None if queue_ms else asyncio.Queue(
                maxsize=max(1, int(AUDIO_READ_AHEAD * 50)))
            END = object()

            async def _read_ahead() -> None:
                try:
                    while True:
                        try:
                            chunk = await proc.stdout.readexactly(
                                chunk_bytes)
                        except asyncio.IncompleteReadError as exc:
                            if exc.partial:
                                await ahead.put(exc.partial + b"\x00" * (
                                    chunk_bytes - len(exc.partial)))
                            break
                        await ahead.put(chunk)
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    try:
                        ahead.put_nowait(END)
                    except asyncio.QueueFull:
                        # Make room: the end must get through.
                        try:
                            ahead.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        ahead.put_nowait(END)

            reader = asyncio.ensure_future(_read_ahead()) \
                if ahead is not None else None
            try:
                while True:
                    await client._resume_event.wait()
                    last = False
                    asked = time.monotonic()
                    if ahead is not None:
                        clock["buffered"] = ahead.qsize() * 0.02
                        data = await ahead.get()
                        if data is END:
                            break
                    else:
                        try:
                            data = await proc.stdout.readexactly(chunk_bytes)
                        except asyncio.IncompleteReadError as exc:
                            # The only legitimate short read: end of
                            # stream.
                            data = exc.partial
                            if not data:
                                break
                            data += b"\x00" * (chunk_bytes - len(data))
                            last = True

                    # How long ffmpeg kept us waiting for this chunk: with
                    # a low loop lag, a long wait means the sound's own
                    # source (the network, YouTube) was the holdup.
                    waited = time.monotonic() - asked
                    if waited > clock.get("wait_max", 0.0):
                        clock["wait_max"] = waited
                    samples = len(data) // (2 * source.num_channels)
                    _note_dry(clock, rtc_source)
                    await rtc_source.capture_frame(rtc.AudioFrame(
                        data=data, sample_rate=source.sample_rate,
                        num_channels=source.num_channels,
                        samples_per_channel=samples))
                    clock["captured"] += samples
                    # No sleep: capture_frame blocks on a full queue and
                    # is the pacing mechanism. See the docstring.
                    if last:
                        break
            finally:
                if reader is not None and not reader.done():
                    reader.cancel()
                if getattr(client, "_corbin_audio_clock", None) is clock:
                    client._corbin_audio_clock = None
                if proc.returncode is None:
                    proc.kill()
                # communicate(), not wait(): asyncio's wait() only returns
                # once stdout is closed, and unread audio left in the pipe
                # kept it open. Stopping one track took 44 s (live ⏮️,
                # 2026-10-17); it's also why stopping the radio once
                # took ~8 s. Draining lets the pipe reach EOF at once.
                try:
                    await asyncio.wait_for(proc.communicate(),
                                           AUDIO_TRACK_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("audio ffmpeg didn't exit within %.0fs",
                                   AUDIO_TRACK_TIMEOUT)
                # Unpublish here, bounded, and clear the publication so the
                # SDK's own (unbounded) unpublish after us is skipped. See
                # AUDIO_TRACK_TIMEOUT.
                publication = getattr(client, "_current_publication", None)
                if publication is not None and client._room is not None:
                    try:
                        await asyncio.wait_for(
                            client._room.local_participant.unpublish_track(
                                publication.sid), AUDIO_TRACK_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning("audio track unpublish hung for "
                                       "%.0fs; carrying on without it",
                                       AUDIO_TRACK_TIMEOUT)
                    except Exception:
                        logger.debug("audio unpublish failed", exc_info=True)
                    client._current_publication = None
                    client._current_track = None

        try:
            client._run_ffmpeg_loop = _loop
            client._corbin_frame_loop = True
        except Exception:
            return False
        return True

    @staticmethod
    def _apply_music_profile(client: Any) -> bool:
        """Publish audio as music rather than as a microphone.

        Wraps the SDK's private ``_publish_track`` because the public
        ``play()`` gives no way to pass ``TrackPublishOptions`` -- it
        builds them internally and hardcodes the microphone source. The
        alternative is reimplementing the ffmpeg read loop here to
        publish ourselves, which is a lot more surface to keep in step
        with the SDK for the same result.

        Returns False if the SDK has moved on, in which case playback
        still works and only sounds worse. The boot probe checks this
        binding so a silent downgrade gets noticed.
        """
        if getattr(client, "_corbin_music_profile", False):
            return True
        original = getattr(client, "_publish_track", None)
        if original is None:
            return False
        try:
            from livekit import rtc
        except Exception:
            return False

        async def _publish_music(source: Any) -> None:
            if getattr(client, "_corbin_music_failed", False):
                await original(source)
                return

            track = rtc.LocalAudioTrack.create_audio_track("audio", source)
            options = rtc.TrackPublishOptions(
                # SOURCE_MICROPHONE, not SOURCE_UNKNOWN. Fluxer's SFU
                # never answers the publish for an unknown source and the
                # request times out, so there is no audio at all -- the
                # source is a routing hint to the server, not the thing
                # that sets the encoder. ``max_bitrate`` is the actual
                # quality lever, and it works fine alongside it.
                source=rtc.TrackSource.SOURCE_MICROPHONE,
                # DTX would gate quiet passages as silence; RED spends
                # bandwidth protecting against loss we are not seeing.
                dtx=False, red=False,
            )
            # Assigned rather than passed to the constructor:
            # ``AudioEncoding`` is a protobuf submessage and is not
            # re-exported on ``livekit.rtc``, so naming the type fails.
            options.audio_encoding.max_bitrate = MUSIC_BITRATE
            try:
                client._current_publication = await asyncio.wait_for(
                    client._room.local_participant.publish_track(
                        track, options), AUDIO_PUBLISH_TIMEOUT)
                client._current_track = track
            except asyncio.TimeoutError:
                # Not the profile's fault: the server didn't answer. The
                # SDK's own publish would hang the same way, so fail the
                # track (the player retries) rather than fall back.
                raise RuntimeError("the voice server didn't take the audio "
                                   f"track within {AUDIO_PUBLISH_TIMEOUT:.0f}s")
            except Exception:
                # Better thin audio than none. A tweak to sound quality
                # must never be able to stop playback -- publishing with
                # SOURCE_UNKNOWN timed out here and silenced the bot
                # entirely, which is a far worse outcome than the mono
                # audio it was trying to improve on.
                logger.warning("music publish profile rejected; falling "
                               "back to the SDK's defaults", exc_info=True)
                client._corbin_music_failed = True
                await original(source)

        try:
            client._publish_track = _publish_music
            client._corbin_music_profile = True
        except Exception:
            return False
        return True

    async def connect(self, guild_id: str, channel_id: str) -> Session:
        """Join a voice channel, replacing any existing session."""
        existing = self.get(guild_id)
        if existing is not None:
            if existing.channel_id == str(channel_id):
                return existing
            # Moving channels: tear the old one down or it leaks.
            await self.disconnect(guild_id)

        channel = await self._fetch_channel(channel_id)
        if channel is None:
            raise RuntimeError(f"cannot resolve voice channel {channel_id}")
        if not getattr(channel, "is_voice_channel", False):
            raise RuntimeError(f"#{getattr(channel, 'name', channel_id)} "
                               "is not a voice channel")

        client = await asyncio.wait_for(channel.connect(self.bot),
                                        timeout=CONNECT_TIMEOUT)
        if not self._apply_music_profile(client):
            logger.warning("could not apply the music publish profile; "
                           "audio will use LiveKit's speech defaults "
                           "(mono, low bitrate)")
        if not self._apply_frame_loop(client):
            logger.warning("could not replace the ffmpeg frame loop; "
                           "audio will crackle (silence padded into "
                           "short reads)")
        session = Session(
            guild_id=str(guild_id), channel_id=str(channel_id),
            client=client,
            channel_name=str(getattr(channel, "name", "") or ""))
        self._sessions[str(guild_id)] = session
        logger.info("joined voice %s in guild %s", session.channel_name,
                    guild_id)
        return session

    async def play_stream(self, guild_id: str, channel_id: str, url: str, *,
                          label: str = "") -> Session:
        from fluxer.voice import FFmpegPCMAudio

        session = await self.connect(guild_id, channel_id)
        if session.playing:
            await self.stop(guild_id)
        source = FFmpegPCMAudio(url,
                                before_options=STREAM_BEFORE_OPTIONS,
                                options=STREAM_OPTIONS)
        await session.client.play(source)
        session.source_url = url
        if label:
            session.label = label
        return session

    def watch(self, guild_id: str, task: "asyncio.Task") -> None:
        """Attach a track-announcer task to a session."""
        session = self.get(guild_id)
        if session is None:
            task.cancel()
            return
        session.cancel_watcher()
        session.watcher = task

    async def stop(self, guild_id: str) -> bool:
        session = self.get(guild_id)
        if session is None:
            return False
        session.cancel_watcher()
        try:
            await session.client.stop()
        except Exception:
            logger.debug("stop failed", exc_info=True)
        return True

    async def disconnect(self, guild_id: str) -> bool:
        """Leave voice. True if there was a session to leave.

        Never hangs: see ``TEARDOWN_STEP_TIMEOUT``. Steps that failed are
        logged and kept for ``teardown_failures`` so the caller doesn't
        claim a clean exit that didn't happen.
        """
        session = self._sessions.pop(str(guild_id), None)
        if session is None:
            return False
        self._credit(session)
        session.cancel_watcher()
        await session.close_listener()
        for closer in list(session.closers):
            try:
                await asyncio.wait_for(closer(), TEARDOWN_STEP_TIMEOUT * 3)
            except Exception:
                logger.warning("voice teardown: a session closer failed in "
                               "guild %s", guild_id, exc_info=True)
        session.closers.clear()
        client = session.client
        failed: list[str] = []

        async def step(name: str, make: Any) -> None:
            started = time.monotonic()
            try:
                await asyncio.wait_for(make(), timeout=TEARDOWN_STEP_TIMEOUT)
                took = time.monotonic() - started
                # Stopping the radio took ~8 s with no step timing out
                # (first live test). Name the slow step when it happens.
                if took >= 1.0:
                    logger.info("voice teardown: %s took %.1fs in guild %s",
                                name, took, guild_id)
            except asyncio.TimeoutError:
                logger.warning("voice teardown: %s timed out after %.0fs "
                               "in guild %s", name, TEARDOWN_STEP_TIMEOUT,
                               guild_id)
                failed.append(name)
            except Exception:
                logger.warning("voice teardown: %s failed in guild %s",
                               name, guild_id, exc_info=True)
                failed.append(name)

        await step("stop playback", client.stop)

        # The SDK's disconnect() re-runs stop() first, which is the call
        # that may just have hung, so do its remaining two steps directly.
        gateway = getattr(client, "_gateway", None)
        if gateway is None or not hasattr(client, "_room"):
            logger.warning("voice client has no _gateway/_room; the SDK "
                           "changed shape, falling back to its disconnect()")
            await step("disconnect", client.disconnect)
        else:
            # Independent of each other, so run together: the slower of
            # the two, not their sum.
            room = client._room
            steps = [step("leave voice channel",
                          lambda: gateway.update_voice_state(
                              guild_id=str(guild_id), channel_id=None))]
            if room is not None:
                steps.append(step("close LiveKit room", room.disconnect))
            await asyncio.gather(*steps)
            client._room = None
            connected = getattr(client, "_connected", None)
            if connected is not None:
                connected.clear()

        self._teardown_failures[str(guild_id)] = tuple(failed)
        if failed:
            logger.warning("left voice in guild %s with failed steps: %s",
                           guild_id, ", ".join(failed))
        else:
            logger.info("left voice in guild %s", guild_id)
        return True

    def teardown_failures(self, guild_id: Optional[str]) -> tuple[str, ...]:
        """Steps that failed on this guild's most recent disconnect."""
        return self._teardown_failures.get(str(guild_id), ())

    async def disconnect_all(self) -> None:
        """Called on shutdown. Collection alone leaves LiveKit handles."""
        for guild_id in list(self._sessions):
            await self.disconnect(guild_id)

    async def _fetch_channel(self, channel_id: str) -> Any:
        for name in ("get_channel", "fetch_channel"):
            getter = getattr(self.bot, name, None)
            if not callable(getter):
                continue
            for key in (str(channel_id), int(channel_id)):
                try:
                    got = getter(key)
                    got = await got if hasattr(got, "__await__") else got
                except Exception:
                    continue
                if got is not None:
                    return got
        return None


#: How long he stays in a voice channel with nobody else in it (s).
#: Owner's choice: about 20 seconds.
EMPTY_LEAVE_AFTER = 20.0


class EmptyChannelWatch:
    """Leave a voice channel once nobody else has been in it for a while.

    He used to stay forever: radio playing to an empty room, voice chat
    listening to nobody, and with voice chat the local model set aside the
    whole time. ``check`` runs on every voice state change and on the
    scheduler tick; a countdown starts when his channel empties, is
    cancelled when someone comes back, and re-checks before leaving.

    ``occupants(channel_id)`` counts the people there other than him.
    ``leave(guild_id)`` does the leaving (the music stop, which also clears
    the queue, or a plain disconnect).
    """

    def __init__(self, voice: Any, *, occupants: Any, leave: Any,
                 delay: float = EMPTY_LEAVE_AFTER) -> None:
        self.voice = voice
        self.occupants = occupants
        self.leave = leave
        self.delay = delay
        #: guild -> (channel the countdown is for, its task).
        self._timers: dict[str, tuple[str, asyncio.Task]] = {}

    def _cancel(self, guild_id: str) -> None:
        entry = self._timers.pop(guild_id, None)
        if entry is not None and not entry[1].done():
            entry[1].cancel()

    def check(self, guild_id: Optional[str]) -> None:
        if not guild_id:
            return
        gid = str(guild_id)
        session = self.voice.get(gid)
        if session is None or self.occupants(session.channel_id) > 0:
            self._cancel(gid)
            return
        running = self._timers.get(gid)
        if running is not None and not running[1].done():
            if running[0] == session.channel_id:
                return
            # Moved to another empty channel: start over for that one.
            self._cancel(gid)
        self._timers[gid] = (session.channel_id, asyncio.create_task(
            self._later(gid, session.channel_id)))

    def sweep(self) -> None:
        """Check every session (backs up a missed voice state event)."""
        for gid in list(getattr(self.voice, "_sessions", {})):
            self.check(gid)

    async def _later(self, guild_id: str, channel_id: str) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return
        session = self.voice.get(guild_id)
        if session is None or session.channel_id != channel_id \
                or self.occupants(channel_id) > 0:
            return
        logger.info("voice: nobody else in %s for %.0fs; leaving",
                    session.channel_name or channel_id, self.delay)
        try:
            await self.leave(guild_id)
        except Exception:
            logger.warning("voice: leaving the empty channel failed in "
                           "guild %s", guild_id, exc_info=True)
        finally:
            entry = self._timers.get(guild_id)
            if entry is not None and entry[1] is asyncio.current_task():
                self._timers.pop(guild_id, None)

