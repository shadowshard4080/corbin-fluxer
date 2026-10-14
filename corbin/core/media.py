"""Describing the pictures Corbin cannot see.

He has no vision: images, stickers and custom emoji reach him as
nothing at all, and an attachment-only message used to vanish from the
transcript entirely. Asked about one, he filled the gap with a guess.

So each piece of media becomes a short human label that goes into the
transcript. He still cannot see it, but he knows it exists and what
kind of thing it is, which is what lets him say so instead of
inventing.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

#: ``<:name:id>`` and ``<a:name:id>`` -- Discord-shaped, which Fluxer uses.
CUSTOM_EMOJI = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff",
             ".heic", ".avif")
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".avi")
AUDIO_EXT = (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus")


def _kind(name: str) -> str:
    low = name.lower()
    if low.endswith(".gif"):
        return "an animated GIF"
    if low.endswith(IMAGE_EXT):
        return "an image"
    if low.endswith(VIDEO_EXT):
        return "a video"
    if low.endswith(AUDIO_EXT):
        return "an audio file"
    return "a file"


def attachment_labels(message: Any) -> list[str]:
    labels = []
    for item in (getattr(message, "attachments", None) or ()):
        name = str(getattr(item, "filename", "") or
                   getattr(item, "name", "") or "").strip()
        if name:
            labels.append(f"{_kind(name)} called {name}")
        else:
            labels.append("an attachment")
    return labels


def emoji_names(content: str) -> list[str]:
    """Custom emoji in a message, by name, in order and deduplicated."""
    seen, names = set(), []
    for _animated, name, _eid in CUSTOM_EMOJI.findall(content or ""):
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def strip_emoji_ids(content: str) -> str:
    """``<:hug:123>`` -> ``:hug:``.

    The raw token is noise that costs tokens and tells him nothing; the
    name is the only part carrying meaning. Same rule as mention
    resolution -- no raw ids in model context.
    """
    return CUSTOM_EMOJI.sub(lambda m: f":{m.group(2)}:", content or "")


def media_labels(message: Any) -> list[str]:
    """Every non-text thing in a message, as short phrases."""
    labels = attachment_labels(message)
    names = emoji_names(getattr(message, "content", "") or "")
    if names:
        shown = ", ".join(f":{n}:" for n in names[:5])
        labels.append(f"custom emoji ({shown})")
    for sticker in (getattr(message, "stickers", None) or ()):
        name = str(getattr(sticker, "name", "") or "").strip()
        labels.append(f"a sticker{f' called {name}' if name else ''}")
    return labels
