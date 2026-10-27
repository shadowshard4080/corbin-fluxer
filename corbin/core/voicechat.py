"""Spoken conversation in a voice channel, through a local VoiceStudio.

VoiceStudio (https://github.com/debpalash/VoiceStudio) runs beside the bot
and does both halves of the speech work over its OpenAI-compatible HTTP
API:

* ``POST /v1/audio/transcriptions`` -- what someone said, as text.
* ``POST /v1/audio/speech`` -- Corbin's reply, as audio.

Both are batch calls: one finished utterance in, one sentence of reply
out. VoiceStudio's streaming transports (the transcription WebSocket,
chunked speech) are not used yet. What is streamed is the reply: each
sentence is spoken as soon as its audio is made (:meth:`Listener._stream`),
and the person he's answering can cut him off by talking (barge-in).

The flow for one turn:

1. Each person in the channel is a LiveKit audio track. It is pulled at
   16 kHz mono and fed to a :class:`Segmenter`, which cuts an utterance
   out at the first sustained pause.
2. The utterance is transcribed. With more than one person in the channel
   it is only answered if it names the bot, so he doesn't talk over a
   conversation he wasn't part of.
3. The reply comes from the same conversation path as text chat, then is
   spoken. If music or radio is playing, the reply is posted in the voice
   channel's text chat instead of cutting the song off.

VoiceStudio only exists on the host while its app or backend is running,
so availability is probed at boot and again whenever voice chat is
started. Nothing here assumes it stayed up.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
import time
import wave
from array import array
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

from .logtext import said

logger = logging.getLogger("corbin.voicechat")

#: Rate and channel count asked of LiveKit for listening. Speech
#: recognisers want 16 kHz mono; asking for it here means LiveKit does the
#: resampling and nothing else has to.
LISTEN_RATE = 16_000

#: Discovery document. Answers without auth on loopback and is cheap, so
#: it doubles as the liveness check.
DISCOVERY_PATH = "/.well-known/voicestudio-speech"
PROBE_TIMEOUT = 3.0
TRANSCRIBE_TIMEOUT = 60.0
#: Speech generation is the slow half, and on a CPU-only host a few
#: sentences can take a while.
SPEAK_TIMEOUT = 120.0

#: Utterances waiting on a busy worker. Past this they are dropped: an
#: answer to something said a minute ago is worse than no answer.
QUEUE_LIMIT = 3
#: Queued utterances older than this are skipped for the same reason.
STALE_AFTER = 30.0


@dataclass
class StudioConfig:
    url: str = "http://127.0.0.1:3900"
    api_key: str = ""
    #: A voice-profile id or name, an engine preset, or ``default``.
    voice: str = "default"
    #: An installed engine id, or an OpenAI alias for the active engine.
    tts_model: str = "tts-1"
    #: Passed to Whisper-family engines; empty lets them detect it.
    language: str = ""
    #: OmniVoice unmasking steps. 32 is its quality preset; 16 is faster.
    steps: int = 32
    #: Words the recogniser should expect, sent as Whisper's ``prompt``.
    #: His name: unprompted, Whisper heard "Corbin" as "Torben", and the
    #: command check then read "Torben, stop the radio" as meant for
    #: someone else (first live test).
    vocabulary: str = ""
    #: A4: transcribe while they talk, over the streaming socket, instead
    #: of uploading the utterance once it ends. Off until
    #: ``tools/check_voicestudio.py`` shows it's faster *and* as accurate
    #: on the installed engine: the socket takes no ``prompt``, so the
    #: vocabulary hint above (his name) doesn't reach it.
    stream_stt: bool = False
    #: ``?model=`` for the socket: a sherpa model id decodes as it goes
    #: (true streaming); empty lets VoiceStudio pick, which on a
    #: Whisper-family engine re-transcribes everything at the end.
    stt_model: str = ""


class VoiceStudio:
    """Thin client for VoiceStudio's OpenAI-compatible audio routes."""

    def __init__(self, cfg: StudioConfig) -> None:
        self.cfg = cfg
        self.base = cfg.url.rstrip("/")
        #: Result of the most recent probe. None until one has run.
        self.available: Optional[bool] = None
        self.last_error = ""
        #: Is the configured voice really there? None until checked.
        #: VoiceStudio answers an unknown voice name with its default
        #: voice and a 200, so a missing profile would otherwise go
        #: unnoticed: he'd just sound like somebody else.
        self.voice_found: Optional[bool] = None
        #: The profile id ``/generate`` needs, looked up from the
        #: configured name by the probe. None: VoiceStudio's default voice.
        self.profile_id: Optional[str] = None

    def _headers(self) -> dict[str, str]:
        if self.cfg.api_key:
            return {"Authorization": f"Bearer {self.cfg.api_key}"}
        return {}

    async def probe(self) -> bool:
        """Is VoiceStudio up and is it actually VoiceStudio?

        A 200 from some other service on the port is not good enough: the
        discovery document has to name the speech protocol.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                resp = await client.get(self.base + DISCOVERY_PATH,
                                        headers=self._headers())
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            body = resp.json()
            protocol = str((body or {}).get("protocol") or "")
            if not protocol.startswith("voicestudio.speech"):
                raise RuntimeError("not a VoiceStudio speech service "
                                   f"(protocol {protocol or 'missing'!r})")
        except Exception as exc:
            reason = describe_error(exc)
            if self.available is not False or reason != self.last_error:
                logger.warning("VoiceStudio unreachable at %s: %s",
                               self.base, reason)
            self.available, self.last_error = False, reason
            return False
        if self.available is not True:
            logger.info("VoiceStudio is up at %s (%s)", self.base, protocol)
        self.available, self.last_error = True, ""
        await self._check_voice()
        return True

    async def _check_voice(self) -> None:
        """Warn loudly when the configured voice isn't a real profile."""
        import httpx

        wanted = (self.cfg.voice or "").strip()
        if not wanted or wanted.lower() == "default":
            self.voice_found = True
            return
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                resp = await client.get(self.base + "/v1/audio/voices",
                                        headers=self._headers())
            voices = (resp.json() or {}).get("voices") or []
        except Exception:
            logger.debug("voice list unavailable", exc_info=True)
            return
        profiles = [v for v in voices if v.get("type") == "profile"]
        match = next((v for v in profiles
                      if str(v.get("voice_id") or "").lower() == wanted.lower()),
                     None) or next(
            (v for v in profiles
             if str(v.get("name") or "").lower() == wanted.lower()), None)
        found = match is not None
        self.profile_id = str(match.get("voice_id")) if match else None
        if not found and self.voice_found is not False:
            logger.warning("VoiceStudio has no voice profile %r; he'll speak "
                           "in its default voice until one is saved under "
                           "that name", wanted)
        self.voice_found = found

    async def transcribe(self, wav: bytes) -> str:
        import httpx

        data = {"model": "whisper-1", "response_format": "json"}
        if self.cfg.language:
            data["language"] = self.cfg.language
        if self.cfg.vocabulary:
            data["prompt"] = self.cfg.vocabulary
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT) as client:
            resp = await client.post(
                self.base + "/v1/audio/transcriptions",
                headers=self._headers(), data=data,
                files={"file": ("speech.wav", wav, "audio/wav")})
        _raise_for(resp)
        return str((resp.json() or {}).get("text") or "").strip()

    def stream_url(self) -> str:
        """The streaming STT socket: raw s16le mono PCM at LISTEN_RATE.
        ``pcm=1`` matters: without it (and without a sherpa ``model``)
        VoiceStudio reads the bytes as WebM/Opus and runs ffmpeg on
        them."""
        from urllib.parse import urlencode

        query = {"pcm": "1", "sr": str(LISTEN_RATE)}
        if self.cfg.stt_model:
            query["model"] = self.cfg.stt_model
        base = self.base.replace("https://", "wss://", 1) \
            .replace("http://", "ws://", 1)
        return f"{base}{STREAM_STT_PATH}?{urlencode(query)}"

    def live(self, *, connect: Optional[Callable[..., Any]] = None
             ) -> "LiveTranscript":
        """A streaming transcription for one utterance (A4)."""
        return LiveTranscript(self.stream_url(), self._headers(),
                              connect=connect)

    async def speak(self, text: str) -> bytes:
        """WAV audio of ``text`` in the configured voice, the same every time.

        Through VoiceStudio's native ``/generate``, not the OpenAI route:
        OmniVoice samples with ``position_temperature`` 5.0 by default,
        so one voice profile came out as a different-sounding raven on
        every line, and a fixed seed didn't help. Only ``/generate``
        takes the temperatures; at 0 the output is deterministic
        (measured: the same line twice was bit-identical) and every line
        sounds like the reference clip. ``effect_preset=raw`` because his
        own effect (``VOICE_FX``) is applied afterwards.
        """
        import httpx

        data = {"text": text, "position_temperature": "0",
                "class_temperature": "0", "num_step": str(self.cfg.steps),
                "effect_preset": "raw"}
        wanted = (self.cfg.voice or "").strip()
        if wanted and wanted.lower() != "default":
            if self.profile_id is None:
                await self._check_voice()
            if self.profile_id is None:
                # Not his voice is not a voice: say() posts the text
                # instead, and the boot card already says why.
                raise RuntimeError(f"no VoiceStudio voice profile "
                                   f"{wanted!r}")
            data["profile_id"] = self.profile_id
        async with httpx.AsyncClient(timeout=SPEAK_TIMEOUT) as client:
            resp = await client.post(self.base + "/generate",
                                     headers=self._headers(), data=data)
        _raise_for(resp)
        return resp.content


