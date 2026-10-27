"""Offline suite for streaming video (corbin/core/video.py): the voice
connection id, the self_stream flag, the one-stream rule and voice chat
giving way. LiveKit is stubbed; no Fluxer, no ffmpeg."""
from __future__ import annotations
import asyncio, logging, sys, types
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run


def _stub_livekit() -> None:
    """Add the video half of ``livekit.rtc`` to whatever stub (or real
    module) is loaded. test_voice_streaming may have stubbed audio only."""
    rtc = sys.modules.get("livekit.rtc")
    if rtc is None:
        try:
            from livekit import rtc  # noqa: F401  real one: leave it
            return
        except Exception:
            rtc = types.ModuleType("livekit.rtc")
            livekit = types.ModuleType("livekit")
            livekit.rtc = rtc
            sys.modules["livekit"] = livekit
            sys.modules["livekit.rtc"] = rtc
    if getattr(rtc, "_corbin_video_stub", False) or \
            hasattr(rtc, "VideoSource") and not hasattr(rtc, "_corbin_stub"):
        return

    class VideoSource:
        def __init__(self, w, h, *, is_screencast=False):
            self.size, self.screencast = (w, h), is_screencast
            self.frames, self.closed = 0, False

        def capture_frame(self, frame, *, timestamp_us=0):
            self.frames += 1

        async def aclose(self):
            self.closed = True

    class LocalVideoTrack:
        @staticmethod
        def create_video_track(name, source):
            return types.SimpleNamespace(name=name, source=source)

    class _Enc:
        max_bitrate = 0
        max_framerate = 0

    class TrackPublishOptions:
        def __init__(self, *, source=None, simulcast=True):
            self.source, self.simulcast = source, simulcast
            self.video_encoding = _Enc()

    rtc.VideoSource = VideoSource
    rtc.LocalVideoTrack = LocalVideoTrack
    rtc.TrackPublishOptions = TrackPublishOptions
    rtc.TrackSource = types.SimpleNamespace(SOURCE_SCREENSHARE="screenshare",
                                            SOURCE_CAMERA="camera")
    rtc.VideoBufferType = types.SimpleNamespace(I420="i420")
    rtc.VideoFrame = lambda *a, **k: a
    if not hasattr(rtc, "AudioFrame"):
        rtc.AudioFrame = lambda **k: k
    rtc._corbin_video_stub = True


_stub_livekit()

from corbin.core import video as vid  # noqa: E402

# Real waits are seconds long; the logic is the same at a fraction.
vid.CONFIRM_TIMEOUT, vid.SETTLE, vid.FLAG_ECHO_TIMEOUT = 0.3, 0.0, 0.3
from corbin.core.video import (ConnectionIds, StreamBusy,  # noqa: E402
                               StreamManager, stream_flags)

ME = "999"


class Participant:
    def __init__(self, refuse=False):
        self.published, self.unpublished = [], []
        self.refuse = refuse

    async def publish_track(self, track, options):
        if self.refuse:
            raise RuntimeError("not allowed")
        self.published.append((track, options))
        return types.SimpleNamespace(sid=f"TR_{len(self.published)}")

    async def unpublish_track(self, sid):
        self.unpublished.append(sid)


class Session:
    def __init__(self, gid, cid, refuse=False):
        self.guild_id, self.channel_id = gid, cid
        self.channel_name = f"vc-{cid}"
        self.client = types.SimpleNamespace(
            _room=types.SimpleNamespace(local_participant=Participant(
                refuse)))
        self.closers = []


class Voice:
    def __init__(self, refuse=False):
        self._sessions = {}
        self.refuse = refuse
        #: (gid, cid) -> None, called on a fresh join (Fluxer confirming).
        self.on_join = None
        self.joins = 0

    def get(self, gid):
        return self._sessions.get(gid)

    async def connect(self, gid, cid):
        s = self._sessions.get(gid)
        if s is not None and s.channel_id == cid:
            return s
        self._sessions[gid] = Session(gid, cid, self.refuse)
        self.joins += 1
        if self.on_join is not None:
            self.on_join(gid, cid)
        return self._sessions[gid]


