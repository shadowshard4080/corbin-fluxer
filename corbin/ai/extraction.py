"""Memory extraction: one cheap call per exchange.

Runs on ``MODEL_FAST`` at temperature 0. Extraction writes durable facts
about real people to disk, so a hallucinated "fact" here persists and gets
fed back into every future prompt; the prompt is strict on purpose.

Never parses with a bare ``json.loads``: MODEL_FAST output is routinely
truncated mid-string, which would discard the whole result.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from ..identity import NAME as BOT_NAME
from .json_utils import extract_json_object

logger = logging.getLogger("corbin.extraction")

MAX_TOKENS = 700
MAX_USER_CHARS = 1000
MAX_BOT_CHARS = 500


def build_prompt(*, display_name: str, user_content: str, bot_response: str,
                 known_facts: Sequence[str],
                 include_server: bool = False,
                 recent: Sequence[dict] = ()) -> str:
    facts_block = "\n".join(f"  [{i}] {f}" for i, f in enumerate(known_facts)) \
        or "  (none yet)"

    # Context matters: "Her name is Ziggy" states nothing on its own, but
    # after "what's my dog's name?" it clearly means the user has a dog
    # called Ziggy. Without the preceding turns the extractor sees an
    # orphaned sentence and correctly refuses to infer anything.
    lead_in = ""
    if recent:
        lines = []
        for turn in list(recent)[-4:]:
            who = display_name if turn.get("role") == "user" else BOT_NAME
            lines.append(f"{who}: {str(turn.get('content') or '')[:200]}")
        lead_in = "--- EARLIER IN THE CONVERSATION ---\n" + \
                  "\n".join(lines) + "\n"

    prompt = (
        "Extract durable memory from a chat exchange.\n\n"
        f"{lead_in}"
        "--- EXCHANGE ---\n"
        f"{display_name} said: {user_content[:MAX_USER_CHARS]}\n"
        f"{BOT_NAME} replied: {bot_response[:MAX_BOT_CHARS]}\n"
        "--- END ---\n\n"
        "Reply with a JSON object containing these keys:\n\n"
        f'1. "user_facts": NEW facts about "{display_name}" as a JSON array '
        "of strings.\n"
        f"  Existing known facts (by index):\n{facts_block}\n"
        "  Rules:\n"
        "  - Only facts the USER states about THEMSELVES, explicit or implied.\n"
        "  - 'My dog wanted to play' means they have a dog. 'I drove the kids "
        "to school' means they have kids.\n"
        "  - Do NOT extract facts about the bot, about other people, or from "
        "hypotheticals, jokes or questions.\n"
        '  - To UPDATE an existing fact, prefix with "UPDATE:<index>:" using '
        "the 0-based index above.\n"
        '  - For a timezone use ONLY a valid IANA id: "TIMEZONE: '
        'America/New_York". Never an abbreviation or a UTC offset.\n'
        "  - Durable facts only. Not moods, not what they are doing right now.\n"
        "  - Never store a request, a task, or something they are "
        "looking for. \"Actively seeking casserole recipes\" is not a "
        "fact, it is an errand, and it gets answered later as though "
        "they had just asked. \"Likes casseroles\" is a fact.\n"
        "  - Write each fact as a stable attribute, not an action in "
        "progress: \"has a dog called Ziggy\", not \"is looking for a "
        "vet for Ziggy\".\n"
        "  - If nothing new, use [].\n"
    )
    if include_server:
        prompt += (
            '\n2. "server_facts": NEW facts about this SERVER as a JSON array '
            'of strings, each prefixed "SERVER:".\n'
            "  Things true of the community, not of one person: recurring "
            "events, rules, in-jokes, roles.\n"
            "  If nothing new, use [].\n"
        )
    prompt += (
        '\nReply with ONLY the JSON object. Example:\n'
        '{"user_facts": ["has a dog named Ziggy"]'
    )
    prompt += ', "server_facts": ["SERVER:game night is Fridays"]}' \
        if include_server else "}"
    return prompt


class MemoryExtractor:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    async def extract(self, *, display_name: str, user_content: str,
                      bot_response: str, known_facts: Sequence[str],
                      include_server: bool = False,
                      recent: Sequence[dict] = ()
                      ) -> tuple[list[str], list[str]]:
        """Return (user_facts, server_facts). Never raises."""
        if not (user_content or "").strip():
            return [], []

        constants = self.llm.constants
        prompt = build_prompt(
            display_name=display_name, user_content=user_content,
            bot_response=bot_response, known_facts=known_facts,
            include_server=include_server, recent=recent)
        try:
            result = await self.llm.call(
                model=constants.MODEL_FAST,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=MAX_TOKENS, temperature=0.0,
            )
        except Exception:
            logger.exception("memory extraction failed")
            return [], []

        return parse(result.text or "")


def parse(text: str) -> tuple[list[str], list[str]]:
    """Lenient parse of the extraction payload."""
    data = extract_json_object(text)
    if not data:
        return [], []

    def _list(key: str) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value
                if isinstance(v, (str, int, float)) and str(v).strip()]

    return _list("user_facts"), _list("server_facts")
