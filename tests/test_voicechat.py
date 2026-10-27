"""Offline suite for voice chat: segmenting, addressing, the VoiceStudio
check, and the listener's turn logic. No LiveKit, no VoiceStudio."""
from __future__ import annotations
import asyncio, io, logging, struct, wave
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import voicechat as vc
from corbin.core.voicechat import (Listener, Segmenter, StudioConfig,
                                   VoiceStudio, addressed, pcm_to_wav,
                                   says_name, speakable,
                                   user_id_from_identity)

RATE = vc.LISTEN_RATE
CHUNK = RATE // 100          # 10 ms, what LiveKit hands over


def tone(seconds: float, level: int = 4000) -> bytes:
    n = int(RATE * seconds)
    return b"".join(struct.pack("<h", level if i % 2 else -level)
                    for i in range(n))


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(RATE * seconds)


def feed_all(seg: Segmenter, pcm: bytes) -> list[bytes]:
    out = []
    step = CHUNK * 2
    for i in range(0, len(pcm), step):
        got = seg.feed(pcm[i:i + step])
        if got is not None:
            out.append(got)
    return out


def test_segmenter(r: Results) -> None:
    r.check("silence alone yields nothing",
            feed_all(Segmenter(), silence(3)), [])

    end = silence(Segmenter.HANGOVER + 0.4)
    got = feed_all(Segmenter(), silence(1) + tone(1.5) + end)
    r.check("one phrase, one utterance", len(got), 1)
    secs = len(got[0]) / (RATE * 2)
    r.ok("utterance holds the speech plus pre-roll and the pause",
         1.5 <= secs <= 1.5 + Segmenter.PRE_ROLL + Segmenter.HANGOVER + 0.05)
    r.ok("pre-roll kept: utterance starts before the speech",
         got[0][:2] == b"\x00\x00")

    r.check("a click is not speech",
            feed_all(Segmenter(), tone(0.1) + end), [])

    got = feed_all(Segmenter(),
                   tone(1) + silence(0.3) + tone(1) + end)
    r.check("a short breath doesn't split a sentence", len(got), 1)

    got = feed_all(Segmenter(), tone(Segmenter.MAX_UTTERANCE + 5))
    r.check("a monologue is cut at the cap", len(got), 1)

    gap = silence(Segmenter.HANGOVER + 0.2)
    got = feed_all(Segmenter(), tone(1) + gap + tone(1) + gap)
    r.check("two phrases, two utterances", len(got), 2)


def test_wav(r: Results) -> None:
    data = pcm_to_wav(tone(0.5))
    with wave.open(io.BytesIO(data)) as w:
        r.check("wav rate", w.getframerate(), RATE)
        r.check("wav mono", w.getnchannels(), 1)
        r.check("wav 16-bit", w.getsampwidth(), 2)
        r.check("wav length", w.getnframes(), int(RATE * 0.5))


def test_names(r: Results) -> None:
    for heard in ("Corbin, what time is it", "hey corbin", "Corbyn?",
                  "is that corbin's", "CORBIN play the radio",
                  "okay corvin stop"):
        r.ok(f"hears his name in {heard!r}", says_name(heard, "Corbin"))
    for heard in ("the cabin is cold", "robin, you there", "carbon dating",
                  "", "Corbinsky said hi"):
        r.ok(f"no name in {heard!r}", not says_name(heard, "Corbin"))
    r.ok("short names match exactly only", not says_name("al", "Ali"))
    r.ok("short names still match exactly", says_name("hey Ali", "Ali"))
    r.ok("multi-word names", says_name("ok mister crow, hi", "Mister Crow"))


def test_addressed(r: Results) -> None:
    r.ok("alone: everything is for him",
         addressed("what's the weather", "Corbin", alone=True))
    r.ok("in company: needs his name",
         not addressed("what's the weather", "Corbin", alone=False))
    r.ok("in company with his name",
         addressed("Corbin what's the weather", "Corbin", alone=False))
    r.ok("follow-up without his name",
         addressed("yes", "Corbin", alone=False, following_up=True))
    r.ok("follow-up turned to a friend is not for him",
         not addressed("Sam, did you hear that", "Corbin", alone=False,
                       following_up=True, others=["Sam"]))
    r.ok("alone but naming someone not here is still for him",
         addressed("did Sam say that", "Corbin", alone=True, others=[]))
    r.ok("his name wins over a friend's",
         addressed("Sam, ask Corbin", "Corbin", alone=False,
                   others=["Sam"]))


def test_speakable(r: Results) -> None:
    r.check("markdown stripped", speakable("**Hello** _there_ `x`"),
            "Hello there x")
    r.check("custom emoji dropped", speakable("hi <:raven:12345> :caw:"),
            "hi")
    r.check("unicode emoji dropped", speakable("nice 🎉👍"), "nice")
    r.check("links summarised", speakable("see https://x.y/z ok"),
            "see a link ok")
    r.check("nothing left", speakable("🎉"), "")


def test_identity(r: Results) -> None:
    r.check("bare snowflake", user_id_from_identity("1234567890123456"),
            "1234567890123456")
    r.check("prefixed", user_id_from_identity("user_1234567890123_abc"),
            "1234567890123")
    r.check("no digits falls back", user_id_from_identity("guest"), "guest")


