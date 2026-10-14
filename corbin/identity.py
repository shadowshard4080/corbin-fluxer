"""Who the bot is. Set in ``.env``; nothing else in the code names him.

Read from the environment at import time, so ``config`` (which loads
``.env``) must be imported first -- ``bot.py`` does that on its first line.
The persona itself (personality, voice, opinions) lives in the persona
files, not here: this is only the facts the code needs.
"""

from __future__ import annotations

import os
import re


def _env(name: str, default: str) -> str:
    value = (os.environ.get(name) or "").strip()
    return value or default


#: The bot's display name, used in prompts, transcripts and replies.
NAME: str = _env("BOT_NAME", "Corbin")

#: The shortcode the persona writes for the bot's own custom emoji, without
#: colons (``corbin`` -> ``:corbin:``). Must match the emoji's name in the
#: server. Only rendered when ``BOT_EMOJI_ID`` is also set.
EMOJI_NAME: str = re.sub(r"[^A-Za-z0-9_]", "",
                         _env("BOT_EMOJI_NAME", NAME.lower())) or "bot"