def _world(*, allowed=True, strip=False, echo=True, refuse=False,
           connection=True, confirm=True, stale=False):
    """A StreamManager with a fake gateway that echoes voice states."""
    ids = ConnectionIds(lambda: ME)
    sent = []

    async def send(body):
        sent.append(body)
        if stale:
            # A late update from before (the join, or the last stop).
            ids.observe("VOICE_STATE_UPDATE", {
                **body, "user_id": ME, "self_stream": False})
        if echo:
            asyncio.get_running_loop().call_later(0.02, ids.observe,
                "VOICE_STATE_UPDATE", {
                    **body, "user_id": ME,
                    "self_stream": body["self_stream"] and not strip})

    if connection:
        for gid in ("g1", "g2"):
            ids.observe("VOICE_SERVER_UPDATE",
                        {"guild_id": gid, "connection_id": f"conn-{gid}",
                         "endpoint": "e"})
    voice = Voice(refuse)
    if confirm and connection:
        voice.on_join = lambda gid, cid: ids.observe(
            "VOICE_STATE_UPDATE", {"guild_id": gid, "channel_id": cid,
                                   "user_id": ME,
                                   "connection_id": f"conn-{gid}"})
    mgr = StreamManager(voice, ids, send_voice_state=send,
                        allowed=lambda g: allowed)
    return mgr, voice, ids, sent


async def frames(n=None):
    i = 0
    while n is None or i < n:
        i += 1
        yield b"\0" * 10
        await asyncio.sleep(0)


def suite_ids(r: Results) -> None:
    ids = ConnectionIds(lambda: ME)
    ids.observe("VOICE_SERVER_UPDATE", {"guild_id": "g", "endpoint": "e",
                                        "connection_id": "c1"})
    r.check("caught from VOICE_SERVER_UPDATE", ids.get("g"), "c1")
    ids.observe("VOICE_STATE_UPDATE", {"guild_id": "g", "user_id": "5",
                                       "channel_id": "v",
                                       "connection_id": "theirs"})
    r.check("someone else's state is ignored", ids.get("g"), "c1")
    ids.observe("VOICE_STATE_UPDATE", {"guild_id": "g", "user_id": ME,
                                       "channel_id": "v",
                                       "connection_id": "c2",
                                       "self_stream": True})
    r.check("our own state updates it", ids.get("g"), "c2")
    r.ok("and its flags are kept", ids.state("g").get("self_stream"))
    ids.observe("VOICE_STATE_UPDATE", {"guild_id": "g", "user_id": ME,
                                       "channel_id": None})
    r.check("leaving forgets the id", ids.get("g"), None)
    ids.observe("VOICE_STATE_UPDATE", "not a dict")
    r.ok("junk is ignored", True)

    body = stream_flags("g", "v", "c9", stream=True)
    r.check("the op 4 carries the connection id", body["connection_id"],
            "c9")
    r.ok("and sets self_stream only",
         body["self_stream"] and not body["self_video"])


def suite_start_stop(r: Results) -> None:
    async def main():
        mgr, voice, ids, sent = _world()
        stream, ended = await mgr.start("g1", "v1", frames(), label="t")
        session = voice._sessions["g1"]
        track, opts = session.client._room.local_participant.published[0]
        r.check("published as a screen share", opts.source, "screenshare")
        r.ok("simulcast off", opts.simulcast is False)
        r.check("at 3 Mbit/s", opts.video_encoding.max_bitrate, vid.BITRATE)
        r.check("720p", track.source.size, (1280, 720))
        r.ok("as a screencast", track.source.screencast)
        r.check("flag sent on our connection", sent[-1]["connection_id"],
                "conn-g1")
        r.ok("with self_stream on", sent[-1]["self_stream"])
        r.check("where comes from the session", stream.where, "vc-v1")
        r.check("nothing ended", ended, [])
        await asyncio.sleep(0.1)
        r.ok("frames flow", stream.frames > 0)
        r.check("streaming_in reports it", mgr.streaming_in(), "vc-v1")

        await mgr.stop("test")
        r.ok("stopped", mgr.active is None)
        r.ok("flag cleared", sent[-1]["self_stream"] is False)
        r.check("track unpublished",
                session.client._room.local_participant.unpublished, ["TR_1"])
        r.ok("source closed", track.source.closed)
        r.check("nothing to report", mgr.streaming_in(), None)
    asyncio.run(main())


