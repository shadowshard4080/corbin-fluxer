"""Organic participation: the bot chiming in unprompted.

A background loop watches channels that have seen activity ("dirty"), asks
the cheap model whether anything is worth answering, and if so has the main
model write one short message.

Two rules, both learned the hard way:

* **Reply targeting must be threaded through.** The evaluator returns *which
  message* to reply to, and that target reaches the generator, which marks
  it in the transcript. Passing only a free-text reason is what produced
  replies anchored to person A whose text was written to person B.
* **Evaluator and generator must share one window.** ``presence_window``
  applies the same last-20 / 30-minute cut to both. The generator used to
  take a raw slice, so it saw participants the evaluator had aged out.

The loop and the direct-reply path share ``cooldowns`` so the two never
answer the same channel at once.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

from ..identity import NAME as BOT_NAME

from .json_utils import extract_json_object  # noqa: F401  (kept for parity)

logger = logging.getLogger("corbin.presence")

#: Seconds between evaluation sweeps, jittered so Corbin is not metronomic.
EVAL_INTERVAL = 300.0
EVAL_JITTER = 120.0
#: Minimum seconds between autonomous messages in one channel.
MIN_INTERVAL = 90.0
#: Transcript window: most recent N, and how fresh they must be.
WINDOW_SIZE = 20
WINDOW_MAX_AGE = 30 * 60
#: Messages shown to the evaluator.
EVAL_MESSAGES = 15
EVAL_CONTENT_LEN = 300
REPLY_MAX_TOKENS = 1500

#: Matched against the FIRST line only, as a prefix. Searching the whole
#: reply for PASS threw away "RESPOND #4 | don't let that question pass".
_RESPOND = re.compile(r"\s*RESPOND\s*#?(\d+)\s*[|:\-]?\s*(.*)", re.IGNORECASE)
_PASS = re.compile(r"\s*(?:PASS|SILENT|IGNORE)\b", re.IGNORECASE)

#: The evaluator's answer is one line, but a reasoning model thinks first,
#: and too small a budget leaves it with no answer at all -- which reads
#: as PASS on every sweep, forever.
EVAL_MAX_TOKENS = 600

EVAL_SYSTEM = """\
You decide whether a chat companion named {name} should say something
unprompted in a channel he is sitting in.

He is a member of this community, not a helpdesk. Lean towards PASS, but
when something comes up that a regular would naturally react to, he SHOULD
join in. A member who never speaks is not part of the conversation.

Reply with EXACTLY one line, one of:
  PASS
  RESPOND #<n> | <short reason>

