"""The model layer: one local, OpenAI-compatible endpoint, nothing else.

Every model call in the bot goes through :class:`LLM`. It routes to one of
two providers:

* ``MODEL_MAIN`` -- conversation, presence chime-ins, greetings, musings.
* ``MODEL_FAST`` -- the intent classifier and memory extraction. Short,
  structured, called often. By default this is the same model as the main
  one; point ``CORBIN_FAST_MODEL`` at something smaller if replies feel
  slow, since the classifier runs on every message addressed to Corbin.

Anything that serves the OpenAI chat-completions API works: Ollama, LM
Studio, llama.cpp's server, vLLM. There is no cloud fallback. If the
model is down, Corbin says nothing and the log says why.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from types import SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger("corbin.llm")


class LLM:
    """Routes calls to the main or fast provider, with a short retry."""

    MAX_ATTEMPTS = 2
    #: Seconds before the retry, plus up to half a second of jitter.
    RETRY_DELAY = 1.0

    def __init__(self, main: Any, fast: Any = None) -> None:
        self.main = main
        self.fast = fast or main
        #: Kept under this name so call sites read ``model=MODEL_MAIN``.
        self.constants = SimpleNamespace(MODEL_MAIN=main.model,
                                         MODEL_FAST=self.fast.model)

    @property
    def local(self) -> Any:
        """The main provider, for health reporting."""
        return self.main

    @property
    def persona_baked_in(self) -> bool:
        """True when the model carries its own persona (a Modelfile SYSTEM)."""
        return bool(getattr(self.main, "persona_baked_in", False))

    @property
    def vision(self) -> bool:
        """True when the main model can read images."""
        return bool(getattr(self.main, "vision", False))

    async def available(self) -> bool:
        return await self.main.available()

    def _provider(self, model: str) -> Any:
        return self.fast if model == self.constants.MODEL_FAST else self.main

    async def call(self, *, model: str, messages: list[dict],
                   system: Optional[str] = None, max_tokens: int = 4096,
                   temperature: Optional[float] = None,
                   tools: Optional[list] = None) -> Any:
        """One completion. Retries once on failure, then raises.

        ``tools`` are OpenAI function schemas. A model that doesn't support
        them usually just ignores them and answers.
        """
        provider = self._provider(model)
        started = time.monotonic()
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                result = await provider.call(
                    messages=messages, system=system, max_tokens=max_tokens,
                    temperature=temperature,
                    **({"tools": tools} if tools else {}))
                logger.info(
                    "AI call success (model=%s, attempt=%d, latency=%.2fs, "
                    "in=%s, out=%s, reasoning=%s%s)",
                    result.model, attempt, time.monotonic() - started,
                    result.input_tokens, result.output_tokens,
                    result.reasoning_tokens,
                    "".join(f", tool={c['name']}" for c in
                            getattr(result, "tool_calls", None) or ()))
                return result
            except Exception as exc:
                last_error = exc
                # The message, not just the type: a bare "RuntimeError"
                # hides why the server refused.
                logger.warning("AI call attempt %d to %s failed (%s: %s)",
                               attempt, provider.model, type(exc).__name__,
                               str(exc)[:300])
                if attempt < self.MAX_ATTEMPTS:
                    await asyncio.sleep(self.RETRY_DELAY
                                        + random.uniform(0, 0.5)
                                        * bool(self.RETRY_DELAY))
        provider.mark_down()
        raise last_error if last_error else RuntimeError("AI call failed")

    def classifier_call(self):
        """A :class:`~corbin.ai.classifier.ModelCall` bound to MODEL_FAST."""
        fast = self.constants.MODEL_FAST

        async def _call(*, system: str, user: str, max_tokens: int) -> str:
            result = await self.call(
                model=fast, system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens)
            return result.text or ""

        return _call
