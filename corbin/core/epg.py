"""What's on: the programme guide for live TV (T4 of ``VOICE_ROADMAP.md``).

**iptv-org no longer publishes guides.** Its EPG project became "run the
grabber yourself" in 2023 (checked 2026-10-19: ``iptv-org.github.io/epg``
is a 404 and every server in its community list was down). The guide
mappings in ``guides.json`` mostly point at scrape *sites*, not files.

**What works: Pluto TV, keyed by the stream URL.** iptv-org's Pluto
streams are ``https://jmp2.uk/plu-<24 hex>.m3u8``, and that hex id is the
``<channel id>`` in i.mjh.nz's free Pluto XMLTV (checked: 2,262 of 2,332
streams matched). Pluto is the biggest single block of the catalogue, so
that covers a lot for one small download. Everything else shows no guide
rather than a guess (no fuzzy name matching against third-party guides:
a wrong programme title is worse than none).

Fetched **on demand**, never in the background: the first "what's on"
for a Pluto channel downloads that country's guide (~0.3-1 MB gzip),
falling back to the all-countries file (7 MB) when the id isn't in it,
and keeps it for :data:`CACHE_TTL`. Parsing is ``iterparse`` in a thread,
keeping only programmes near now.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("corbin.epg")

#: i.mjh.nz's Pluto guides: one per country, plus every country at once.
PLUTO_GUIDE = "https://i.mjh.nz/PlutoTV/{region}.xml.gz"
PLUTO_ALL = "all"
_PLUTO_ID = re.compile(r"/plu-([0-9a-f]{24})(?:\.m3u8|[/?#]|$)", re.I)
#: A guide is reused this long. Its listings run about a day ahead.
CACHE_TTL = 6 * 3600
#: A guide that failed to download isn't asked for again for this long.
FAIL_TTL = 30 * 60
#: Programmes kept around now: enough for "now" and "next".
KEEP_BEFORE, KEEP_AFTER = 3600, 8 * 3600
HTTP_TIMEOUT = 20.0
#: Refuse anything bigger, compressed: all.xml.gz measured 7.4 MB.
MAX_DOWNLOAD = 20_000_000

Fetch = Callable[[str], Awaitable[Optional[bytes]]]

#: One worker process for parsing guides, at nice 15; made on first use.
#: Not for the event loop (measured: a parse in a thread froze it for
#: ~30 ms at worst, harmless) but for the CPU: the host has one core, and
#: the 7 MB all-countries guide took 7.4 s of it on the server (live,
#: 2026-10-20). At normal priority that CPU comes out of ffmpeg and the
#: video encoder, which is stutter. At nice 15 the parse only gets what
#: the stream leaves: measured on one core, +1 s with a stream using 60%
#: of it, +7.5 s at 90%. A "what's on" answer waits a little longer
#: under load; the stream doesn't.
_POOL: Any = None


def _lower_priority() -> None:
    try:
        import os

        os.nice(15)
    except Exception:
        pass


async def parse_off_loop(data: bytes, now: float) -> dict:
    """:func:`parse_guide` in the low-priority worker process, so a parse
    can't stall playback. Falls back to a thread if the pool breaks."""
    global _POOL
    loop = asyncio.get_running_loop()
    try:
        if _POOL is None:
            from concurrent.futures import ProcessPoolExecutor

            _POOL = ProcessPoolExecutor(max_workers=1,
                                        initializer=_lower_priority)
        return await loop.run_in_executor(_POOL, parse_guide, data, now)
    except Exception:
        logger.warning("guide worker process failed; parsing in a thread",
                       exc_info=True)
        _POOL = None
        return await asyncio.to_thread(parse_guide, data, now)


class GuideUnavailable(RuntimeError):
    """The guide for this stream couldn't be downloaded. Distinct from
    "the guide doesn't list it": answering "no guide for that channel"
    when the download failed is the plausible wrong answer (live,
    2026-10-19: Investiga, a Pluto channel, "had no guide")."""