def _with_transport(handler):
    """Point every httpx.AsyncClient at a fake server for one block."""
    import httpx

    real = httpx.AsyncClient

    class Patched(real):  # type: ignore[misc,valid-type]
        def __init__(self, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(**kw)

    class _Ctx:
        def __enter__(self):
            httpx.AsyncClient = Patched

        def __exit__(self, *_):
            httpx.AsyncClient = real

    return _Ctx()


def test_probe(r: Results) -> None:
    import httpx

    studio = VoiceStudio(StudioConfig(url="http://vs.test/"))

    def good(req):
        r.check("probe path", req.url.path, vc.DISCOVERY_PATH)
        return httpx.Response(200, json={"protocol": "voicestudio.speech.v1"})

    with _with_transport(good):
        r.ok("up", asyncio.run(studio.probe()))
    r.check("available after a good probe", studio.available, True)

    def other(req):
        return httpx.Response(200, json={"hello": "world"})

    with _with_transport(other):
        r.ok("some other service is not VoiceStudio",
             not asyncio.run(studio.probe()))
    r.ok("reason names the protocol", "protocol" in studio.last_error)

    def down(req):
        raise httpx.ConnectError("refused", request=req)

    with _with_transport(down):
        r.ok("down", not asyncio.run(studio.probe()))
    r.check("down is reported plainly", studio.available, False)
    r.ok("reason says to start it", "running" in studio.last_error)

    def keyed(req):
        r.check("api key sent", req.headers.get("authorization"),
                "Bearer s3cret")
        return httpx.Response(200, json={"protocol": "voicestudio.speech.v1"})

    with _with_transport(keyed):
        asyncio.run(VoiceStudio(StudioConfig(url="http://vs.test",
                                             api_key="s3cret")).probe())

    def failing(req):
        return httpx.Response(400, json={"error": {
            "message": "model_not_active", "type": "invalid_request_error"}})

    with _with_transport(failing):
        try:
            asyncio.run(studio.speak("hi"))
        except RuntimeError as exc:
            r.ok("error carries VoiceStudio's message",
                 "model_not_active" in str(exc))
        else:
            r.ok("speak raised on 400", False)


class FakeStudio:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, wav: bytes) -> str:
        return self.text


class FakeRoom:
    def __init__(self, people: int) -> None:
        self.remote_participants = {str(i): object() for i in range(people)}


class FakeSession:
    guild_id, channel_id, channel_name = "g1", "vc1", "Lounge"
    playing = False


def _listener(text: str, people: int):
    replies, said = [], []

    async def reply(gid, cid, uid, heard, as_text):
        replies.append((uid, heard) if not as_text else (uid, heard, "text"))
        return "sure"

    async def post(cid, body):
        pass

    listener = Listener(FakeStudio(text), FakeSession(), bot_name="Corbin",
                        reply=reply, post=post)
    listener._room = FakeRoom(people)

    async def say(body, **_kw):
        said.append(body)

    listener.say = say
    return listener, replies, said


def test_turns(r: Results) -> None:
    listener, replies, said = _listener("what's up", people=1)
    asyncio.run(listener._turn("u1", b""))
    r.check("one-to-one answers without the name", len(replies), 1)
    r.check("and speaks the reply", said, ["sure"])

    listener, replies, said = _listener("did you see that game", people=3)
    asyncio.run(listener._turn("u1", b""))
    r.check("people talking to each other: silent", replies, [])

    listener, replies, said = _listener("Corbin, play the radio", people=3)
    asyncio.run(listener._turn("u1", b""))
    r.check("named in company: handled", replies,
            [("u1", "Corbin, play the radio")])

    # "yes" right after he answered -- a confirmation, a follow-up.
    listener.studio = FakeStudio("yes")
    asyncio.run(listener._turn("u1", b""))
    r.check("follow-up from the same person counts", len(replies), 2)
    asyncio.run(listener._turn("u2", b""))
    r.check("but not from someone else", len(replies), 2)

    listener._names = {"u1": "Robin", "u2": "Sam"}
    listener.studio = FakeStudio("Sam, you hear that?")
    asyncio.run(listener._turn("u1", b""))
    r.check("follow-up turned to a friend is skipped", len(replies), 2)

    listener, replies, said = _listener("...", people=1)
    asyncio.run(listener._turn("u1", b""))
    r.check("no words, no turn", replies, [])

    listener, replies, said = _listener(
        "Corbin, send the recipe in the chat", people=3)
    asyncio.run(listener._turn("u1", b""))
    r.check("asked for it in the chat: the callback is told",
            replies, [("u1", "Corbin, send the recipe in the chat", "text")])
    r.check("and he only says that he posted it", said, [vc.POSTED_LINE])


def test_close_from_own_turn(r: Results) -> None:
    """ "Stop listening" said in voice closes the listener from inside
    its own worker. That must not cancel the turn mid-disconnect."""
    listener, _replies, _said = _listener("x", people=1)
    finished = []

    async def turn():
        listener._worker = asyncio.current_task()
        await listener.close()
        await asyncio.sleep(0)
        finished.append(True)

    async def main():
        await asyncio.create_task(turn())

    asyncio.run(main())
    r.check("the closing turn runs to the end", finished, [True])
    r.ok("and the listener is closed", listener.closed)


def test_allowlist(r: Results) -> None:
    from corbin.core.voicechat import VoiceChat

    async def nop(*_a):
        return None

    allowed = {"111111111111111111", "222222222222222222"}
    chat = VoiceChat(None, VoiceStudio(StudioConfig()), bot_name="Corbin",
                     reply=nop, post=nop, allowed=lambda g: g in allowed)
    for gid in allowed:
        r.ok(f"allowed in {gid}", chat.allowed(gid))
    r.ok("not elsewhere", not chat.allowed("999"))
    r.ok("not in DMs", not chat.allowed(None))
    nowhere = VoiceChat(None, VoiceStudio(StudioConfig()), bot_name="Corbin",
                        reply=nop, post=nop)
    r.ok("no allowlist means nowhere",
         not nowhere.allowed("111111111111111111"))

    def broken(_g):
        raise OSError("settings unreadable")

    shaky = VoiceChat(None, VoiceStudio(StudioConfig()), bot_name="Corbin",
                      reply=nop, post=nop, allowed=broken)
    r.ok("an unreadable allowlist allows nothing",
         not shaky.allowed("111111111111111111"))
    try:
        asyncio.run(chat.start("999", "vc1"))
    except PermissionError as exc:
        r.ok("start refuses a server not on the list, before probing",
             "isn't turned on" in str(exc))
    else:
        r.ok("start refused", False)


