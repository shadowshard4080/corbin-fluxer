"""Unprompted musings: the bot bringing something up on his own.

Presence mode only ever *reacts*. This lets him start something now and
then -- a stray thought, or something tied to what the server is into --
so not everything he says is an answer to someone.

Two halves, kept apart so the schedule is testable without a model:

* :class:`MusingPlanner` decides **when**. Each guild rolls a random
  number of posts per local day (0-3, weighted so some days have none) at
  random times. A due slot waits until a channel is ready rather than
  firing into the void, and there is a hard cap of ``MAX_PER_DAY`` in any
  rolling 24 hours with ``MIN_GAP`` between posts. State is persisted, so
  a restart neither resets the cap nor re-rolls the day.
* :class:`Musings` decides **what**: mostly whatever is on his mind
  (``open``), sometimes something tied to the server's stored notes
  (``server``). The model may answer ``SKIP``; a weak musing is worse
  than none.

The presence loop drives both (``PresenceEngine.muse``), and it picks the
channel: the general chat an admin named (``chat_mode`` asks for it
when global mode is turned on), else the most recently active
presence-enabled channel that is not an always-on AI channel. Either way
only once it has gone quiet, so he never cuts into a live conversation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

logger = logging.getLogger("corbin.musings")

#: (posts that day, weight). Mean ~1.2/day; a quarter of days are silent.
DAILY_WEIGHTS = ((0, 25), (1, 40), (2, 25), (3, 10))
#: Hard cap in any rolling 24 hours, whatever the plan says.
MAX_PER_DAY = 3
#: Minimum seconds between two musings in one guild.
MIN_GAP = 3 * 3600
#: A channel qualifies only if a human spoke in it this recently...
ACTIVE_WITHIN = 4 * 3600
#: ...and nobody has spoken in it for at least this long.
QUIET_FOR = 15 * 60
#: Topics remembered per guild so he doesn't repeat himself.
RECENT_TOPICS = 40
#: The "day" musings are planned against.
TZ_NAME = os.environ.get("MUSINGS_TIMEZONE") or "America/Los_Angeles"
STATE_FILE = "fluxer_musings.json"
MAX_TOKENS = 1200


def _local_tz() -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:
        return dt.timezone(dt.timedelta(hours=-8))


class MusingPlanner:
    """When a guild is due a musing. Pure bookkeeping, no model calls."""

    def __init__(self, path: str | os.PathLike[str] | None, *,
                 rng: Optional[random.Random] = None,
                 clock: Callable[[], float] = time.time,
                 tz: Optional[dt.tzinfo] = None) -> None:
        self.path = Path(path) if path else None
        self.rng = rng or random.Random()
        self.clock = clock
        self.tz = tz or _local_tz()
        self._state: dict[str, dict] = self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict:
        if not self.path or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.warning("unreadable %s; starting a fresh plan", self.path)
            return {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("could not persist %s", self.path)

    # -- planning ---------------------------------------------------------

    def _today(self, now: float) -> tuple[str, float, float]:
        local = dt.datetime.fromtimestamp(now, self.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=1)
        return start.date().isoformat(), start.timestamp(), end.timestamp()

    def _guild(self, guild_id: str) -> dict:
        now = self.clock()
        day, start, end = self._today(now)
        g = self._state.setdefault(str(guild_id), {})
        if g.get("day") != day:
            count = self.rng.choices([n for n, _ in DAILY_WEIGHTS],
                                     [w for _, w in DAILY_WEIGHTS])[0]
            g["day"] = day
            g["slots"] = sorted(self.rng.uniform(start, end)
                                for _ in range(count))
            g.setdefault("sent", [])
            g.setdefault("topics", [])
            self._save()
            if count:
                logger.info("musings for guild %s on %s: %d planned",
                            guild_id, day, count)
        return g

    def due(self, guild_id: str) -> bool:
        """True when a planned slot has passed and the caps allow a post."""
        g = self._guild(guild_id)
        now = self.clock()
        if not any(s <= now for s in g["slots"]):
            return False
        sent = [t for t in g["sent"] if now - t < 86400]
        if len(sent) >= MAX_PER_DAY:
            return False
        return not sent or now - max(sent) >= MIN_GAP

    def consume(self, guild_id: str, topic: Optional[str] = None) -> None:
        """Use up the earliest due slot; record a post if one went out.

        A SKIP consumes the slot too, or a guild with nothing to say would
        spend a model call on every sweep for the rest of the day.
        """
        g = self._guild(guild_id)
        now = self.clock()
        due = [s for s in g["slots"] if s <= now]
        if due:
            g["slots"].remove(min(due))
        if topic is not None:
            g["sent"] = [t for t in g["sent"] if now - t < 86400] + [now]
            if topic:
                g["topics"] = (g["topics"] + [topic])[-RECENT_TOPICS:]
        self._save()

    def recent_topics(self, guild_id: str) -> list[str]:
        return list(self._guild(guild_id).get("topics") or [])

    def today_label(self) -> str:
        local = dt.datetime.fromtimestamp(self.clock(), self.tz)
        return f"{local:%A, %B} {local.day}, {local.year}"


_TOPIC = re.compile(r"^\s*TOPIC\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_MESSAGE = re.compile(r"^\s*MESSAGE\s*:\s*(.+)", re.IGNORECASE
                      | re.MULTILINE | re.DOTALL)


def parse_musing(text: str) -> Optional[tuple[str, str]]:
    """``(message, topic)`` from the model's reply, or None to skip."""
    raw = (text or "").strip()
    if not raw or re.match(r"\s*SKIP\b", raw, re.IGNORECASE):
        return None
    msg = _MESSAGE.search(raw)
    if not msg:
        return None
    message = msg.group(1).strip()
    topic = _TOPIC.search(raw)
    if not message or re.match(r"SKIP\b", message, re.IGNORECASE):
        return None
    return message, (topic.group(1).strip()[:80] if topic else message[:60])


