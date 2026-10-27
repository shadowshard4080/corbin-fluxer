#!/usr/bin/env python3
"""How much of iptv-org actually plays from this server. Run this ON THE
SERVER (Phase 0f of ``VOICE_ROADMAP.md``); its numbers size Track T.

iptv-org is user-submitted links. A large share is expected to be dead,
geo-blocked from wherever this host sits, or up only some of the day, and
the ``Geo-blocked`` label is only as good as whoever submitted the stream.
So this samples streams across countries and categories and, for each one,
records what ffmpeg on *this* host makes of it:

* does it open (ffprobe, ``--open-timeout``, default 10 s) and how long that took
* how long to the first decoded frame
* resolution, codec and bitrate, plus every resolution an HLS master offers
* CPU seconds to decode ``--decode`` (5 s) of it and scale to 720p, since
  the host is one core and that is what T2 will pay for every viewer-less
  second of playback
* why it failed when it did (timeout, 403, 404, DNS, ...)

then prints a summary: the ok rate overall and by label, start-up times,
the resolution and codec mix, and how long a full health sweep of the
catalogue would take at the measured pace.

Needs ffmpeg/ffprobe on PATH and nothing else (stdlib only). It does not
touch the bot, its settings or its voice connection, so the bot can keep
running; the timings are kinder if it isn't streaming at the time.

    python3 tools/check_iptv.py
    python3 tools/check_iptv.py --sample 100 --concurrency 2
    python3 tools/check_iptv.py --country GB --category news
    python3 tools/check_iptv.py --include-geo     # also try labelled ones
    python3 tools/check_iptv.py --out iptv_check.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field

API = "https://iptv-org.github.io/api"
#: What the bot's ffmpeg already sends for live audio and music video.
RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "5"]
#: Quoted from the label the project uses, so a rename shows up here.
GEO_LABEL = "Geo-blocked"
NOT_247_LABEL = "Not 24/7"


# ---------------------------------------------------------------- catalogue

def fetch_json(name: str) -> list:
    req = urllib.request.Request(f"{API}/{name}",
                                 headers={"User-Agent": "corbin-check-iptv"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def load_catalogue(include_geo: bool) -> tuple[list[dict], Counter]:
    """Streams joined to their channels, filtered as T1 will filter them.
    Returns the playable list and a count of what was dropped and why."""
    channels = {c["id"]: c for c in fetch_json("channels.json")}
    streams = fetch_json("streams.json")
    blocked = {b.get("channel") for b in fetch_json("blocklist.json")}

    dropped: Counter = Counter()
    out = []
    for s in streams:
        cid = s.get("channel")
        chan = channels.get(cid) if cid else None
        # Follow a closed channel to its replacement, once.
        if chan and chan.get("closed") and chan.get("replaced_by"):
            repl = channels.get(str(chan["replaced_by"]).split("@")[0])
            if repl and not repl.get("closed"):
                chan = repl
        labels = s.get("labels") or []
        if chan is None:
            dropped["no channel record"] += 1
        elif chan.get("closed"):
            dropped["channel closed"] += 1
        elif chan.get("is_nsfw"):
            dropped["nsfw"] += 1
        elif chan["id"] in blocked:
            dropped["blocklisted"] += 1
        elif GEO_LABEL in labels and not include_geo:
            dropped["labelled Geo-blocked"] += 1
        elif not s.get("url"):
            dropped["no url"] += 1
        else:
            out.append({
                "channel": chan["id"],
                "name": chan.get("name") or chan["id"],
                "country": chan.get("country") or "?",
                "categories": chan.get("categories") or ["(none)"],
                "url": s["url"],
                "referrer": s.get("referrer"),
                "user_agent": s.get("user_agent"),
                "quality": s.get("quality"),
                "labels": labels,
            })
    return out, dropped


def sample(streams: list[dict], n: int, seed: int,
           country: str | None, category: str | None) -> list[dict]:
    """Spread the sample: one stream per channel, preferring a country and
    a category not yet picked, so the result isn't 40 US news channels."""
    pool = [s for s in streams
            if (not country or s["country"].upper() == country.upper())
            and (not category or category.lower()
                 in (c.lower() for c in s["categories"]))]
    random.Random(seed).shuffle(pool)
    picked: list[dict] = []
    seen_chan: set = set()
    seen_country: set = set()
    seen_cat: set = set()
    # Pass 0 wants a new country AND a new category, pass 1 either, pass 2
    # anything (still one per channel).
    for want in (2, 1, 0):
        for s in pool:
            if len(picked) >= n:
                return picked
            if s["channel"] in seen_chan:
                continue
            novelty = ((s["country"] not in seen_country)
                       + (s["categories"][0] not in seen_cat))
            if novelty < want:
                continue
            picked.append(s)
            seen_chan.add(s["channel"])
            seen_country.add(s["country"])
            seen_cat.add(s["categories"][0])
    return picked


