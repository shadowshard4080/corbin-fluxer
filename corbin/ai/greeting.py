"""AI-written welcome messages.

A greeting is the first thing a new member ever sees from the bot, so the
prompt forbids inventing anything about the server or the person.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("corbin.greeting")

MAX_TOKENS = 1500


def build_prompt(*, server_name: str, display_name: str, username: str,
                 custom_info: str = "") -> str:
    custom_block = ""
    if (custom_info or "").strip():
        custom_block = (
            "The server moderators want you to work the following into the "
            "welcome naturally:\n"
            f"{custom_info.strip()}\n\n"
        )
    return (
        f'A new member just joined the server "{server_name}".\n\n'
        f"Their display name is: {display_name}\n"
        f"Their username is: {username}\n\n"
        f"{custom_block}"
        "Write a short, warm welcome (2-4 sentences) in your natural voice.\n"
        "- Greet them and welcome them to the server\n"
        "- Sound like yourself, not a template. Vary it every time\n"
        "- Do NOT include a mention; one is prepended automatically\n"
        "- Do NOT open with their name, the mention already identifies them\n"
        "- Do not invent facts about the server or about them"
    )


class Greeter:
    def __init__(self, llm: Any, soul: str = "") -> None:
        self.llm = llm
        self.soul = soul

    async def greet(self, *, server_name: str, display_name: str,
                    username: str, custom_info: str = "") -> Optional[str]:
        constants = self.llm.constants
        prompt = build_prompt(server_name=server_name,
                              display_name=display_name, username=username,
                              custom_info=custom_info)
        try:
            result = await self.llm.call(
                model=constants.MODEL_MAIN, system=self.soul or None,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=MAX_TOKENS,
            )
        except Exception:
            logger.exception("greeting generation failed")
            return None

        text = (result.text or "").strip()
        if getattr(result, "truncated", False):
            from .conversation import trim_incomplete_tail

            text = trim_incomplete_tail(text)
        return text or None