_FORMAT = """\
Reply in EXACTLY this format, or with the single word SKIP:
TOPIC: <three to six words naming the subject>
MESSAGE: <what you post>"""

_RULES = """\
You're posting this unprompted, like a regular in the chat who just thought
of something. Keep it short, the way people actually type. Don't greet
anyone, don't address the group, and don't tack on "what do you all
think?". You've never been anywhere or done anything, so don't claim you
have. Anything you state as fact has to be true: no invented numbers,
dates or holidays. If you've got nothing worth saying, or it would repeat
something under "Already shared", reply SKIP."""

#: How often each kind is picked, before dropping the unavailable ones.
#: "open" leads on purpose: when every musing was a fact about the server
#: or a dated observance, he read as a calendar bot.
KIND_WEIGHTS = (("open", 60), ("server", 40))

#: Loose starting points for an open musing. Without one the model lands
#: on the same handful of "fun facts" every time.
SEEDS = (
    "food", "words and where they come from", "the sky", "music",
    "animals that aren't birds", "birds", "something old", "the ocean",
    "how people behave", "a small everyday annoyance", "games",
    "something you've been wondering about", "an opinion you'd defend",
    "weather", "plants", "space", "a strange law or custom", "sleep",
    "colours", "machines and how they work", "smells", "maps and places",
)