# ------------------------------------------------------------------ probing

@dataclass
class Result:
    name: str
    channel: str
    country: str
    category: str
    url: str
    labels: list
    ok: bool = False
    failure: str = ""
    detail: str = ""
    open_s: float | None = None
    first_frame_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    bitrate_kbps: int | None = None
    variant_heights: list = field(default_factory=list)
    decode_cpu_s: float | None = None
    decoded_s: float | None = None


def header_args(s: dict) -> list[str]:
    args = []
    if s.get("user_agent"):
        args += ["-user_agent", s["user_agent"]]
    if s.get("referrer"):
        args += ["-headers", f"Referer: {s['referrer']}\r\n"]
    return args


_FAILURES = [
    ("403 forbidden", re.compile(r"403|Forbidden", re.I)),
    ("404 not found", re.compile(r"404|Not Found", re.I)),
    ("other 4xx", re.compile(r"Server returned 4\d\d|HTTP error 4\d\d", re.I)),
    ("5xx", re.compile(r"Server returned 5(\d\d|XX)|HTTP error 5\d\d", re.I)),
    ("dns", re.compile(r"resolve|Name or service not known|"
                       r"nodename nor servname", re.I)),
    ("refused", re.compile(r"Connection refused", re.I)),
    ("unreachable", re.compile(r"No route to host|Network is "
                               r"unreachable", re.I)),
    ("tls", re.compile(r"TLS|SSL|certificate|handshake", re.I)),
    ("timeout", re.compile(r"timed out|Timeout", re.I)),
    ("no video", re.compile(r"no video stream", re.I)),
    ("bad data", re.compile(r"Invalid data|could not find codec|"
                            r"Error opening input", re.I)),
]


def classify(stderr: str) -> str:
    for name, pat in _FAILURES:
        if pat.search(stderr):
            return name
    return "other"


def last_error_line(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1][:160] if lines else ""