def test_fx(r: Results) -> None:
    import shutil
    from corbin.core.voicechat import (FX_PRESETS, apply_fx, decode_to_pcm,
                                       resolve_fx)

    r.check("preset by name", resolve_fx("raven"), FX_PRESETS["raven"])
    r.check("names are case-insensitive", resolve_fx("Raven"),
            FX_PRESETS["raven"])
    for off in ("", "off", "OFF", "none", "0"):
        r.check(f"{off!r} turns it off", resolve_fx(off), "")
    r.check("anything else is a raw chain", resolve_fx("volume=2"),
            "volume=2")

    if shutil.which("ffmpeg") is None:
        return
    src = pcm_to_wav(tone(1.0, level=3000))
    out = asyncio.run(apply_fx(src, FX_PRESETS["raven"]))
    pcm = asyncio.run(decode_to_pcm(out, RATE))
    secs = len(pcm) / (RATE * 2)
    r.ok("raven keeps the length (pitch shift, not speed)",
         0.95 <= secs <= 1.05)
    try:
        asyncio.run(apply_fx(src, "notafilter=1"))
    except RuntimeError as exc:
        r.ok("a bad chain raises with ffmpeg's reason", "ffmpeg" in str(exc))
    else:
        r.ok("a bad chain raised", False)


def test_as_text(r: Results) -> None:
    from corbin.core.voicechat import wants_text

    for heard in ("Corbin, send that in the chat",
                  "can you put the link in the text chat",
                  "type it out for me", "write that down please",
                  "post the recipe to the channel", "share it to chat"):
        r.ok(f"wants text: {heard!r}", wants_text(heard))
    for heard in ("what did you say in chat yesterday",
                  "I was in the chat earlier", "Corbin, play the radio",
                  "send my love to your mum"):
        r.ok(f"spoken: {heard!r}", not wants_text(heard))


def test_strip_name(r: Results) -> None:
    from corbin.core.voicechat import strip_name

    r.check("leading", strip_name("Corbin, yes.", "Corbin"), "yes.")
    r.check("misheard", strip_name("Corbyn play the radio", "Corbin"),
            "play the radio")
    r.check("trailing", strip_name("Play the radio, Corbin.", "Corbin"),
            "Play the radio")
    r.check("the middle stays",
            strip_name("ask Corbin what he thinks", "Corbin"),
            "ask Corbin what he thinks")
    r.check("just the name is left alone",
            strip_name("Corbin", "Corbin"), "Corbin")
    from corbin.core.confirm import is_affirmative
    r.ok("a stripped spoken yes confirms",
         is_affirmative(strip_name("Corbin, yes.", "Corbin")))


def test_optout(r: Results) -> None:
    import tempfile
    from pathlib import Path
    from corbin.core.settings import ChatSettings
    from corbin.core.voicechat import VoiceOptOut

    with tempfile.TemporaryDirectory() as tmp:
        settings = ChatSettings(Path(tmp) / "s.json")
        opt = VoiceOptOut(settings)
        r.ok("everyone is heard by default", not opt("u1"))
        opt.set("u1", True)
        r.ok("opted out", opt("u1"))
        r.ok("only them", not opt("u2"))
        again = VoiceOptOut(ChatSettings(Path(tmp) / "s.json"))
        r.ok("survives a restart", again("u1"))
        opt.set("u1", False)
        r.ok("opted back in", not opt("u1"))

    class Broken:
        class store:
            @staticmethod
            def get(*_a, **_k):
                raise OSError("unreadable")

    r.ok("privacy fails closed: unreadable means not heard",
         VoiceOptOut(Broken())("u1"))

    # Dropping someone mid-session cancels their audio task.
    listener, _r, _s = _listener("x", people=2)

    async def go():
        task = asyncio.create_task(asyncio.sleep(60))
        listener._tracks["t1"] = task
        listener._track_users["t1"] = "u1"
        dropped = listener.forget("u1")
        await asyncio.sleep(0)
        return dropped, task.cancelled(), listener.forget("u2")

    dropped, cancelled, none = asyncio.run(go())
    r.check("their track is dropped", dropped, 1)
    r.ok("and its audio task stopped", cancelled)
    r.check("nobody else's", none, 0)


