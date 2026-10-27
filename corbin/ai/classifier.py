"""Natural-language intent classification.

One ``MODEL_FAST`` call returns both axes the dispatcher needs:

* ``confidence`` -- did I parse the right command with the right arguments?
* ``directive``  -- was this an instruction, or banter/hypothetical/quote?

They are independent.  The classifier can be 95% sure it parsed
``ban(@Steve)`` out of *"lol someone should ban Steve"* -- a correct parse of
something that was never an order.  Collapsing them into one number is what
makes a bot offer to execute jokes.

The model call is injected (:class:`ModelCall`) so the pipeline runs offline
in tests with no network, no SDK, and no API key.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from ..core.dispatch import Intent
from ..core.registry import Registry, Risk
from ..identity import NAME as BOT_NAME
from .json_utils import coerce_bool, coerce_float, extract_json_object
from ..core.logtext import said

logger = logging.getLogger("corbin.classifier")

#: The JSON answer itself is well under 200 tokens. The rest is headroom
#: for a reasoning model, which spends tokens thinking before it writes
#: anything: too small a budget and it stops mid-thought with no answer,
#: which reads as "no command matched". The few-shot examples in
#: SYSTEM_PROMPT do most of the work, so a non-reasoning model is fine here
#: and much faster.
CLASSIFIER_MAX_TOKENS = 1024

SYSTEM_PROMPT = """\
You convert a chat message into a bot command for a Discord-style bot named
{NAME}, or decline.

Output TWO INDEPENDENT judgements. Do not conflate them:

1. "confidence" (0.0-1.0): how sure you are that you picked the RIGHT command
   with the RIGHT arguments. Parsing accuracy ONLY.

2. "is_instruction" (true/false): is the speaker telling {NAME} to DO this,
   right now?

