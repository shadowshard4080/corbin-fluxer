#!/usr/bin/env python3
"""Is streamed transcription worth turning on? Phase 0b / A4 of
``VOICE_ROADMAP.md``. Run it ON THE SERVER, with the bot's venv, while
VoiceStudio is up on the GPU box:

    .venv/bin/python tools/check_voicestudio.py
    .venv/bin/python tools/check_voicestudio.py --wav me_asking.wav \\
        --expect "hey corbin what's the weather in denver tomorrow"
    .venv/bin/python tools/check_voicestudio.py --models "" sherpa-zipformer-en-20m

What it does, through the bot's own client code (``corbin.core.voicechat``,
so it tests what the bot would actually run):

1. **Reachability**: ``/health``, the speech discovery document, and the
   ASR engines VoiceStudio reports.
2. **A test utterance**: your ``--wav``, or a sentence spoken by his own
   voice profile through ``/generate``. A real recording of a person is a
   better test (TTS is cleaner than a microphone); use ``--wav`` once you
   have one. Converted to 16 kHz mono with ffmpeg if it isn't already.
3. **The upload route** (what the bot does today): one POST of the whole
   utterance. Timed: that's ``stt=`` in the ``voice turn`` log line.
4. **The streaming route** for each ``--models`` entry ("" is
   VoiceStudio's default engine): the utterance sent in real time, 20 ms
   chunks, then the 0.8 s pause the bot waits for, then the end frame.
   Timed from the end frame to the final transcript: that's what ``stt=``
   would become.
5. **Accuracy**: each transcript against ``--expect`` (word error rate),
   and whether the bot's name survived (the socket takes no vocabulary hint).

Then it says which settings to put in ``.env``, if any. It doesn't touch
the bot, its settings or its voice connection, so the bot can keep
running. A sherpa model that isn't installed may be **downloaded** by
VoiceStudio on first use (``status: downloading``): give it time, or run
it again.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_SENTENCE = "Hey {name}, what's the weather going to be like in Denver tomorrow?"
CHUNK_MS = 20


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split()


def wer(ref: str, hyp: str) -> float:
    """Word error rate: edits to turn hyp into ref, over ref's length."""
    a, b = _words(ref), _words(hyp)
    if not a:
        return 0.0 if not b else 1.0
    prev = list(range(len(b) + 1))
    for i, wa in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, wb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (wa != wb))
        prev = cur
    return prev[-1] / len(a)


def to_pcm16k(data: bytes) -> bytes:
    """Any audio -> raw s16le mono 16 kHz, via ffmpeg unless it's already
    a 16 kHz mono 16-bit WAV."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == \
                    (16000, 1, 2):
                return w.readframes(w.getnframes())
    except Exception:
        pass
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le",
         "-ar", "16000", "-ac", "1", "pipe:1"],
        input=data, capture_output=True, check=True)
    return out.stdout


async def get_json(client, url: str, headers: dict):
    try:
        resp = await client.get(url, headers=headers)
        body = resp.json() if resp.headers.get("content-type", "").startswith(
            "application/json") else resp.text[:300]
        return resp.status_code, body
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


async def stream_once(studio, pcm: bytes, model: str,
                      final_timeout: float) -> dict:
    """The bot's LiveTranscript, fed in real time like a voice channel."""
    from corbin.core import voicechat as vc

    studio.cfg.stt_model = model
    live = studio.live()
    step = 16000 * 2 * CHUNK_MS // 1000
    pause = b"\0\0" * int(16000 * vc.Segmenter.HANGOVER)
    audio = pcm + pause
    began = time.monotonic()
    live.start(audio[:step])
    for i in range(step, len(audio), step):
        await asyncio.sleep(CHUNK_MS / 1000)
        live.feed(audio[i:i + step])
    task = live.finish()
    ended = time.monotonic()
    result = {"model": model or "(default)", "text": "", "error": "",
              "final_after_end": None, "first_partial": None,
              "engine": ""}
    try:
        result["text"] = await asyncio.wait_for(task, final_timeout)
        result["final_after_end"] = time.monotonic() - ended
    except asyncio.TimeoutError:
        live.abort()
        result["error"] = f"no final within {final_timeout:.0f}s"
    except Exception as exc:
        result["error"] = vc.describe_error(exc)
    if live.first_partial is not None:
        result["first_partial"] = live.first_partial - began
    result["engine"] = live.engine
    return result