def test_grok_while_in_voice(r: Results) -> None:
    """Owner's rule: the local model is set aside while voice chat runs
    (VoiceStudio and Ollama don't fit on the GPU together), and comes
    back when the last voice chat ends."""
    from test_tool_calls import FakeLocal, bridge
    from corbin.ai.ollama import OllamaResult

    local = FakeLocal(OllamaResult(text="local answer"))
    b = bridge(local)
    r.ok("no holds: local is available",
         asyncio.run(b.local_available()))
    r.ok("first hold reports itself", b.hold_local("voice:g1"))
    r.ok("second hold doesn't", not b.hold_local("voice:g2"))
    r.ok("held: local reads as unavailable",
         not asyncio.run(b.local_available()))
    try:
        asyncio.run(b.call(model="m", messages=[], prefer_local=True))
    except Exception:
        pass        # the test bridge's cloud refuses; reaching it is the point
    r.ok("held: prefer_local doesn't reach the local model",
         local.seen is None)
    b.release_local("voice:g1")
    r.ok("one voice chat still running: still held", b.local_held)
    b.release_local("voice:g2")
    b.release_local("voice:g2")
    r.ok("all released (twice is harmless): local again",
         asyncio.run(b.local_available()))
    got = asyncio.run(b.call(model="m", messages=[], prefer_local=True))
    r.check("and calls go local", getattr(got, "text", None), "local answer")

    # VoiceChat runs the hook before listening, and the listener's close
    # runs the release, however it closes.
    from corbin.core.voicechat import VoiceChat
    order = []

    class FakeVoice:
        def __init__(self):
            self.session = None

        def get(self, gid):
            return self.session

        async def connect(self, gid, cid):
            order.append("connect")
            self.session = type("S", (), {
                "listener": None, "client": type("C", (), {"_room": None})(),
                "guild_id": gid, "channel_id": cid, "channel_name": "Lounge",
                "playing": False})()
            return self.session

    class UpStudio(VoiceStudio):
        async def probe(self):
            self.available = True
            return True

    async def started(gid):
        order.append(f"hold {gid}")

    def stopped(gid):
        order.append(f"release {gid}")

    async def nop(*_a):
        return None

    chat = VoiceChat(FakeVoice(), UpStudio(StudioConfig()),
                     bot_name="Corbin", reply=nop, post=nop,
                     allowed=lambda g: True, on_start=started,
                     on_stop=stopped)
    try:
        asyncio.run(chat.start("g1", "vc1"))
    except RuntimeError:
        pass        # no LiveKit room in a test: start() fails after the hook
    r.check("hold before listening, released when it couldn't start",
            order, ["connect", "hold g1", "release g1"])


def test_echo(r: Results) -> None:
    """His own voice, coming back through a listener's microphone."""
    from corbin.core.voicechat import is_echo
    from corbin.commands.voicechat_cmds import LISTENING_NOTICE

    # Verbatim from the first live test: he answered this.
    r.ok("the tail of his join notice",
         is_echo("Say my name if it's for me.", [LISTENING_NOTICE]))
    r.ok("apostrophes don't matter",
         is_echo("say my name if its for me", [LISTENING_NOTICE]))
    reply = "Just hanging around, watching the crows plot. You?"
    r.ok("a whole reply", is_echo(reply, [reply]))
    r.ok("a long-enough fragment", is_echo("watching the crows plot",
                                           [reply]))
    r.ok("two words aren't enough", not is_echo("the crows", [reply]))
    r.ok("a real question about it isn't an echo",
         not is_echo("Corbin, what are the crows plotting?", [reply]))
    r.ok("nothing said, nothing echoed", not is_echo("hello there", []))
    # Verbatim from a live test: dropped as his own voice.
    r.ok("a short greeting near a short line of his isn't an echo",
         not is_echo("Hey Corbin.", ["Hey, Robin."]))
    r.ok("...nor when his name is in it and the line had none",
         not is_echo("Hey Corbin, how's it going today?",
                     ["Hey Robin, how's it going today?"], "Corbin"))
    r.ok("a short line of his, word for word, still is",
         is_echo("You?", ["You?"]))
    r.ok("his own name in his own line still echoes",
         is_echo("I'm Corbin, the resident raven.",
                 ["I'm Corbin, the resident raven."], "Corbin"))

    listener, replies, said = _listener("Say my name if it's for me.",
                                        people=1)
    import time as _t
    listener._said.append((_t.monotonic(), LISTENING_NOTICE))
    asyncio.run(listener._turn("u1", b""))
    r.check("the listener ignores it", replies, [])
    listener._said = [(_t.monotonic() - 60, LISTENING_NOTICE)]
    asyncio.run(listener._turn("u1", b""))
    r.check("...but only for a while", len(replies), 1)


def test_misheard_name(r: Results) -> None:
    """Whisper heard "Torben, stop the radio" (first live test); the
    command check read it as an order for someone else."""
    from corbin.core.voicechat import strip_name

    for heard, want in (("Torben, stop the radio.", "stop the radio."),
                        ("Hey Corban, play the radio", "play the radio"),
                        ("Corbin, yes.", "yes.")):
        r.check(f"loose: {heard!r}", strip_name(heard, "Corbin", loose=True),
                want)
    for heard in ("Sam, stop that", "okay, stop the radio",
                  "Well, I think so"):
        r.check(f"left alone: {heard!r}",
                strip_name(heard, "Corbin", loose=True), heard)
    r.check("strict mode doesn't guess",
            strip_name("Torben, stop the radio.", "Corbin"),
            "Torben, stop the radio.")

    import httpx
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode("latin-1")
        return httpx.Response(200, json={"text": "Corbin, stop the radio."})

    with _with_transport(handler):
        asyncio.run(VoiceStudio(StudioConfig(
            url="http://vs.test", vocabulary="Corbin.")).transcribe(b"RIFF"))
    r.ok("his name goes to the recogniser as a prompt",
         'name="prompt"' in seen.get("body", "")
         and "Corbin." in seen.get("body", ""))