def suite_rules(r: Results) -> None:
    async def main():
        mgr, voice, ids, sent = _world()
        await mgr.start("g1", "v1", frames(), label="t")
        try:
            await mgr.start("g2", "v9", frames(), label="t")
            r.ok("a second server is refused", False)
        except StreamBusy as exc:
            r.ok("a second server is refused", True)
            r.ok("and told where", "vc-v1" in str(exc))
        r.check("the first stream is untouched", mgr.active.guild_id, "g1")

        first = mgr.active
        await mgr.start("g1", "v2", frames(), label="t2")
        r.check("same server: the new one replaces it", mgr.active.label,
                "t2")
        r.ok("the old track came down", voice._sessions["g1"]
             .client._room.local_participant.published)
        # The old stream's closer must be a no-op now (deadlock guard).
        await mgr._stop_if_current(first, "left voice")
        r.check("a replaced stream's closer leaves the new one",
                mgr.active.label, "t2")
        # Leaving voice by any route ends the current one.
        for closer in voice._sessions["g1"].closers:
            await closer()
        r.ok("leaving voice ends the stream", mgr.active is None)

        off, *_ = _world(allowed=False)
        try:
            await off.start("g1", "v1", frames(), label="t")
            r.ok("not on the allowlist: refused", False)
        except PermissionError:
            r.ok("not on the allowlist: refused", True)
    asyncio.run(main())


def suite_failures(r: Results) -> None:
    async def main():
        mgr, voice, ids, sent = _world(connection=False)
        try:
            await mgr.start("g1", "v1", frames(), label="t")
            r.ok("no connection id: refused", False)
        except RuntimeError as exc:
            r.ok("no connection id: refused", "id" in str(exc))
        r.ok("and nothing left running", mgr.active is None)
        r.check("no op 4 sent without an id (it'd be a new join)", sent, [])

        mgr, voice, ids, sent = _world(strip=True)
        try:
            await mgr.start("g1", "v1", frames(), label="t")
            r.ok("flag stripped by Fluxer: reported", False)
        except RuntimeError as exc:
            r.ok("flag stripped by Fluxer: reported", "flag" in str(exc))
        r.ok("and torn down", mgr.active is None)

        # The first live start: a stale "self_stream false" arrived first
        # and was read as Fluxer refusing.
        mgr, *_ = _world(stale=True)
        await mgr.start("g1", "v1", frames(), label="t")
        r.ok("a stale echo isn't a refusal", mgr.active is not None)
        await mgr.stop()

        # A fresh join waits for Fluxer to confirm the connection.
        mgr, voice, ids, sent = _world(confirm=False)
        import time as _t
        began = _t.monotonic()
        await mgr.start("g1", "v1", frames(), label="t")
        r.ok("unconfirmed: waits out the timeout, then streams anyway",
             _t.monotonic() - began >= vid.CONFIRM_TIMEOUT * 0.9
             and mgr.active is not None)
        await mgr.stop()
        mgr, voice, ids, sent = _world()
        began = _t.monotonic()
        await mgr.start("g1", "v1", frames(), label="t")
        r.ok("confirmed: no wait", _t.monotonic() - began
             < vid.CONFIRM_TIMEOUT)
        session = voice._sessions["g1"]
        before = voice.joins
        await mgr.stop()
        await mgr.start("g1", "v1", frames(), label="t2")
        r.check("restarting in the same channel doesn't rejoin",
                voice.joins, before)
        await mgr.stop()

        mgr, *_ = _world(refuse=True)
        try:
            await mgr.start("g1", "v1", frames(), label="t")
            r.ok("publish refused: reported", False)
        except RuntimeError as exc:
            r.ok("publish refused: reported", "refused" in str(exc))

        mgr, *_ = _world()
        told = []

        async def ended(stream, reason):
            told.append(reason)

        mgr.on_ended = ended
        await mgr.start("g1", "v1", frames(3), label="short")
        for _ in range(50):
            if told:
                break
            await asyncio.sleep(0.02)
        r.check("a video that runs out ends the stream", mgr.active, None)
        r.check("and the channel is told why", told, ["the video ended"])
    asyncio.run(main())


