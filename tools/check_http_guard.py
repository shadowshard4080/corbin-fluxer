"""Reproduce fluxer.py's stuck-lock hang against the REAL installed SDK.

Run with the bot's venv:  .venv/bin/python tools/check_http_guard.py

A request is cancelled mid-flight (as the typing indicator's refresh task
is, every time a reply finishes), then a second request to the same bucket
is made. Unguarded, the second waits forever on the leaked lock. Guarded,
it goes straight through. Exit status 0 means the guard works.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fluxer.http import HTTPClient, Route  # noqa: E402

from corbin.platforms.fluxer.http_guard import install_http_guards  # noqa


class _Resp:
    status = 200
    headers: dict = {}

    async def json(self):
        return {"ok": True}


class _Call:
    def __init__(self, hang: bool):
        self.hang = hang

    async def __aenter__(self):
        if self.hang:
            await asyncio.sleep(3600)   # a stalled connection
        return _Resp()

    async def __aexit__(self, *exc):
        return False


class _Session:
    closed = False

    def __init__(self):
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        return _Call(hang=self.calls == 1)


async def trial(guarded: bool) -> bool:
    http = HTTPClient("token")
    session = _Session()

    async def ensure():
        return session
    http._ensure_session = ensure
    if guarded:
        client = type("C", (), {"_http": http})()
        assert install_http_guards(client), "guards did not install"
        # The session guard wraps _ensure_session; keep using the fake.
    route = Route("POST", "/channels/{channel_id}/messages", channel_id="1")

    first = asyncio.ensure_future(http.request(route))
    await asyncio.sleep(0.05)
    first.cancel()
    try:
        await first
    except asyncio.CancelledError:
        pass

    try:
        await asyncio.wait_for(http.request(route), 2.0)
        return True
    except asyncio.TimeoutError:
        return False


async def main() -> int:
    unguarded = await trial(guarded=False)
    guarded = await trial(guarded=True)
    print(f"unguarded SDK: second request {'went through' if unguarded else 'HUNG (the bug)'}")
    print(f"guarded:       second request {'went through' if guarded else 'HUNG'}")
    return 0 if guarded else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
