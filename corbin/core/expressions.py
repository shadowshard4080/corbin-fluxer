"""Copying emojis and stickers from a Discord guild to a Fluxer one.

Reads the source guild over Discord's REST API with the Discord bot's
token -- the Fluxer bot is not in that guild and has no gateway
connection to it, but a bot token plus ``GET /guilds/{id}/emojis`` needs
neither.

Three things make this less trivial than "loop and POST":

* **Lottie stickers cannot be copied.** Discord's animated stickers are
  JSON vector animations, not images. There is nothing to upload, so they
  are reported as skipped rather than failing one at a time.
* **Expression creation is rate limited hard** on both platforms. Uploads
  are paced and the run stops early on repeated rejections rather than
  hammering through a hundred failures.
* **Re-running must be safe.** Anything whose name already exists on the
  destination is skipped, so an interrupted copy is resumed by simply
  running it again.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("corbin.expressions")

DISCORD_API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
#: Discord's own ceiling for an emoji upload. Fluxer's is unpublished;
#: anything Discord accepted should be within it.
MAX_BYTES = 256 * 1024
#: Seconds between uploads. Expression endpoints are far more tightly
#: limited than messages, and a burst gets the whole run throttled.
UPLOAD_DELAY = 1.5
#: Consecutive failures after which the run gives up.
FAIL_STREAK = 5
HTTP_TIMEOUT = 30.0
#: Discord's Lottie sticker format. No image to copy.
FORMAT_LOTTIE = 3


@dataclass
class Expression:
    kind: str          # "emoji" | "sticker"
    name: str
    url: str
    animated: bool = False
    lottie: bool = False


@dataclass
class CopyReport:
    copied: int = 0
    skipped_existing: int = 0
    skipped_lottie: int = 0
    failed: int = 0
    errors: list[str] = None          # type: ignore[assignment]
    stopped_early: bool = False

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def touched(self) -> int:
        return self.copied + self.failed


def emoji_url(emoji: dict) -> str:
    ext = "gif" if emoji.get("animated") else "png"
    return f"{CDN}/emojis/{emoji['id']}.{ext}?size=256"


def sticker_url(sticker: dict) -> str:
    # format_type 1 = PNG, 2 = APNG, 3 = Lottie, 4 = GIF
    ext = "gif" if sticker.get("format_type") == 4 else "png"
    return f"{CDN}/stickers/{sticker['id']}.{ext}"


async def fetch_discord(token: str, guild_id: str) -> list[Expression]:
    """Everything copyable in the source guild. Raises on auth failure."""
    import httpx

    headers = {"Authorization": f"Bot {token}"}
    found: list[Expression] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 headers=headers) as client:
        for kind, path in (("emoji", "emojis"), ("sticker", "stickers")):
            resp = await client.get(
                f"{DISCORD_API}/guilds/{guild_id}/{path}")
            if resp.status_code == 401:
                raise RuntimeError(
                    "Discord rejected the bot token (401). Check "
                    "DISCORD_TOKEN in the shared .env.")
            if resp.status_code == 403:
                raise RuntimeError(
                    f"The Discord bot cannot read {path} in guild "
                    f"{guild_id} (403) — is it still in that server?")
            resp.raise_for_status()
            for item in resp.json() or []:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                if kind == "emoji":
                    found.append(Expression(
                        "emoji", name, emoji_url(item),
                        animated=bool(item.get("animated"))))
                else:
                    found.append(Expression(
                        "sticker", name, sticker_url(item),
                        lottie=item.get("format_type") == FORMAT_LOTTIE))
    return found


async def copy_to_fluxer(
    items: list[Expression], *, adapter: Any, guild_id: str,
    existing: set[str],
    progress: Optional[Callable[[int, int], Any]] = None,
) -> CopyReport:
    """Upload each expression, skipping what is already there.

    ``existing`` holds destination names, lowercased, per kind as
    ``"<kind>:<name>"``. Matching on name rather than id is the only
    option: the two platforms mint their own ids.
    """
    import httpx

    report = CopyReport()
    streak = 0
    todo = [e for e in items
            if f"{e.kind}:{e.name.lower()}" not in existing]
    report.skipped_existing = len(items) - len(todo)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 follow_redirects=True) as client:
        for index, item in enumerate(todo, start=1):
            if item.lottie:
                report.skipped_lottie += 1
                continue
            try:
                resp = await client.get(item.url)
                resp.raise_for_status()
                data = resp.content
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{item.name}: download failed "
                                     f"({type(exc).__name__})")
                streak += 1
            else:
                if len(data) > MAX_BYTES:
                    report.failed += 1
                    report.errors.append(
                        f"{item.name}: {len(data) // 1024} KB, over the "
                        f"{MAX_BYTES // 1024} KB limit")
                    continue
                error = await adapter.create_expression(
                    guild_id, kind=item.kind, name=item.name, image=data)
                if error:
                    report.failed += 1
                    report.errors.append(f"{item.name}: {error}")
                    streak += 1
                else:
                    report.copied += 1
                    streak = 0

            if streak >= FAIL_STREAK:
                report.stopped_early = True
                # Name the actual reason instead of guessing. Five
                # identical structural errors is a bug on our side; five
                # different ones is the server pushing back. Reporting
                # "most likely rate limited" for the former sent an hour
                # of debugging at the wrong problem.
                recent = report.errors[-FAIL_STREAK:]
                causes = {e.split(": ", 1)[-1] for e in recent}
                if len(causes) == 1:
                    report.errors.append(
                        f"gave up after {FAIL_STREAK} in a row, all the "
                        f"same: {causes.pop()}. That's not rate limiting.")
                else:
                    report.errors.append(
                        f"gave up after {FAIL_STREAK} failures in a row — "
                        f"most likely rate limited or out of slots")
                break
            if progress is not None and index % 10 == 0:
                try:
                    await progress(index, len(todo))
                except Exception:
                    pass
            await asyncio.sleep(UPLOAD_DELAY)
    return report
