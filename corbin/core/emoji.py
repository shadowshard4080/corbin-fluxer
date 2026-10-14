"""Rendering the bot's own emoji on the way out.

The persona tells him to write ``:name:`` (``BOT_EMOJI_NAME``), and the
shorthand is what belongs in a prompt -- but a bot's message only renders
a custom emoji when the *outgoing* text carries the full ``<:name:id>``
token. Without this expansion he posts the literal shortcode.

Expanded at send time only, never in stored history: the model keeps
seeing the shorthand it was taught, and no raw id ever enters its
context. Same reasoning as mention resolution.
"""

from __future__ import annotations

import re
from typing import Optional

from ..identity import EMOJI_NAME

#: The shorthand as the persona writes it, and the already-rendered form so
#: a double pass cannot nest tokens.
SHORTHAND = f":{EMOJI_NAME}:"
_RENDERED = re.compile(rf"<a?:{EMOJI_NAME}:\d+>")


def token(emoji_id: str, *, animated: bool = False) -> str:
    return f"<{'a' if animated else ''}:{EMOJI_NAME}:{emoji_id}>"


def render(text: str, emoji_id: Optional[str], *,
           animated: bool = False) -> str:
    """Expand the ``:name:`` shorthand into a renderable custom-emoji token.

    Returns the text untouched when no emoji is configured -- the
    literal shorthand is a better outcome than a broken token pointing
    at an id that does not exist.
    """
    if not text or not emoji_id:
        return text
    if SHORTHAND not in text:
        return text
    # Skip anything already rendered, so this is safe to apply twice.
    if _RENDERED.search(text):
        parts = _RENDERED.split(text)
        found = _RENDERED.findall(text)
        out = []
        for i, part in enumerate(parts):
            out.append(part.replace(SHORTHAND,
                                    token(emoji_id, animated=animated)))
            if i < len(found):
                out.append(found[i])
        return "".join(out)
    return text.replace(SHORTHAND, token(emoji_id, animated=animated))
