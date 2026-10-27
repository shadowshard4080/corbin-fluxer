"""Offline suite for the audio frame loop.

The SDK's ``_run_ffmpeg_loop`` reads with ``read(n)``, which returns *up
to* n bytes, and pads anything short to frame size with silence. Short
reads from a pipe are routine, so that injects zero-samples right
through the stream rather than only at the end -- audibly, a constant
crackle. These checks pin the corrected behaviour against a pipe that
dribbles bytes the way a real one does.
"""
from __future__ import annotations
import asyncio, logging, struct
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

FRAME = 960 * 2 * 2          # 20 ms, stereo, s16le


def ramp(frames: int) -> bytes:
    """Audio with no silence in it, so injected zeros are detectable."""
    return b"".join(struct.pack("<h", ((i % 997) + 1) * 30 % 30000)
                    for i in range(960 * 2 * frames))


class DribblingPipe:
    """A pipe that hands back awkward sizes, like ffmpeg's stdout."""

    def __init__(self, data: bytes, sizes: list[int]) -> None:
        self.data, self.sizes, self.pos = data, list(sizes), 0

    def _take(self, limit: int) -> bytes:
        size = self.sizes.pop(0) if self.sizes else limit
        out = self.data[self.pos:self.pos + min(limit, size)]
        self.pos += len(out)
        return out

    async def read(self, n: int) -> bytes:
        return self._take(n)

    async def readexactly(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            piece = self._take(n - len(out))
            if not piece:
                raise asyncio.IncompleteReadError(out, n)
            out += piece
        return out


def silent_samples(data: bytes) -> int:
    return sum(1 for i in range(0, len(data) - 1, 2)
               if data[i:i + 2] == b"\x00\x00")


async def _sdk_style(pipe: DribblingPipe) -> bytes:
    """What the SDK does today."""
    out = b""
    while True:
        data = await pipe.read(FRAME)
        if not data:
            break
        if len(data) < FRAME:
            data += b"\x00" * (FRAME - len(data))
        out += data
    return out


async def _corrected(pipe: DribblingPipe) -> bytes:
    """What the replacement does: only EOF may be short."""
    out = b""
    while True:
        try:
            data = await pipe.readexactly(FRAME)
        except asyncio.IncompleteReadError as exc:
            data = exc.partial
            if not data:
                break
            out += data + b"\x00" * (FRAME - len(data))
            break
        out += data
    return out


def suite_short_reads(r: Results) -> None:
    audio = ramp(5)
    sizes = [700, 3840, 100, 3840, 2000, 3840, 3840, 3840]

    sdk = asyncio.run(_sdk_style(DribblingPipe(audio, list(sizes))))
    ours = asyncio.run(_corrected(DribblingPipe(audio, list(sizes))))

    r.ok("the SDK's loop corrupts the stream", sdk[:len(audio)] != audio)
    r.ok("...by injecting silence mid-stream",
         silent_samples(sdk) > silent_samples(audio) + 100)
    r.ok("...and stretching it longer than the source", len(sdk) > len(audio))

    r.check("the corrected loop preserves the audio byte for byte",
            ours[:len(audio)], audio)
    r.check("...adding no silence of its own",
            silent_samples(ours), silent_samples(audio))
    r.check("...and not changing its length", len(ours), len(audio))


def suite_clean_reads(r: Results) -> None:
    """With frame-aligned reads both agree — the bug needs short reads."""
    audio = ramp(4)
    sizes = [FRAME] * 4
    sdk = asyncio.run(_sdk_style(DribblingPipe(audio, list(sizes))))
    ours = asyncio.run(_corrected(DribblingPipe(audio, list(sizes))))
    r.check("identical when every read is a whole frame", sdk, ours)
    r.check("...and equal to the source", ours, audio)


def suite_tail(r: Results) -> None:
    """A real partial frame at EOF still gets padded and sent."""
    audio = ramp(2) + b"\x11\x22" * 100      # 2 frames + a stub
    ours = asyncio.run(_corrected(DribblingPipe(audio, [FRAME, FRAME, 200])))
    r.check("the tail is padded up to a whole frame",
            len(ours) % FRAME, 0)
    r.check("...and nothing is dropped", len(ours), 3 * FRAME)
    r.ok("the real samples survive", ours.startswith(audio))

    exact = ramp(2)
    r.check("audio ending on a frame boundary gains no padding frame",
            len(asyncio.run(_corrected(DribblingPipe(exact, [FRAME, FRAME])))),
            2 * FRAME)


class _FakeGateway:
    def __init__(self, hang: bool = False) -> None:
        self.hang, self.left = hang, []

    async def update_voice_state(self, *, guild_id, channel_id=None):
        if self.hang:
            await asyncio.Event().wait()
        self.left.append((guild_id, channel_id))


class _FakeRoom:
    def __init__(self, hang: bool) -> None:
        self.hang, self.closed = hang, False

    async def disconnect(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        self.closed = True


class _FakeVoiceClient:
    """The SDK client's shape, with steps that can wedge like LiveKit."""

    def __init__(self, *, hang_stop=False, hang_room=False,
                 hang_leave=False) -> None:
        self.hang_stop = hang_stop
        self._gateway = _FakeGateway(hang_leave)
        self._room = _FakeRoom(hang_room)
        self._connected = asyncio.Event()
        self._connected.set()
        self.is_playing = True

    async def stop(self) -> None:
        if self.hang_stop:
            await asyncio.Event().wait()
        self.is_playing = False

    async def disconnect(self) -> None:
        raise AssertionError("must not call the SDK's unbounded disconnect")


def _teardown(client: _FakeVoiceClient):
    from corbin.core import voice as voice_mod
    from corbin.core.voice import Session, VoiceManager

    voice_mod.TEARDOWN_STEP_TIMEOUT = 0.05
    vm = VoiceManager(bot=None)
    vm._sessions["1"] = Session(guild_id="1", channel_id="2", client=client)

    async def go():
        return await asyncio.wait_for(vm.disconnect("1"), timeout=5)

    return vm, asyncio.run(go())


def suite_teardown(r: Results) -> None:
    """Leaving voice can't hang, and a hung stop still leaves."""
    from corbin.core import voice as voice_mod
    saved = voice_mod.TEARDOWN_STEP_TIMEOUT
    try:
        c = _FakeVoiceClient()
        vm, ok = _teardown(c)
        r.ok("a clean disconnect reports a session", ok)
        r.check("...sends the gateway leave", c._gateway.left, [("1", None)])
        r.ok("...closes the room", c._room is None)
        r.check("...with no failed steps", vm.teardown_failures("1"), ())

        # The reported bug: audio stopped, then the command never returned.
        c = _FakeVoiceClient(hang_stop=True, hang_room=True)
        vm, ok = _teardown(c)
        r.ok("hung stop + hung room still returns", ok)
        r.check("...and still sends the gateway leave",
                c._gateway.left, [("1", None)])
        r.check("...naming what hung", vm.teardown_failures("1"),
                ("stop playback", "close LiveKit room"))
        r.ok("...and the session is gone", vm.get("1") is None)
        r.ok("...and the client is marked disconnected",
             not c._connected.is_set())

        c = _FakeVoiceClient(hang_leave=True)
        vm, _ = _teardown(c)
        r.ok("a hung gateway leave is reported, not claimed as success",
             "leave voice channel" in vm.teardown_failures("1"))
    finally:
        voice_mod.TEARDOWN_STEP_TIMEOUT = saved


def _real_loop(feed_chunks: int, *, queue_ms=None):
    """Run voice.py's actual frame loop against a stub LiveKit and a fake
    ffmpeg whose stdout holds ``feed_chunks`` frames. Returns a harness
    to drive it."""
    import sys, types
    from corbin.core import voice as V

    captured = []

    class AudioSource:
        def __init__(self, rate, channels, queue_size_ms=None):
            self.queued_duration = 0.0
            self.queue_ms = queue_size_ms

        async def capture_frame(self, frame):
            captured.append(frame.data)
            await asyncio.sleep(0)

    class AudioFrame:
        def __init__(self, *, data, sample_rate, num_channels,
                     samples_per_channel):
            self.data = data

    rtc = types.ModuleType("livekit.rtc")
    rtc.AudioSource, rtc.AudioFrame = AudioSource, AudioFrame
    lk = types.ModuleType("livekit")
    lk.rtc = rtc
    saved = {k: sys.modules.get(k) for k in ("livekit", "livekit.rtc")}
    sys.modules["livekit"], sys.modules["livekit.rtc"] = lk, rtc

    class Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.returncode = None
            self.stdout.feed_data(ramp(feed_chunks))
            self.stdout.feed_eof()

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            return b"", b""

    class Client:
        _room = object()
        _current_publication = None

        def __init__(self):
            self._resume_event = asyncio.Event()
            self._resume_event.set()

        async def _publish_track(self, src):
            self.src = src

        async def _run_ffmpeg_loop(self, source):
            pass

    procs = []

    async def fake_exec(*a, **k):
        procs.append(Proc())
        return procs[-1]

    source = types.SimpleNamespace(executable="ffmpeg", before_options="",
                                   options="", path="u", num_channels=2,
                                   sample_rate=48000)
    if queue_ms:
        source.corbin_queue_ms = queue_ms
    client = Client()
    old_exec = V.asyncio.create_subprocess_exec
    V.asyncio.create_subprocess_exec = fake_exec

    def restore():
        V.asyncio.create_subprocess_exec = old_exec
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    ok = V.VoiceManager._apply_frame_loop(client)
    client.procs = procs
    return client, source, captured, ok, restore


def suite_read_ahead(r: Results) -> None:
    """Live 2026-10-20: "longest audio wait 4.94s, loop lag 3ms": the
    song's audio arrived unevenly from YouTube and Corbin read it only as
    fast as he played it. Now music reads ahead (AUDIO_READ_AHEAD), so a
    slow patch drains a buffer instead of being heard."""
    async def paused_then_resumed():
        client, source, captured, ok, restore = _real_loop(250)
        try:
            client._resume_event.clear()        # paused before it starts
            task = asyncio.ensure_future(client._run_ffmpeg_loop(source))
            await asyncio.sleep(0.2)
            drained = client.procs[0].stdout.at_eof()
            buffered_while_paused = len(captured), drained
            # Let it go: what's buffered plays out, then the end.
            client._resume_event.set()
            await asyncio.wait_for(task, 5)
            return ok, buffered_while_paused, captured
        finally:
            restore()
    ok, (played_while_paused, drained), captured = asyncio.run(
        paused_then_resumed())
    r.ok("the corrected loop is installed", ok)
    r.check("paused: nothing played", played_while_paused, 0)
    r.ok("...but the whole song was read into the buffer meanwhile, so "
         "playback never waits on the source", drained)
    r.check("resumed: every byte comes out, in order",
            b"".join(captured), ramp(250))

    async def tv_style():
        client, source, captured, ok, restore = _real_loop(10, queue_ms=300)
        try:
            await asyncio.wait_for(client._run_ffmpeg_loop(source), 5)
            return captured, client.src.queue_ms
        finally:
            restore()
    captured, qms = asyncio.run(tv_style())
    r.ok("TV / the video library (a short LiveKit queue) are left as they "
         "were: no read-ahead, their ffmpeg also feeds the picture",
         qms == 300 and b"".join(captured) == ramp(10))


SUITES = [
    ("audio: reads ahead of playback", suite_read_ahead),
    ("audio frame short reads", suite_short_reads),
    ("audio frame aligned reads", suite_clean_reads),
    ("audio frame end of stream", suite_tail),
    ("voice teardown", suite_teardown),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
