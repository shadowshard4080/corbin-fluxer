"""Turning a posted image into something the model can look at.

A port of the Discord build's ``_prepare_image_for_api``, with the same
hard-won rules:

* **Grok's vision input accepts PNG and JPEG only.** WebP, BMP, TIFF and
  GIF are re-encoded. Fluxer serves plenty of WebP, so this is not an
  edge case.
* **Undecodable bytes are dropped, never forwarded.** The media type is
  declared in the request, unknown magic bytes default to ``image/jpeg``,
  and Grok rejects the *entire request* on a mismatch -- so one unreadable
  attachment would take down the whole reply rather than just itself.
* **Animations become a tiled contact sheet.** A sweep or reveal collapses
  to nothing meaningful as frame zero, so frames are sampled across the
  timeline and tiled; the prompt is told to read them in order.

``prepare`` is **blocking and CPU-bound** -- roughly 25 ms for a 720p
WebP and up to ~360 ms for an oversized PNG. Always call it through
``asyncio.to_thread``; inline it stalls every channel and the gateway
heartbeat for that long.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Optional

logger = logging.getLogger("corbin.vision")

#: Under the 5 MB API limit with room for the base64 expansion.
MAX_IMAGE_BYTES = 4_800_000
#: What Grok's vision input will accept. Everything else is re-encoded.
API_FORMATS = {"PNG", "JPEG"}
#: Frames sampled from an animation, tiled into one square sheet.
ANIM_FRAMES = 4
ANIM_MAX_PX = 768
#: Fewer frames than this and a still says the same thing.
ANIM_MIN_FRAMES = 3
#: Largest download accepted, before decoding.
MAX_DOWNLOAD_BYTES = 12_000_000

#: Appended when a montage is sent, or the model describes a GIF as a
#: four-panel collage. Keep this next to the tiling code -- if the grid
#: shape changes, this has to change in the same commit.
ANIMATION_GRID_HINT = (
    " The image is a grid of frames sampled from one short animation, in "
    "order left to right, top to bottom. Read it as motion over time, not "
    "as separate pictures, and never mention the grid.")


def detect_media_type(raw: bytes) -> str:
    """Media type from magic bytes, defaulting to JPEG."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:2] == b"\xff\xd8":
        return "image/jpeg"
    if raw[:4] == b"GIF8":
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def montage(img) -> Optional[object]:
    """Tile frames sampled across an animation into one square sheet."""
    from PIL import Image as PILImage

    try:
        total = getattr(img, "n_frames", 1)
    except Exception:
        total = 1
    if total < ANIM_MIN_FRAMES:
        return None

    step = max(1, total // ANIM_FRAMES)
    frames = []
    for index in range(0, total, step):
        if len(frames) >= ANIM_FRAMES:
            break
        try:
            img.seek(index)
            frames.append(img.convert("RGB").copy())
        except Exception:
            break
    if len(frames) < 2:
        return None

    cell = max(1, ANIM_MAX_PX // 2)
    frames = [f.resize((cell, cell), PILImage.LANCZOS) for f in frames]
    while len(frames) < ANIM_FRAMES:
        frames.append(frames[-1])

    sheet = PILImage.new("RGB", (cell * 2, cell * 2))
    for i, frame in enumerate(frames[:ANIM_FRAMES]):
        sheet.paste(frame, ((i % 2) * cell, (i // 2) * cell))
    return sheet


def prepare(raw: bytes) -> Optional[tuple[str, str, bool]]:
    """``(base64, media_type, was_animated)``, or None if unusable.

    Blocking. Call via ``asyncio.to_thread``.
    """
    if not raw:
        return None
    from PIL import Image as PILImage

    try:
        img = PILImage.open(io.BytesIO(raw))
        animated = bool(getattr(img, "is_animated", False))
        fmt = (img.format or "").upper()

        # A PNG or JPEG already under the cap needs no work at all.
        if not animated and len(raw) <= MAX_IMAGE_BYTES and fmt in API_FORMATS:
            return (base64.b64encode(raw).decode("ascii"),
                    detect_media_type(raw), False)

        tiled = False
        if animated:
            sheet = montage(img)
            if sheet is not None:
                img, tiled = sheet, True
            else:
                # Too few frames to tile. It is a still now, so the
                # caller must NOT be told to read it as a grid.
                img.seek(0)

        if img.mode == "P" or "transparency" in getattr(img, "info", {}):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        for scale in (1.0, 0.75, 0.5, 0.35, 0.25):
            resized = (img if scale == 1.0 else
                       img.resize((max(1, int(img.width * scale)),
                                   max(1, int(img.height * scale))),
                                  PILImage.LANCZOS))
            buf = io.BytesIO()
            if resized.mode == "RGBA":
                resized.save(buf, format="PNG", optimize=True)
                media = "image/png"
            else:
                resized.save(buf, format="JPEG", quality=85, optimize=True)
                media = "image/jpeg"
            if buf.tell() <= MAX_IMAGE_BYTES:
                return (base64.b64encode(buf.getvalue()).decode("ascii"),
                        media, tiled)
        return None
    except Exception:
        # Dropped, never forwarded raw: an unreadable image would be
        # declared image/jpeg and Grok rejects the whole request on a
        # type mismatch, losing the reply as well as the picture.
        logger.debug("could not decode an image; dropping it",
                     exc_info=True)
        return None


async def fetch(url: str, *, timeout: float = 15.0) -> Optional[bytes]:
    """Download an image, refusing anything implausibly large."""
    if not url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception:
        logger.info("could not fetch %s", url, exc_info=True)
        return None
    if len(data) > MAX_DOWNLOAD_BYTES:
        logger.info("image at %s is %d bytes; too large", url, len(data))
        return None
    return data


def image_block(b64: str, media_type: str) -> dict:
    """An Anthropic-shaped image block; llm.py translates it for Grok."""
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": b64}}