def suite_voice_chat(r: Results) -> None:
    from corbin.core.voicechat import VoiceChat

    class Listener:
        def __init__(self):
            self.said = []

        async def say(self, text, **kw):
            self.said.append(text)

    class Chat:
        def __init__(self):
            self.listening = {"g2": Listener()}
            self.stopped = []

        def active(self, gid):
            return gid in self.listening

        def listener(self, gid):
            return self.listening.get(gid)

        async def stop(self, gid):
            self.stopped.append(gid)
            self.listening.pop(gid, None)
            return True

    async def main():
        mgr, voice, *_ = _world()
        voice._sessions["g2"] = Session("g2", "talk")
        chat = Chat()
        listener = chat.listening["g2"]
        mgr.voicechat = chat
        stream, ended = await mgr.start("g1", "v1", frames(), label="t")
        r.check("voice chat elsewhere is ended", ended, ["g2"])
        r.ok("after saying why", listener.said and
             "stream" in listener.said[0])
        await mgr.stop()

    asyncio.run(main())

    class Studio:
        available = True

        async def probe(self):
            return True

    real = VoiceChat(Voice(), Studio(), bot_name="Corbin", reply=None,
                     post=None, allowed=lambda g: True)
    real.streaming_in = lambda: "Lounge"
    try:
        asyncio.run(real.start("g1", "v1"))
        r.ok("voice chat refuses while streaming", False)
    except PermissionError as exc:
        r.ok("voice chat refuses while streaming", "streaming" in str(exc))
        r.ok("and names where", "Lounge" in str(exc))


SUITES = [
    ("video: connection id", suite_ids),
    ("video: start and stop", suite_start_stop),
    ("video: one stream, servers", suite_rules),
    ("video: failures", suite_failures),
    ("video: voice chat gives way", suite_voice_chat),
]



def suite_leaving(r: Results) -> None:
    """Live 2026-10-17: after "stop the stream" he sat in the channel,
    and "leave the vc" was routed to stop_voice_chat, which said "I'm not
    listening in voice here." and stayed."""
    from corbin.commands.stream_cmds import stop_stream
    from corbin.commands.voicechat_cmds import stop_voice_chat

    class VoiceMgr:
        def __init__(self, playing=False):
            self.session = types.SimpleNamespace(playing=playing,
                                                 channel_id="v1")
            self.left = False

        def get(self, gid):
            return None if self.left else self.session

        async def disconnect(self, gid):
            was = not self.left
            self.left = True
            return was

        def teardown_failures(self, gid):
            return ()

    class Chat:
        def active(self, gid):
            return False

        def listener(self, gid):
            return None

        async def stop(self, gid):
            return False

    def ctx(**extras):
        return types.SimpleNamespace(guild_id="g1", extras=extras,
                                     invoker=types.SimpleNamespace(id="u"))

    async def main():
        mgr, *_ = _world()
        await mgr.start("g1", "v1", frames(), label="t")
        voice = VoiceMgr()
        reply = await stop_stream(ctx(streams=mgr, voice=voice,
                                      voicechat=Chat()))
        r.ok("stopping the stream leaves an otherwise idle channel",
             voice.left and "left" in reply)

        mgr, *_ = _world()
        await mgr.start("g1", "v1", frames(), label="t")
        busy = VoiceMgr(playing=True)
        await stop_stream(ctx(streams=mgr, voice=busy, voicechat=Chat()))
        r.ok("but stays while music plays", not busy.left)

        voice = VoiceMgr()
        reply = await stop_voice_chat(ctx(voice=voice, voicechat=Chat()))
        r.ok("'leave the vc' via stop_voice_chat leaves when not listening",
             voice.left and "left" in reply.lower())
        nobody = VoiceMgr()
        nobody.left = True
        reply = await stop_voice_chat(ctx(voice=nobody, voicechat=Chat()))
        r.check("and outside voice it says so", reply,
                "I'm not listening in voice here.")

    asyncio.run(main())


SUITES.append(("video: leaving after a stream", suite_leaving))