#: VoiceStudio's streaming STT socket (``capture_ws.py``; one connection
#: per utterance, closed by the server after the final).
STREAM_STT_PATH = "/v1/audio/transcriptions/stream"
#: From "they stopped talking" to the final transcript. Past this the
#: utterance is transcribed the batch way instead: a stuck socket must
#: not lose what someone said.
STREAM_FINAL_TIMEOUT = 6.0
STREAM_CONNECT_TIMEOUT = 5.0
_END = object()


class LiveTranscript:
    """One utterance, transcribed while it's spoken.

    ``start(pcm)`` opens the socket with the audio so far (the segmenter's
    pre-roll and first loud chunk); ``feed(pcm)`` sends each later chunk;
    ``finish()`` sends ``{"type": "input_audio.end"}`` and returns a task
    resolving to the final text. ``abort()`` drops it (a cough: too short
    to be speech). Nothing here blocks the audio read loop: sending runs
    in its own task off a queue.

    Server messages (VoiceStudio ``capture_ws.py``): ``partial`` with the
    text so far; ``final`` with the text, and on sherpa engines several
    ``final_kind: "utterance"`` frames then a closing ``"summary"``. The
    summary's text wins when it has any; otherwise the utterance finals
    are joined. An ``error`` frame fails it, and the caller falls back to
    the batch route.
    """

    def __init__(self, url: str, headers: dict, *,
                 connect: Optional[Callable[..., Any]] = None) -> None:
        self.url, self.headers = url, headers
        self._connect = connect or _aiohttp_ws
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self.opened_at = 0.0
        self.ended_at = 0.0
        #: When the first partial arrived, and the latest partial text.
        self.first_partial: Optional[float] = None
        self.partial = ""
        self.engine = ""

    def start(self, pcm: bytes) -> None:
        self.opened_at = time.monotonic()
        if pcm:
            self._queue.put_nowait(bytes(pcm))
        self._task = asyncio.ensure_future(self._run())

    def feed(self, pcm: bytes) -> None:
        if self._task is not None and pcm:
            self._queue.put_nowait(bytes(pcm))

    def finish(self) -> "asyncio.Task":
        self.ended_at = time.monotonic()
        self._queue.put_nowait(_END)
        return self._task

    def abort(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _run(self) -> str:
        ws = await asyncio.wait_for(self._connect(self.url, self.headers),
                                    STREAM_CONNECT_TIMEOUT)
        try:
            reader = asyncio.ensure_future(self._read(ws))
            try:
                while True:
                    item = await self._queue.get()
                    if item is _END:
                        await ws.send_json({"type": "input_audio.end"})
                        break
                    await ws.send_bytes(item)
                    if reader.done():
                        break     # the server failed or closed early
                return await reader
            finally:
                if not reader.done():
                    reader.cancel()
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def _read(self, ws: Any) -> str:
        pieces: list[str] = []
        async for msg in ws:
            data = msg if isinstance(msg, dict) else _ws_json(msg)
            if data is None:
                continue
            kind = data.get("type")
            if kind == "partial":
                if self.first_partial is None:
                    self.first_partial = time.monotonic()
                self.partial = str(data.get("text") or "")
            elif kind == "final":
                self.engine = str(data.get("engine") or self.engine)
                text = str(data.get("refined_text") or data.get("text")
                           or "").strip()
                if data.get("final_kind") == "utterance":
                    if text:
                        pieces.append(text)
                    continue
                return text or " ".join(pieces).strip()
            elif kind == "error":
                raise RuntimeError(
                    f"streaming STT: {data.get('message') or 'error'}"
                    + (f" ({data.get('kind')})" if data.get("kind") else ""))
        # Closed with no summary: whatever utterances came, or nothing.
        if pieces:
            return " ".join(pieces).strip()
        raise RuntimeError("streaming STT closed without a transcript")


def _ws_json(msg: Any) -> Optional[dict]:
    """aiohttp WSMessage -> dict for TEXT frames; None for anything else."""
    import json

    data = getattr(msg, "data", None)
    if getattr(msg, "type", None) is not None and \
            getattr(msg.type, "name", "") not in ("TEXT",):
        if getattr(msg.type, "name", "") in ("ERROR",):
            raise RuntimeError(f"streaming STT socket error: {data}")
        return None
    try:
        out = json.loads(data)
    except Exception:
        return None
    return out if isinstance(out, dict) else None


async def _aiohttp_ws(url: str, headers: dict) -> Any:
    """An aiohttp websocket that closes its own session with it.
    (aiohttp comes with fluxer.py; httpx has no websockets.)"""
    import aiohttp

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None,
                                      connect=STREAM_CONNECT_TIMEOUT))
    try:
        ws = await session.ws_connect(url, headers=headers, heartbeat=20)
    except BaseException:
        await session.close()
        raise
    close = ws.close

    async def closing(*a: Any, **k: Any) -> Any:
        try:
            return await close(*a, **k)
        finally:
            await session.close()
    ws.close = closing
    return ws


def _raise_for(resp: Any) -> None:
    """Raise with VoiceStudio's own message, not just a status code.

    Its errors use OpenAI's shape, and the message is usually the fix
    ("install the model", "ambiguous_voice ...").
    """
    if resp.status_code < 400:
        return
    message = ""
    try:
        body = resp.json()
        message = str(((body or {}).get("error") or {}).get("message")
                      or (body or {}).get("detail") or "")
    except Exception:
        message = (resp.text or "")[:200]
    raise RuntimeError(f"VoiceStudio HTTP {resp.status_code}"
                       + (f": {message}" if message else ""))


def describe_error(exc: BaseException) -> str:
    """A short reason for the log and for the person asking."""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    # A name that doesn't resolve is a ConnectError too. Saying "nothing
    # is listening" for it sent the first live test looking at VoiceStudio
    # when the server simply couldn't look up the Tailscale name.
    if any(m in low for m in ("name or service not known", "nodename nor",
                              "temporary failure in name resolution",
                              "getaddrinfo", "name resolution")):
        return ("can't resolve that host name (is this machine using "
                "Tailscale's DNS? `tailscale set --accept-dns=true`)")
    if "ConnectError" in name or isinstance(exc, ConnectionError):
        return ("nothing is listening there (is VoiceStudio running, with "
                f"Tailscale serve on?){': ' + text if text else ''}")
    if "Timeout" in name:
        return "it didn't answer in time"
    return str(exc) or name


# -- audio ------------------------------------------------------------------

