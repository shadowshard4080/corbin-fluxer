"""Catching the event loop being blocked, and saying by what.

Everything in the bot shares one asyncio loop: chat replies, commands,
and the frame loops that feed a stream's sound and picture. Anything
that runs for a second without yielding stalls them all, and a stalled
audio loop is a gap people hear (live 2026-10-20: a 1080p music video at
"clock 0.71x, sound ran dry 13x"). The owner asked whether answering
messages could cause it; this is how to find out rather than guess.

A heartbeat task on the loop ticks every ``TICK``. A plain thread checks
it; when the loop hasn't ticked for ``STALL``, the thread grabs the main
thread's stack right then (``sys._current_frames``) and logs it: the
file, line and function that's holding the loop, while it's still
holding it. Rate-limited (``LOG_EVERY``), so a long stall is one warning.

``worst()`` gives the longest lag since it was last read, for the stream
stats line (``loop lag``). Costs a 10 Hz wakeup and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger("corbin.loopwatch")

TICK = 0.1
STALL = 0.75
LOG_EVERY = 30.0
#: Stack lines kept in the warning: enough to see the caller's caller.
DEPTH = 12


class LoopWatch:
    def __init__(self) -> None:
        self._beat = time.monotonic()
        self._worst = 0.0
        self._logged = 0.0
        self._main_ident: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stalls = 0

    def start(self) -> None:
        """Idempotent (on_ready fires on every reconnect)."""
        if self._task is not None and not self._task.done():
            return
        self._main_ident = threading.get_ident()
        self._beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heart())
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._watch,
                                            name="corbin-loopwatch",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

    async def _heart(self) -> None:
        while True:
            now = time.monotonic()
            lag = now - self._beat - TICK
            if lag > self._worst:
                self._worst = lag
            self._beat = now
            await asyncio.sleep(TICK)

    def worst(self, reset: bool = True) -> float:
        """The longest loop lag (seconds) since the last call."""
        w = max(0.0, self._worst)
        if reset:
            self._worst = 0.0
        return w

    def _watch(self) -> None:
        reported = False
        while not self._stop.wait(TICK):
            stalled = time.monotonic() - self._beat
            if stalled < STALL:
                reported = False
                continue
            if reported:
                continue
            reported = True
            self.stalls += 1
            if time.monotonic() - self._logged < LOG_EVERY:
                continue
            self._logged = time.monotonic()
            frame = sys._current_frames().get(self._main_ident)
            where = "".join(traceback.format_stack(frame)[-DEPTH:]) \
                if frame is not None else "(no stack)"
            logger.warning("event loop blocked for %.1fs (and counting); "
                           "it was running:\n%s", stalled, where.rstrip())


#: The process's one watch, started from bot.py's on_ready.
WATCH = LoopWatch()
