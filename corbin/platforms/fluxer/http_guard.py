"""Guards for fluxer.py's REST client, which can hang without a trace.

Two defects in ``fluxer/http.py`` (0.4.x), patched at runtime the same way
``core/voice.py`` patches the voice loop:

1. **No request timeout.** The aiohttp session is built with no timeout,
   so it inherits aiohttp's default of *five minutes* per attempt, and a
   request is retried up to four times. A stalled call to Fluxer sits
   silently for up to ~20 minutes: Corbin shows "typing", the reply is
   generated, and nothing is ever posted or logged.

2. **A cancelled request never releases its rate-limit lock.** Each route
   bucket (one kind of request, in one channel) has an ``asyncio.Lock``,
   taken in ``RateLimiter.acquire`` and released only when a response
   arrives or an aiohttp error is caught. Cancel the request in between --
   the typing indicator's refresh task is cancelled every time a reply
   finishes -- and the lock stays held forever. Every later request in
   that bucket then waits on it: messages to that channel simply stop,
   with no error and no log line.

The fix gives the session a real timeout and makes the limiter release a
lock on *any* exit, tracked by the task that took it, so it can never
release someone else's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("corbin.fluxer.http")

#: Per attempt. Fluxer answers a message send in well under a second;
#: thirty is generous and still a hundredth of aiohttp's default.
TOTAL_TIMEOUT = 30.0
CONNECT_TIMEOUT = 10.0


def install_http_guards(client: Any) -> bool:
    """Patch ``client._http`` in place. Returns False if the SDK moved.

    Idempotent. Never raises: if the internals it expects aren't there, the
    client is left exactly as it was and this says so.
    """
    http = getattr(client, "_http", None)
    limiter = getattr(http, "_rate_limiter", None)
    if http is None or limiter is None:
        logger.warning("fluxer.py has no _http/_rate_limiter; HTTP guards "
                       "NOT installed, so a stalled request can still hang")
        return False
    if getattr(http, "_corbin_guarded", False):
        return True
    needed = ("_locks", "_reset_times", "_global_lock", "_get_lock")
    if not all(hasattr(limiter, name) for name in needed) or \
            not hasattr(http, "request") or not hasattr(http, "_ensure_session"):
        logger.warning("fluxer.py's rate limiter changed shape; HTTP guards "
                       "NOT installed")
        return False

    _guard_session(http)
    _guard_limiter(limiter)
    guard_request(http)
    http._corbin_guarded = True
    logger.info("fluxer HTTP guards installed (%.0fs timeout, lock-safe "
                "cancellation)", TOTAL_TIMEOUT)
    return True


def _guard_session(http: Any) -> None:
    original = http._ensure_session
    # ClientSession warns on unknown attributes, so which sessions already
    # carry our timeout is tracked here rather than on the session.
    guarded: set[int] = set()

    async def ensure_session() -> Any:
        import aiohttp

        session = getattr(http, "_session", None)
        if session is None or session.closed:
            session = await original()
        # The SDK builds the session with no timeout. Set ours on the one it
        # made (headers and all stay the SDK's own) rather than building a
        # replica that could drift from it.
        if id(session) not in guarded:
            # ``_timeout`` is a declared ClientSession attribute (aiohttp
            # 3.x ATTRS) and is what each request falls back to.
            session._timeout = aiohttp.ClientTimeout(
                total=TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT)
            guarded.add(id(session))
        return session

    http._ensure_session = ensure_session


def _guard_limiter(limiter: Any) -> None:
    owners: dict[str, asyncio.Task] = {}
    limiter._corbin_owners = owners

    async def acquire(bucket: str) -> None:
        await limiter._global_lock.wait()
        lock = limiter._get_lock(bucket)
        await lock.acquire()
        # Recorded the instant it is held, before the reset sleep below, so
        # a cancellation during that sleep is covered too.
        owners[bucket] = asyncio.current_task()
        try:
            reset_at = limiter._reset_times.get(bucket)
            if reset_at is not None:
                delay = reset_at - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
        except BaseException:
            _release_if_mine(bucket)
            raise

    def release(bucket: str, headers: dict) -> None:
        remaining = (headers or {}).get("X-RateLimit-Remaining")
        reset_after = (headers or {}).get("X-RateLimit-Reset-After")
        try:
            if remaining is not None and int(remaining) == 0 and \
                    reset_after is not None:
                limiter._reset_times[bucket] = (
                    asyncio.get_running_loop().time() + float(reset_after))
            else:
                limiter._reset_times.pop(bucket, None)
        except (TypeError, ValueError):
            limiter._reset_times.pop(bucket, None)
        _release_if_mine(bucket)

    def _release_if_mine(bucket: str) -> None:
        # The SDK calls release twice on some error paths. Releasing only
        # the holder's own lock makes that harmless instead of freeing a
        # lock another request is holding.
        if owners.get(bucket) is asyncio.current_task():
            owners.pop(bucket, None)
            lock = limiter._get_lock(bucket)
            if lock.locked():
                lock.release()

    limiter.acquire = acquire
    limiter.release = release
    limiter._corbin_release_if_mine = _release_if_mine


def guard_request(http: Any) -> None:
    """Release the bucket lock whenever ``request`` exits abnormally."""
    limiter = http._rate_limiter
    original = http.request

    async def request(route: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await original(route, *args, **kwargs)
        except BaseException:
            limiter._corbin_release_if_mine(getattr(route, "bucket", ""))
            raise

    http.request = request