def test_caws(r: Results) -> None:
    import os, shutil, tempfile
    from corbin.core.voicechat import (CawSounds, SPEAK_RATE, caw_files,
                                       split_caws)

    r.check("no caw is one stretch", split_caws("Hello there."),
            [("say", "Hello there.")])
    r.check("caw between words",
            split_caws("Well *caw* that's that."),
            [("say", "Well "), ("caw", 1), ("say", " that's that.")])
    r.check("caw caw is two", split_caws("*Caw caw!*"), [("caw", 2)])
    r.check("capped at three", split_caws("*caw caw caw caw caw*"),
            [("caw", 3)])
    r.check("an action counts once", split_caws("_caws indignantly_ fine"),
            [("caw", 1), ("say", " fine")])
    r.check("the bare word is just a word", split_caws("I said caw."),
            [("say", "I said caw.")])
    r.check("other actions untouched", split_caws("*ruffles feathers*"),
            [("say", "*ruffles feathers*")])

    said = []

    class Studio:
        async def speak(self, text):
            said.append(text)
            return pcm_to_wav(tone(0.5), RATE)

    async def say_it(listener, text):
        return await listener._render(text)

    # No recordings: the word is spoken, as before.
    plain = Listener(Studio(), FakeSession(), bot_name="Corbin", reply=None,
                     post=None, caws=CawSounds(""))
    plain._roll = lambda: 0.0     # the habit would fire...
    audio, words = asyncio.run(say_it(plain, "Hi *caw caw* there"))
    r.check("no recordings: says the word", words, "Hi Caw. Caw. there")
    r.ok("no recordings: one speech call", said == ["Hi Caw. Caw. there"])
    said.clear()
    asyncio.run(say_it(plain, "Just words."))
    r.check("...but never as the spoken word", said, ["Just words."])
    said.clear()
    r.check("nothing to say", asyncio.run(say_it(plain, "**")), (None, ""))

    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "notes.txt"), "w").close()
        r.check("no audio files", caw_files(d), [])
        r.check("missing path", caw_files(os.path.join(d, "nope")), [])
        if shutil.which("ffmpeg") is None:
            return
        quiet = silence(0.3)
        for name in ("a.wav", "b.wav"):
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(pcm_to_wav(quiet + tone(0.4) + quiet))
        r.check("audio files found", [os.path.basename(f)
                                      for f in caw_files(d)],
                ["a.wav", "b.wav"])
        sounds = CawSounds(d)
        calls = asyncio.run(sounds.load())
        r.check("both decoded", len(calls), 2)
        secs = len(calls[0]) / (SPEAK_RATE * 2)
        r.ok(f"silence trimmed ({secs:.2f}s)", 0.35 <= secs <= 0.45)
        picked = []
        for _ in range(3):
            sounds.pick(lambda p: p[0])
            picked.append(sounds._last)
        r.check("never the same call twice running", picked, [0, 1, 0])

        said.clear()
        listener = Listener(Studio(), FakeSession(), bot_name="Corbin",
                            reply=None, post=None, caws=sounds)
        audio, words = asyncio.run(say_it(listener,
                                          "Hello *caw* there *caw caw*"))
        r.check("words exclude the caws", words, "Hello there")
        r.check("one speech call per stretch", sorted(said),
                ["Hello", "there"])
        import io, wave
        with wave.open(io.BytesIO(audio)) as w:
            r.check("spliced at the speech rate", w.getframerate(),
                    SPEAK_RATE)
            total = w.getnframes() / SPEAK_RATE
        # 2 x 0.5s speech, 3 x 0.4s calls, 5 gaps of 0.15s.
        r.ok(f"speech plus three calls ({total:.2f}s)", 2.85 <= total <= 3.05)

        def length(text, roll):
            listener._roll = lambda: roll
            audio, _ = asyncio.run(say_it(listener, text))
            with wave.open(io.BytesIO(audio)) as w:
                return w.getnframes() / w.getframerate()

        plain_len = length("Hello there.", 0.99)
        habit_len = length("Hello there.", 0.0)
        r.ok(f"a lucky roll adds a caw ({plain_len:.2f}s -> "
             f"{habit_len:.2f}s)", 0.6 <= habit_len - plain_len <= 0.75)
        r.ok("not on top of one he asked for",
             abs(length("Hi *caw*", 0.0) - length("Hi *caw*", 0.99)) < 0.01)
        listener._roll = lambda: 0.0
        r.check("nothing to say stays silent",
                asyncio.run(say_it(listener, "**")), (None, ""))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chunks(r: Results) -> None:
    """Long replies are spoken a chunk at a time: a 368-character answer
    slurred towards the end in one generation."""
    import wave
    from corbin.core.voicechat import (CHUNK_GAP, SPEAK_RATE, TTS_MAX_CHARS,
                                       tts_chunks)

    r.check("a short line is one chunk", tts_chunks("Hello there. Hi."),
            ["Hello there. Hi."])
    # The reply that went "tin can" halfway: two sentences, two calls.
    r.check("one sentence per generation",
            tts_chunks("At that rate you'd feel about three g's pinning you "
                       "back. Like a fighter jet launch, only on the road."),
            ["At that rate you'd feel about three g's pinning you back.",
             "Like a fighter jet launch, only on the road."])
    r.check("a short sentence rides with the next",
            tts_chunks("Not quite. It's more like two seconds flat."),
            ["Not quite. It's more like two seconds flat."])
    # The answer that slurred, near enough.
    long = ("Not quite. The new Tesla Roadster is claimed to hit sixty in "
            "under two seconds, which would already be absurd. Point eight "
            "one seconds isn't something any road car has done, and Tesla "
            "hasn't shown it either. I'd treat that number as hype until "
            "someone independent times it.")
    chunks = tts_chunks(long)
    r.ok(f"a long answer splits ({len(chunks)} chunks)", len(chunks) >= 2)
    r.ok("...none over the limit",
         all(len(c) <= TTS_MAX_CHARS for c in chunks))
    r.check("...at sentence ends, nothing lost", " ".join(chunks), long)
    r.ok("decimals aren't sentence ends",
         tts_chunks("It does it in 0.81 seconds. Wow.") ==
         ["It does it in 0.81 seconds. Wow."])
    huge = "word, " * 60 + "end."
    r.ok("a sentence too long for one chunk splits at commas",
         len(tts_chunks(huge)) >= 2)
    r.check("one long word can't be split", tts_chunks("x" * 200),
            ["x" * 200])

    said = []

    class Studio:
        async def speak(self, text):
            said.append(text)
            return pcm_to_wav(tone(0.5), RATE)

    listener = Listener(Studio(), FakeSession(), bot_name="Corbin",
                        reply=None, post=None)
    listener._roll = lambda: 0.99
    audio, words = asyncio.run(listener._render(long))
    r.check("one speech call per chunk", said, chunks)
    r.check("the echo guard still gets the whole text", words, long)
    if audio is not None:
        import io
        with wave.open(io.BytesIO(audio)) as w:
            secs = w.getnframes() / w.getframerate()
        want = 0.5 * len(chunks) + CHUNK_GAP * (len(chunks) - 1)
        r.ok(f"joined with a short pause ({secs:.2f}s)",
             abs(secs - want) < 0.05)
    said.clear()
    asyncio.run(listener._render("Short and sweet."))
    r.check("a short line is still one call", said, ["Short and sweet."])


