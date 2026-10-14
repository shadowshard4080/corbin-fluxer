"""Local model provider over the OpenAI-compatible ``/v1`` API.

Written against Ollama, but anything serving ``/v1/chat/completions``
(LM Studio, llama.cpp's server, vLLM) works. Three things drive the
implementation:

* **The persona can be baked into the model.** An Ollama Modelfile can
  carry Corbin's persona as its SYSTEM prompt, so sending ``SOUL.md``
  again would duplicate it and burn thousands of tokens a call. See
  ``persona_baked_in``.
* **Reasoning models** split output across ``content`` and a separate
  ``reasoning`` field, and a stray ``</think>`` can leak into content.
  Both are cleaned here.
* **A dead host must not stall the bot.** Availability is probed with a
  short timeout and the result cached, so health checks cost milliseconds
  rather than a TCP timeout each.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("corbin.ollama")

#: How long a health verdict is trusted.
UP_TTL = 60.0
DOWN_TTL = 30.0
#: Health probes must be quick, but not so quick that a slow network hop
#: reads as "down". Override with LLM_PROBE_TIMEOUT.
PROBE_TIMEOUT = float(os.environ.get("LLM_PROBE_TIMEOUT", "5.0"))
GENERATE_TIMEOUT = 180.0
#: Ollama's own default when a Modelfile sets no num_ctx.
OLLAMA_DEFAULT_CTX = 4096
#: Below this, Corbin's 2.5-4K-token prompts leave too little room to think
#: and answer: the model hits the ceiling mid-thought (finish=length at
#: in+out == num_ctx) and the reply comes back empty.
MIN_CONTEXT = 8192

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_STRAY_TAG = re.compile(r"</?think>", re.IGNORECASE)

#: Em-dash used as a pause. SOUL.md forbids it and the persona repeats the
#: rule, but the local model emits it in roughly a third of replies anyway.
#: Small models imitate far better than they comply, so this is enforced
#: deterministically at the boundary rather than asked for again.
#: Only spaced/padded dashes are touched: "1939—1945" and "well-known" must
#: survive untouched.
_PAUSE_DASH = re.compile(r"\s+[—–]\s+|\s*[—–]\s+|\s+[—–]\s*")


def strip_pause_dashes(text: str) -> str:
    """Replace em/en-dash pauses with a comma.

    A comma is grammatical in every position an em-dash-as-pause occupies,
    which a full stop is not, so this cannot produce a fragment.
    """
    if not text:
        return text

    def _sub(match: "re.Match[str]") -> str:
        before = text[:match.start()].rstrip()
        # A dash straight after terminal punctuation is not a pause.
        if before.endswith((".", "!", "?", ",", ":", ";")):
            return " "
        return ", "

    return _PAUSE_DASH.sub(_sub, text)


def clean_output(text: str) -> str:
    """Strip reasoning-tag residue from model output.

    A model that stops on ``<think>`` but not ``</think>`` ends replies
    with a bare closing tag. Posting that to chat looks broken.
    """
    if not text:
        return ""
    text = _THINK_BLOCK.sub("", text)
    text = _STRAY_TAG.sub("", text)
    text = strip_pause_dashes(text)
    return text.strip()


#: Degenerate-output guard. The local model at low temperature falls into
#: repetition loops and posts them as a reply -- measured on two real ones:
#: "I've been doing lobotomies on my corvids for a while, and I've been
#: doing lobotomies on my corvids for a while" (a sentence four times), and
#: an aquarium answer of "a few different colored mushrooms with different
#: colored flowers and mushrooms" bullets, where 25% of all 4-word phrases
#: were repeats. Ordinary replies, lists and comparisons measured 0%. Such
#: a reply is discarded: a loop is not an answer, and saying nothing is
#: better than posting it.
_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE = re.compile(r"[^.!?\n]+")
#: A sentence this long appearing twice is a loop, not emphasis.
DEGENERATE_SENTENCE_WORDS = 6
#: Below this, a few repeated phrases are normal ("if you do / if you don't").
DEGENERATE_MIN_WORDS = 40
#: Fraction of 4-word phrases that repeat an earlier one. Loops measured
#: 0.25-0.27; normal replies 0.0.
DEGENERATE_NGRAM_RATIO = 0.18


def degenerate_reason(text: str) -> Optional[str]:
    """Why ``text`` looks like a repetition loop, or None if it doesn't."""
    if not text:
        return None
    sentences = [" ".join(_WORD.findall(chunk.lower()))
                 for chunk in _SENTENCE.findall(text)]
    counts = Counter(s for s in sentences
                     if len(s.split()) >= DEGENERATE_SENTENCE_WORDS)
    for sentence, n in counts.items():
        if n > 1:
            return f"sentence repeated {n}x: {sentence[:60]!r}"

    words = _WORD.findall(text.lower())
    if len(words) < DEGENERATE_MIN_WORDS:
        return None
    grams = Counter(zip(words, words[1:], words[2:], words[3:]))
    total = len(words) - 3
    repeated = sum(n - 1 for n in grams.values()) / total
    if repeated >= DEGENERATE_NGRAM_RATIO:
        return f"{repeated:.0%} of 4-word phrases repeat"
    return None