RESPOND when:
- someone asked a question nobody has answered
- someone mentions {name} by name, or talks about him
- a topic he knows or cares about came up (animals, birds, tech, music,
  games, art, science, anything he'd have an opinion on)
- someone shared something (art, news, a win, a problem) and nobody reacted
- there is a genuinely funny or interesting moment worth a reaction
- someone is clearly looking for a reply and got none

PASS when:
- the newest message is his own
- two people are in a private back-and-forth that he'd be butting into
- it is only greetings or logistics ("brb", "ok", "see you")
- all he could add is agreeing, or restating what was said

<n> is the numbered line he should reply to.""".format(name=BOT_NAME)


def presence_window(entries: Sequence[dict],
                    limit: int = WINDOW_SIZE) -> list[dict]:
    """Recent, still-fresh history. Used by BOTH evaluator and generator."""
    recent = list(entries)[-limit:]
    now = dt.datetime.now(dt.timezone.utc)
    fresh = [
        e for e in recent
        if (now - e.get("timestamp", now)).total_seconds() <= WINDOW_MAX_AGE
    ]
    return fresh or recent


def numbered_transcript(entries: Sequence[dict], *,
                        limit: int = EVAL_MESSAGES,
                        max_len: int = EVAL_CONTENT_LEN
                        ) -> tuple[str, list[dict]]:
    picked = list(entries)[-limit:]
    lines = []
    for index, entry in enumerate(picked, start=1):
        body = str(entry.get("content") or "")[:max_len]
        who = entry.get("author") or "someone"
        lines.append(f"#{index} [{who}]: {body}")
    return "\n".join(lines), picked


def parse_decision(text: str, indexed: Sequence[dict]
                   ) -> tuple[Optional[dict], str]:
    """Return (target_entry, reason). ``None`` target means stay quiet."""
    raw = (text or "").strip()
    first = raw.splitlines()[0].strip() if raw else ""
    if not first or _PASS.match(first):
        return None, "passed"
    match = _RESPOND.match(first)
    if not match:
        return None, f"unparsed: {first[:80]}"
    index = int(match.group(1)) - 1
    if not (0 <= index < len(indexed)):
        return None, f"index {index + 1} out of range"
    entry = indexed[index]
    if entry.get("is_bot"):
        # Never reply to yourself.
        return None, "target was the bot's own message"
    reason = match.group(2).strip() or "no reason given"
    return entry, reason


def reply_target(response: str, entries: Sequence[dict],
                 target: Optional[dict]) -> Optional[dict]:
    """Move the reply arrow if the text clearly addresses someone else.

    Deliberately conservative: only when exactly one *other* participant is
    named and the intended target is not. Any ambiguity keeps the original.
    """
    if not target:
        return None
    window = presence_window(entries)
    participants = {
        e["author"] for e in window
        if not e.get("is_bot") and e.get("author") and len(e["author"]) >= 3
    }

    def named(name: str) -> bool:
        return re.search(rf"(?<!\w)@?{re.escape(name)}(?!\w)", response,
                         re.IGNORECASE) is not None

    target_name = target.get("author") or ""
    if target_name and named(target_name):
        return target
    others = [p for p in participants if p != target_name and named(p)]
    if len(others) != 1:
        return target
    for entry in reversed(window):
        if entry.get("author") == others[0] and not entry.get("is_bot"):
            return entry
    return target


class PresenceEngine:
    """Owns the dirty set, the cooldowns, and the evaluation loop."""

    def __init__(self, llm: Any, conversation: Any, *,
                 send: Callable[[str, str, Optional[dict]], Awaitable[None]],
                 is_enabled: Callable[[str, str], bool],
                 min_interval: float = MIN_INTERVAL,
                 eval_interval: float = EVAL_INTERVAL,
                 musings: Any = None) -> None:
        self.llm = llm
        self.conversation = conversation
        self._send = send
        self._is_enabled = is_enabled
        self.min_interval = min_interval
        self.eval_interval = eval_interval
        #: "guild:channel" keys with unseen activity.
        self.dirty: set[str] = set()
        #: Shared with the direct-reply path so the two never overlap.
        self.cooldowns: dict[str, float] = {}
        #: guild -> {channel: last human message time}, for musings.
        self.activity: dict[str, dict[str, float]] = {}
        #: Optional ``musings.Musings``; set after construction because the
        #: search pipeline it uses is built later.
        self.musings = musings
        self._task: Optional[asyncio.Task] = None

    # -- bookkeeping ------------------------------------------------------

    @staticmethod
    def key(guild_id: str, channel_id: str) -> str:
        return f"{guild_id}:{channel_id}"

    def mark_dirty(self, guild_id: Optional[str], channel_id: str) -> None:
        if guild_id:
            self.dirty.add(self.key(guild_id, channel_id))
            self.activity.setdefault(str(guild_id), {})[str(channel_id)] = \
                time.time()

    def mark_spoke(self, guild_id: Optional[str], channel_id: str) -> None:
        """Called whenever the bot speaks, from EITHER path."""
        if guild_id:
            k = self.key(guild_id, channel_id)
            self.cooldowns[k] = time.time()
            self.dirty.discard(k)

    def on_cooldown(self, guild_id: str, channel_id: str) -> bool:
        last = self.cooldowns.get(self.key(guild_id, channel_id), 0.0)
        return (time.time() - last) < self.min_interval

    # -- loop -------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            logger.info("presence loop started (every %.0fs, %.0fs per-channel "
                        "cooldown)", self.eval_interval, self.min_interval)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(
                    self.eval_interval + random.uniform(-EVAL_JITTER,
                                                        EVAL_JITTER))
                await self.sweep()
                await self.muse()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("presence sweep failed")

    async def sweep(self) -> int:
        """Evaluate every dirty channel once. Returns how many spoke."""
        dirty, self.dirty = list(self.dirty), set()
        spoke = 0
        for key in dirty:
            try:
                guild_id, channel_id = key.split(":", 1)
            except ValueError:
                continue
            try:
                if await self.consider(guild_id, channel_id):
                    spoke += 1
            except Exception:
                logger.exception("presence failed for %s", key)
        return spoke

    async def muse(self) -> int:
        """Post any due unprompted musings. Returns how many went out."""
        if self.musings is None:
            return 0
        from .musings import pick_channel

        planner = self.musings.planner
        now = time.time()
        posted = 0
        for guild_id, channels in list(self.activity.items()):
            try:
                if not planner.due(guild_id):
                    continue
                general = self.musings.general_channel(guild_id)
                if general:
                    if (not self._is_enabled(guild_id, general)
                            or self.on_cooldown(guild_id, general)):
                        continue
                    # Activity anywhere counts: people are around, and
                    # the general chat may be the quiet one.
                    candidates = list(channels.items())
                else:
                    # Always-on channels are where people talk AT him;
                    # that is where every musing used to land.
                    candidates = [
                        (cid, ts) for cid, ts in channels.items()
                        if self._is_enabled(guild_id, cid)
                        and not self.on_cooldown(guild_id, cid)
                        and not self.musings.is_always_on(guild_id, cid)]
                channel_id = pick_channel(candidates, self._last_message_at,
                                          now, general=general)
                if channel_id is None:
                    continue  # the slot waits for a channel to be ready
                composed = await self.musings.compose(guild_id, channel_id)
                if not composed:
                    planner.consume(guild_id)
                    continue
                text, topic = composed
                await self._send(channel_id, text, None)
                self.mark_spoke(guild_id, channel_id)
                planner.consume(guild_id, topic)
                posted += 1
                logger.info("Musing in %s | topic=%s | sent=%s", channel_id,
                            topic, text[:200])
            except Exception:
                logger.exception("musing failed for guild %s", guild_id)
        return posted

    def _last_message_at(self, channel_id: str) -> Optional[float]:
        entries = self.conversation.entries(channel_id)
        if not entries:
            return None
        stamp = entries[-1].get("timestamp")
        return stamp.timestamp() if stamp else None

    async def consider(self, guild_id: str, channel_id: str) -> bool:
        if not self._is_enabled(guild_id, channel_id):
            return False
        if self.on_cooldown(guild_id, channel_id):
            return False

        entries = self.conversation.entries(channel_id)
        window = presence_window(entries)
        if len(window) < 2:
            return False
        if all(e.get("is_bot") for e in window):
            return False
        # Nothing new since Corbin last spoke.
        if window[-1].get("is_bot"):
            return False

        target, reason = await self.evaluate(window)
        if target is None:
            logger.info("presence PASS in %s (%s)", channel_id, reason)
            return False

        text = await self.generate(window, target)
        if not text:
            return False

        final = reply_target(text, entries, target)
        await self._send(channel_id, text, final)
        self.mark_spoke(guild_id, channel_id)
        logger.info("Presence response in %s | reason=%s | sent=%s",
                    channel_id, reason[:120], text[:200])
        return True

    # -- model calls ------------------------------------------------------

    async def evaluate(self, window: Sequence[dict]
                       ) -> tuple[Optional[dict], str]:
        transcript, indexed = numbered_transcript(window)
        if not transcript:
            return None, "empty transcript"
        constants = self.llm.constants
        try:
            result = await self.llm.call(
                model=constants.MODEL_FAST, system=EVAL_SYSTEM,
                messages=[{"role": "user", "content": transcript}],
                # 0.0 gave a borderline channel the same PASS on every
                # sweep, forever.
                max_tokens=EVAL_MAX_TOKENS, temperature=0.7,
            )
        except Exception:
            logger.exception("presence evaluation failed")
            return None, "evaluator error"
        return parse_decision(result.text or "", indexed)

    async def generate(self, window: Sequence[dict],
                       target: dict) -> Optional[str]:
        lines = []
        for entry in window:
            mark = ("   <<< YOU ARE REPLYING TO THIS MESSAGE"
                    if entry is target else "")
            lines.append(f"[{entry['author']}]: {entry['content']}{mark}")
        addressee = target.get("author") or "them"

        prompt = (
            "You are sitting in a chat channel as a member of this community. "
            "Something came up that interests you, so you are saying one "
            "thing about it.\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"Reply to {addressee}, to the marked message. One or two "
            "sentences, the way a person types in chat. Do not greet anyone, "
            "do not summarise the conversation, do not offer help, and do "
            "not announce that you are joining in. Just say the thing."
        )
        constants = self.llm.constants
        soul = getattr(self.conversation, "soul", "") or None
        try:
            result = await self.llm.call(
                model=constants.MODEL_MAIN, system=soul,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=REPLY_MAX_TOKENS,
            )
        except Exception:
            logger.exception("presence generation failed")
            return None

        text = (result.text or "").strip()
        if getattr(result, "truncated", False):
            from .conversation import trim_incomplete_tail

            text = trim_incomplete_tail(text)
        return text or None