def test_say_lock(r: Results) -> None:
    """A holding line runs alongside the model call; the answer must wait
    for it, not talk over it on the same track."""
    from corbin.core.voicechat import Listener

    events = []

    async def main():
        listener = Listener(FakeStudio("x"), FakeSession(), bot_name="Corbin",
                            reply=None, post=None)

        async def fake_say(text, **_kw):
            events.append(("start", text))
            await asyncio.sleep(0.05)
            events.append(("end", text))

        listener._say = fake_say
        holding = asyncio.create_task(listener.say("Let me think about that."))
        await asyncio.sleep(0.01)
        await listener.say("Here's the answer.")
        await holding

    asyncio.run(main())
    r.check("one line at a time, in order", events, [
        ("start", "Let me think about that."),
        ("end", "Let me think about that."),
        ("start", "Here's the answer."),
        ("end", "Here's the answer.")])


def test_join_lines(r: Results) -> None:
    from corbin.commands.voicechat_cmds import JOIN_LINES, pick_join_line

    first = pick_join_line(lambda pool: pool[0])
    second = pick_join_line(lambda pool: pool[0])
    r.ok("never the same line twice running", first != second)
    r.ok("all from the list", {first, second} <= set(JOIN_LINES))
    r.ok("none of them recite the instructions",
         not any("listen" in line.lower() for line in JOIN_LINES))


def test_goodbye(r: Results) -> None:
    """Told to leave, he says goodbye first: after the listener closes
    there's no voice left to say it with."""
    from types import SimpleNamespace
    from corbin.commands import radio_cmds, voicechat_cmds as vcc

    first = vcc.pick_goodbye(lambda pool: pool[0])
    r.ok("never the same goodbye twice running",
         first != vcc.pick_goodbye(lambda pool: pool[0]))

    events = []

    class Listener_:
        closed = False

        async def say(self, text):
            events.append(("say", text in vcc.GOODBYE_LINES))

    class Chat:
        def __init__(self, on):
            self.on = on

        def listener(self, gid):
            return Listener_() if self.on else None

        def active(self, gid):
            return self.on

        async def stop(self, gid):
            events.append(("stop",))
            was, self.on = self.on, False
            return was

    class Voice:
        def __init__(self, playing=False, here=True):
            self.session = (SimpleNamespace(playing=playing) if here
                            else None)

        def get(self, gid):
            return self.session

        async def disconnect(self, gid):
            events.append(("leave",))
            return True

        def teardown_failures(self, gid):
            return []

    def ctx(chat, voice):
        return SimpleNamespace(guild_id="g",
                               extras={"voicechat": chat, "voice": voice})

    asyncio.run(vcc.stop_voice_chat(ctx(Chat(True), Voice())))
    r.check("stop listening: goodbye, then stop, then leave", events,
            [("say", True), ("stop",), ("leave",)])

    events.clear()
    asyncio.run(vcc.stop_voice_chat(ctx(Chat(True), Voice(playing=True))))
    r.check("...but no goodbye when the music keeps him there", events,
            [("stop",)])

    events.clear()
    asyncio.run(radio_cmds.stop_radio(ctx(Chat(True), Voice())))
    r.check("leave the channel: goodbye, then leave", events,
            [("say", True), ("leave",)])

    events.clear()
    asyncio.run(radio_cmds.stop_radio(ctx(Chat(False), Voice())))
    r.check("...only the radio playing: nothing to say it with", events,
            [("leave",)])


def test_empty_channel(r: Results) -> None:
    """He leaves a voice channel ~20 s after the last person does."""
    from corbin.core.voice import EMPTY_LEAVE_AFTER, EmptyChannelWatch

    r.check("the owner's 20 seconds", EMPTY_LEAVE_AFTER, 20.0)

    class Voice:
        def __init__(self):
            self._sessions = {"g1": type("S", (), {
                "channel_id": "vc1", "channel_name": "Lounge"})()}

        def get(self, gid):
            return self._sessions.get(gid)

    async def scenario(steps):
        voice, left, people = Voice(), [], {"vc1": 1}
        watch = EmptyChannelWatch(
            voice, occupants=lambda cid: people.get(cid, 0),
            leave=lambda gid: _left(left, gid), delay=0.05)
        for action, arg in steps:
            if action == "people":
                people["vc1"] = arg
                watch.check("g1")
            elif action == "move":
                target, there = arg
                people[target] = there
                voice._sessions["g1"].channel_id = target
                watch.check("g1")
            elif action == "wait":
                await asyncio.sleep(arg)
            elif action == "sweep":
                watch.sweep()
        return left

    async def _left(left, gid):
        left.append(gid)

    r.check("empty for the whole wait: leaves",
            asyncio.run(scenario([("people", 0), ("wait", 0.1)])), ["g1"])
    r.check("someone back in time: stays",
            asyncio.run(scenario([("people", 0), ("wait", 0.02),
                                  ("people", 1), ("wait", 0.1)])), [])
    r.check("company the whole time: stays",
            asyncio.run(scenario([("people", 1), ("wait", 0.1)])), [])
    r.check("repeated checks don't stack timers",
            asyncio.run(scenario([("people", 0), ("sweep", None),
                                  ("sweep", None), ("wait", 0.1)])), ["g1"])
    r.check("moved to a busy channel: stays",
            asyncio.run(scenario([("people", 0), ("wait", 0.02),
                                  ("move", ("vc2", 1)), ("wait", 0.1)])), [])
    r.check("moved to another empty channel: still leaves",
            asyncio.run(scenario([("people", 0), ("wait", 0.02),
                                  ("move", ("vc2", 0)), ("wait", 0.1)])),
            ["g1"])