def parse_num_ctx(show: Any) -> Optional[int]:
    """The ``num_ctx`` a model runs with, from Ollama's ``/api/show``.

    None when the Modelfile doesn't set one, which means Ollama's default
    (OLLAMA_DEFAULT_CTX) unless the host overrides it globally.
    """
    params = (show or {}).get("parameters") or ""
    for line in str(params).splitlines():
        bits = line.split()
        if len(bits) >= 2 and bits[0] == "num_ctx":
            try:
                return int(bits[1])
            except ValueError:
                return None
    return None


def parse_tool_calls(raw: Any) -> list:
    """OpenAI-shaped ``tool_calls`` -> ``[{"name", "arguments"}]``.

    ``arguments`` arrives as a JSON *string* on the OpenAI-compatible
    endpoint. A call whose arguments don't parse keeps its name with empty
    arguments: the caller can still act on "search" without a query, and
    dropping it would silently turn a search request into no reply.
    """
    calls: list = []
    for item in raw or ():
        fn = (item or {}).get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                logger.warning("tool call %s had unparseable arguments: %r",
                               name, args[:200])
                args = {}
        calls.append({"name": name,
                      "arguments": args if isinstance(args, dict) else {}})
    return calls


@dataclass
class OllamaResult:
    """Shaped like ``llm.LLMResult`` so call sites do not branch on provider."""

    text: str
    finish_reason: str = ""
    provider: str = "ollama"
    model: str = ""
    truncated: bool = False
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    raw: Any = None
    #: ``[{"name": str, "arguments": dict}]`` when the model asked for a
    #: tool instead of (or before) answering. Content is usually empty then.
    tool_calls: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text or self.tool_calls)