@dataclass(frozen=True)
class Programme:
    title: str
    start: float          # unix seconds
    stop: float
    subtitle: str = ""

    def remaining(self, now: float) -> int:
        """Whole minutes left, at least 1 while it's on."""
        return max(1, int((self.stop - now + 59) // 60))


def pluto_id(url: str) -> Optional[str]:
    """The Pluto channel id in an iptv-org Pluto stream URL, or None."""
    m = _PLUTO_ID.search(url or "")
    return m.group(1).lower() if m else None


def parse_time(text: str) -> Optional[float]:
    """XMLTV ``20261019215100 +0000`` -> unix seconds. None if unreadable
    (a guide entry with a broken time is skipped, never guessed)."""
    text = (text or "").strip()
    m = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?$", text)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    offset = 0
    if m.group(2):
        sign = -1 if m.group(2)[0] == "-" else 1
        offset = sign * (int(m.group(2)[1:3]) * 3600
                         + int(m.group(2)[3:5]) * 60)
    return dt.replace(tzinfo=timezone.utc).timestamp() - offset


def parse_guide(data: bytes, now: float) -> dict:
    """``{channel id: [Programme, ...]}`` sorted by start, keeping only
    programmes overlapping [now - KEEP_BEFORE, now + KEEP_AFTER].
    ``data`` may be gzip or plain XML. Blocking: run in a thread."""
    import xml.etree.ElementTree as ET

    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    lo, hi = now - KEEP_BEFORE, now + KEEP_AFTER
    out: dict = {}
    for _event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag != "programme":
            if el.tag == "channel":
                el.clear()
            continue
        start = parse_time(el.get("start", ""))
        stop = parse_time(el.get("stop", ""))
        cid = (el.get("channel") or "").lower()
        if start is not None and stop is not None and cid \
                and stop > lo and start < hi and stop > start:
            title = (el.findtext("title") or "").strip()
            if title:
                out.setdefault(cid, []).append(Programme(
                    title=title, start=start, stop=stop,
                    subtitle=(el.findtext("sub-title") or "").strip()))
        el.clear()
    for progs in out.values():
        progs.sort(key=lambda p: p.start)
    return out


def now_next(progs: list, now: float) -> tuple:
    """``(on now, next)`` from one channel's sorted programmes; either
    may be None."""
    current = nxt = None
    for p in progs:
        if p.start <= now < p.stop:
            current = p
        elif p.start >= now:
            nxt = p
            break
    return current, nxt


async def fetch_bytes(url: str) -> Optional[bytes]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                     follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as exc:
        logger.warning("guide download failed for %s: %s: %s", url,
                       type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        logger.warning("guide download for %s: HTTP %s", url,
                       resp.status_code)
        return None
    if len(resp.content) > MAX_DOWNLOAD:
        logger.warning("guide %s is %d bytes; refused", url,
                       len(resp.content))
        return None
    return resp.content


class Guide:
    """On-demand programme guide. ``lookup(stream, country)`` answers
    ``(now, next)`` or None when there's no guide for that stream."""

    def __init__(self, *, fetch: Fetch = fetch_bytes,
                 clock: Callable[[], float] = time.time,
                 parse: Optional[Callable[[bytes, float],
                                          Awaitable[dict]]] = None) -> None:
        self.fetch = fetch
        self.clock = clock
        #: How a downloaded guide is parsed (the worker process, by
        #: default; the tests pass an in-thread one).
        self.parse = parse or parse_off_loop
        #: url -> (fetched at, {channel id: programmes} or None if failed)
        self._cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def covers(self, url: str) -> bool:
        """Could this stream have a guide at all? (Free; no download.)"""
        return pluto_id(url) is not None

    async def _guide(self, url: str) -> Optional[dict]:
        now = self.clock()
        cached = self._cache.get(url)
        if cached is not None:
            age = now - cached[0]
            if cached[1] is not None and age < CACHE_TTL:
                return cached[1]
            if cached[1] is None and age < FAIL_TTL:
                return None
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            cached = self._cache.get(url)
            if cached is not None and cached[0] > now - 1:
                return cached[1]
            began = time.monotonic()
            data = await self.fetch(url)
            guide = None
            if data:
                try:
                    guide = await self.parse(data, self.clock())
                except Exception as exc:
                    logger.warning("guide %s didn't parse: %s", url, exc)
            self._cache[url] = (self.clock(), guide)
            if guide is not None:
                logger.info("guide %s: %d channels in %.1fs", url,
                            len(guide), time.monotonic() - began)
            return guide

    def peek(self, stream_url: str, country: str = "") -> Optional[tuple]:
        """:meth:`lookup` from what's already downloaded: never fetches,
        never raises. For listings re-rendered on a page turn, where
        there's no time to download (and :meth:`lookup` already ran for
        the first page)."""
        pid = pluto_id(stream_url)
        if pid is None:
            return None
        now = self.clock()
        for region in dict.fromkeys(r for r in ((country or "").lower(),
                                                PLUTO_ALL) if r):
            cached = self._cache.get(PLUTO_GUIDE.format(region=region))
            guide = cached[1] if cached else None
            if guide and pid in guide:
                current, nxt = now_next(guide[pid], now)
                if current or nxt:
                    return current, nxt
        return None

    async def warm(self, country: str = "") -> bool:
        """Download the Pluto guides a listing will peek at. False if
        none could be had."""
        ok = False
        for region in dict.fromkeys(r for r in ((country or "").lower(),
                                                PLUTO_ALL) if r):
            try:
                ok = await self._guide(
                    PLUTO_GUIDE.format(region=region)) is not None or ok
            except Exception:
                logger.warning("guide warm-up failed", exc_info=True)
        return ok

    async def lookup(self, stream_url: str,
                     country: str = "") -> Optional[tuple]:
        """``(on now, next)`` for a stream, or None when there's no guide
        for it or the guide doesn't list it. Raises
        :class:`GuideUnavailable` when the guide it needed couldn't be
        downloaded at all."""
        pid = pluto_id(stream_url)
        if pid is None:
            return None
        regions = [r for r in ((country or "").lower(), PLUTO_ALL) if r]
        now = self.clock()
        got_any = False
        for region in dict.fromkeys(regions):
            try:
                guide = await self._guide(PLUTO_GUIDE.format(region=region))
            except Exception:
                logger.warning("guide lookup failed", exc_info=True)
                guide = None
            if guide is None:
                continue
            got_any = True
            if pid in guide:
                current, nxt = now_next(guide[pid], now)
                if current or nxt:
                    return current, nxt
        if not got_any:
            raise GuideUnavailable("the Pluto TV guide couldn't be "
                                   "downloaded")
        return None


def short(found: Optional[tuple], now: float) -> str:
    """One listing line's worth: "Star Trek: The Cage (25 min left)",
    or "next: X" when nothing is on yet, or ""."""
    if not found:
        return ""
    current, nxt = found
    if current is not None:
        return f"{current.title} ({current.remaining(now)} min left)"
    return f"next: {nxt.title}" if nxt is not None else ""


def describe(found: Optional[tuple], now: float) -> str:
    """"On now: Star Trek (32 min left) · Next: Voyager", or ""."""
    if not found:
        return ""
    current, nxt = found
    bits = []
    if current is not None:
        title = current.title + (f": {current.subtitle}"
                                 if current.subtitle else "")
        bits.append(f"On now: **{title}** "
                    f"({current.remaining(now)} min left)")
    if nxt is not None:
        bits.append(f"Next: {nxt.title}")
    return " · ".join(bits)
