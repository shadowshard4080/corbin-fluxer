"""What a posted link shows, read from the preview the chat app made.

Fluxer unfurls a link into an embed (a tweet's author and text, a video's
title, an article's summary), usually a second or so after the message
arrives, as a message update. That text is free: no fetch, no model call.
Without it he had only the URL, and made up what was behind it.

Only reply builders render these (the direct reply and the presence
chime-in). The presence gate and the classifiers never see them: the
preview is context for what he says, not a reason to say something.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

#: Per preview, and how many previews one message may contribute.
MAX_PREVIEW_CHARS = 500
MAX_PREVIEWS = 3

_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def has_link(text: str) -> bool:
    return bool(_URL.search(text or ""))


def _get(obj: Any, key: str) -> Any:
    # fluxer 0.4.2 hands embeds over as dicts; tolerate objects anyway.
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def embeds_of(raw: Any) -> list:
    """The embeds on a raw message, whether it is a dict or an object."""
    return list(_get(raw, "embeds") or []) if raw is not None else []


def preview_text(embed: Any) -> Optional[str]:
    """One embed as a line of text, or None when it carries no words.

    An image-only embed (a bare picture link, a GIF) has nothing to read
    and is skipped; the attachment labels cover those.
    """
    author = _text(_get(_get(embed, "author") or {}, "name"))
    title = _text(_get(embed, "title"))
    description = _text(_get(embed, "description"))
    words = [p for p in (author, title, description) if p]
    if not words:
        return None
    source = (_text(_get(_get(embed, "provider") or {}, "name"))
              or _text(_get(_get(embed, "footer") or {}, "text")))
    line = " | ".join(dict.fromkeys(words))  # an author repeated as title
    if source and source not in line:
        line = f"{source}: {line}"
    if len(line) > MAX_PREVIEW_CHARS:
        line = line[:MAX_PREVIEW_CHARS - 1].rstrip() + "…"
    return line


def preview_texts(embeds: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for embed in embeds or ():
        line = preview_text(embed)
        if line and line not in out:
            out.append(line)
        if len(out) >= MAX_PREVIEWS:
            break
    return out


def render_inline(previews: Iterable[str]) -> str:
    """Suffix for a transcript line."""
    return "".join(f" [link preview: {p}]" for p in previews or ())


def prompt_block(previews: Iterable[str]) -> str:
    """The block appended to the message he is answering."""
    lines = [f"- {p}" for p in previews or ()]
    if not lines:
        return ""
    return ("[Tool:LinkPreview] The link in this message, as the chat app "
            "previewed it. That preview is all you have of it: react to "
            "what it says, and don't claim to have read or watched more.\n"
            + "\n".join(lines))