class OllamaProvider:
    def __init__(self, base_url: str, model: str, *,
                 persona_baked_in: bool = False,
                 vision: bool = False,
                 temperature: Optional[float] = None,
                 presence_penalty: Optional[float] = None,
                 probe_timeout: float = PROBE_TIMEOUT,
                 timeout: float = GENERATE_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        #: When True, callers should NOT prepend SOUL.md -- it is already in
        #: the Modelfile's system prompt.
        self.persona_baked_in = persona_baked_in
        #: When True, image blocks are sent as OpenAI ``image_url`` parts.
        #: Only for a model that can actually see (llava, qwen2.5-vl,
        #: gemma3, ...); a text-only model would describe nothing.
        self.vision = vision
        #: Overrides the Modelfile's own temperature when set.
        self.temperature = temperature
        #: Sent on every call when set (0/None sends nothing).
        self.presence_penalty = presence_penalty
        self.probe_timeout = probe_timeout
        self.timeout = timeout
        self._verdict: Optional[bool] = None
        self._verdict_until: float = 0.0
        #: The context window, once checked. None = not known.
        self.num_ctx: Optional[int] = None
        self._ctx_checked = False

    # -- health -----------------------------------------------------------

    async def available(self, *, force: bool = False) -> bool:
        """Is the host up? Cached, so an offline PC costs ~nothing."""
        now = time.monotonic()
        if not force and self._verdict is not None and now < self._verdict_until:
            return self._verdict

        import httpx

        ok = False
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout) as client:
                resp = await client.get(f"{self.base_url}/models")
            ok = resp.status_code == 200
            if ok:
                names = {m.get("id") for m in (resp.json().get("data") or [])}
                if self.model not in names:
                    logger.warning("ollama is up but %r is not loaded (have: "
                                   "%s)", self.model, ", ".join(sorted(names)))
                    ok = False
        except Exception as exc:
            logger.debug("ollama probe failed: %s", type(exc).__name__)
            ok = False

        if ok and not self._ctx_checked:
            await self._check_context()
        if ok != self._verdict:
            logger.info("local model is %s", "available" if ok else "offline")
        self._verdict = ok
        self._verdict_until = now + (UP_TTL if ok else DOWN_TTL)
        return ok

    async def _check_context(self) -> None:
        """Warn, once, when the context window is too small for our prompts.

        Uses Ollama's native ``/api/show`` (the base URL minus ``/v1``),
        since the OpenAI-compatible API has no way to report it.
        """
        import httpx

        self._ctx_checked = True
        native = self.base_url[:-3] if self.base_url.endswith("/v1") \
            else self.base_url
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout) as client:
                resp = await client.post(f"{native}/api/show",
                                         json={"model": self.model})
            if resp.status_code != 200:
                return
            ctx = parse_num_ctx(resp.json())
        except Exception:
            logger.debug("could not read the model's context size",
                         exc_info=True)
            return
        self.num_ctx = ctx or OLLAMA_DEFAULT_CTX
        if (ctx or OLLAMA_DEFAULT_CTX) < MIN_CONTEXT:
            logger.warning(
                "%s runs with a %s-token context (%s). Corbin's prompts are "
                "2.5-4K tokens, so replies run out of room mid-thought and "
                "come back empty, and an oversized prompt loses its persona "
                "off the front. Add `PARAMETER num_ctx 16384` to the "
                "Modelfile and run `ollama create`.",
                self.model, self.num_ctx,
                "Modelfile setting" if ctx else "Ollama default, not set in "
                "the Modelfile")
        else:
            logger.info("%s context window: %d tokens", self.model, ctx)

    def mark_down(self) -> None:
        """Called after a failed generation, so health reports it at once."""
        self._verdict = False
        self._verdict_until = time.monotonic() + DOWN_TTL

    # -- generation -------------------------------------------------------

    async def call(self, *, messages: list[dict],
                   system: Optional[str] = None,
                   max_tokens: int = 2048,
                   temperature: Optional[float] = None,
                   tools: Optional[list] = None) -> OllamaResult:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai(messages, system,
                                        vision=self.vision),
            "max_tokens": max_tokens,
            "stream": False,
        }
        effective_temp = (temperature if temperature is not None
                          else self.temperature)
        if effective_temp is not None:
            payload["temperature"] = effective_temp
        if self.presence_penalty:
            payload["presence_penalty"] = self.presence_penalty
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/chat/completions",
                                     json=payload)
        if resp.status_code != 200:
            raise RuntimeError(
                f"ollama returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason") or ""

        text = clean_output(message.get("content") or "")
        reasoning = message.get("reasoning") or ""
        if not text and reasoning:
            # Surface nothing rather than leaking a reasoning trace into
            # chat. WHY it ended matters: "length" is a budget problem,
            # "stop" means it finished thinking and wrote no answer -- most
            # likely it opened a second <think>, which the Modelfile stops
            # on. The tail shows what it was about to do.
            raw_content = message.get("content") or ""
            used = (usage.get("prompt_tokens") or 0) + \
                (usage.get("completion_tokens") or 0)
            ctx = self.num_ctx or OLLAMA_DEFAULT_CTX
            why = (f" -- CONTEXT WINDOW FULL ({used}/{ctx} tokens); raise "
                   f"num_ctx in the Modelfile"
                   if finish == "length" and used >= ctx - 16 else "")
            logger.warning(
                "ollama produced only reasoning (%d chars, finish=%s, "
                "raw content=%r); treating as empty%s | reasoning ends: %r",
                len(reasoning), finish or "?", raw_content[:80], why,
                reasoning[-160:].replace("\n", " "))

        loop = degenerate_reason(text)
        if loop:
            logger.warning("ollama reply discarded as a repetition loop (%s, "
                           "finish=%s): %r", loop, finish or "?", text[:160])
            text = ""

        tool_calls = parse_tool_calls(message.get("tool_calls"))

        return OllamaResult(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish,
            model=data.get("model") or self.model,
            truncated=(finish == "length"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            reasoning_tokens=(len(reasoning.split()) if reasoning else 0),
            cached_input_tokens=(usage.get("prompt_tokens_details") or {})
            .get("cached_tokens"),
            raw=data,
        )

    @staticmethod
    def _to_openai(messages: list[dict], system: Optional[str], *,
                   vision: bool = False) -> list[dict]:
        """Anthropic-style turns -> OpenAI chat messages.

        The bot builds turns as ``{"role", "content"}`` where content is a
        string or a list of ``text`` / ``image`` blocks. Without
        ``vision``, block lists are flattened to their text and images are
        dropped rather than sent as JSON the model can't use.
        """
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for turn in messages:
            content = turn.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text")
                images = [b for b in content if isinstance(b, dict)
                          and b.get("type") == "image"] if vision else []
                if images:
                    parts: list[dict] = []
                    for block in images:
                        src = block.get("source") or {}
                        parts.append({"type": "image_url", "image_url": {
                            "url": f"data:{src.get('media_type')};base64,"
                                   f"{src.get('data')}"}})
                    if text.strip():
                        parts.append({"type": "text", "text": text})
                    out.append({"role": turn.get("role", "user"),
                                "content": parts})
                    continue
                content = text
            if not (content or "").strip():
                continue
            out.append({"role": turn.get("role", "user"), "content": content})
        return out
