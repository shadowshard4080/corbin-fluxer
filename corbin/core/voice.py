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


class VoiceManager:
    """Tracks one voice session per guild."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._sessions: dict[str, Session] = {}
        #: Guild -> teardown steps that failed on the last disconnect.
        self._teardown_failures: dict[str, tuple[str, ...]] = {}

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

            rtc_source = rtc.AudioSource(source.sample_rate,
                                         source.num_channels)
            await client._publish_track(rtc_source)
            try:
                while True:
                    await client._resume_event.wait()
                    last = False
                    try:
                        data = await proc.stdout.readexactly(chunk_bytes)
                    except asyncio.IncompleteReadError as exc:
                        # The only legitimate short read: end of stream.
                        data = exc.partial
                        if not data:
                            break
                        data += b"\x00" * (chunk_bytes - len(data))
                        last = True

                    await rtc_source.capture_frame(rtc.AudioFrame(
                        data=data, sample_rate=source.sample_rate,
                        num_channels=source.num_channels,
                        samples_per_channel=(
                            len(data) // (2 * source.num_channels))))
                    # No sleep: capture_frame blocks on a full queue and
                    # is the pacing mechanism. See the docstring.
                    if last:
                        break
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()

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
                client._current_publication = \
                    await client._room.local_participant.publish_track(
                        track, options)
                client._current_track = track
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
        session.cancel_watcher()
        client = session.client
        failed: list[str] = []

        async def step(name: str, make: Any) -> None:
            try:
                await asyncio.wait_for(make(), timeout=TEARDOWN_STEP_TIMEOUT)
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
            await step("leave voice channel",
                       lambda: gateway.update_voice_state(
                           guild_id=str(guild_id), channel_id=None))
            room = client._room
            if room is not None:
                await step("close LiveKit room", room.disconnect)
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