async def main() -> int:
    import config

    NAME = config.BOT_NAME.lower()
    SENTENCE = _SENTENCE.format(name=config.BOT_NAME)
    from corbin.core.voicechat import (DISCOVERY_PATH, StudioConfig,
                                       VoiceStudio, pcm_to_wav)

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default=config.VOICESTUDIO_URL)
    ap.add_argument("--wav", help="a recording to test with (any format)")
    ap.add_argument("--expect", help="what the recording says")
    ap.add_argument("--models", nargs="*",
                    default=["", "sherpa-zipformer-en-20m"],
                    help='socket models to try; "" is the default engine')
    ap.add_argument("--final-timeout", type=float, default=60.0,
                    help="seconds to wait for a final (a model download "
                         "can take a while)")
    args = ap.parse_args()

    import httpx

    studio = VoiceStudio(StudioConfig(
        url=args.url, api_key=config.VOICESTUDIO_API_KEY,
        voice=config.VOICESTUDIO_VOICE, steps=config.VOICESTUDIO_STEPS,
        language=config.VOICESTUDIO_LANGUAGE, vocabulary=f"{config.BOT_NAME}."))
    headers = studio._headers()
    print(f"VoiceStudio at {studio.base}\n")

    async with httpx.AsyncClient(timeout=15.0) as client:
        for path in ("/health", DISCOVERY_PATH, "/engines/asr",
                     "/system/asr-backends"):
            code, body = await get_json(client, studio.base + path, headers)
            text = json.dumps(body)[:400] if not isinstance(body, str) \
                else body
            print(f"  GET {path:<34} {code}  {text}")
    if not await studio.probe():
        print(f"\nVoiceStudio isn't reachable: {studio.last_error}")
        return 1

    if args.wav:
        raw = Path(args.wav).read_bytes()
        expect = args.expect or ""
        print(f"\nUsing {args.wav}" + (f": expecting {expect!r}"
                                       if expect else
                                       " (no --expect: no accuracy score)"))
    else:
        print(f"\nNo --wav: having his voice say {SENTENCE!r}")
        raw = await studio.speak(SENTENCE)
        expect = args.expect or SENTENCE
    pcm = to_pcm16k(raw)
    seconds = len(pcm) / 32000
    print(f"  {seconds:.1f}s of audio")

    print("\nUpload route (today):")
    t = time.monotonic()
    try:
        batch_text = await studio.transcribe(pcm_to_wav(pcm))
        batch_time = time.monotonic() - t
        print(f"  {batch_time:.2f}s  {batch_text!r}")
    except Exception as exc:
        batch_text, batch_time = "", None
        print(f"  FAILED: {exc}")

    print("\nStreaming route, real time, 20 ms chunks:")
    results = []
    for model in args.models:
        res = await stream_once(studio, pcm, model, args.final_timeout)
        results.append(res)
        if res["error"]:
            print(f"  {res['model']:<26} FAILED: {res['error']}")
            continue
        fp = (f", first partial at {res['first_partial']:.2f}s"
              if res["first_partial"] is not None else ", no partials")
        print(f"  {res['model']:<26} final {res['final_after_end']:.2f}s "
              f"after the end{fp}  [{res['engine'] or '?'}]  "
              f"{res['text']!r}")

    print("\nVerdict:")
    if expect:
        if batch_time is not None:
            print(f"  upload     wer {wer(expect, batch_text):.0%}, "
                  f"name {'kept' if NAME in batch_text.lower() else 'LOST'}")
        for res in results:
            if not res["error"]:
                print(f"  {res['model']:<10} wer {wer(expect, res['text']):.0%}, "
                      f"name {'kept' if NAME in res['text'].lower() else 'LOST'}")
    good = [r for r in results if not r["error"] and r["text"]
            and (not expect or wer(expect, r["text"])
                 <= wer(expect, batch_text) + 0.10)
            and (not expect or NAME in r["text"].lower()
                 or NAME not in expect.lower())]
    if batch_time is None and good:
        best = min(good, key=lambda r: r["final_after_end"])
    else:
        faster = [r for r in good if batch_time is not None
                  and r["final_after_end"] < batch_time * 0.7]
        best = min(faster, key=lambda r: r["final_after_end"]) \
            if faster else None
    if best is None:
        print("  Keep the upload route (VOICE_STREAM_STT=0): no "
              "streaming option was clearly faster without hurting "
              "accuracy or losing his name.")
        return 0
    saved = (batch_time - best["final_after_end"]) if batch_time else 0
    print(f"  Turn it on: {best['model']} saves ~{saved:.1f}s a turn.")
    print("  Put these in the bot's .env and restart it:\n")
    print("    VOICE_STREAM_STT=1")
    model = "" if best["model"] == "(default)" else best["model"]
    print(f"    VOICESTUDIO_STT_MODEL={model}")
    print("\n  Then watch the 'voice turn' lines: stt= should drop and say "
          "(streamed). '(batch after stream failed)' means it fell back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
