"""Welcome and farewell: settings, and the welcome card.

Three related features, all per-guild and all off by default:

* **welcome**  -- a message (optionally an image card) when someone joins
* **farewell** -- a message (optionally an image card) when someone leaves
* **greeting** -- an AI-written welcome in Corbin's voice, instead of a
  fixed template

On Discord the AI greeting replies to Discord's own ``new_member`` system
message. Fluxer has no such message, so it fires directly on the join event,
which is simpler and removes a whole class of ordering bug.

Image generation needs Pillow and is skipped cleanly when it is missing --
a missing font must not stop somebody being welcomed.
"""

from __future__ import annotations

import io
import logging
import random
import textwrap
from typing import Any, Optional

logger = logging.getLogger("corbin.welcome")

#: Same canvas as the Discord build's cards. 500x180 left no room to put
#: the avatar between the two lines of text, so it sat on top of them.
CARD_SIZE = (500, 300)
AVATAR_MIN, AVATAR_MAX = 80, 150
DEFAULT_WELCOME = "Welcome to {server}, {member}!"
DEFAULT_FAREWELL = "{member} has left {server}."
DEFAULT_WELCOME_CARD = "Welcome to {server}!"
DEFAULT_FAREWELL_CARD = "Goodbye, {member}."
#: Stored backgrounds, under the bot's own data root.
BACKGROUND_DIR = "fluxer_card_backgrounds"
#: A background upload bigger than this is refused rather than decoded.
MAX_BACKGROUND_BYTES = 8 * 1024 * 1024


def render_template(template: str, *, member: str, server: str,
                    count: Optional[int] = None) -> str:
    """Fill a user-supplied template. Unknown placeholders are left alone."""
    out = (template or "").replace("{member}", member).replace(
        "{server}", server).replace("{user}", member)
    if count is not None:
        out = out.replace("{count}", f"{count:,}")
    return out.strip()


def avatar_url(user_id: str, avatar: Optional[str]) -> Optional[str]:
    """A fetchable avatar URL from whatever the gateway handed us.

    Join and leave events carry the avatar *hash*, not a URL. The card
    code passed that hash straight to the downloader, which only accepts
    http links, so every card went out with no avatar at all. Same URL
    scheme as fluxer.py's ``User.avatar_url``; no custom avatar means the
    default one, as on Discord.
    """
    value = str(avatar or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if value and user_id:
        ext = "gif" if value.startswith("a_") else "png"
        return (f"https://fluxerusercontent.com/avatars/{user_id}/"
                f"{value}.{ext}")
    if str(user_id).isdigit():
        return f"https://fluxerstatic.com/avatars/{int(user_id) % 6}.png"
    return None


def prepare_background(data: bytes) -> Optional[bytes]:
    """Validate and normalise an uploaded background. None if unusable.

    Decoded and cropped to the card NOW, when someone is setting it and
    can be told it failed, not at the next join when nobody is watching.
    Blocking (Pillow): run in a thread.
    """
    if not data or len(data) > MAX_BACKGROUND_BYTES:
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)   # first frame of a GIF
            fitted = ImageOps.fit(img.convert("RGB"), CARD_SIZE,
                                  Image.Resampling.LANCZOS)
        out = io.BytesIO()
        fitted.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


def background_path(root: Any, guild_id: str, kind: str) -> Any:
    from pathlib import Path

    safe = "".join(c for c in str(guild_id) if c.isdigit()) or "unknown"
    return Path(root) / BACKGROUND_DIR / f"{safe}_{kind}.png"


def make_card(top_text: str, username: str,
              avatar_bytes: Optional[bytes] = None,
              background_bytes: Optional[bytes] = None,
              subtitle: Optional[str] = None) -> Optional[bytes]:
    """Render a welcome or farewell card, or None if Pillow is unavailable.

    Layout as on Discord: the message across the top, the avatar centred
    in the space left, the name along the bottom. Text is outlined so it
    stays readable on a bright background. Blocking: run in a thread.
    """
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        logger.info("Pillow not installed; skipping the card")
        return None

    try:
        width, height = CARD_SIZE
        if background_bytes:
            with Image.open(io.BytesIO(background_bytes)) as img:
                base = ImageOps.fit(img.convert("RGBA"), CARD_SIZE,
                                    Image.Resampling.LANCZOS)
        else:
            dark = (random.randint(0, 100), random.randint(0, 100),
                    random.randint(0, 100))
            base = Image.new("RGBA", CARD_SIZE, color=dark)
        draw = ImageDraw.Draw(base)
        font = _load_font(26)
        small = _load_font(18)
        stroke = dict(stroke_width=2)

        def centred(text: str, y: int, f: Any) -> int:
            box = draw.textbbox((0, 0), text, font=f, **stroke)
            draw.text(((width - (box[2] - box[0])) // 2, y), text,
                      fill=(255, 255, 255), font=f, stroke_fill=(0, 0, 0),
                      **stroke)
            return box[3] - box[1]

        y = 12
        for line in textwrap.wrap(top_text or "", width=32)[:3]:
            y += centred(line, y, font) + 4
        top_end = y

        bottom = height - 12
        sub_h = 0
        if subtitle:
            box = draw.textbbox((0, 0), subtitle, font=small, **stroke)
            sub_h = box[3] - box[1] + 10
        name = (username or "")[:32]
        name_box = draw.textbbox((0, 0), name, font=font, **stroke)
        name_h = name_box[3] - name_box[1]
        name_y = bottom - sub_h - name_h
        centred(name, name_y, font)
        if subtitle:
            centred(subtitle, bottom - sub_h + 8, small)

        # Only draw an avatar when we actually have one. Pasting a
        # transparent placeholder through the circular mask renders a solid
        # black disc, which looks like a rendering fault.
        if avatar_bytes:
            room = name_y - top_end - 16
            size = max(AVATAR_MIN, min(AVATAR_MAX, room))
            with Image.open(io.BytesIO(avatar_bytes)) as img:
                img.seek(0)
                avatar = ImageOps.fit(img.convert("RGBA"), (size, size),
                                      Image.Resampling.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            top = top_end + max(0, (name_y - top_end - size) // 2)
            left = (width - size) // 2
            ring = 3
            draw.ellipse((left - ring, top - ring, left + size + ring,
                          top + size + ring), fill=(255, 255, 255))
            base.paste(avatar, (left, top), mask)

        out = io.BytesIO()
        base.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        logger.exception("card rendering failed")
        return None


def make_welcome_card(username: str, message: str,
                      avatar_bytes: Optional[bytes] = None,
                      background_bytes: Optional[bytes] = None,
                      subtitle: Optional[str] = None) -> Optional[bytes]:
    """Kept for existing callers; see :func:`make_card`."""
    return make_card(message, username, avatar_bytes, background_bytes,
                     subtitle)


def _load_font(size: int) -> Any:
    from PIL import ImageFont

    for finder in (_font_via_matplotlib, _font_via_common_paths):
        path = finder()
        if path:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _font_via_matplotlib() -> Optional[str]:
    try:
        from matplotlib import font_manager

        return font_manager.findfont(
            font_manager.FontProperties(family="DejaVu Sans"))
    except Exception:
        return None


def _font_via_common_paths() -> Optional[str]:
    import os

    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/TTF/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        if os.path.exists(path):
            return path
    return None