def rms(pcm: bytes) -> float:
    """Root-mean-square level of s16le samples. ``audioop`` is gone in 3.13."""
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def pcm_to_wav(pcm: bytes, rate: int = LISTEN_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buf.getvalue()


class Segmenter:
    """Cuts one person's audio into utterances on pauses.

    Plain energy gating, which is enough for a voice channel: clients
    already run their own noise suppression and voice activity detection,
    so between phrases the track is close to digital silence.

    ``feed`` takes any size of s16le mono chunk and returns a finished
    utterance (PCM bytes) when a pause closes one, else None.
    """

    #: RMS above this counts as speech. s16 full scale is 32767.
    THRESHOLD = 500.0
    #: A pause this long ends the utterance.
    HANGOVER = 0.8
    #: Shorter than this is a cough or a click, not speech.
    MIN_SPEECH = 0.4
    #: Cut here even mid-sentence, so a monologue still gets answered.
    MAX_UTTERANCE = 20.0
    #: Audio kept from before speech was detected, so the first syllable
    #: (quieter than the rest) isn't clipped.
    PRE_ROLL = 0.25

    def __init__(self, rate: int = LISTEN_RATE) -> None:
        self.rate = rate
        self._bytes_per_sec = rate * 2
        self._pre = bytearray()
        self._buf = bytearray()
        self._speaking = False
        self._voiced = 0.0
        self._silence = 0.0

    def _seconds(self, nbytes: int) -> float:
        return nbytes / self._bytes_per_sec

    @property
    def speaking(self) -> bool:
        """An utterance is under way (loud enough to have started)."""
        return self._speaking

    def pending(self) -> bytes:
        """The utterance so far, pre-roll included."""
        return bytes(self._buf)

    @property
    def voiced(self) -> float:
        """Seconds of speech in the utterance under way; 0 between them.
        What barge-in watches: it can't wait for the pause that ends it."""
        return self._voiced if self._speaking else 0.0

    def feed(self, pcm: bytes) -> Optional[bytes]:
        duration = self._seconds(len(pcm))
        loud = rms(pcm) >= self.THRESHOLD

        if not self._speaking:
            if not loud:
                self._pre += pcm
                excess = len(self._pre) - int(self.PRE_ROLL
                                              * self._bytes_per_sec)
                if excess > 0:
                    del self._pre[:excess + excess % 2]
                return None
            self._speaking = True
            self._buf = bytearray(self._pre)
            self._pre.clear()
            self._voiced = self._silence = 0.0

        self._buf += pcm
        if loud:
            self._voiced += duration
            self._silence = 0.0
        else:
            self._silence += duration

        if self._silence >= self.HANGOVER or \
                self._seconds(len(self._buf)) >= self.MAX_UTTERANCE:
            return self._finish()
        return None

    def _finish(self) -> Optional[bytes]:
        out, voiced = bytes(self._buf), self._voiced
        self._buf = bytearray()
        self._speaking = False
        self._voiced = self._silence = 0.0
        return out if voiced >= self.MIN_SPEECH else None


# -- text -------------------------------------------------------------------

_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>|:\w{2,}:")
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_`~#>|]+")
_SPACE = re.compile(r"\s+")
#: Pictographic emoji. Read aloud they come out as "smiling face with
#: open mouth", which no one wants.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def speakable(text: str) -> str:
    """Strip what reads fine in chat but sounds wrong spoken."""
    text = _CUSTOM_EMOJI.sub(" ", text or "")
    text = _URL.sub(" a link ", text)
    text = _MARKUP.sub("", text)
    text = _EMOJI.sub("", text)
    return _SPACE.sub(" ", text).strip()


_WORD = re.compile(r"[a-z0-9']+")
#: How close a heard word must be to the name to count as it. Speech
#: recognition spells names however it likes: "Corbyn", "Corbin's",
#: "Korban". 0.75 takes those and still rejects "cabin" or "robin".
NAME_SIMILARITY = 0.75


def _words(text: str) -> list[str]:
    return [w.removesuffix("'s") for w in _WORD.findall((text or "").lower())]


def says_name(transcript: str, name: str) -> bool:
    """Does the transcript contain ``name``, allowing for misspelling?"""
    from difflib import SequenceMatcher

    target = _words(name)
    if not target:
        return False
    words = _words(transcript)
    n = len(target)
    want = " ".join(target)
    for i in range(len(words) - n + 1):
        heard = " ".join(words[i:i + n])
        if heard == want:
            return True
        # Short names are exact only: fuzzy matching "Al" hits everything.
        # The first sound has to agree too ("c" and "k" are the same
        # sound to a recogniser), which keeps "robin" from matching.
        # A much longer word is a different name ("Corbinsky").
        if len(want) >= 4 and abs(len(heard) - len(want)) <= 2 \
                and _first_sound(heard) == _first_sound(want) \
                and SequenceMatcher(None, heard, want).ratio() \
                >= NAME_SIMILARITY:
            return True
    return False


def _first_sound(word: str) -> str:
    first = word[:1]
    return "k" if first == "c" else first


_VOCATIVE = re.compile(
    r"^\s*(?:(?:hey|hi|ok|okay|oi|yo)\b[,\s]+)?([a-z']+)\s*,\s*(\S.*)$",
    re.IGNORECASE | re.DOTALL)


def strip_name(transcript: str, name: str, *, loose: bool = False) -> str:
    """Drop his name from the start or end of an utterance.

    "Corbin, yes." -> "yes." and "Play the radio, Corbin." -> "Play the
    radio." Speech recognition may misspell the name, so this uses the
    same matching as :func:`says_name`. The middle is left alone: "ask
    Corbin's opinion" means something.

    ``loose``, for an utterance already known to be for him: also drop a
    leading "<word>," that only roughly resembles his name. Whisper heard
    "Torben, stop the radio"; left in, the command check read it as an
    order for someone called Torben, and he answered that he couldn't.
    """
    if loose:
        from difflib import SequenceMatcher

        m = _VOCATIVE.match(transcript or "")
        target = " ".join(_words(name))
        if m and target and " " not in target:
            word = m.group(1).lower().replace("'", "")
            if abs(len(word) - len(target)) <= 2 and SequenceMatcher(
                    None, word, target).ratio() >= 0.6:
                return m.group(2).strip()
    text = (transcript or "").strip()
    words = text.split()
    if not words:
        return text
    n = max(1, len(_words(name)))
    lead = " ".join(words[:n]).strip(",.!?;: ")
    if says_name(lead, name) and len(_words(lead)) == n:
        text = " ".join(words[n:]).lstrip(",.!?;: ")
        words = text.split()
    tail = " ".join(words[-n:]).strip(",.!?;: ") if words else ""
    if tail and says_name(tail, name) and len(_words(tail)) == n \
            and len(words) > n:
        text = " ".join(words[:-n]).rstrip(",;: ")
    return text.strip() or (transcript or "").strip()


def addressed(transcript: str, bot_name: str, *, alone: bool,
              following_up: bool = False,
              others: Sequence[str] = ()) -> bool:
    """Should a heard utterance be answered?

    * Saying his name always counts.
    * One-to-one, everything is for him.
    * ``following_up``: he just answered this person, so carrying on
      without repeating his name counts -- unless they name somebody
      else in the channel, which is them turning to talk to that person.
    * Otherwise it's people talking to each other, and he stays out of it.
    """
    if says_name(transcript, bot_name):
        return True
    if any(says_name(transcript, other) for other in others if other):
        return False
    return alone or following_up


#: "Send it in the chat", "can you type that out", "put the link in text
#: chat", "drop it in the channel". Asked for by voice, the answer is posted
#: as a normal written reply, and he only says that he did.
_AS_TEXT = re.compile(
    r"\b(?:send|post|put|type|write|drop|paste|share|text)\b"
    r"[^.?!]{0,60}?"
    r"\b(?:in|into|to|on)\s+(?:the\s+|our\s+|this\s+)?"
    r"(?:text\s+)?(?:chat|channel|text)\b"
    r"|\btype\s+(?:it|that|this|them)\s+(?:out|up)\b"
    r"|\bwrite\s+(?:it|that|this|them)\s+(?:down|out)\b",
    re.IGNORECASE)


def wants_text(transcript: str) -> bool:
    """Did they ask for the answer written in chat rather than spoken?"""
    return _AS_TEXT.search(transcript or "") is not None


#: Said aloud when the answer went to the text chat instead.
POSTED_LINE = "It's in the chat."


_DIGITS = re.compile(r"\d{6,}")


def user_id_from_identity(identity: str) -> str:
    """The Fluxer user id inside a LiveKit participant identity.

    Fluxer's identity format is undocumented; the snowflake is the one
    long run of digits in it. Falls back to the whole identity, which
    still keeps speakers apart.
    """
    found = _DIGITS.search(identity or "")
    return found.group(0) if found else (identity or "")


# -- opting out ------------------------------------------------------------

class VoiceOptOut:
    """People who asked Corbin not to listen to them ("don't listen to me").

    Honoured before any audio leaves the bot: an opted-out person's track
    is never subscribed, so nothing of theirs is transcribed or sent to
    VoiceStudio. Global, not per server -- it's about the person.

    Kept in the chat settings under ``_voice_optout`` beside the feature
    allowlist; no new state file.
    """

    BUCKET = "_voice_optout"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def __call__(self, user_id: str) -> bool:
        return self.opted_out(user_id)

    def opted_out(self, user_id: str) -> bool:
        try:
            return bool(self.settings.store.get(self.BUCKET, str(user_id),
                                                False))
        except Exception:
            # Privacy fails closed: unreadable means don't listen.
            logger.warning("voice opt-out unreadable; not listening to %s",
                           user_id, exc_info=True)
            return True

    def set(self, user_id: str, out: bool) -> None:
        if out:
            self.settings.store.set(self.BUCKET, str(user_id), True)
        else:
            self.settings.store.unset(self.BUCKET, str(user_id))
        logger.info("voice chat: %s opted %s", user_id,
                    "out" if out else "back in")


# -- the session ------------------------------------------------------------

#: (guild_id, channel_id, user_id, transcript, as_text) -> the reply, or
#: None. The callback decides between a command and conversation; the
#: listener only hears and speaks. ``as_text``: they asked for it in the
#: chat, so the reply is written, not spoken -- full length, formatting
#: allowed -- and the callback posts it itself.
ReplyFn = Callable[[str, str, str, str, bool], Awaitable[Optional[str]]]
#: (channel_id, text) -> post the reply as text instead of speaking it.
PostFn = Callable[[str, str], Awaitable[Any]]

#: After answering someone, how long they can carry on without saying his
#: name. Long enough for "yes" to a confirmation or a follow-up question;
#: short enough that he drops out once they turn to each other.
FOLLOW_UP_WINDOW = 15.0

#: One thought said with a breath in it arrives as two utterances (live,
#: 2026-10-21: "...Be precise and..." then "Give a good explanation.",
#: answered as two turns). Before answering, he waits for the rest: while
#: the same person is still talking, or briefly when the transcript ends
#: mid-thought (``looks_unfinished``). Never longer than this in all.
CONTINUE_MAX_WAIT = 8.0
#: After an unfinished-sounding transcript, how long they have to start
#: talking again. A finished one doesn't wait at all.
UNFINISHED_GRACE = 1.5
_UNFINISHED_RE = re.compile(
    r"(?:\.\.\.|…|,|\b(?:and|or|but|so|because|cause|then|if|that|the|a|"
    r"an|to|of|with|for|like|um+|uh+|er+|my|your|is|was))\W*$",
    re.IGNORECASE)


def looks_unfinished(transcript: str) -> bool:
    """The transcript stops mid-thought: a trailing "and", "the", comma or
    ellipsis (Whisper marks a trailing-off sentence with "...")."""
    text = (transcript or "").strip()
    if not text or text.endswith(("?", "!")):
        return False
    if text.endswith(".") and not text.endswith(("...", "…")):
        return False
    return bool(_UNFINISHED_RE.search(text))


#: Speech is published at this rate, mono, on its own track.
SPEAK_RATE = 48_000
#: 10 ms frames, what LiveKit's own sources use.
SPEAK_FRAME = SPEAK_RATE // 100
#: Opus bitrate for his voice. LiveKit's default for a microphone track is
#: its low speech profile with DTX, and the first live test said he sounded
#: "a bit robotic": the raven effect's flutter and grit are exactly what a
#: low-bitrate speech codec mangles. The music track's settings (DTX and
#: RED off, an explicit bitrate) are known to be accepted by Fluxer's SFU.
SPEECH_BITRATE = 64_000

#: Barge-in: the person he is answering talking over him for this many
#: seconds of speech stops him mid-reply. 1.2 s (owner, 2026-10-17):
#: a real interruption, not an "mm", a laugh or "yeah" while he talks.
BARGE_IN_AFTER = 1.2
#: An utterance this soon after it cut him off, that turns out to be his
#: own voice coming back, means that person's mic hears him. Barge-in is
#: then off for them for the session, or he'd interrupt himself.
BARGE_IN_ECHO_WINDOW = 30.0

#: How long his own words count as "just said" for the echo guard (s).
ECHO_WINDOW = 20.0
#: How close a transcript must be to something he just said to be his
#: own voice coming back through someone's microphone.
ECHO_SIMILARITY = 0.75
#: Below this many words a transcript is only an echo if it matches a
#: line exactly. "Hey Corbin." scored 0.8 against a short line of his and
#: was dropped as his own voice; at two words, similarity means little.
ECHO_MIN_WORDS = 4


def is_echo(transcript: str, recent: Sequence[str],
            bot_name: str = "") -> bool:
    """Is ``transcript`` his own recent speech, picked up again?

    Someone listening on speakers feeds his voice back into their mic.
    The first live test transcribed the end of his join notice ("Say my
    name if it's for me.") as the user, and he answered himself. A
    fragment of a line counts, as does a close match.

    Someone saying his name (``bot_name``) is talking to him, not an echo,
    unless the line it might echo has his name in it too.
    """
    from difflib import SequenceMatcher

    def norm(text: str) -> list[str]:
        # Recognisers drop or keep apostrophes at will ("it's" / "its").
        # Not _words(): that trims "'s" for name matching, so "it's"
        # would become "it" and stop matching "its".
        return [w.replace("'", "")
                for w in _WORD.findall((text or "").lower())]

    words = norm(transcript)
    heard = " ".join(words)
    if not heard:
        return False
    named = bool(bot_name) and says_name(transcript, bot_name)
    for line in recent:
        said = " ".join(norm(line))
        if not said:
            continue
        if named and not says_name(line, bot_name):
            continue
        if len(words) < ECHO_MIN_WORDS:
            if heard == said:
                return True
            continue
        # A fragment of what he said, if it's long enough to mean it;
        # a two-word "I'm listening" from a person is just as likely.
        if len(words) >= 3 and f" {heard} " in f" {said} ":
            return True
        if SequenceMatcher(None, heard, said).ratio() >= ECHO_SIMILARITY:
            return True
    return False


#: Named voice effects for ``VOICE_FX``: ffmpeg audio filter chains run
#: over VoiceStudio's output before it is played. Each starts by fixing the
#: rate so the pitch maths below means the same thing whatever rate the
#: voice engine produced.
FX_PRESETS = {
    # Corbin's raven: pitch up ~5% at the same speed, less chest and a
    # nasal push around 1.6 kHz (the beak), a fast shallow flutter for a
    # throaty croak, light soft-clipping for grit. Levelled last so it
    # sits at the same loudness as the plain voice. Picked by ear from
    # several blends; "blend B" in the session that tuned it.
    "raven": (
        "aresample=24000,"
        "asetrate=24000*1.05,aresample=24000,atempo=1/1.05,"
        "highpass=f=180,"
        "equalizer=f=1600:t=q:w=1.0:g=6,"
        "equalizer=f=250:t=q:w=1:g=-4,"
        "vibrato=f=32:d=0.05,"
        "volume=2.2,asoftclip=type=atan,volume=0.6,"
        "loudnorm=I=-19:TP=-1.5,aresample=24000"),
}


def resolve_fx(setting: str) -> str:
    """``VOICE_FX`` -> a filter chain. A preset name, a raw ffmpeg chain,
    or empty/"off" for the voice as VoiceStudio made it."""
    value = (setting or "").strip()
    if value.lower() in ("", "off", "none", "0"):
        return ""
    return FX_PRESETS.get(value.lower(), value)


async def apply_fx(audio: bytes, chain: str) -> bytes:
    """Run ``audio`` through an ffmpeg filter chain; WAV out."""
    return await _ffmpeg(audio, ["-af", chain, "-f", "wav"],
                         "apply the voice effect")


async def _ffmpeg(audio: bytes, args: list[str], what: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", *args, "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(audio)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not {what}: "
                           + err.decode(errors="replace").strip()[:200])
    return out


async def decode_to_pcm(audio: bytes, rate: int = SPEAK_RATE) -> bytes:
    """Any audio file -> s16le mono at ``rate``, through ffmpeg."""
    return await _ffmpeg(audio, ["-f", "s16le", "-ar", str(rate),
                                 "-ac", "1"], "decode the speech")


# -- caws -------------------------------------------------------------------

#: A caw in a reply: ``*caw*``, ``*caw caw*``, ``*caws indignantly*``,
#: the way he'd write an action in chat. Spoken, each "caw" in it is one
#: real raven call; TTS saying the word sounds like a man saying "caw".
_CAW = re.compile(r"[*_]+\s*(caw[^*_\n]{0,30}?)\s*[*_]+", re.I)
_CAW_WORD = re.compile(r"\bcaw", re.I)
#: "*caw caw caw caw caw*" is still three.
MAX_CAWS = 3
#: Silence either side of a call, so it doesn't run into the words.
CAW_GAP = 0.15
#: Chance of a caw after a line that didn't ask for one: a habit, not a
#: punctuation mark.
CAW_CHANCE = 0.2
#: Recordings are trimmed of leading/trailing silence, cut to 2.5 s, and
#: levelled to the raven effect's loudness so a call is no louder than
#: his voice.
CAW_FILTER = ("silenceremove=start_periods=1:start_threshold=-45dB,"
              "areverse,"
              "silenceremove=start_periods=1:start_threshold=-45dB,"
              "areverse,"
              "atrim=0:2.5,afade=t=out:st=2.4:d=0.1,"
              "loudnorm=I=-19:TP=-1.5")
CAW_SUFFIXES = (".wav", ".mp3", ".ogg", ".opus", ".flac", ".m4a", ".webm")


#: Speech is generated a sentence at a time. In one generation the voice
#: drifts after the first sentence: a 368-character answer slurred
#: towards the end, and a 137-character one (two sentences) came out in
#: his voice and then "out of a tin can". Each sentence starting fresh
#: keeps it his. A sentence longer than this splits at its commas.
TTS_MAX_CHARS = 150
#: Sentences shorter than this ride with the next one: "Not quite." on
#: its own comes out clipped.
TTS_MIN_CHARS = 30
#: Silence between chunks: about a sentence break.
CHUNK_GAP = 0.12
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def tts_chunks(text: str, limit: int = TTS_MAX_CHARS,
               least: int = TTS_MIN_CHARS) -> list[str]:
    """``text`` as one piece per sentence, for one generation each.

    A sentence under ``least`` characters joins the next. One over
    ``limit`` is split at its commas; one that still won't split goes as
    it is: a long chunk is better than one cut mid-phrase.
    """
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split((text or "").strip()):
        if len(sentence) <= limit:
            pieces.append(sentence)
        else:
            pieces.extend(_CLAUSE_END.split(sentence))
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        if current and len(current) >= least:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def split_caws(text: str) -> list[tuple[str, Any]]:
    """``text`` as ``("say", words)`` and ``("caw", count)`` in order."""
    text = text or ""
    parts: list[tuple[str, Any]] = []
    pos = 0
    for match in _CAW.finditer(text):
        if match.start() > pos:
            parts.append(("say", text[pos:match.start()]))
        count = len(_CAW_WORD.findall(match.group(1)))
        parts.append(("caw", max(1, min(count, MAX_CAWS))))
        pos = match.end()
    if pos < len(text) or not parts:
        parts.append(("say", text[pos:]))
    return parts


def caw_files(path: str) -> list[str]:
    """The recordings at ``path``: one file, or every audio file in a
    directory. Empty when there are none."""
    if not path:
        return []
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        return []
    return sorted(os.path.join(path, name) for name in os.listdir(path)
                  if name.lower().endswith(CAW_SUFFIXES))


class CawSounds:
    """Real raven calls spliced into his speech where a reply says
    ``*caw*``. Decoded on first use and kept; a different recording each
    time when there's more than one."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._calls: Optional[list[bytes]] = None
        self._last = -1

    async def load(self) -> list[bytes]:
        if self._calls is None:
            calls = []
            for name in caw_files(self.path):
                try:
                    with open(name, "rb") as fh:
                        raw = fh.read()
                    pcm = await _ffmpeg(raw, ["-af", CAW_FILTER, "-f",
                                              "s16le", "-ar", str(SPEAK_RATE),
                                              "-ac", "1"], "decode a caw")
                except Exception as exc:
                    logger.warning("voice chat: skipped caw %s: %s",
                                   name, describe_error(exc))
                    continue
                if pcm:
                    calls.append(pcm)
            if calls:
                logger.info("voice chat: %d caw recording(s) loaded",
                            len(calls))
            self._calls = calls
        return self._calls

    def pick(self, choice: Optional[Callable[[Sequence[int]], int]] = None
             ) -> bytes:
        """A loaded call, not the previous one. Call ``load`` first."""
        import random

        calls = self._calls or []
        pool = [i for i in range(len(calls)) if i != self._last] or [0]
        self._last = (choice or random.choice)(pool)
        return calls[self._last]


class Listener:
    """Hears one voice channel and answers out loud.

    Attached to a :class:`~corbin.core.voice.Session` and closed with it,
    so leaving voice by any route stops listening.

    Speech goes out on a **second track** of its own rather than through
    the session's player, so talking never stops a song: music is paused
    while he speaks and resumed after. If the server won't take a second
    track, he falls back to the player when nothing is playing, and to the
    voice channel's text chat when something is.
    """

    def __init__(self, studio: VoiceStudio, session: Any, *,
                 bot_name: str, reply: ReplyFn, post: PostFn,
                 fx: str = "",
                 opted_out: Optional[Callable[[str], bool]] = None,
                 caws: Optional["CawSounds"] = None,
                 caw_chance: float = CAW_CHANCE,
                 barge_in: bool = True) -> None:
        import random

        self.studio = studio
        #: Raven calls for ``*caw*`` in a reply, or None to say the word.
        self.caws = caws
        #: Odds of an unasked caw after a line (needs recordings).
        self.caw_chance = caw_chance
        self._roll = random.random
        self.session = session
        self.bot_name = bot_name
        #: user id -> True when they asked not to be listened to. Their
        #: audio is never subscribed: nothing of theirs is even heard.
        self._opted_out = opted_out or (lambda _uid: False)
        #: track sid -> user id, to drop someone mid-session.
        self._track_users: dict[str, str] = {}
        #: ffmpeg filter chain for his voice (``resolve_fx``), or "".
        self.fx = fx
        self._reply = reply
        self._post = post
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._tracks: dict[str, asyncio.Task] = {}
        self._streams: dict[str, Any] = {}
        #: user id -> participant display name, for telling when someone
        #: is talking to somebody else.
        self._names: dict[str, str] = {}
        #: user id -> when he last answered them.
        self._answered: dict[str, float] = {}
        self._worker: Optional[asyncio.Task] = None
        #: user id -> their track's segmenter, to see if they're still
        #: mid-sentence (``_continuation``).
        self._segmenters: dict[str, Segmenter] = {}
        self._room: Any = None
        self._handlers: list[tuple[str, Callable]] = []
        self._seen_identity = False
        #: The speech track: (AudioSource, publication), or False once the
        #: server has refused it.
        self._voice_out: Any = None
        #: The publish of that track while it's in flight.
        self._publishing: Optional[asyncio.Task] = None
        self.closed = False
        #: Run once on close, by any route (stop, move, revoke, shutdown).
        self.on_close: Optional[Callable[[], Any]] = None
        #: (monotonic time, text) of what he said aloud, for is_echo.
        self._said: list[tuple[float, str]] = []
        #: One line at a time on his track: a holding line ("Let me think
        #: about that.") runs alongside the model call, and the answer
        #: must wait for it rather than talk over it.
        self._say_lock = asyncio.Lock()
        #: Stop when the person he's answering talks over him.
        self.barge_in = barge_in
        #: Whose speech can cut the current line off (None: nobody).
        self._speaking_to: Optional[str] = None
        self._interrupt = asyncio.Event()
        #: user id -> when they last cut him off, for the echo check.
        self._barged: dict[str, float] = {}
        #: People whose mic picks him up: they can't cut him off.
        self._no_barge: set[str] = set()
        #: Did the last line get cut off?
        self.last_interrupted = False

    def start(self) -> None:
        room = getattr(self.session.client, "_room", None)
        if room is None:
            raise RuntimeError("not connected to the voice channel")
        self._room = room

        def on_subscribed(track: Any, publication: Any,
                          participant: Any) -> None:
            self._listen(track, participant)

        def on_unsubscribed(track: Any, publication: Any,
                            participant: Any) -> None:
            self._drop(getattr(track, "sid", ""))

        for event, handler in (("track_subscribed", on_subscribed),
                               ("track_unsubscribed", on_unsubscribed)):
            room.on(event, handler)
            self._handlers.append((event, handler))

        # People already talking before he joined: their tracks were
        # subscribed during connect, before the handler existed.
        for participant in list(room.remote_participants.values()):
            for pub in list(participant.track_publications.values()):
                track = getattr(pub, "track", None)
                if track is not None:
                    self._listen(track, participant)

        self._worker = asyncio.create_task(self._work())
        logger.info("voice chat listening in %s (guild %s)",
                    self.session.channel_name, self.session.guild_id)

    def _listen(self, track: Any, participant: Any) -> None:
        from livekit import rtc

        if self.closed or getattr(track, "kind", None) != \
                rtc.TrackKind.KIND_AUDIO:
            return
        sid = str(getattr(track, "sid", "") or id(track))
        if sid in self._tracks:
            return
        identity = str(getattr(participant, "identity", "") or "")
        name = str(getattr(participant, "name", "") or "")
        if not self._seen_identity:
            # Logged once so the identity format can be checked against
            # a real user id; see user_id_from_identity.
            self._seen_identity = True
            logger.info("voice chat: first participant identity %r "
                        "(name %r)", identity, name)
        user_id = user_id_from_identity(identity)
        if name:
            self._names[user_id] = name
        if self._opted_out(user_id):
            logger.info("voice chat: not listening to %s (opted out)",
                        user_id)
            return
        stream = rtc.AudioStream(track, sample_rate=LISTEN_RATE,
                                 num_channels=1)
        self._streams[sid] = stream
        self._track_users[sid] = user_id
        self._tracks[sid] = asyncio.create_task(self._read(stream, user_id))

    def rescan(self) -> None:
        """Pick up tracks skipped earlier (someone opted back in)."""
        if self.closed or self._room is None:
            return
        for participant in list(self._room.remote_participants.values()):
            for pub in list(participant.track_publications.values()):
                track = getattr(pub, "track", None)
                if track is not None:
                    self._listen(track, participant)

    def forget(self, user_id: str) -> int:
        """Stop hearing someone right now (they opted out mid-session).
        Returns how many of their tracks were dropped."""
        sids = [sid for sid, uid in self._track_users.items()
                if uid == str(user_id)]
        for sid in sids:
            self._drop(sid)
        return len(sids)

    def _drop(self, sid: str) -> None:
        self._track_users.pop(sid, None)
        task = self._tracks.pop(sid, None)
        if task is not None:
            task.cancel()
        stream = self._streams.pop(sid, None)
        if stream is not None:
            asyncio.create_task(_quiet_close(stream))

    async def _read(self, stream: Any, user_id: str) -> None:
        segmenter = self._segmenters[user_id] = Segmenter()
        streaming = bool(getattr(getattr(self.studio, "cfg", None),
                                 "stream_stt", False))
        live: Optional[LiveTranscript] = None
        try:
            async for event in stream:
                chunk = bytes(event.frame.data)
                was = segmenter.speaking
                utterance = segmenter.feed(chunk)
                task = None
                if streaming:
                    live, task = self._stream_step(live, segmenter, chunk,
                                                   was, utterance)
                if (self._speaking_to == user_id
                        and segmenter.voiced >= BARGE_IN_AFTER
                        and not self._interrupt.is_set()):
                    self._interrupt.set()
                    self._barged[user_id] = time.monotonic()
                    logger.info("voice chat: %s talked over me; stopping",
                                user_id)
                if utterance is None:
                    continue
                try:
                    self._queue.put_nowait((time.monotonic(), user_id,
                                            utterance, task))
                except asyncio.QueueFull:
                    if task is not None:
                        task.cancel()
                    logger.info("voice chat busy; dropped an utterance "
                                "from %s", user_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("voice chat: audio stream for %s failed",
                           user_id, exc_info=True)
        finally:
            if live is not None:
                live.abort()
            if self._segmenters.get(user_id) is segmenter:
                del self._segmenters[user_id]

    def _stream_step(self, live: Optional["LiveTranscript"],
                     segmenter: "Segmenter", chunk: bytes, was: bool,
                     utterance: Optional[bytes]) -> tuple:
        """Keep the live transcript in step with the segmenter.
        Returns ``(live, final_task)``; the task only when an utterance
        just ended. Never raises: streaming is an optimisation, and the
        batch route is always there behind it."""
        try:
            if not was and segmenter.speaking:
                live = self.studio.live()
                live.start(segmenter.pending())
            elif was and live is not None:
                live.feed(chunk)
                if not segmenter.speaking:
                    if utterance is not None:
                        return None, live.finish()
                    live.abort()          # too short: a cough, not speech
                    return None, None
        except Exception:
            logger.debug("streaming STT step failed", exc_info=True)
            if live is not None:
                live.abort()
            return None, None
        return live, None

    async def _work(self) -> None:
        """One turn at a time, so replies never talk over each other."""
        while not self.closed:
            heard_at, user_id, pcm, live = await self._queue.get()
            if time.monotonic() - heard_at > STALE_AFTER:
                if live is not None:
                    live.cancel()
                continue
            try:
                await self._turn(user_id, pcm, heard_at=heard_at, live=live)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("voice chat turn failed")

    def _take_queued(self, user_id: str) -> Optional[tuple]:
        """Pop the oldest queued utterance from ``user_id``, leaving
        everyone else's in order. None when they have none."""
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        found = None
        for item in items:
            if found is None and item[1] == user_id:
                found = item
            else:
                self._queue.put_nowait(item)
        return found

    async def _continuation(self, user_id: str, text: str) -> str:
        """``text`` plus whatever the same person says straight after it.

        Takes their next utterance when it's already queued, or when
        they're still talking, or (after a transcript that trails off)
        when they start again within ``UNFINISHED_GRACE``. Bounded by
        ``CONTINUE_MAX_WAIT``; a finished sentence followed by silence
        costs nothing."""
        started = time.monotonic()
        quiet_since = started
        parts = 1
        while time.monotonic() - started < CONTINUE_MAX_WAIT \
                and not self.closed:
            item = self._take_queued(user_id)
            if item is not None:
                _heard, _uid, pcm, live = item
                try:
                    more, _how = await self._transcribe(pcm, live)
                except Exception as exc:
                    logger.warning("voice chat: transcription failed: %s",
                                   describe_error(exc))
                    more = ""
                if more and any(ch.isalnum() for ch in more):
                    text = f"{text.rstrip()} {more.strip()}"
                    parts += 1
                quiet_since = time.monotonic()
                continue
            segmenter = self._segmenters.get(user_id)
            if segmenter is not None and segmenter.speaking:
                quiet_since = time.monotonic()
            elif not looks_unfinished(text) or \
                    time.monotonic() - quiet_since >= UNFINISHED_GRACE:
                break
            await asyncio.sleep(0.05)
        if parts > 1:
            logger.info("voice chat: joined %d utterances from %s into one "
                        "turn", parts, user_id)
        return text

    def _alone(self) -> bool:
        try:
            return len(self._room.remote_participants) <= 1
        except Exception:
            return False

    def _following_up(self, user_id: str) -> bool:
        at = self._answered.get(user_id)
        return at is not None and time.monotonic() - at <= FOLLOW_UP_WINDOW

    async def _transcribe(self, pcm: bytes,
                          live: Optional["asyncio.Task"]) -> tuple:
        """``(text, how)``: the live transcript when there is one and it
        arrives in time, else the batch route on the same audio. ``how``
        ("streamed", "batch", "batch after stream failed") goes in the
        voice turn line."""
        if live is not None:
            try:
                text = await asyncio.wait_for(live, STREAM_FINAL_TIMEOUT)
                if text and any(ch.isalnum() for ch in text):
                    return text, "streamed"
                logger.info("voice chat: streamed transcript empty; "
                            "trying the upload")
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                logger.warning("voice chat: streaming STT failed (%s); "
                               "uploading the utterance instead",
                               describe_error(exc) if isinstance(
                                   exc, Exception) else type(exc).__name__)
            text = await self.studio.transcribe(pcm_to_wav(pcm))
            return text, "batch after stream failed"
        return await self.studio.transcribe(pcm_to_wav(pcm)), "batch"

    async def _turn(self, user_id: str, pcm: bytes, *,
                    heard_at: Optional[float] = None,
                    live: Optional["asyncio.Task"] = None) -> None:
        started = time.monotonic()
        heard_at = started if heard_at is None else heard_at
        try:
            text, how = await self._transcribe(pcm, live)
        except Exception as exc:
            logger.warning("voice chat: transcription failed: %s",
                           describe_error(exc))
            return
        if not text or not any(ch.isalnum() for ch in text):
            return
        now = time.monotonic()
        self._said = [(t, w) for t, w in self._said if now - t <= ECHO_WINDOW]
        stt = time.monotonic() - started
        if is_echo(text, [w for _, w in self._said], self.bot_name):
            logger.info("voice chat: ignored my own voice picked up by %s's "
                        "microphone: %s", user_id, said(text))
            barged = self._barged.pop(user_id, None)
            if barged is not None and now - barged <= BARGE_IN_ECHO_WINDOW \
                    and user_id not in self._no_barge:
                self._no_barge.add(user_id)
                logger.warning("voice chat: %s's mic hears me, so that "
                               "interruption was my own voice; they can't "
                               "cut me off for the rest of this session",
                               user_id)
            return
        text = await self._continuation(user_id, text)
        others = [n for uid, n in self._names.items() if uid != user_id]
        if not addressed(text, self.bot_name, alone=self._alone(),
                         following_up=self._following_up(user_id),
                         others=others):
            logger.debug("voice chat: not for me: %r", text[:80])
            return
        logger.info("voice chat heard %s: %s", user_id, said(text, 120))

        session = self.session
        as_text = wants_text(text)
        asked = time.monotonic()
        reply = await self._reply(session.guild_id, session.channel_id,
                                  user_id, text, as_text)
        answered = time.monotonic() - asked
        if self.closed:
            # The turn itself may have ended voice chat ("stop
            # listening", "leave the channel").
            return
        if not reply:
            return
        self._answered[user_id] = time.monotonic()
        # Written replies were posted by the callback; just say so.
        speaking = time.monotonic()
        first = await self.say(POSTED_LINE if as_text else reply,
                               to=user_id)
        # The A4 budget, read off real turns: where the time went between
        # the end of their sentence and his first word. "total" starts
        # when the pause ended the utterance, so add ``pause`` for the
        # time since they actually stopped talking.
        logger.info(
            "voice turn: %s stt=%.2fs (%s) reply=%.2fs first_audio=%s "
            "total=%s pause=%.1fs%s%s", user_id, stt, how, answered,
            f"{first:.2f}s" if first is not None else "-",
            f"{speaking - heard_at + first:.2f}s" if first is not None
            else "-",
            Segmenter.HANGOVER,
            f" queued={started - heard_at:.2f}s"
            if started - heard_at >= 0.05 else "",
            " interrupted" if self.last_interrupted else "")

    async def say(self, reply: str, *,
                  to: Optional[str] = None) -> Optional[float]:
        """Speak ``reply``, or post it where speaking isn't possible.

        ``to``: the person being answered, who can cut him off by
        talking (barge-in). Returns seconds until the first audio went
        out, or None when nothing was spoken.
        """
        called = time.monotonic()
        async with self._say_lock:
            # Waiting behind a holding line counts: it's time they spent
            # not hearing the answer.
            waited = time.monotonic() - called
            first = await self._say(reply, to=to)
        return None if first is None else waited + first

    async def _say(self, reply: str, *,
                   to: Optional[str] = None) -> Optional[float]:
        session = self.session
        self.last_interrupted = False
        source = None if self.closed else await self._speech_track()
        if source is not None:
            try:
                return await self._stream(source, reply, to)
            except _NothingSpoken as exc:
                logger.warning("voice chat: speech failed: %s",
                               describe_error(exc.__cause__ or exc))
                await self._post(session.channel_id, reply)
                return None
        # No track of his own: the whole line is made first, then played
        # through the player (or posted, when the player is busy).
        started = time.monotonic()
        try:
            audio, spoken = await self._render(reply)
        except Exception as exc:
            logger.warning("voice chat: speech failed: %s",
                           describe_error(exc))
            await self._post(session.channel_id, reply)
            return
        if audio is None:
            return None
        # Remembered as he starts speaking: the echo comes back while he
        # is still talking.
        if spoken:
            self._said.append((time.monotonic(), spoken))
        if self.closed:
            return None
        if session.playing:
            # No second track and the player is busy: don't cut the
            # music off to talk; the channel's chat gets it instead.
            await self._post(session.channel_id, reply)
            return None
        first = time.monotonic() - started
        await self._play(audio)
        return first

    async def _stream(self, source: Any, reply: str,
                      to: Optional[str]) -> Optional[float]:
        """Speak on his own track, each chunk as soon as it's made.

        Every chunk is requested at once, as before, but the first plays
        the moment it arrives instead of after the last: a three-sentence
        answer used to wait for all three generations. A chunk that isn't
        ready when the one before it ends leaves a short silence, nothing
        worse. Raises :class:`_NothingSpoken` if it fails before any
        audio went out (the caller posts the text); after that, a failure
        just ends the line.
        """
        plan = await self._plan(reply)
        if plan is None:
            return None
        parts, chunked, spoken = plan
        started = time.monotonic()
        tasks = [asyncio.create_task(self._speech_pcm(c))
                 for chunks in chunked for c in chunks]
        pending = iter(tasks)
        gap = b"\x00\x00" * int(SPEAK_RATE * CAW_GAP)
        pause = b"\x00\x00" * int(SPEAK_RATE * CHUNK_GAP)
        segments: list[Any] = []
        for (kind, value), chunks in zip(parts, chunked):
            if kind == "caw":
                segments.extend(gap + self.caws.pick() for _ in range(value))
                segments.append(gap)
                continue
            for i, _ in enumerate(chunks):
                if i:
                    segments.append(pause)
                segments.append(next(pending))

        client = self.session.client
        paused = False
        first: Optional[float] = None
        self._interrupt.clear()
        self._speaking_to = (to if self.barge_in and to not in self._no_barge
                             else None)
        try:
            for segment in segments:
                if isinstance(segment, asyncio.Task):
                    try:
                        pcm = await segment
                    except Exception as exc:
                        if first is None:
                            raise _NothingSpoken() from exc
                        logger.warning("voice chat: speech failed mid-line: "
                                       "%s", describe_error(exc))
                        break
                else:
                    pcm = segment
                if first is None:
                    first = time.monotonic() - started
                    # Remembered as he starts: the echo comes back while
                    # he's still talking.
                    if spoken:
                        self._said.append((time.monotonic(), spoken))
                    if self.closed:
                        return first
                    # Pause the music under him rather than talk over
                    # it. Only undo a pause he made himself.
                    paused = bool(self.session.playing
                                  and not getattr(client, "is_paused", False))
                    if paused:
                        client.pause()
                if not await self._push(source, pcm):
                    break
            else:
                await self._drain(source)
        finally:
            for task in tasks:
                task.cancel()
            self._speaking_to = None
            self.last_interrupted = self._interrupt.is_set()
            if paused:
                client.resume()
        return first

    async def _push(self, source: Any, pcm: bytes) -> bool:
        """Queue ``pcm`` on the track a frame at a time. False when cut
        off (barge-in) or closed, with whatever was queued dropped."""
        from livekit import rtc

        step = SPEAK_FRAME * 2
        for i in range(0, len(pcm), step):
            if self.closed or self._interrupt.is_set():
                source.clear_queue()
                return False
            chunk = pcm[i:i + step]
            if len(chunk) < step:
                chunk += b"\x00" * (step - len(chunk))
            await source.capture_frame(rtc.AudioFrame(
                data=chunk, sample_rate=SPEAK_RATE, num_channels=1,
                samples_per_channel=SPEAK_FRAME))
        return True

    async def _drain(self, source: Any) -> None:
        """Wait for the queued audio to play out, unless he's cut off."""
        playout = asyncio.ensure_future(source.wait_for_playout())
        stop = asyncio.ensure_future(self._interrupt.wait())
        try:
            done, _ = await asyncio.wait(
                {playout, stop}, return_when=asyncio.FIRST_COMPLETED)
            if playout not in done:
                source.clear_queue()
        finally:
            stop.cancel()
            playout.cancel()

    async def _plan(self, reply: str
                    ) -> Optional[tuple[list, list[list[str]], str]]:
        """What to say: (parts, TTS chunks per part, the spoken words).
        None when there's nothing to say."""
        parts = split_caws(reply)
        asked = any(k == "caw" for k, _ in parts)
        # Now and then, a caw after the last word. Only a line he wrote
        # none into, and only with recordings: TTS saying "caw" at random
        # would just be a verbal tic.
        habit = (not asked and self._roll() < self.caw_chance
                 and bool(speakable(reply)))
        calls: list[bytes] = []
        if self.caws is not None and (asked or habit):
            calls = await self.caws.load()
        if habit and calls:
            parts.append(("caw", 1))
        if not calls:
            # No recordings: he says the word, as he always did.
            parts = [("say", " ".join(
                v if k == "say" else " ".join(["Caw."] * v)
                for k, v in parts))]
        texts = [speakable(v) if k == "say" else "" for k, v in parts]
        spoken = " ".join(t for t in texts if t)
        chunked = [tts_chunks(t) if t else [] for t in texts]
        if not any(chunked) and not calls:
            return None
        return parts, chunked, spoken

    async def _render(self, reply: str) -> tuple[Optional[bytes], str]:
        """``reply`` as one audio file, and the words in it (for the echo
        guard). (None, "") when there's nothing to say. For the player
        fallback; his own track uses :meth:`_stream`."""
        plan = await self._plan(reply)
        if plan is None:
            return None, ""
        parts, chunked, spoken = plan
        jobs = [c for chunks in chunked for c in chunks]
        if len(jobs) == 1 and len(parts) == 1:
            # The usual case: one short line, one call, no splicing.
            return await self._speech(jobs[0]), spoken
        # One VoiceStudio call per chunk, all at once.
        voiced = iter(await asyncio.gather(*(
            self._speech_pcm(j) for j in jobs)))
        gap = b"\x00\x00" * int(SPEAK_RATE * CAW_GAP)
        pause = b"\x00\x00" * int(SPEAK_RATE * CHUNK_GAP)
        pcm = bytearray()
        for (kind, value), chunks in zip(parts, chunked):
            if kind == "caw":
                for _ in range(value):
                    pcm += gap + self.caws.pick()
                pcm += gap
                continue
            for i, _ in enumerate(chunks):
                if i:
                    pcm += pause
                pcm += next(voiced)
        return pcm_to_wav(bytes(pcm), SPEAK_RATE), spoken

    async def _speech(self, text: str) -> bytes:
        """``text`` in his voice, through his effect."""
        audio = await self.studio.speak(text)
        if self.fx:
            try:
                audio = await apply_fx(audio, self.fx)
            except Exception as exc:
                # A typo in a custom chain shouldn't silence him; the
                # plain voice is still his voice.
                logger.warning("voice chat: %s; speaking without it", exc)
        return audio

    async def _speech_pcm(self, text: str) -> bytes:
        return await decode_to_pcm(await self._speech(text))

    def warm_speech_track(self) -> None:
        """Start publishing the speech track now, in the background.

        Publishing on first use put the SFU round trip in front of the
        join greeting, after the line had already waited on the model and
        before its speech could start; now it overlaps both.
        """
        if self._voice_out is None and self._publishing is None:
            task = self._publishing = asyncio.create_task(
                self._publish_speech_track())
            # Read here so a failure isn't "never retrieved"; the first
            # line to need the track sees it again.
            task.add_done_callback(
                lambda t: t.cancelled() or t.exception())

    async def _speech_track(self) -> Any:
        """The published speech source, publishing it on first use."""
        if self._voice_out is None:
            task = self._publishing
            if task is None:
                task = self._publishing = asyncio.create_task(
                    self._publish_speech_track())
            try:
                # Shielded: a line cancelled (barge-in, leave) mustn't
                # abort a publish the next line will want.
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._publishing is task:
                    self._publishing = None
                raise
        return self._voice_out[0] if self._voice_out else None

    async def _publish_speech_track(self) -> None:
        """Publish his own speech track into ``_voice_out`` (False when
        the server refuses it)."""
        from livekit import rtc

        source = rtc.AudioSource(SPEAK_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("corbin-voice",
                                                       source)
        options = rtc.TrackPublishOptions(
            # Same routing hint as music; Fluxer's SFU drops anything else.
            source=rtc.TrackSource.SOURCE_MICROPHONE,
            # See SPEECH_BITRATE: DTX gates quiet passages, RED isn't
            # needed, and the default bitrate made him sound robotic.
            dtx=False, red=False)
        # Assigned, not passed: AudioEncoding isn't re-exported on rtc.
        options.audio_encoding.max_bitrate = SPEECH_BITRATE
        try:
            publication = await asyncio.wait_for(
                self._room.local_participant.publish_track(track, options),
                timeout=10.0)
        except Exception as exc:
            logger.warning("voice chat: the server refused a separate "
                           "speech track (%s); speaking through the player "
                           "instead", describe_error(exc))
            self._voice_out = False
            try:
                await source.aclose()
            except Exception:
                pass
            return
        if self.closed:
            # Closed while the publish was in flight: close() found
            # nothing to unpublish, so this is the last chance.
            try:
                await asyncio.wait_for(
                    self._room.local_participant.unpublish_track(
                        publication.sid), timeout=5.0)
            except Exception:
                logger.debug("speech track unpublish failed", exc_info=True)
            try:
                await source.aclose()
            except Exception:
                pass
            return
        self._voice_out = (source, publication)

    async def _play(self, audio: bytes) -> None:
        """Speak through the session's player. Waits until finished."""
        from fluxer.voice import FFmpegPCMAudio

        fd, path = tempfile.mkstemp(prefix="corbin-say-", suffix=".wav")
        with os.fdopen(fd, "wb") as out:
            out.write(audio)
        done = asyncio.get_running_loop().create_future()

        def _after(error: Optional[Exception]) -> None:
            try:
                os.unlink(path)
            except OSError:
                pass
            if not done.done():
                done.set_result(error)

        try:
            await self.session.client.play(FFmpegPCMAudio(path),
                                           after=_after)
        except Exception:
            _after(None)
            raise
        error = await done
        if error is not None:
            logger.warning("voice chat: playback failed: %s", error)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                logger.warning("voice chat close hook failed", exc_info=True)
        room = self._room
        for event, handler in self._handlers:
            try:
                room.off(event, handler)
            except Exception:
                pass
        self._handlers.clear()
        # A voice command ("stop listening", "leave") closes the listener
        # from inside its own worker. Cancelling that task here would
        # abort the very disconnect that called us halfway through; the
        # loop sees ``closed`` and ends on its own instead.
        if self._worker is not None and \
                self._worker is not asyncio.current_task():
            self._worker.cancel()
        for sid in list(self._tracks):
            self._drop(sid)
        if self._voice_out:
            source, publication = self._voice_out
            self._voice_out = None
            try:
                await asyncio.wait_for(
                    room.local_participant.unpublish_track(publication.sid),
                    timeout=5.0)
            except Exception:
                logger.debug("speech track unpublish failed", exc_info=True)
            try:
                await source.aclose()
            except Exception:
                pass
        logger.info("voice chat stopped in guild %s", self.session.guild_id)


class _NothingSpoken(Exception):
    """Speech failed before any of the line went out."""


async def _quiet_close(stream: Any) -> None:
    try:
        await stream.aclose()
    except Exception:
        logger.debug("audio stream close failed", exc_info=True)


class VoiceChat:
    """Starts and stops listeners on top of the voice session manager."""

    def __init__(self, voice: Any, studio: VoiceStudio, *, bot_name: str,
                 reply: ReplyFn, post: PostFn,
                 allowed: Optional[Callable[[Optional[str]], bool]] = None,
                 opted_out: Optional[Callable[[str], bool]] = None,
                 fx: str = "",
                 caws: str = "",
                 caw_chance: float = CAW_CHANCE,
                 barge_in: bool = True,
                 on_start: Optional[Callable[[str], Awaitable[Any]]] = None,
                 on_stop: Optional[Callable[[str], Any]] = None) -> None:
        self.voice = voice
        #: Awaited before listening starts in a guild (bot.py: set the
        #: local LLM aside and unload it so the speech models fit on the
        #: GPU), and called when that guild's listener closes.
        self._on_start = on_start
        self._on_stop = on_stop
        self.fx = resolve_fx(fx)
        #: Shared by every listener, so recordings are decoded once.
        self.caws = CawSounds(caws) if caws else None
        self.caw_chance = caw_chance
        self.barge_in = barge_in
        #: () -> where he's streaming, or None. Set by bot.py from the
        #: StreamManager; voice chat refuses while it says somewhere.
        self.streaming_in: Optional[Callable[[], Optional[str]]] = None
        self.studio = studio
        self.bot_name = bot_name
        self._reply = reply
        self._post = post
        #: guild id -> is voice on the owner's allowlist there
        #: (``FeatureAllowlist.allowed(gid, "voice")``). None: nowhere.
        self._allowed = allowed
        self._opted_out = opted_out

    def allowed(self, guild_id: Optional[str]) -> bool:
        if self._allowed is None or not guild_id:
            return False
        try:
            return bool(self._allowed(str(guild_id)))
        except Exception:
            logger.warning("voice chat: allowlist check failed for %s",
                           guild_id, exc_info=True)
            return False

    def listener(self, guild_id: Optional[str]) -> Optional["Listener"]:
        session = self.voice.get(guild_id)
        listener = getattr(session, "listener", None)
        return listener if listener is not None and not listener.closed \
            else None

    def rescan_everywhere(self) -> None:
        for session in list(getattr(self.voice, "_sessions", {}).values()):
            listener = getattr(session, "listener", None)
            if listener is not None and not listener.closed:
                listener.rescan()

    def forget_everywhere(self, user_id: str) -> int:
        """Drop someone's audio in every channel he is listening in."""
        dropped = 0
        for session in list(getattr(self.voice, "_sessions", {}).values()):
            listener = getattr(session, "listener", None)
            if listener is not None and not listener.closed:
                dropped += listener.forget(user_id)
        return dropped

    def active(self, guild_id: Optional[str]) -> bool:
        session = self.voice.get(guild_id)
        listener = getattr(session, "listener", None)
        return listener is not None and not listener.closed

    async def start(self, guild_id: str, channel_id: str) -> Any:
        """Join (or stay in) a channel and listen. Returns the session.

        Raises RuntimeError naming the reason if VoiceStudio is down,
        before anything joins: a bot that sits in the channel unable to
        hear is worse than one that says why it can't.
        """
        if not self.allowed(guild_id):
            logger.info("allowlist: refused voice in %s", guild_id)
            raise PermissionError("Voice chat isn't turned on for this "
                                  "server")
        where = self.streaming_in() if self.streaming_in else None
        if where:
            # Owner's rule (2026-10-17): no voice chat while streaming.
            raise PermissionError(f"I'm streaming in {where} right now, "
                                  "and voice chat is off while I stream")
        if not await self.studio.probe():
            raise RuntimeError(self.studio.last_error or "unreachable")
        session = await self.voice.connect(guild_id, channel_id)
        if session.listener is not None and not session.listener.closed:
            return session
        if self._on_start is not None:
            await self._on_start(str(guild_id))
        listener = Listener(self.studio, session, bot_name=self.bot_name,
                            reply=self._reply, post=self._post, fx=self.fx,
                            opted_out=self._opted_out, caws=self.caws,
                            caw_chance=self.caw_chance,
                            barge_in=self.barge_in)
        if self._on_stop is not None:
            gid = str(guild_id)
            listener.on_close = lambda: self._on_stop(gid)
        try:
            listener.start()
        except Exception:
            await listener.close()
            raise
        session.listener = listener
        listener.warm_speech_track()
        return session

    async def stop(self, guild_id: str) -> bool:
        """Stop listening. True if he was."""
        session = self.voice.get(guild_id)
        if session is None or session.listener is None:
            return False
        await session.close_listener()
        return True