def suite_quality_and_cards(r: Results) -> None:
    """T4's 1080p and V3's title card, through the real stream manager."""
    async def main():
        mgr, voice, ids, sent = _world()
        stream, _ = await mgr.start("g1", "v1", frames(), label="t",
                                    quality=vid.QUALITIES["1080p"])
        track, opts = voice._sessions["g1"].client._room.local_participant \
            .published[0]
        r.check("1080p: the source and the bitrate follow the quality",
                (track.source.size, opts.video_encoding.max_bitrate),
                ((1920, 1080), vid.QUALITIES["1080p"].bitrate))
        await mgr.stop("t")

        # A slow first frame: cards until it arrives, then the real ones.
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            for _ in range(3):
                yield b"real"
                await asyncio.sleep(0)

        live = []

        async def on_live(s):
            live.append(s.frames)
        mgr, voice, *_ = _world()
        stream, _ = await mgr.start("g1", "v1",
                                    vid.with_card(slow(), b"card"),
                                    label="t", on_live=on_live)
        await asyncio.sleep(0.15)
        r.ok("the card fills the wait", stream.frames > 1 and not live)
        gate.set()
        await asyncio.sleep(0.2)
        r.ok("on_live fires on the first REAL frame, after the cards",
             len(live) == 1 and live[0] > 1)

        async def late():
            mgr2, *_ = _world()
            st, _ = await mgr2.start("g1", "v1", frames(), label="t")
            ok = await mgr2.swap("g1", frames(), clock=lambda: 1.0,
                                 on_frames_end=None)
            try:
                await mgr2.swap("g1", frames(), nonsense=1)
                bad = False
            except TypeError:
                bad = True
            return st, ok, bad
        st, ok, bad = await late()
        r.ok("swap can replace the clock (TV switching channels)",
             ok and st.clock() == 1.0)
        r.ok("...and refuses a hook it doesn't know", bad)

    asyncio.run(main())

    async def passthrough():
        async def two():
            yield b"a"
            yield b"b"
        return [x async for x in vid.with_card(two(), None)]
    r.check("no card: frames pass straight through",
            asyncio.run(passthrough()), [b"a", b"b"])

    async def empty():
        async def none():
            return
            yield
        return [x async for x in vid.with_card(none(), b"c")]
    r.ok("frames that end before starting: no endless cards",
         all(isinstance(x, vid.CardFrame) for x in asyncio.run(empty())))
    q = vid.QUALITIES["720p"]
    r.check("a card is exactly one frame at the size",
            len(vid.title_card("BBC News", "Tuning in…", q)), q.frame_bytes)
    r.check("...at 1080p too", len(vid.title_card(
        "x" * 300, "y" * 300, vid.QUALITIES["1080p"])),
        vid.QUALITIES["1080p"].frame_bytes)
    r.check("unknown quality names fall back to 720p",
            vid.quality("4k").name, "720p")


SUITES.append(("video: quality and title cards", suite_quality_and_cards))