class _FakeWS:
    """A VoiceStudio streaming socket. ``script(sent)`` returns the
    server's messages once the client has sent its end frame."""
    def __init__(self, script):
        self.sent, self.closed, self.script = [], False, script
        self._ended = asyncio.Event()

    async def send_bytes(self, b):
        self.sent.append(bytes(b))

    async def send_json(self, obj):
        self.sent.append(obj)
        self._ended.set()

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        early = self.script("early")
        for m in early:
            yield m
        await self._ended.wait()
        for m in self.script("end"):
            yield m


def test_streaming_stt(r: Results) -> None:
    studio = vc.VoiceStudio(StudioConfig(url="http://voicestudio.example",
                                         stream_stt=True,
                                         stt_model="sherpa-zipformer-en-20m"))
    url = studio.stream_url()
    r.ok("socket url: ws scheme, raw pcm at the listen rate, the model",
         url.startswith("ws://voicestudio.example/v1/audio/transcriptions/stream?")
         and "pcm=1" in url and f"sr={vc.LISTEN_RATE}" in url
         and "model=sherpa-zipformer-en-20m" in url)
    r.ok("https becomes wss", vc.VoiceStudio(StudioConfig(
        url="https://x")).stream_url().startswith("wss://x/"))

    def run_live(script, chunks=(b"a", b"b")):
        async def go():
            ws = _FakeWS(script)

            async def connect(u, h):
                return ws
            live = vc.LiveTranscript("ws://x", {}, connect=connect)
            live.start(b"pre")
            for c in chunks:
                live.feed(c)
            task = live.finish()
            try:
                return await task, ws, live
            except Exception as exc:
                return exc, ws, live
        return asyncio.run(go())

    text, ws, live = run_live(lambda when: (
        [{"type": "session.started"}, {"type": "partial", "text": "hey"}]
        if when == "early" else
        [{"type": "final", "text": "Hey Corbin.", "engine": "whisper"}]))
    r.check("the final transcript", text, "Hey Corbin.")
    r.check("audio in order, then the end frame", ws.sent,
            [b"pre", b"a", b"b", {"type": "input_audio.end"}])
    r.ok("partials noted; socket closed", live.partial == "hey"
         and live.first_partial is not None and ws.closed)

    text, *_ = run_live(lambda when: [] if when == "early" else [
        {"type": "final", "final_kind": "utterance", "text": "turn it"},
        {"type": "final", "final_kind": "utterance", "text": "up"},
        {"type": "final", "final_kind": "summary", "text": ""}])
    r.check("sherpa: utterance finals joined when the summary is empty",
            text, "turn it up")
    text, *_ = run_live(lambda when: [] if when == "early" else [
        {"type": "final", "text": "raw", "refined_text": "Refined."}])
    r.check("a refined final wins", text, "Refined.")
    err, *_ = run_live(lambda when: [] if when == "early" else [
        {"type": "error", "message": "no model", "kind": "load"}])
    r.ok("an error frame fails it, with the server's words",
         isinstance(err, RuntimeError) and "no model (load)" in str(err))
    err, *_ = run_live(lambda when: [])
    r.ok("closed with nothing: fails, so the upload takes over",
         isinstance(err, RuntimeError))

    # The listener: streamed when it works, the upload when it doesn't.
    class Studio:
        cfg = StudioConfig(stream_stt=True)

        def __init__(self):
            self.uploads = 0

        async def transcribe(self, wav):
            self.uploads += 1
            return "uploaded"

    def transcribe(live_result, timeout=None):
        async def go():
            listener = Listener.__new__(Listener)
            listener.studio = Studio()

            async def live():
                if timeout:
                    await asyncio.sleep(timeout)
                if isinstance(live_result, Exception):
                    raise live_result
                return live_result
            task = None if live_result is None else \
                asyncio.ensure_future(live())
            out = await listener._transcribe(b"\0\0" * 160, task)
            return out, listener.studio.uploads
        return asyncio.run(go())
    r.check("streamed: no upload", transcribe("hi there"),
            (("hi there", "streamed"), 0))
    r.check("stream failed: the same audio is uploaded",
            transcribe(RuntimeError("boom")),
            (("uploaded", "batch after stream failed"), 1))
    r.check("stream came back empty: uploaded",
            transcribe("  "), (("uploaded", "batch after stream failed"), 1))
    r.check("no stream: the upload, as before", transcribe(None),
            (("uploaded", "batch"), 1))
    old = vc.STREAM_FINAL_TIMEOUT
    vc.STREAM_FINAL_TIMEOUT = 0.05
    r.check("a stuck stream times out into the upload",
            transcribe("late", timeout=0.5),
            (("uploaded", "batch after stream failed"), 1))
    vc.STREAM_FINAL_TIMEOUT = old

    # The segmenter drives the live transcript.
    class Live:
        def __init__(self):
            self.started = self.fed = None
            self.chunks, self.aborted, self.finished = [], False, False

        def start(self, pcm):
            self.started = pcm

        def feed(self, pcm):
            self.chunks.append(pcm)

        def finish(self):
            self.finished = True
            return "task"

        def abort(self):
            self.aborted = True

    made = []

    class S:
        cfg = StudioConfig(stream_stt=True)

        def live(self):
            made.append(Live())
            return made[-1]

    listener = Listener.__new__(Listener)
    listener.studio = S()
    seg = Segmenter()
    loud = struct.pack("<h", 4000) * 320            # 20 ms at 16 kHz
    quiet = b"\0\0" * 320
    live = task = None
    tasks = []
    for chunk in [quiet] * 3 + [loud] * 30 + [quiet] * 45:
        was = seg.speaking
        utt = seg.feed(chunk)
        live, task = listener._stream_step(live, seg, chunk, was, utt)
        if task:
            tasks.append(task)
    r.ok("opened at the first loud chunk, with the pre-roll in it",
         len(made) == 1 and len(made[0].started) == 4 * len(loud))
    r.ok("every later chunk fed, then finished when the pause ended it",
         made[0].finished and tasks == ["task"]
         and len(made[0].chunks) == 29 + 40)
    seg2, live2 = Segmenter(), None
    for chunk in [loud] * 5 + [quiet] * 45:     # 0.1 s: a cough
        was = seg2.speaking
        utt = seg2.feed(chunk)
        live2, t = listener._stream_step(live2, seg2, chunk, was, utt)
    r.ok("too short to be speech: dropped, never transcribed",
         made[-1].aborted and not made[-1].finished)


