"""Queue and playback.

The subtle part is failure detection. When a stream URL is expired or
PO-token-gated, FFmpeg 403s and the track simply **ends immediately** --
yt-dlp raises nothing and the voice client reports a clean finish. So a
track that ends sooner than ``IMMEDIATE_FAIL_SECS`` is treated as a
failure, re-extracted with a different player client, and retried.

Without that, a broken stream looks like a song that played for zero
seconds and the queue silently drains.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

from .audio import (IMMEDIATE_FAIL_SECS, MAX_PLAY_RETRIES,
                    MIN_RETRYABLE_DURATION, RETRY_CLIENT_SETS, Track)
from .voice import STREAM_BEFORE_OPTIONS, STREAM_OPTIONS

logger = logging.getLogger("corbin.player")

MAX_QUEUE = 500


@dataclass
class GuildPlayer:
    guild_id: str
    queue: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    loop_track: bool = False
    loop_queue: bool = False
    started_at: float = 0.0
    #: When the current pause began (0 = not paused), and how long earlier
    #: pauses lasted, so ``elapsed`` doesn't count time spent paused.
    paused_at: float = 0.0
    paused_total: float = 0.0
    #: Bumped on every playback attempt. A finish callback carries the
    #: number it was started with, and a stale one is ignored: when a
    #: dead stream is retried, the first attempt's "finished" used to
    #: start the NEXT song too, and post a second Now Playing card.
    generation: int = 0
    #: (id of track, channel) of the last card posted, so the same track
    #: is never announced twice in one channel.
    last_card: tuple = ()
    #: Set while a track is being advanced, so two finishes cannot race.
    advancing: bool = False
    announce_to: Optional[str] = None

    @property
    def playing(self) -> bool:
        return self.current is not None

    @property
    def paused(self) -> bool:
        return bool(self.paused_at)

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        now = time.monotonic()
        held = self.paused_total + ((now - self.paused_at)
                                    if self.paused_at else 0.0)
        return max(0.0, now - self.started_at - held)

    def mark_started(self) -> None:
        self.started_at = time.monotonic()
        self.paused_at = self.paused_total = 0.0

    def add(self, track: Track) -> bool:
        if len(self.queue) >= MAX_QUEUE:
            return False
        self.queue.append(track)
        return True

    def shuffle(self) -> None:
        random.shuffle(self.queue)

    def clear(self) -> None:
        self.queue.clear()

    def remove(self, index: int) -> Optional[Track]:
        if 0 <= index < len(self.queue):
            return self.queue.pop(index)
        return None

    def next_track(self) -> Optional[Track]:
        if self.loop_track and self.current:
            return self.current
        if not self.queue:
            return None
        track = self.queue.pop(0)
        if self.loop_queue:
            self.queue.append(track)
        return track


class PlayerManager:
    """One :class:`GuildPlayer` per guild, driving a VoiceManager session."""

    def __init__(self, voice: Any, extractor: Any, *,
                 announce: Optional[Callable[[str, Track], Awaitable[None]]]
                 = None) -> None:
        self.voice = voice
        self.extractor = extractor
        self.announce = announce
        self._players: dict[str, GuildPlayer] = {}
        #: One per guild, serialising the commands that mutate playback.
        self._locks: dict[str, asyncio.Lock] = {}
        #: Background playlist imports, one per guild. Cancelled by stop.
        self._imports: dict[str, asyncio.Task] = {}
        #: ``hook(guild_id, track)`` as soon as a track's audio starts, and
        #: ``hook(guild_id)`` when the queue runs out. The music video
        #: follows the queue through these. Run as tasks: a hook that
        #: fetches a video must never hold up the next song.
        self.on_track_started: list[Callable[..., Awaitable[Any]]] = []
        self.on_queue_end: list[Callable[..., Awaitable[Any]]] = []

    def _fire(self, hooks: list, *args: Any) -> None:
        for hook in hooks:
            try:
                asyncio.get_running_loop().create_task(hook(*args))
            except Exception:
                logger.debug("player hook failed to start", exc_info=True)

    def player(self, guild_id: str) -> GuildPlayer:
        return self._players.setdefault(str(guild_id),
                                        GuildPlayer(guild_id=str(guild_id)))

    def get(self, guild_id: Optional[str]) -> Optional[GuildPlayer]:
        return self._players.get(str(guild_id)) if guild_id else None

    def lock(self, guild_id: str) -> asyncio.Lock:
        """Serialises ``skip``/``stop`` for one guild.

        Two skip commands arriving together used to both read
        ``player.current`` before either advanced, so both reported
        skipping the same track and posted identical messages -- while
        ``advance``'s own guard quietly dropped the second skip. Two
        messages one track apart is not a rare race: "skip this" followed
        by "next song" a second later is how people actually talk, and
        ``play_now`` holds the player busy for ``IMMEDIATE_FAIL_SECS``
        while it checks the stream is real.

        Held across the whole operation, not just the state read, so the
        second caller observes the first one's result rather than a
        half-applied one. ``advance`` deliberately does NOT take it: it is
        called from inside ``skip`` and from the ffmpeg ``after``
        callback, and has its own ``advancing`` flag.
        """
        return self._locks.setdefault(str(guild_id), asyncio.Lock())

    # -- playback ---------------------------------------------------------

    async def play_now(self, guild_id: str, channel_id: str,
                       track: Track, *,
                       announce: bool = True) -> Optional[str]:
        """Start ``track`` immediately. Returns an error string or None.

        ``announce=False`` for a caller that posts its own embed. The
        announce hook exists for tracks that start on their own -- the
        queue advancing, a skip -- where nobody is waiting on a reply.
        The ``play`` command posts its own "Now playing", so letting the
        hook fire too put the same embed on screen twice.
        """
        from fluxer.voice import FFmpegPCMAudio

        player = self.player(guild_id)
        try:
            session = await self.voice.connect(guild_id, channel_id)
        except Exception as exc:
            return f"I couldn't join voice: {exc}"

        attempt = 0
        from .relay import RELAY

        while True:
            # Through the range relay: YouTube paces one long download of
            # a song's audio to ~playback speed (below it from the
            # server), ranges go full speed. See corbin/core/relay.py.
            source = FFmpegPCMAudio(
                RELAY.wrap(track.url), before_options=STREAM_BEFORE_OPTIONS,
                options=STREAM_OPTIONS)
            player.current = track
            player.mark_started()
            player.generation += 1
            gen = player.generation
            asked = time.monotonic()
            try:
                await session.client.play(
                    source,
                    after=lambda _e, g=gen: self._schedule_advance(guild_id,
                                                                   g))
                if time.monotonic() - asked > 2.0:
                    # play() first stops the old track, which is where a
                    # wedged unpublish used to hang ⏮️ forever.
                    logger.warning("starting %r took %.1fs (stopping the "
                                   "previous audio was slow)", track.title,
                                   time.monotonic() - asked)
            except Exception as exc:
                logger.info("play failed: %s", exc)
                return f"Playback failed: {exc}"
            if attempt == 0:
                # Now, not after the IMMEDIATE_FAIL_SECS check below: the
                # music video started 4 s+ behind the audio on every song
                # while it waited (live, 2026-10-17).
                self._fire(self.on_track_started, guild_id, track)

            # Give it long enough to prove it is really playing. A 403 shows
            # up as an immediate, silent finish.
            await asyncio.sleep(IMMEDIATE_FAIL_SECS)
            if session.playing or (track.duration or 0) < MIN_RETRYABLE_DURATION:
                if announce and self.announce:
                    await self.announce(guild_id, track)
                return None

            if attempt >= MAX_PLAY_RETRIES:
                player.current = None
                return ("That stream kept dying immediately, which usually "
                        "means an expired or token-gated URL.")
            clients = RETRY_CLIENT_SETS[attempt % len(RETRY_CLIENT_SETS)]
            logger.info("track died immediately; re-extracting with %s",
                        clients)
            refreshed = await self._reextract(track, clients)
            if refreshed is None:
                player.current = None
                return "I couldn't get a working stream for that."
            track = refreshed
            attempt += 1

    async def _reextract(self, track: Track,
                         clients: list[str]) -> Optional[Track]:
        from .audio import track_from_info

        target = track.webpage_url or track.url
        try:
            info = await self.extractor.extract(target, clients=clients)
        except Exception:
            return None
        fresh = track_from_info(info, requested_by=track.requested_by,
                                requester_name=track.requester_name)
        return fresh

    def _schedule_advance(self, guild_id: str,
                          generation: Optional[int] = None) -> None:
        """``after`` callback: hop back onto the loop to advance.

        Ignored when ``generation`` is no longer current: that attempt was
        replaced (a retry, a skip, a new song) and is not "the track
        finished".
        """
        player = self.get(guild_id)
        if (generation is not None and player is not None
                and generation != player.generation):
            return
        try:
            asyncio.get_running_loop().create_task(self.advance(guild_id))
        except RuntimeError:
            pass

    async def advance(self, guild_id: str) -> None:
        player = self.player(guild_id)
        if player.advancing:
            return
        player.advancing = True
        try:
            session = self.voice.get(guild_id)
            nxt = player.next_track()
            if nxt is None or session is None:
                player.current = None
                player.started_at = 0.0
                self._fire(self.on_queue_end, guild_id)
                return
            await self.play_now(guild_id, session.channel_id, nxt)
        finally:
            player.advancing = False

    def toggle_pause(self, guild_id: str) -> Optional[bool]:
        """Pause or resume. Returns the new paused state, or None if
        nothing is playing.

        The voice client's frame loop waits on its resume event before
        every frame, so pausing simply stops feeding audio. A skip or stop
        while paused is safe: the client's stop cancels the loop and sets
        the event again.
        """
        player = self.get(guild_id)
        session = self.voice.get(guild_id)
        if player is None or player.current is None or session is None:
            return None
        client = session.client
        if player.paused:
            client.resume()
            player.paused_total += time.monotonic() - player.paused_at
            player.paused_at = 0.0
            return False
        client.pause()
        player.paused_at = time.monotonic()
        return True

    async def restart(self, guild_id: str) -> Optional[Track]:
        """Play the current track again from the start. None if nothing
        is playing (or it couldn't be restarted).

        Asked for so a music video can start with its song: the audio
        begins before the video can. The video restarts with it through
        ``on_track_started``.
        """
        began = time.monotonic()
        async with self.lock(guild_id):
            player = self.get(guild_id)
            session = self.voice.get(guild_id)
            if player is None or player.current is None or session is None:
                return None
            track = player.current
            logger.info("restarting %r in guild %s", track.title, guild_id)
            if player.paused:
                session.client.resume()
            failure = await self.play_now(guild_id, session.channel_id,
                                          track, announce=False)
            logger.info("restart of %r %s in %.1fs", track.title,
                        "failed" if failure else "done",
                        time.monotonic() - began)
            if failure:
                logger.info("restart of %r failed: %s", track.title, failure)
                return None
            return track

    async def skip(self, guild_id: str) -> Optional[Track]:
        async with self.lock(guild_id):
            player = self.player(guild_id)
            skipped = player.current
            if skipped is None:
                return None
            # Looping a single track would replay it forever on skip.
            player.loop_track = False
            await self.voice.stop(guild_id)
            await self.advance(guild_id)
            return skipped

    # -- playlists ----------------------------------------------------------

    def importing(self, guild_id: str) -> bool:
        task = self._imports.get(str(guild_id))
        return task is not None and not task.done()

    def start_import(self, guild_id: str, channel_id: str,
                     queries: Sequence[str], *, requested_by: str = "",
                     requester_name: str = "") -> None:
        """Queue the rest of a playlist without making anyone wait for it.

        The first track is already playing; each of these is resolved in
        turn and appended. A second playlist replaces an import still in
        progress rather than interleaving with it.
        """
        self.cancel_import(guild_id)
        self._imports[str(guild_id)] = asyncio.create_task(self._import(
            str(guild_id), channel_id, list(queries),
            requested_by=requested_by, requester_name=requester_name))

    def cancel_import(self, guild_id: str) -> None:
        task = self._imports.pop(str(guild_id), None)
        if task is not None and not task.done():
            task.cancel()

    async def _import(self, guild_id: str, channel_id: str,
                      queries: list[str], *, requested_by: str,
                      requester_name: str) -> None:
        added = failed = 0
        try:
            for query in queries:
                tracks, _err = await self.extractor.resolve(
                    query, requested_by=requested_by,
                    requester_name=requester_name)
                if not tracks:
                    failed += 1
                    continue
                async with self.lock(guild_id):
                    player = self.player(guild_id)
                    if player.playing or player.advancing:
                        if not player.add(tracks[0]):
                            logger.info("playlist import stopped: queue "
                                        "full in guild %s", guild_id)
                            break
                    else:
                        # The queue ran dry before the importer caught up
                        # (a short first track). Nothing will advance on
                        # its own, so start this one.
                        failure = await self.play_now(guild_id, channel_id,
                                                      tracks[0])
                        if failure:
                            failed += 1
                            continue
                added += 1
        except asyncio.CancelledError:
            logger.info("playlist import cancelled in guild %s after %d "
                        "track(s)", guild_id, added)
            raise
        finally:
            if self._imports.get(guild_id) is asyncio.current_task():
                self._imports.pop(guild_id, None)
        logger.info("playlist import done in guild %s: %d added, %d not "
                    "found", guild_id, added, failed)

    async def stop(self, guild_id: str) -> None:
        # Before taking the lock: the importer takes it per track, and a
        # stop must not be followed by the next playlist song starting.
        self.cancel_import(guild_id)
        async with self.lock(guild_id):
            player = self.player(guild_id)
            player.clear()
            player.current = None
            player.loop_track = player.loop_queue = False
            await self.voice.disconnect(guild_id)