class Musings:
    """What to say. One model call."""

    def __init__(self, llm: Any, conversation: Any, planner: MusingPlanner,
                 *, memory: Any = None,
                 guild_name: Optional[Callable[[str],
                                               Awaitable[str]]] = None,
                 general_channel: Optional[Callable[[str],
                                                    Optional[str]]] = None,
                 always_on: Optional[Callable[[str, str], bool]] = None,
                 rng: Optional[random.Random] = None) -> None:
        self.llm = llm
        self.conversation = conversation
        self.planner = planner
        self.memory = memory
        self.guild_name = guild_name
        self._general = general_channel
        self._always_on = always_on
        self.rng = rng or random.Random()

    def general_channel(self, guild_id: str) -> Optional[str]:
        """The channel an admin named as general chat, if any."""
        return self._general(guild_id) if self._general else None

    def is_always_on(self, guild_id: str, channel_id: str) -> bool:
        return bool(self._always_on and self._always_on(guild_id, channel_id))

    def _server_facts(self, guild_id: str) -> list[str]:
        if self.memory is None:
            return []
        try:
            return list(self.memory.get_server(guild_id).facts)
        except Exception:
            logger.exception("server memory lookup failed")
            return []

    def _pick_kind(self, has_facts: bool) -> str:
        allowed = {"open": True, "server": has_facts}
        kinds = [(k, w) for k, w in KIND_WEIGHTS if allowed[k]]
        return self.rng.choices([k for k, _ in kinds],
                                [w for _, w in kinds])[0]

    async def compose(self, guild_id: str, channel_id: str, *,
                      kind: Optional[str] = None
                      ) -> Optional[tuple[str, str]]:
        """``(message, topic)`` or None. Never raises.

        The channel's recent messages are deliberately NOT in the prompt.
        They were once, and nearly every musing became a follow-up to
        whatever had just been said.
        """
        facts = self._server_facts(guild_id)
        kind = kind or self._pick_kind(bool(facts))
        if kind not in ("open", "server") or (kind == "server" and not facts):
            kind = "open"
        shared = self.planner.recent_topics(guild_id)

        parts = []
        if kind == "server":
            name = ""
            if self.guild_name is not None:
                try:
                    name = await self.guild_name(guild_id) or ""
                except Exception:
                    name = ""
            if name:
                parts.append(f"You are in the server \"{name}\".")
            parts.append("Some of what this server is into (background, "
                         "not a script):\n"
                         + "\n".join(f"- {f}" for f in facts[-20:]))
            parts.append("Bring up something connected to one of those: a "
                         "thought, a question, an opinion or a fact. Not a "
                         "recap of what they've told you.")
        else:
            seed = self.rng.choice(SEEDS)
            parts.append("Something's on your mind and you're bringing it "
                         "up. It can be anything: a question you've been "
                         "turning over, an opinion, a strange fact you "
                         "like, an observation. It doesn't have to connect "
                         "to today, this server, or anything anyone said. "
                         f"If you need a starting point, somewhere near "
                         f"{seed}.")
        if shared:
            parts.append("Already shared, do not repeat:\n"
                         + "\n".join(f"- {t}" for t in shared[-20:]))
        parts.append(_RULES)
        parts.append(_FORMAT)

        constants = self.llm.constants
        soul = getattr(self.conversation, "soul", "") or None
        try:
            result = await self.llm.call(
                model=constants.MODEL_MAIN, system=soul,
                messages=[{"role": "user", "content": "\n\n".join(parts)}],
                max_tokens=MAX_TOKENS,
            )
        except Exception:
            logger.exception("musing generation failed")
            return None
        if getattr(result, "truncated", False):
            logger.info("musing for guild %s was truncated; skipping",
                        guild_id)
            return None
        parsed = parse_musing(result.text or "")
        logger.info("musing for guild %s (%s): %s", guild_id, kind,
                    f"topic={parsed[1]!r}" if parsed else "SKIP")
        return parsed


def pick_channel(candidates: Sequence[tuple[str, float]],
                 last_message_at: Callable[[str], Optional[float]],
                 now: float, *, general: Optional[str] = None
                 ) -> Optional[str]:
    """Where a musing goes, once that channel has gone quiet.

    ``candidates`` is ``(channel_id, last_human_activity)``. Nothing goes
    out unless a human spoke in one of them within ``ACTIVE_WITHIN``: a
    musing into a dead server is talking to nobody.

    With a ``general`` channel set by an admin, that is the only place he
    posts. Without one, the busiest candidate wins. Either way the channel
    must have been quiet for ``QUIET_FOR`` (the bot included); if it is
    still mid-conversation the answer is "not yet", not a quieter side
    channel, since a fun fact in #art while everyone talks in #general
    reads as a bot on a timer.
    """
    if not candidates:
        return None
    busiest, active = max(candidates, key=lambda c: c[1])
    if now - active > ACTIVE_WITHIN:
        return None
    channel_id = general or busiest
    last = last_message_at(channel_id)
    if last is not None and now - last < QUIET_FOR:
        return None
    return channel_id
