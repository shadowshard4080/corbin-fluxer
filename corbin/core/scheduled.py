"""Scheduled content: the daily quote and the hourly animal.

**Shared-fetch is the important pattern here.** When several guilds are due
at the same tick, the source is fetched ONCE and the same result fanned out.
Looping ``send_to_guild()`` per guild hammers a third-party API from one IP
in tight succession and gets rate-limited -- the exact bug the Discord
build's ``quote_scheduler_task`` was rewritten to avoid.

On persistent fetch failure nothing is sent. A placeholder posted daily is
worse than a silent skip, and the retry happens tomorrow anyway.

Landscapes are deliberately absent: that cog used ``source.unsplash.com``,
which no longer exists.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger("corbin.scheduled")

ZENQUOTES_URL = "https://zenquotes.io/api/random"
TINYFOX_URL = "https://api.tinyfox.dev/img?animal={animal}"
FETCH_TIMEOUT = 10.0
#: Backoff between fetch attempts, in seconds.
FETCH_BACKOFF = (0.0, 1.0, 3.0)

ANIMALS: dict[str, str] = {
    "fox": "Fox", "yeen": "Hyena", "dog": "Dog", "guara": "Maned Wolf",
    "serval": "Serval", "ott": "Otter", "jackal": "Jackal", "bleat": "Goat",
    "woof": "Wolf", "chi": "African Wild Dog", "puma": "Puma",
    "skunk": "Skunk", "tig": "Tiger", "wah": "Panda", "manul": "Pallas Cat",
    "snep": "Snow Leopard", "jaguar": "Jaguar", "badger": "Badger",
    "chee": "Cheetah", "racc": "Raccoon", "bear": "Bear", "capy": "Capybara",
    "bun": "Bunny", "marten": "Marten", "caracal": "Caracal", "snek": "Snake",
    "shiba": "Shiba Inu", "dook": "Ferret", "leo": "Lion", "yote": "Coyote",
    "poss": "Possum", "lynx": "Lynx",
}


def resolve_animal(name: Optional[str]) -> Optional[str]:
    """Accept either the api key ("yeen") or the display name ("Hyena")."""
    if not name:
        return None
    text = str(name).strip().lower()
    if text in ANIMALS:
        return text
    for key, label in ANIMALS.items():
        if label.lower() == text:
            return key
    return None


@dataclass
class Quote:
    text: str
    author: str

    def render(self) -> str:
        return f"> {self.text}\n— *{self.author}*"


async def fetch_quote() -> Optional[Quote]:
    """One quote from Zenquotes, retried with backoff. None if all fail.

    Reads ``q`` and ``a`` from the JSON. Do NOT reintroduce splitting the
    rendered string on " - "; author names contain hyphens.
    """
    import httpx

    for attempt, delay in enumerate(FETCH_BACKOFF, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
                resp = await client.get(ZENQUOTES_URL)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                entry = data[0]
                text = str(entry.get("q") or "").strip()
                author = str(entry.get("a") or "Unknown").strip() or "Unknown"
                if text:
                    return Quote(text=text, author=author)
            logger.info("zenquotes attempt %d returned nothing usable",
                        attempt)
        except Exception as exc:
            logger.info("zenquotes attempt %d failed: %s", attempt,
                        type(exc).__name__)
    logger.warning("zenquotes unreachable; skipping this run")
    return None


def animal_image_url(key: str) -> str:
    return TINYFOX_URL.format(animal=key)


def pick_animal(preferred: Optional[str] = None) -> tuple[str, str]:
    key = resolve_animal(preferred) or random.choice(list(ANIMALS))
    return key, ANIMALS[key]


def next_run_at(now: datetime, hour: int, minute: int) -> datetime:
    """Next occurrence of hour:minute, today or tomorrow."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def is_due(now: datetime, hour: int, minute: int,
           last_sent: Optional[str], tolerance_minutes: int = 30) -> bool:
    """True when the scheduled time has passed today and we have not run.

    Guarded by a per-day marker rather than an exact time match, so a
    restart or a slow tick does not skip the day entirely -- the same
    catch-up behaviour the digest scheduler uses.
    """
    today = now.date().isoformat()
    if last_sent == today:
        return False
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False
    return (now - scheduled) <= timedelta(minutes=tolerance_minutes) or \
        last_sent != today


class Scheduler:
    """Runs due jobs on a tick, fetching shared content once per sweep."""

    def __init__(self, *, tick_seconds: float = 60.0) -> None:
        self.tick_seconds = tick_seconds
        self._jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._task: Optional[asyncio.Task] = None

    def add(self, name: str, job: Callable[[], Awaitable[None]]) -> None:
        self._jobs.append((name, job))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            logger.info("scheduler started with %d job(s), %.0fs tick",
                        len(self._jobs), self.tick_seconds)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick_seconds)
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("scheduler tick failed")

    async def tick(self) -> None:
        for name, job in self._jobs:
            try:
                await job()
            except Exception:
                logger.exception("scheduled job %s failed", name)