def suite_reanchor(r: Results) -> None:
    """ActiveStream.max_lead: a frame too far ahead of the clock is shown
    now, and the offset sticks for the frames after it."""
    st = vid.ActiveStream(guild_id="g", channel_id="c", label="t",
                          where="w", max_lead=0.5, clock=lambda: 10.0)
    r.check("within the lead: untouched",
            vid.StreamManager._reanchor(st, 10.4), 10.4)
    r.check("too far ahead: moved to now",
            vid.StreamManager._reanchor(st, 11.0), 10.0)
    r.ok("...the offset sticks and it's counted",
         st.pts_offset == -1.0 and st.resyncs == 1)
    music = vid.ActiveStream(guild_id="g", channel_id="c", label="t",
                             where="w", clock=lambda: 10.0)
    r.check("no max_lead (music video starts ahead on purpose): waits",
            vid.StreamManager._reanchor(music, 15.0), 15.0)
    idle = vid.ActiveStream(guild_id="g", channel_id="c", label="t",
                            where="w", max_lead=0.5, clock=lambda: None)
    r.check("no clock yet: untouched",
            vid.StreamManager._reanchor(idle, 15.0), 15.0)

    async def main():
        mgr, voice, *_ = _world()

        import time as _t
        t0 = _t.monotonic()

        def sound():               # a sound clock playing in real time
            return _t.monotonic() - t0

        async def ahead():
            # The picture 2 s ahead of the sound, as after a few ad breaks.
            for n in range(10):
                yield 2.0 + n / 30, b"\0" * 10
                await asyncio.sleep(0)
        stream, _ = await mgr.start("g1", "v1", ahead(), label="tv",
                                    kind="tv", clock=sound,
                                    hold=True, max_lead=0.5)
        await asyncio.sleep(0.6)
        shown, resyncs = stream.frames, stream.resyncs
        ok = await mgr.swap("g1", ahead(), clock=lambda: 0.0, max_lead=0.5)
        offset_after_swap = stream.pts_offset
        await mgr.stop("t")
        return shown, resyncs, ok, offset_after_swap
    shown, resyncs, ok, offset = asyncio.run(main())
    r.ok("a picture 2 s ahead re-anchors once and keeps going, instead "
         "of waiting 2 s (and starving the sound)",
         shown == 10 and resyncs == 1)
    r.ok("swap takes the new clock's settings and starts the offset over",
         ok and offset == 0.0)

    from corbin.core.voice import _note_dry, times_dry

    class Src:
        queued_duration = 0.3
    clock, src = {"rate": 48000, "captured": 0, "dry": 0}, Src()
    _note_dry(clock, src)
    src.queued_duration = 0.0
    _note_dry(clock, src)
    r.check("an empty queue in the first second isn't a gap", clock["dry"], 0)
    clock["captured"] = 96000
    for q in (0.0, 0.0, 0.3, 0.0):
        src.queued_duration = q
        _note_dry(clock, src)
    r.check("counted as gaps (transitions), not frames", clock["dry"], 2)
    import types
    r.check("times_dry reads it off the client",
            times_dry(types.SimpleNamespace(_corbin_audio_clock=clock)), 2)
    r.check("...None with no track",
            times_dry(types.SimpleNamespace()), None)


SUITES.append(("video: re-anchoring and sound gaps", suite_reanchor))

def suite_stats_line(r: Results) -> None:
    """The stats line carries the clock's real speed (is the sound the
    picture follows stalling?) without clobbering the Mbit/s figure."""
    import logging as _logging

    lines = []

    class Grab(_logging.Handler):
        def emit(self, rec):
            if "stream stats" in rec.getMessage():
                lines.append(rec.getMessage())

    log = _logging.getLogger("corbin.video")
    handler = Grab()
    log.addHandler(handler)
    old_level = log.level
    log.setLevel(_logging.INFO)
    saved = (vid.STATS_FIRST, vid.STATS_EVERY, vid.outbound_layers)
    sent = [0]

    async def layers(track):
        sent[0] += 1_000_000
        return [{"frame_width": 1920, "frame_height": 1080,
                 "frames_per_second": 30, "bytes_sent": sent[0],
                 "quality_limitation_reason": "LIMITATION_NONE"}]
    vid.STATS_FIRST, vid.STATS_EVERY, vid.outbound_layers = 0.05, 0.1, layers
    try:
        async def main():
            mgr, *_ = _world()
            t0 = __import__("time").monotonic()
            half = lambda: (__import__("time").monotonic() - t0) * 0.5  # noqa

            async def frames():
                while True:
                    yield 0.0, b"\0" * 10
                    await asyncio.sleep(0.01)
            st, _ = await mgr.start("g1", "v1", frames(), label="m",
                                    kind="music", clock=half, hold=True,
                                    extra_stats=lambda: ", sound ran dry 3x")
            await asyncio.sleep(0.4)
            await mgr.stop("t")
        asyncio.run(main())
    finally:
        vid.STATS_FIRST, vid.STATS_EVERY, vid.outbound_layers = saved
        log.removeHandler(handler)
        log.setLevel(old_level)
    last = lines[-1] if lines else ""
    r.ok("a clock running at half speed shows as ~0.50x",
         any(f"clock 0.{d}" in last for d in ("45", "46", "47", "48", "49",
                                               "50", "51", "52", "53",
                                               "54", "55")))
    r.ok("...the Mbit/s figure is intact", "Mbit/s" in last)
    r.ok("...and the extra stats follow", last.endswith(
        ", sound ran dry 3x") or ", sound ran dry 3x" in last)


SUITES.append(("video: stats line diagnostics", suite_stats_line))

