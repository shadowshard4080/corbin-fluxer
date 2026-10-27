"""Offline suite for the YouTube range relay (corbin/core/relay.py).

Measured against real YouTube on 2026-10-20: one open-ended request for
Thriller's audio came at 2.0x real time, 10 MB byte ranges at 1,586x,
and real ffmpeg through the relay decoded it at 306x, seeking included.
These tests pin the range maths and, with a fake googlevideo server,
that the relay really fetches in chunks and passes the bytes through
intact. The end-to-end part needs aiohttp and httpx (both ship with the
bot); without them it says so and skips."""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import relay as R


def suite_maths(r: Results) -> None:
    yt = ("https://rr3---sn-x.googlevideo.com/videoplayback?itag=140"
          "&clen=13303014&dur=822")
    r.ok("googlevideo streams are relayed", R.is_relayable(yt))
    r.ok("the radio and everything else are not",
         not R.is_relayable("https://listen.kzlf.net/stream")
         and not R.is_relayable("https://evil.com/?x=.googlevideo.com"))
    r.check("the size comes from clen=", R.content_length(yt), 13303014)
    r.check("no clen: unknown", R.content_length("https://a.googlevideo"
                                                 ".com/x"), None)
    for header, want in ((None, (0, 999)), ("bytes=0-", (0, 999)),
                         ("bytes=100-199", (100, 199)),
                         ("bytes=900-5000", (900, 999)),
                         ("bytes=-100", (900, 999)),
                         ("bytes=1000-", None), ("junk", (0, 999))):
        r.check(f"Range {header!r} of 1000 bytes",
                R.parse_range(header, 1000), want)
    r.check("chunks cover the span exactly", R.plan(5, 30, chunk=10),
            [(5, 14), (15, 24), (25, 30)])
    relay = R.RangeRelay()
    r.check("not running: URLs go straight to ffmpeg, as before",
            relay.wrap(yt), yt)


def suite_end_to_end(r: Results) -> None:
    try:
        import httpx  # noqa: F401
        from aiohttp import web
    except ImportError:
        r.ok("aiohttp/httpx not installed here: end-to-end skipped", True)
        return

    body = bytes(range(256)) * 400          # 102,400 bytes
    seen = []

    async def upstream(request):
        rng = request.headers.get("Range")
        seen.append(rng)
        span = R.parse_range(rng, len(body))
        a, b = span
        return web.Response(status=206, body=body[a:b + 1], headers={
            "Content-Range": f"bytes {a}-{b}/{len(body)}"})

    async def main():
        app = web.Application()
        app.router.add_get("/videoplayback", upstream)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        relay = R.RangeRelay(chunk=16384)
        await relay.start()
        # Registered by hand: the fake upstream isn't on googlevideo.com.
        import time as _t
        relay._tokens["t"] = (f"http://127.0.0.1:{port}/videoplayback",
                              {"X-Test": "1"}, len(body), _t.monotonic())
        base = f"http://127.0.0.1:{relay.port}/r/t"
        async with __import__("httpx").AsyncClient() as c:
            whole = await c.get(base)
            part = await c.get(base, headers={"Range": "bytes=50000-50099"})
            gone = await c.get(f"http://127.0.0.1:{relay.port}/r/nope")
        await relay.stop()
        await runner.cleanup()
        return whole, part, gone
    whole, part, gone = asyncio.run(main())
    r.ok("the whole stream comes through byte for byte",
         whole.status_code == 200 and whole.content == body)
    r.ok("...fetched upstream in chunks, never one open-ended request",
         len([s for s in seen if s and s.startswith("bytes=0-")]) == 1
         and len(seen) >= 7 and all(s and not s.endswith("-")
                                    for s in seen))
    r.ok("a Range from ffmpeg (a seek) is honoured",
         part.status_code == 206 and part.content == body[50000:50100]
         and part.headers["content-range"] == f"bytes 50000-50099/"
                                               f"{len(body)}")
    r.check("an unknown token is a 404", gone.status_code, 404)


SUITES = [
    ("relay: ranges and wrapping", suite_maths),
    ("relay: end to end against a fake upstream", suite_end_to_end),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