class SeqStudio:
    """Transcribes each upload as the next of ``texts``."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    async def transcribe(self, wav: bytes) -> str:
        return self.texts.pop(0) if self.texts else ""


def test_continuation(r: Results) -> None:
    import time as _time
    from corbin.core.voicechat import Segmenter, looks_unfinished

    for text in ["Can you explain geometry? Be precise and...",
                 "I was thinking that", "so, um", "and the,",
                 "Tell me about the"]:
        r.ok(f"unfinished: {text!r}", looks_unfinished(text))
    for text in ["Give a good explanation.", "What stocks should I buy?",
                 "Play the radio!", "hey Corbin", ""]:
        r.ok(f"finished: {text!r}", not looks_unfinished(text))

    # Live 2026-10-21: the second half was queued before the first reply
    # was written. It is now one turn.
    listener, replies, said = _listener("", people=1)
    listener.studio = SeqStudio("Explain geometry. Be precise and...",
                                "Give a good explanation.")
    listener._queue.put_nowait((_time.monotonic(), "u2", b"x", None))
    listener._queue.put_nowait((_time.monotonic(), "u1", b"x", None))
    asyncio.run(listener._turn("u1", b""))
    r.check("a queued continuation joins the turn", replies,
            [("u1", "Explain geometry. Be precise and... "
                    "Give a good explanation.")])
    r.check("...one answer, not two", said, ["sure"])
    r.check("...and someone else's utterance stays queued",
            [item[1] for item in listener._queue._queue], ["u2"])

    listener, replies, _ = _listener("What time is it?", people=1)
    t0 = _time.monotonic()
    asyncio.run(listener._turn("u1", b""))
    r.ok("a finished sentence doesn't wait", _time.monotonic() - t0 < 0.3)
    r.check("...and is answered as said", replies, [("u1", "What time is it?")])

    async def still_talking():
        listener, replies, _ = _listener("", people=1)
        listener.studio = SeqStudio("So my question is", "what's a rhombus?")
        seg = listener._segmenters["u1"] = Segmenter()
        seg._speaking = True

        async def finish_later():
            await asyncio.sleep(0.3)
            seg._speaking = False
            listener._queue.put_nowait((_time.monotonic(), "u1", b"x", None))
        asyncio.create_task(finish_later())
        await listener._turn("u1", b"")
        return replies
    r.check("waits while the same person is still talking",
            asyncio.run(still_talking()),
            [("u1", "So my question is what's a rhombus?")])

    listener, replies, _ = _listener("Tell me about the", people=1)
    t0 = _time.monotonic()
    asyncio.run(listener._turn("u1", b""))
    waited = _time.monotonic() - t0
    r.ok(f"a trailing-off line waits only the grace ({waited:.1f}s)",
         1.0 < waited < 2.5)
    r.check("...then answers what there is", replies,
            [("u1", "Tell me about the")])


SUITES = [
    ("voice chat continuations", test_continuation),
    ("voice chat streaming transcription (A4)", test_streaming_stt),
    ("voice leaves empty channels", test_empty_channel),
    ("voice chat one line at a time", test_say_lock),
    ("voice chat join lines", test_join_lines),
    ("voice chat goodbye", test_goodbye),
    ("voice chat misheard name", test_misheard_name),
    ("voice chat echo guard", test_echo),
    ("voice chat send-in-chat", test_as_text),
    ("voice chat name stripping", test_strip_name),
    ("voice chat opt-out", test_optout),
    ("voice chat effect", test_fx),
    ("voice chat caws", test_caws),
    ("voice chat long replies", test_chunks),
    ("voice chat allowlist", test_allowlist),
    ("voice chat segmenter", test_segmenter),
    ("voice chat wav", test_wav),
    ("voice chat name matching", test_names),
    ("voice chat addressing", test_addressed),
    ("voice chat speakable text", test_speakable),
    ("voice chat identities", test_identity),
    ("voice chat VoiceStudio check", test_probe),
    ("voice chat turns", test_turns),
    ("voice chat self-close", test_close_from_own_turn),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