def suite_timer(r: Results) -> None:
    """The small "elapsed / total" timer (drawtext). The escaping was
    checked against real ffmpeg; these pin its shape and its off switches,
    since a broken filter would stop every stream starting."""
    q = vid.QUALITIES["1080p"]
    saved = vid.TIMER, vid._TIMER_OK
    try:
        vid.TIMER, vid._TIMER_OK = True, True
        f = vid.timer_filter(q, start=754.0, total=1402.6)
        r.ok("elapsed from the frames' own time, offset by the seek",
             r"%{pts\:gmtime\:754.000\:%M\\\:%S}" in f)
        r.ok("the total, colon escaped", r" / 23\:22'" in f)
        r.ok("small, bottom right, in a faint box", "fontsize=20" in f
             and "x=w-tw-15" in f and "y=h-th-15" in f
             and "boxcolor=black@0.35" in f)
        long = vid.timer_filter(q, start=0, total=7322)
        r.ok("over an hour: hours on both sides", r"%H\\\:%M\\\:%S" in long
             and r" / 02\:02\:02'" in long)
        r.ok("no total (live): just the time", " / " not in
             vid.timer_filter(q, start=0, total=None))
        r.check("14 px at 720p", "fontsize=14" in vid.timer_filter(
            vid.QUALITIES["720p"], total=60), True)
        vid._TIMER_OK = False
        r.check("drawtext failed its self-test here: no timer, never a "
                "broken stream", vid.timer_filter(q, total=60), "")
        vid.TIMER, vid._TIMER_OK = False, True
        r.check("CORBIN_STREAM_TIMER=0: none", vid.timer_filter(q, total=60),
                "")
        vid.TIMER, vid._TIMER_OK = True, True
        from corbin.core.music_video import video_args
        mv = video_args("u", {}, 30.0, q, total=200.0)
        r.ok("music video carries it, before the pixel format",
             "drawtext=" in mv[mv.index("-vf") + 1]
             and mv[mv.index("-vf") + 1].endswith("format=yuv420p"))
        from corbin.core import video_player as VP
        lv = VP.av_args("/v/x.mkv", start=10, fifo=None, q=q, audio=None,
                        sub=None, total=1400.0)
        r.ok("so do local videos", "drawtext=" in lv[lv.index("-vf") + 1])
        from corbin.core import tv as TV
        tv = TV.av_args("u", __import__("corbin.core.iptv", fromlist=["x"])
                        .Stream(url="u", channel="c"), fifo=None)
        r.ok("live TV doesn't (no total, no position)",
             "drawtext" not in " ".join(tv))
    finally:
        vid.TIMER, vid._TIMER_OK = saved


SUITES.append(("video: the on-screen timer", suite_timer))

def suite_loopwatch(r: Results) -> None:
    """A real one-second block of the event loop is caught while it's
    happening, and the warning names the code doing it."""
    import logging as _logging
    import time as _t
    from corbin.core import loopwatch as LW

    lines = []

    class Grab(_logging.Handler):
        def emit(self, rec):
            lines.append(rec.getMessage())

    log = _logging.getLogger("corbin.loopwatch")
    h = Grab()
    log.addHandler(h)
    old_level = log.level
    log.setLevel(_logging.WARNING)
    w = LW.LoopWatch()

    def a_slow_blocking_helper():
        _t.sleep(1.1)                 # what a careless sync call does

    async def main():
        w.start()
        await asyncio.sleep(0.25)
        a_slow_blocking_helper()
        await asyncio.sleep(0.25)
        worst = w.worst()
        calm = w.worst()
        w.stop()
        return worst, calm
    try:
        worst, calm = asyncio.run(main())
    finally:
        log.removeHandler(h)
        log.setLevel(old_level)
    r.ok("the stall is measured", worst >= 0.9)
    r.ok("...and reset after it's read", calm < 0.2)
    r.ok("the warning names the function holding the loop",
         lines and "event loop blocked" in lines[0]
         and "a_slow_blocking_helper" in lines[0])
    r.check("one warning per stall", w.stalls, 1)


SUITES.append(("video: event-loop stall watch", suite_loopwatch))


if __name__ == "__main__":
    raise SystemExit(run(SUITES))
