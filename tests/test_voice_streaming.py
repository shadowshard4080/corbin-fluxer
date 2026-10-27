"""Offline suite for A4: speaking each chunk as it's made, barge-in, and
the per-turn timing. LiveKit is stubbed; no VoiceStudio, no ffmpeg."""
from __future__ import annotations
import asyncio, logging, struct, sys, time, types
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run


def _stub_livekit() -> None:
    """Just enough of ``livekit.rtc`` for the listener's speech path."""
    if "livekit" in sys.modules:
        return
    rtc = types.ModuleType("livekit.rtc")

    class AudioFrame:
        def __init__(self, *, data, sample_rate, num_channels,
                     samples_per_channel):
            self.data = data

    rtc.AudioFrame = AudioFrame
    livekit = types.ModuleType("livekit")
    livekit.rtc = rtc
    sys.modules["livekit"] = livekit
    sys.modules["livekit.rtc"] = rtc


_stub_livekit()

from corbin.core import voicechat as vc  # noqa: E402
from corbin.core.voicechat import (BARGE_IN_AFTER, Listener,  # noqa: E402
                                   Segmenter)

RATE = vc.LISTEN_RATE
STEP = (RATE // 100) * 2


def tone(seconds: float, rate: int = RATE, level: int = 4000) -> bytes:
    return b"".join(struct.pack("<h", level if i % 2 else -level)
                    for i in range(int(rate * seconds)))


class Source:
    """A LiveKit AudioSource double that records what it was given."""

    def __init__(self, on_frame=None):
        self.frames = 0
        self.cleared = 0
        self.on_frame = on_frame

    async def capture_frame(self, frame):
        self.frames += 1
        if self.on_frame is not None:
            self.on_frame(self.frames)
        await asyncio.sleep(0)

    def clear_queue(self):
        self.cleared += 1

    async def wait_for_playout(self):
        await asyncio.sleep(0)


class Client:
    is_paused = False

    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")


class Session:
    guild_id, channel_id, channel_name = "g1", "vc1", "Lounge"

    def __init__(self, playing=False):
        self.playing = playing
        self.client = Client()


def _listener(session=None, **kw):
    posted = []

    async def post(cid, body):
        posted.append(body)

    listener = Listener(None, session or Session(), bot_name="Corbin",
                        reply=None, post=post, **kw)
    listener._roll = lambda: 0.99     # no habit caws
    return listener, posted


#: Three sentences long enough to be three TTS chunks.
LONG = ("The first sentence is long enough to be its own chunk. "
        "The second one is also comfortably over thirty characters. "
        "And the third closes the answer off nicely.")


def suite_streaming(r: Results) -> None:
    listener, _ = _listener()
    events = []
    second_done = asyncio.Event()

    async def speech(text):
        if text.startswith("The first"):
            events.append("made 1")
            return tone(0.1, vc.SPEAK_RATE)
        # The rest are slow: the first must not wait for them.
        await asyncio.sleep(0.05)
        events.append("made rest")
        second_done.set()
        return tone(0.1, vc.SPEAK_RATE)

    listener._speech_pcm = speech
    source = Source(on_frame=lambda n: n == 1 and events.append("played"))
    first = asyncio.run(listener._stream(source, LONG, None))
    r.ok("the first chunk plays before the rest are made",
         events.index("played") < events.index("made rest"))
    r.ok("first-audio time is reported", first is not None and first < 0.05)
    r.ok("everything was played", source.frames >= 30)
    r.check("the echo guard has the whole line", listener._said[-1][1], LONG)
    r.ok("not interrupted", not listener.last_interrupted)

    # Music pauses when he starts and resumes when he's done.
    session = Session(playing=True)
    listener, _ = _listener(session)

    async def quick(text):
        return tone(0.05, vc.SPEAK_RATE)

    listener._speech_pcm = quick
    asyncio.run(listener._stream(Source(), "Short line, nothing more.", None))
    r.check("music paused under him and resumed", session.client.calls,
            ["pause", "resume"])

    # Failing before any audio: the caller posts the text instead.
    listener, posted = _listener()

    async def broken(text):
        raise RuntimeError("VoiceStudio HTTP 500")

    listener._speech_pcm = broken

    async def own_track():
        return Source()

    listener._speech_track = own_track
    got = asyncio.run(listener._say("Hello there, how are you?"))
    r.check("nothing spoken, nothing timed", got, None)
    r.check("so the line is posted", posted, ["Hello there, how are you?"])

    # Failing after the first chunk: he stops, and doesn't also post it.
    listener, posted = _listener()

    async def half(text):
        if text.startswith("The first"):
            return tone(0.05, vc.SPEAK_RATE)
        raise RuntimeError("VoiceStudio HTTP 500")

    listener._speech_pcm = half
    listener._speech_track = own_track
    got = asyncio.run(listener._say(LONG))
    r.ok("half a line still counts as spoken", got is not None)
    r.check("and isn't posted on top", posted, [])


def suite_barge_in(r: Results) -> None:
    listener, _ = _listener()
    made = []

    async def speech(text):
        made.append(text)
        return tone(0.2, vc.SPEAK_RATE)

    listener._speech_pcm = speech
    # Someone talks over him 5 frames in.
    source = Source(on_frame=lambda n: n == 5 and listener._interrupt.set())
    asyncio.run(listener._stream(source, LONG, "u1"))
    r.ok("cut off: stops early", source.frames < 15)
    r.ok("and drops what was queued", source.cleared >= 1)
    r.ok("the line is marked interrupted", listener.last_interrupted)
    r.check("nobody can cut off the next line yet",
            listener._speaking_to, None)

    # Off: he finishes the line even if the flag is raised.
    listener, _ = _listener(barge_in=False)
    listener._speech_pcm = speech
    asyncio.run(listener._stream(Source(), LONG, "u1"))
    r.check("barge-in off: nobody is listened for", listener._speaking_to,
            None)

    # Segmenter.voiced is what the reader watches.
    seg = Segmenter()
    pcm = tone(BARGE_IN_AFTER + 0.1)
    peak = 0.0
    for i in range(0, len(pcm), STEP):
        seg.feed(pcm[i:i + STEP])
        peak = max(peak, seg.voiced)
    r.ok(f"voiced grows while they talk ({peak:.2f}s)",
         peak >= BARGE_IN_AFTER)
    r.check("and is 0 between utterances", Segmenter().voiced, 0.0)

    # The reader: only the person being answered can cut him off.
    class Frame:
        def __init__(self, data):
            self.frame = types.SimpleNamespace(data=data)

    class Stream:
        def __init__(self, pcm):
            self.pcm = pcm

        def __aiter__(self):
            async def gen():
                for i in range(0, len(self.pcm), STEP):
                    yield Frame(self.pcm[i:i + STEP])
            return gen()

    def talk(who, answering):
        listener, _ = _listener()
        listener._speaking_to = answering
        asyncio.run(listener._read(Stream(tone(BARGE_IN_AFTER + 0.4)), who))
        return listener

    heard = talk("u1", "u1")
    r.ok("the person he's answering cuts him off",
         heard._interrupt.is_set())
    r.ok("and it's remembered for the echo check", "u1" in heard._barged)
    r.ok("someone else talking doesn't",
         not talk("u2", "u1")._interrupt.is_set())
    r.ok("nor anyone during a line nobody asked for",
         not talk("u1", None)._interrupt.is_set())

    short = _listener()[0]
    short._speaking_to = "u1"
    asyncio.run(short._read(Stream(tone(BARGE_IN_AFTER / 2)), "u1"))
    r.ok("an 'mm' is too short to count", not short._interrupt.is_set())
    r.check("it takes 1.2 s of talking over him", BARGE_IN_AFTER, 1.2)
    brief = _listener()[0]
    brief._speaking_to = "u1"
    asyncio.run(brief._read(Stream(tone(0.8)), "u1"))
    r.ok("a quick 'yeah, totally' doesn't stop him",
         not brief._interrupt.is_set())


def suite_barge_in_echo(r: Results) -> None:
    """His own voice through someone's speakers mustn't keep cutting him
    off: once an interruption turns out to be an echo, that person's mic
    can't interrupt again."""
    line = "That's a bigger question than it sounds, honestly."

    class Studio:
        async def transcribe(self, wav):
            return "That's a bigger question than it sounds"

    listener, _ = _listener()
    listener.studio = Studio()
    listener._said = [(time.monotonic(), line)]
    listener._barged["u1"] = time.monotonic()
    asyncio.run(listener._turn("u1", b""))
    r.ok("an echo right after a barge-in disables it for them",
         "u1" in listener._no_barge)

    async def speech(text):
        return tone(0.05, vc.SPEAK_RATE)

    listener._speech_pcm = speech
    seen = []
    source = Source(on_frame=lambda n: seen.append(listener._speaking_to))
    asyncio.run(listener._stream(source, "A reply that runs a little while.",
                                 "u1"))
    r.ok("and their speech no longer counts", set(seen) == {None})

    other, _ = _listener()
    other.studio = Studio()
    other._said = [(time.monotonic(), line)]
    asyncio.run(other._turn("u1", b""))
    r.ok("an ordinary echo leaves barge-in alone", not other._no_barge)


def suite_turn_log(r: Results) -> None:
    class Studio:
        async def transcribe(self, wav):
            return "what time is it"

    async def reply(*_a):
        return "Quarter past."

    listener = Listener(Studio(), Session(), bot_name="Corbin", reply=reply,
                        post=None)
    listener._room = types.SimpleNamespace(remote_participants={"1": 0})

    async def say(body, **kw):
        return 0.4

    listener.say = say
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger("corbin.voicechat")
    handler = Catch()
    log.addHandler(handler)
    old = log.level
    log.setLevel(logging.INFO)
    try:
        asyncio.run(listener._turn("u1", b"", heard_at=time.monotonic()))
    finally:
        log.removeHandler(handler)
        log.setLevel(old)
    line = next((m for m in records if m.startswith("voice turn:")), "")
    r.ok("a voice turn line is logged", bool(line))
    for part in ("stt=", "reply=", "first_audio=0.40s", "total=", "pause="):
        r.ok(f"it carries {part}", part in line)


SUITES = [
    ("voice streaming: chunk by chunk", suite_streaming),
    ("voice streaming: barge-in", suite_barge_in),
    ("voice streaming: barge-in echo", suite_barge_in_echo),
    ("voice streaming: turn timing", suite_turn_log),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