async def run(args: list[str], timeout: float,
              on_line=None) -> tuple[int | None, str, str]:
    """(returncode or None on timeout, stdout, stderr).

    Both pipes are drained by their own task the whole time, and the
    process is killed in ``finally``, so a timeout, an error or a
    cancellation can never leave an ffmpeg running. Every wait after the
    kill is bounded too. (The first version read stderr in one task and
    then called communicate(), which read it again: RuntimeError, the
    whole run aborted, and the other ffmpeg was never killed -- it hung
    at 24/50 on the server.) ``on_line(text)`` sees stdout line by line."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out_parts: list[str] = []

    async def read_out() -> None:
        async for raw in proc.stdout:
            text = raw.decode(errors="replace")
            out_parts.append(text)
            if on_line:
                on_line(text)

    out_task = asyncio.ensure_future(read_out())
    err_task = asyncio.ensure_future(proc.stderr.read())
    timed_out = False
    try:
        done, _ = await asyncio.wait({out_task, err_task}, timeout=timeout)
        timed_out = len(done) < 2
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        # After a kill the pipes close at once, unless something else
        # holds them; don't wait forever on that.
        await asyncio.wait({out_task, err_task}, timeout=5)
        for t in (out_task, err_task):
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
    err = (err_task.result().decode(errors="replace")
           if err_task.done() and not err_task.cancelled()
           and err_task.exception() is None else "")
    code = None if timed_out else proc.returncode
    return code, "".join(out_parts), err


def _fps(rate: str | None) -> float | None:
    try:
        num, den = (rate or "").split("/")
        return round(int(num) / int(den), 2) if int(den) else None
    except ValueError:
        return None


async def probe_open(s: dict, r: Result, timeout: float) -> bool:
    t0 = time.monotonic()
    code, out, err = await run(
        ["ffprobe", "-v", "error", "-rw_timeout", str(int(timeout * 1e6)),
         *header_args(s), "-show_streams", "-show_format", "-of", "json",
         s["url"]], timeout)
    r.open_s = round(time.monotonic() - t0, 2)
    if code is None:
        r.failure, r.detail = "timeout", f"no answer in {timeout:.0f}s"
        return False
    if code != 0:
        r.failure, r.detail = classify(err), last_error_line(err)
        return False
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        r.failure, r.detail = "bad data", "ffprobe printed no JSON"
        return False
    videos = [st for st in info.get("streams", [])
              if st.get("codec_type") == "video" and st.get("height")]
    if not videos:
        r.failure, r.detail = "no video", "opened, but no video stream"
        return False
    r.variant_heights = sorted({int(v["height"]) for v in videos})
    # ffmpeg picks the highest-resolution variant by default; describe that.
    best = max(videos, key=lambda v: int(v["height"]))
    r.width, r.height = int(best.get("width") or 0), int(best["height"])
    r.codec = best.get("codec_name")
    r.fps = _fps(best.get("avg_frame_rate")) or _fps(best.get("r_frame_rate"))
    rate = ((best.get("tags") or {}).get("variant_bitrate")
            or best.get("bit_rate")
            or (info.get("format") or {}).get("bit_rate"))
    try:
        r.bitrate_kbps = int(rate) // 1000 if rate else None
    except ValueError:
        pass
    return True


_PROGRESS_FRAME = re.compile(r"^frame=(\d+)", re.M)
_BENCH = re.compile(r"bench: utime=([\d.]+)s")
_OUT_TIME = re.compile(r"^out_time_us=(\d+)", re.M)


async def probe_decode(s: dict, r: Result, seconds: float,
                       timeout: float) -> None:
    """Decode ``seconds`` of the stream, scaled to 720p as T2 will, and
    note when the first frame appeared and the CPU time it cost."""
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-benchmark",
            "-loglevel", "info", "-nostats", "-progress", "pipe:1",
            "-stats_period", "0.2", *RECONNECT, *header_args(s),
            "-i", s["url"], "-t", str(seconds), "-map", "0:v:0", "-an",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease",
            "-pix_fmt", "yuv420p", "-f", "null", "-"]
    t0 = time.monotonic()

    def watch(text: str) -> None:
        m = _PROGRESS_FRAME.match(text)
        if m and int(m.group(1)) > 0 and r.first_frame_s is None:
            r.first_frame_s = round(time.monotonic() - t0, 2)

    code, progress, err = await run(args, timeout, on_line=watch)
    timed_out = code is None

    if (m := _BENCH.search(err)):
        r.decode_cpu_s = float(m.group(1))
    times = _OUT_TIME.findall(progress)
    if times:
        r.decoded_s = round(int(times[-1]) / 1e6, 2)
    if r.first_frame_s is None:
        r.failure = "timeout" if timed_out else classify(err)
        r.detail = ("opened, but no frame in "
                    f"{timeout:.0f}s" if timed_out else last_error_line(err))
        return
    r.ok = True
    if timed_out:
        # Frames arrived but slower than the decode window: worth knowing.
        r.detail = f"slow: {r.decoded_s or 0:.1f}s of video in {timeout:.0f}s"


async def check(s: dict, a: argparse.Namespace) -> Result:
    r = Result(name=s["name"], channel=s["channel"], country=s["country"],
               category=s["categories"][0], url=s["url"], labels=s["labels"])
    decode_timeout = a.decode + a.open_timeout + 10

    async def both() -> None:
        if await probe_open(s, r, a.open_timeout):
            await probe_decode(s, r, a.decode, decode_timeout)

    # A backstop over the per-process timeouts: one stream can never stall
    # the run. Cancelling kills its ffmpeg (run()'s finally).
    try:
        await asyncio.wait_for(both(), a.open_timeout + decode_timeout + 30)
    except asyncio.TimeoutError:
        r.ok, r.failure, r.detail = False, "timeout", "probe overran; killed"
    except Exception as exc:
        r.ok, r.failure = False, "probe error"
        r.detail = f"{type(exc).__name__}: {exc}"[:160]
    return r


def report_line(i: int, n: int, r: Result) -> str:
    tag = "  ok  " if r.ok else " FAIL "
    labels = f" [{', '.join(r.labels)}]" if r.labels else ""
    head = f"[{tag}] {i:>3}/{n} {r.name[:30]:<30} {r.country:<3} " \
           f"{r.category[:13]:<13}"
    if r.ok:
        cpu = (f" cpu {r.decode_cpu_s / r.decoded_s:.2f}x"
               if r.decode_cpu_s and r.decoded_s else "")
        return (f"{head} {r.width}x{r.height} {r.codec} "
                f"{r.bitrate_kbps or '?'}k first frame "
                f"{r.first_frame_s:.1f}s{cpu}{labels}"
                + (f"  ({r.detail})" if r.detail else ""))
    return f"{head} {r.failure}: {r.detail}{labels}"


# ------------------------------------------------------------------ summary

def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100 * part / whole:.0f}%)" if whole else "0/0"


def quantiles(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    xs = sorted(xs)
    p90 = xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]
    return f"median {statistics.median(xs):.1f}s, p90 {p90:.1f}s, " \
           f"max {xs[-1]:.1f}s"


def height_bucket(h: int | None) -> str:
    if not h:
        return "?"
    for limit, name in ((480, "<=480p"), (576, "576p"), (720, "720p"),
                        (1080, "1080p")):
        if h <= limit:
            return name
    return ">1080p"


def summarise(results: list[Result], catalogue: int, dropped: Counter,
              wall: float, concurrency: int) -> None:
    n = len(results)
    ok = [r for r in results if r.ok]
    print("\n== Summary ==")
    print(f"catalogue after filtering: {catalogue} streams")
    for why, count in dropped.most_common():
        print(f"  dropped {count:>6}  {why}")
    print(f"\nplays from here: {pct(len(ok), n)}")
    for label in (NOT_247_LABEL, GEO_LABEL):
        sub = [r for r in results if label in r.labels]
        if sub:
            print(f"  labelled {label}: {pct(sum(r.ok for r in sub), len(sub))}")
    plain = [r for r in results if not r.labels]
    print(f"  no labels: {pct(sum(r.ok for r in plain), len(plain))}")

    fails = Counter(r.failure for r in results if not r.ok)
    if fails:
        print("\nwhy the rest failed:")
        for why, count in fails.most_common():
            print(f"  {count:>4}  {why}")

    print(f"\nopen (ffprobe), playable ones: "
          f"{quantiles([r.open_s for r in ok if r.open_s])}")
    print(f"first frame (ffmpeg):         "
          f"{quantiles([r.first_frame_s for r in ok if r.first_frame_s])}")
    failed_open = [r.open_s for r in results if not r.ok and r.open_s]
    print(f"time to give up on dead ones: {quantiles(failed_open)}")

    print("\nresolution (highest variant): " + ", ".join(
        f"{k} {v}" for k, v in Counter(
            height_bucket(r.height) for r in ok).most_common()))
    has720 = sum(1 for r in ok if any(h <= 720 and h >= 540
                                      for h in r.variant_heights))
    print(f"  offer a 540-720p variant (no downscale needed): "
          f"{pct(has720, len(ok))}")
    print("codec: " + ", ".join(f"{k} {v}" for k, v in Counter(
        r.codec for r in ok).most_common()))
    rates = [r.bitrate_kbps for r in ok if r.bitrate_kbps]
    if rates:
        print(f"bitrate: median {statistics.median(rates):.0f} kbit/s, "
              f"max {max(rates)} kbit/s")
    loads = [r.decode_cpu_s / r.decoded_s for r in ok
             if r.decode_cpu_s and r.decoded_s]
    if loads:
        heavy = sum(1 for x in loads if x > 0.5)
        print(f"decode+scale CPU per second of video: median "
              f"{statistics.median(loads):.2f} cores, max {max(loads):.2f}; "
              f"{heavy} over 0.5 core (the encoder already takes ~0.25)")

    per = wall * concurrency / n if n else 0
    print(f"\npace: {wall:.0f}s for {n} streams at concurrency "
          f"{concurrency} (~{per:.1f}s of probe per stream)")
    if per:
        # The health check needs only the ffprobe half, not the decode.
        opens = [r.open_s for r in results if r.open_s]
        each = statistics.mean(opens) if opens else per
        for c in (1, 2, 4):
            hours = catalogue * each / c / 3600
            print(f"  a full ffprobe-only sweep of {catalogue} at "
                  f"concurrency {c}: ~{hours:.1f} h")


# --------------------------------------------------------------------- main

async def main_async(a: argparse.Namespace) -> int:
    print("fetching the iptv-org catalogue...")
    try:
        streams, dropped = load_catalogue(a.include_geo)
    except Exception as exc:
        print(f"[ FAIL ] couldn't fetch {API}: {type(exc).__name__}: {exc}")
        return 1
    picked = sample(streams, a.sample, a.seed, a.country, a.category)
    if not picked:
        print("[ FAIL ] nothing matched those filters")
        return 1
    countries = len({s["country"] for s in picked})
    cats = len({s["categories"][0] for s in picked})
    print(f"probing {len(picked)} streams ({countries} countries, {cats} "
          f"categories), {a.concurrency} at a time, seed {a.seed}\n")

    sem = asyncio.Semaphore(a.concurrency)
    results: list[Result] = []
    done = 0
    t0 = time.monotonic()

    async def one(s: dict) -> None:
        nonlocal done
        async with sem:
            r = await check(s, a)
        done += 1
        results.append(r)
        print(report_line(done, len(picked), r), flush=True)

    await asyncio.gather(*(one(s) for s in picked))
    wall = time.monotonic() - t0
    summarise(results, len(streams), dropped, wall, a.concurrency)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "args": vars(a), "catalogue": len(streams),
                       "dropped": dict(dropped),
                       "results": [asdict(r) for r in results]}, fh,
                      indent=1)
        print(f"\nwrote {a.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--sample", type=int, default=50)
    p.add_argument("--concurrency", type=int, default=2,
                   help="streams probed at once (host is one core)")
    p.add_argument("--open-timeout", type=float, default=10.0)
    p.add_argument("--decode", type=float, default=5.0,
                   help="seconds of video to decode per playable stream")
    p.add_argument("--seed", type=int, default=int(time.time()) % 10000)
    p.add_argument("--country", help="ISO code, e.g. GB")
    p.add_argument("--category", help="e.g. news")
    p.add_argument("--include-geo", action="store_true",
                   help="also try streams labelled Geo-blocked")
    p.add_argument("--out", help="write every result to this JSON file")
    a = p.parse_args()
    try:
        return asyncio.run(main_async(a))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