IMPORTANT: a message addressed to {NAME} ("{NAME}, ban @X", "@{NAME} play
music") is the CLEAREST possible case of is_instruction = true. Being spoken
to is evidence FOR an instruction, never against it.

A QUESTION IS STILL AN INSTRUCTION when it asks for something a command can
provide. "What's the weather in Oslo?", "who is @sam?", "what time is it in
Tokyo?" are all is_instruction = true -- the user wants the answer, and
phrasing a request politely does not make it hypothetical.

A SUGGESTION OR INVITATION TO {NAME} IS AN INSTRUCTION. "you should join
the gaming vc", "come hang out in voice", "why don't you play some music",
"you could check the weather" all ask {NAME} to do it -- casual phrasing is
still a request. So is a POLITE QUESTION with a concrete action: "can you
join vc?", "could you play some lofi?", "would you check the weather in
Oslo?", "mind skipping this song?" are requests, worded kindly. Only a
suggestion aimed at SOMEONE ELSE ("someone should ban Steve", "Sam should
play music") is not.

Set "is_instruction": false ONLY when the message is not a live request:
- joking or exaggerating        -> "lol ban him", "someone should ban Steve"
- hypothetical or conditional   -> "what if you banned me", "imagine if"
- asking about your ABILITIES in general, not for a thing now ->
  "can you even ban people?", "are you able to play music?", "do you have
  weather?", "is it possible for you to join voice?" (a general ability
  question, often with "even", "able to", "possible" or no specific target.
  "can you play X" naming the thing to do is a request, see above)
- about {NAME} in third person  -> "{NAME} would totally ban him"
- quoting or reminiscing        -> "he told me to ban you", "remember when"
- narrating a past action       -> "I banned him yesterday"

A perfect parse can still be non-instruction. Set high confidence and
is_instruction=false in that case -- that combination is normal and correct.

EXAMPLES

msg: {NAME}, ban @Steve for posting scam links
{"command":"ban","args":{"user":"@Steve","reason":"posting scam links"},
 "confidence":0.97,"is_instruction":true,"why_not":""}

msg: lol someone should ban Steve
{"command":"ban","args":{"user":"Steve"},"confidence":0.9,
 "is_instruction":false,"why_not":"joking, not addressed to {NAME}"}

msg: {NAME} who is @sam
{"command":"whois","args":{"user":"@sam"},"confidence":0.95,
 "is_instruction":true,"why_not":""}

msg: What's the weather in Denver, CO?
{"command":"weather","args":{"location":"Denver, CO"},"confidence":0.97,
 "is_instruction":true,"why_not":""}

msg: you should join the gaming vc
{"command":"voice-chat","args":{"channel":"gaming vc"},"confidence":0.9,
 "is_instruction":true,"why_not":""}

msg: {NAME} can you join vc?
{"command":"voice-chat","args":{},"confidence":0.9,
 "is_instruction":true,"why_not":""}

msg: {NAME}, what do you have streaming on tv
{"command":"tv-channels","args":{},"confidence":0.9,
 "is_instruction":true,"why_not":""}

msg: Will that be a full pot?
{"command":null,"args":{},"confidence":0.0,"is_instruction":false,
 "why_not":"about cooking, not the moon"}

msg: this is the entire log (again), can you look for it?
{"command":null,"args":{},"confidence":0.0,"is_instruction":false,
 "why_not":"a pasted file, not the server's logging"}

msg: can you even ban people?
{"command":null,"args":{},"confidence":0.0,"is_instruction":false,
 "why_not":"asking about capability"}

msg: hey what's up
{"command":null,"args":{},"confidence":0.0,"is_instruction":false,"why_not":""}

RULES
- Respond with ONLY the JSON object. No prose, no code fences.
- Never invent a command name that is not in the list.
- One shared word is not a match. Pick a command only when the message is
  about what that command does: "full" in "a full pot" is not the moon, a
  "log" someone pasted is not server logging, and "TV" means the live TV
  channels, not a video stream.
- Never invent argument values the user did not supply -- omit them instead.
- Keep the user's wording for user/role arguments (e.g. "@Steve"); do not
  guess numeric ids.
""".replace("{NAME}", BOT_NAME)


class ModelCall(Protocol):
    """A single completion call. ``LLM.classifier_call`` in the bot."""

    async def __call__(
        self, *, system: str, user: str, max_tokens: int
    ) -> str: ...


def build_vocabulary(registry: Registry, *, include_risk: bool = True) -> str:
    """Render the registry as the classifier's command vocabulary."""
    lines: list[str] = []
    for spec in registry.ai_invocable():
        params = []
        for p in spec.params:
            token = f"{p.name}:{p.kind}"
            if not p.required:
                token += "?"
            params.append(token)
        sig = f"{spec.name}({', '.join(params)})"
        note = ""
        if include_risk and spec.risk is not Risk.SAFE:
            note = f"  [{spec.risk.value}]"
        lines.append(f"- {sig} — {spec.description}{note}")
        for example in spec.examples[:3]:
            lines.append(f"    e.g. \"{example}\"")
    return "\n".join(lines)


def build_prompt(text: str, registry: Registry, *,
                 hint_non_directive: bool = False) -> str:
    parts = [
        "Available commands:",
        build_vocabulary(registry),
        "",
    ]
    if hint_non_directive:
        parts.append(
            "NOTE: this message contains markers often associated with humour "
            "or hypotheticals. Weigh that, but judge the message as a whole — "
            "a real instruction can still contain the word 'lol'."
        )
        parts.append("")
    parts.append(f"Message:\n{text}")
    return "\n".join(parts)


def parse_intent(raw: str, registry: Registry) -> Intent:
    """Turn raw model output into a validated :class:`Intent`."""
    data = extract_json_object(raw)
    if not data:
        logger.warning("classifier returned unparseable output: %s",
                       said(raw, 200))
        return Intent(command=None, confidence=0.0, directive=False,
                      why_not="unparseable classifier output")

    name = data.get("command")
    if not isinstance(name, str) or not name.strip():
        return Intent(command=None, confidence=0.0)

    spec = registry.get(name)
    if spec is None:
        # Hallucinated a command name. Refuse rather than guess a neighbour.
        logger.info("classifier hallucinated command %r", name)
        return Intent(command=None, confidence=0.0,
                      why_not=f"unknown command {name!r}")

    args = data.get("args")
    if not isinstance(args, dict):
        args = {}
    # Drop anything the command doesn't declare, so a hallucinated argument
    # can never reach a handler.
    declared = {p.name for p in spec.params}
    args = {k: v for k, v in args.items() if k in declared and v is not None}

    return Intent(
        command=spec.name,
        args=args,
        confidence=max(0.0, min(1.0, coerce_float(data.get("confidence"), 0.0))),
        directive=coerce_bool(
            data.get("is_instruction", data.get("directive")), True),
        why_not=str(data.get("why_not") or "")[:200],
    )


class ModelIntentClassifier:
    """Registry-driven classifier backed by one fast-model call."""

    def __init__(self, call: ModelCall, *,
                 max_tokens: int = CLASSIFIER_MAX_TOKENS) -> None:
        self._call = call
        self._max_tokens = max_tokens

    async def classify(self, text: str, *, registry: Registry,
                       hint_non_directive: bool) -> Intent:
        prompt = build_prompt(text, registry,
                              hint_non_directive=hint_non_directive)
        try:
            raw = await self._call(
                system=SYSTEM_PROMPT, user=prompt,
                max_tokens=self._max_tokens,
            )
        except Exception:
            # A classifier outage must degrade to conversation, never to a
            # guessed command.
            logger.exception("intent classification failed")
            return Intent(command=None, confidence=0.0, directive=False,
                          why_not="classifier error")
        return parse_intent(raw or "", registry)


class ScriptedClassifier:
    """Test double: maps exact message text to a canned :class:`Intent`."""

    def __init__(self, script: Optional[dict[str, Intent]] = None,
                 default: Optional[Intent] = None) -> None:
        self.script = script or {}
        self.default = default or Intent(command=None)
        self.calls: list[str] = []

    async def classify(self, text: str, *, registry: Registry,
                       hint_non_directive: bool) -> Intent:
        self.calls.append(text)
        return self.script.get(text.strip(), self.default)
