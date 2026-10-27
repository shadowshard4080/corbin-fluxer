"""A local relay that fetches YouTube streams in byte ranges.

**Why** (measured 2026-10-20, Thriller's audio): YouTube's video servers
(``*.googlevideo.com``) pace one long open-ended download of a DASH
stream to about playback speed, but serve byte-range requests at full
line speed:

    one open-ended request (what ffmpeg does)    31 KB/s =    2.0x real time
    10 MB byte ranges (what yt-dlp does)      25,066 KB/s = 1,586x real time

From the server's data-centre address the pacing was worse: a 1080p
music video ran at "clock 0.51x, audio buffer 0.0s", the song's own
audio arriving at half speed while the local video files (no YouTube)
played flawlessly. yt-dlp downloads in ~10 MB chunks for this reason.

**How:** ffmpeg is handed ``http://127.0.0.1:<port>/r/<token>`` instead of
the googlevideo URL. The relay answers ffmpeg's own ``Range`` requests
(so ``-ss`` seeking still works) by fetching the same bytes upstream in
``CHUNK``-sized ranges and passing them straight on. Nothing is stored
on disk; at most one chunk is in flight per stream.

Only googlevideo URLs are relayed (``wrap``); anything else, or a relay
that isn't running, gets the original URL back, so the worst case is the
old behaviour, never a broken stream.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("corbin.relay")

CHUNK = 10 * 1024 * 1024
#: A relayed URL stays valid this long (YouTube's own links last ~6 h).
TOKEN_TTL = 6 * 3600
MAX_TOKENS = 200
UPSTREAM_TIMEOUT = 30.0
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def is_relayable(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host.endswith(".googlevideo.com")


def content_length(url: str) -> Optional[int]:
    """googlevideo URLs carry their size as ``clen=``."""
    try:
        v = parse_qs(urlparse(url).query).get("clen", [None])[0]
        return int(v) if v else None
    except (ValueError, TypeError):
        return None


def parse_range(header: Optional[str], total: int) -> Optional[tuple]:
    """``(start, end)`` inclusive for a ``Range`` header against
    ``total`` bytes; ``(0, total-1)`` with no header; None if it can't be
    satisfied. Suffix ranges ("bytes=-500") count from the end."""
    if total <= 0:
        return None
    if not header:
        return 0, total - 1
    m = _RANGE.fullmatch(header.strip())
    if not m:
        return 0, total - 1
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return 0, total - 1
    if a == "":
        n = int(b)
        return (max(0, total - n), total - 1) if n > 0 else None
    start = int(a)
    end = min(int(b), total - 1) if b else total - 1
    if start >= total or start > end:
        return None
    return start, end


def plan(start: int, end: int, chunk: int = CHUNK) -> list:
    """The upstream ranges covering [start, end]."""
    out, pos = [], start
    while pos <= end:
        stop = min(end, pos + chunk - 1)
        out.append((pos, stop))
        pos = stop + 1
    return out


class RangeRelay:
    def __init__(self, chunk: int = CHUNK) -> None:
        self.chunk = chunk
        self.port: Optional[int] = None
        #: token -> (url, headers, total or None, created)
        self._tokens: dict[str, tuple] = {}
        self._runner: Any = None
        self._client: Any = None

    @property
    def running(self) -> bool:
        return self.port is not None

    async def start(self) -> bool:
        """Listen on 127.0.0.1 (a free port). False, with a warning, if
        it can't: streams then go direct, as before."""
        if self.running:
            return True
        try:
            import httpx
            from aiohttp import web

            app = web.Application()
            app.router.add_get("/r/{token}", self._handle)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            sock = site._server.sockets[0]          # the port we were given
            self.port = sock.getsockname()[1]
            self._runner = runner
            self._client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT,
                                             follow_redirects=True)
        except Exception:
            logger.warning("YouTube range relay couldn't start; streams go "
                           "direct (and may be throttled)", exc_info=True)
            self.port = None
            return False
        logger.info("YouTube range relay on 127.0.0.1:%d", self.port)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._client is not None:
            await self._client.aclose()
        self.port = None

    def wrap(self, url: str, headers: Optional[dict] = None) -> str:
        """The URL ffmpeg should open: a relayed one for a googlevideo
        stream while the relay runs, else ``url`` unchanged."""
        if not self.running or not url or not is_relayable(url):
            return url
        now = time.monotonic()
        for tok, entry in list(self._tokens.items()):
            if now - entry[3] > TOKEN_TTL:
                del self._tokens[tok]
        while len(self._tokens) >= MAX_TOKENS:
            self._tokens.pop(next(iter(self._tokens)))
        token = secrets.token_urlsafe(12)
        self._tokens[token] = (url, dict(headers or {}),
                               content_length(url), now)
        return f"http://127.0.0.1:{self.port}/r/{token}"

    async def _total(self, token: str) -> Optional[int]:
        url, headers, total, created = self._tokens[token]
        if total:
            return total
        # No clen: ask for one byte and read the size off Content-Range.
        r = await self._client.get(url, headers={**self._headers(headers),
                                                 "Range": "bytes=0-0"})
        m = re.search(r"/(\d+)", r.headers.get("content-range", ""))
        total = int(m.group(1)) if m else None
        self._tokens[token] = (url, headers, total, created)
        return total

    @staticmethod
    def _headers(headers: dict) -> dict:
        h = {k: v for k, v in headers.items()
             if k.lower() not in ("range", "host", "accept-encoding")}
        h.setdefault("User-Agent", DEFAULT_UA)
        return h

    async def _handle(self, request: Any) -> Any:
        from aiohttp import web

        token = request.match_info["token"]
        if token not in self._tokens:
            return web.Response(status=404)
        url, headers = self._tokens[token][0], self._tokens[token][1]
        try:
            total = await self._total(token)
        except Exception as exc:
            logger.info("relay: size lookup failed: %s", exc)
            return web.Response(status=502)
        if not total:
            return web.Response(status=502)
        span = parse_range(request.headers.get("Range"), total)
        if span is None:
            return web.Response(status=416, headers={
                "Content-Range": f"bytes */{total}"})
        start, end = span
        partial = "Range" in request.headers
        resp = web.StreamResponse(
            status=206 if partial else 200,
            headers={"Accept-Ranges": "bytes",
                     "Content-Length": str(end - start + 1),
                     "Content-Type": "application/octet-stream",
                     **({"Content-Range": f"bytes {start}-{end}/{total}"}
                        if partial else {})})
        await resp.prepare(request)
        sent = 0
        try:
            for a, b in plan(start, end, self.chunk):
                async with self._client.stream(
                        "GET", url, headers={**self._headers(headers),
                                             "Range": f"bytes={a}-{b}"}) as up:
                    if up.status_code >= 400:
                        logger.info("relay: upstream HTTP %s at byte %d",
                                    up.status_code, a)
                        break
                    async for data in up.aiter_bytes():
                        await resp.write(data)
                        sent += len(data)
        except (ConnectionResetError, ConnectionError):
            pass                          # ffmpeg closed it (seek, stop)
        except Exception as exc:
            logger.info("relay: stream ended early after %d bytes: %s",
                        sent, type(exc).__name__)
        return resp


#: The process's relay; bot.py starts it. ``wrap`` is a no-op until then.
RELAY = RangeRelay()
