"""Server events: a calendar channel, bell subscriptions, DM reminders.

Owner, 2026-11-18. A server's ``#events`` channel holds one calendar
card he keeps up to date. Mods add events in plain words, several at
once ("Movie night Fri 8pm PT, game night Saturday at 7"); a model turns
the message into a list, and every date is then checked here and spelled
out in the reply, so a misread is seen at once instead of firing a
reminder at the wrong time. Anyone who reacts 🔔 on the calendar is
DMed and asked how long before each event they want reminding, and the
``events`` scheduler job sends those DMs.

Stored under ``events`` in each guild's chat settings::

    {channel_id, message_id, tz, next_id,
     events: {"<id>": {title, start, tz, description, by, created,
                       sent: ["<user>:<offset>"]}},
     subscribers: {"<user>": {offsets: [seconds], since}}}

Times are unix seconds (UTC). Nothing here guesses: an event with no
time, no timezone it can find, or a date in the past is refused, with
the reason, in the mod's reply.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .context import Embed
from .weather_alerts import resolve_tz, tz_name

logger = logging.getLogger("corbin.events")

KEY = "events"
CHANNEL_NAME = "events"
CHANNEL_TOPIC = ("Upcoming server events. React 🔔 on the calendar for DM "
                 "reminders before each one.")
BELL = "🔔"
COLOUR = 0x5865F2
#: Reminders a new subscriber gets until they answer the DM.
DEFAULT_OFFSETS = (3600,)
MAX_OFFSETS = 5
MAX_OFFSET = 30 * 86400
MIN_OFFSET = 60
MAX_EVENTS = 100
MAX_AHEAD = timedelta(days=366)
MAX_TITLE = 100
MAX_DESCRIPTION = 300
#: An event drops off the calendar this long after it starts.
KEEP_AFTER_START = 3 * 3600
#: Fields the calendar card shows; the rest are counted, and "what
#: events are coming up?" lists them all.
CARD_EVENTS = 20
#: How long the DM question stays answerable.
QUESTION_TTL = 24 * 3600.0


# -- storage ------------------------------------------------------------------

class EventStore:
    """One server's calendar. ``settings`` is a ChatSettings."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def get(self, guild_id: str) -> dict:
        value = self.settings.store.get(str(guild_id), KEY, {})
        return json.loads(json.dumps(value)) if isinstance(value, dict) \
            else {}

    def put(self, guild_id: str, cfg: dict) -> None:
        self.settings.store.set(str(guild_id), KEY, cfg)

    def guilds(self) -> list[str]:
        return [g for g in self.settings.store.guilds()
                if not g.startswith("_") and self.get(g).get("channel_id")]

    def find_calendar(self, message_id: str,
                      guild_id: Optional[str] = None) -> Optional[str]:
        """The guild whose calendar card is ``message_id``, or None."""
        for gid in ([str(guild_id)] if guild_id else self.guilds()):
            if str(self.get(gid).get("message_id") or "") == str(message_id):
                return gid
        return None

    # -- events --

    def add(self, guild_id: str, events: list[dict], by: str) -> list[dict]:
        cfg = self.get(guild_id)
        stored = cfg.setdefault("events", {})
        if len(stored) + len(events) > MAX_EVENTS:
            raise ValueError(f"a calendar holds up to {MAX_EVENTS} events")
        added = []
        now = int(time.time())
        for ev in events:
            eid = int(cfg.get("next_id") or 1)
            cfg["next_id"] = eid + 1
            entry = {"title": ev["title"], "start": int(ev["start"]),
                     "tz": ev["tz"], "description": ev.get("description")
                     or "", "by": str(by), "created": now, "sent": []}
            stored[str(eid)] = entry
            added.append(dict(entry, id=eid))
        self.put(guild_id, cfg)
        return added

    def remove(self, guild_id: str, ref: str) -> list[dict]:
        """By number ("3", "#3") or by title words; every match goes."""
        cfg = self.get(guild_id)
        stored = cfg.get("events") or {}
        key = str(ref or "").strip().lstrip("#")
        if key.isdigit():
            hits = [key] if key in stored else []
        else:
            want = _norm(key)
            hits = [k for k, e in stored.items()
                    if want and want in _norm(e.get("title", ""))]
        gone = [dict(stored.pop(k), id=int(k)) for k in hits]
        if gone:
            self.put(guild_id, cfg)
        return gone

    def upcoming(self, guild_id: str, now: Optional[float] = None
                 ) -> list[dict]:
        now = time.time() if now is None else now
        evs = [dict(e, id=int(k)) for k, e in
               (self.get(guild_id).get("events") or {}).items()
               if e.get("start", 0) + KEEP_AFTER_START > now]
        return sorted(evs, key=lambda e: (e["start"], e["id"]))

    def prune(self, guild_id: str, now: Optional[float] = None) -> int:
        """Drop events long past. Returns how many went."""
        now = time.time() if now is None else now
        cfg = self.get(guild_id)
        stored = cfg.get("events") or {}
        old = [k for k, e in stored.items()
               if e.get("start", 0) + KEEP_AFTER_START <= now]
        for k in old:
            stored.pop(k)
        if old:
            self.put(guild_id, cfg)
        return len(old)

    # -- subscribers --

    def subscribe(self, guild_id: str, user_id: str,
                  offsets: Optional[list[int]] = None) -> dict:
        cfg = self.get(guild_id)
        subs = cfg.setdefault("subscribers", {})
        entry = subs.get(str(user_id)) or {"since": int(time.time())}
        entry["offsets"] = sorted(set(offsets if offsets is not None
                                      else entry.get("offsets")
                                      or DEFAULT_OFFSETS), reverse=True)
        subs[str(user_id)] = entry
        self.put(guild_id, cfg)
        return entry

    def unsubscribe(self, guild_id: str, user_id: str) -> bool:
        cfg = self.get(guild_id)
        if (cfg.get("subscribers") or {}).pop(str(user_id), None) is None:
            return False
        self.put(guild_id, cfg)
        return True

    def offsets(self, guild_id: str, user_id: str) -> Optional[list[int]]:
        entry = (self.get(guild_id).get("subscribers") or {}).get(
            str(user_id))
        return list(entry.get("offsets") or []) if entry else None

    # -- reminders --

    def due(self, guild_id: str, now: Optional[float] = None
            ) -> list[tuple[dict, str, int]]:
        """``(event, user_id, offset)`` reminders to send now, marked
        sent before they're returned (a crash mid-send must not repeat
        them on every tick).

        Due once its time has come and the event hasn't started. A time
        that was already past when they subscribed, or when the event
        was added, never fires: "1 day before" for an event added an
        hour ahead is not a reminder anyone asked for."""
        now = time.time() if now is None else now
        cfg = self.get(guild_id)
        out = []
        for key, ev in (cfg.get("events") or {}).items():
            start = ev.get("start", 0)
            if start <= now:
                continue
            sent = set(ev.get("sent") or [])
            for uid, sub in (cfg.get("subscribers") or {}).items():
                floor = max(sub.get("since", 0), ev.get("created", 0)) - 60
                for off in sub.get("offsets") or ():
                    at = start - off
                    tag = f"{uid}:{off}"
                    if at <= now and at >= floor and tag not in sent:
                        sent.add(tag)
                        out.append((dict(ev, id=int(key)), uid, off))
            ev["sent"] = sorted(sent)
        if out:
            self.put(guild_id, cfg)
        return out


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", str(text).lower()).strip()


# -- showing them --------------------------------------------------------------

def when_text(ev: dict) -> str:
    """The event's time in its own zone, plus a Fluxer timestamp that
    each viewer sees in theirs."""
    zone = resolve_tz(ev.get("tz")) or timezone.utc
    local = datetime.fromtimestamp(ev["start"], zone)
    clock = local.strftime("%I:%M %p").lstrip("0")
    return (f"{local.strftime('%a %d %b %Y')}, {clock} "
            f"{local.strftime('%Z')} · <t:{int(ev['start'])}:R>")


def calendar_embed(events: list[dict], *, guild_name: str = "",
                   limit: int = CARD_EVENTS) -> Embed:
    embed = Embed(title="📅 Upcoming events", colour=COLOUR,
                  description=None if events else
                  "Nothing planned yet. Mods can say “add an event: Movie "
                  "night Friday at 8pm PT”.")
    for ev in events[:limit]:
        value = when_text(ev)
        if ev.get("description"):
            value += f"\n{ev['description']}"
        embed.add_field(f"#{ev['id']} · {ev['title']}", value)
    if len(events) > limit:
        embed.add_field("…", f"and {len(events) - limit} more: ask me "
                             "“what events are coming up?”")
    embed.footer = (f"React {BELL} for a DM before each event · "
                    + (guild_name or "this server"))
    return embed


def reminder_text(ev: dict, offset: int, guild_name: str) -> str:
    when = f"<t:{int(ev['start'])}:R>"
    text = (f"⏰ **{ev['title']}** starts {when} "
            f"({when_text(ev).split(' · ')[0]}) in **{guild_name}**.")
    if ev.get("description"):
        text += f"\n{ev['description']}"
    return text + (f"\n-# You asked for this {describe(offset)} before. "
                   f"Take your {BELL} off the calendar to stop.")


def describe(seconds: int) -> str:
    for unit, size in (("week", 604800), ("day", 86400), ("hour", 3600),
                       ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    mins = round(seconds / 60)
    return f"{mins} minute{'s' if mins != 1 else ''}"


def describe_all(offsets: list[int]) -> str:
    parts = [describe(o) for o in sorted(offsets, reverse=True)]
    return parts[0] if len(parts) == 1 else \
        ", ".join(parts[:-1]) + " and " + parts[-1]


# -- "how long before?" ----------------------------------------------------------

_DURATION = re.compile(
    r"(\d+(?:\.\d+)?|an?|one|two|three|four|five|six|ten|twelve|"
    r"fifteen|thirty|half an?)\s*-?\s*"
    r"(w(?:ee)?ks?|d(?:ays?)?|h(?:ou)?rs?|h|m(?:in(?:ute)?s?)?)\b",
    re.IGNORECASE)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
          "five": 5, "six": 6, "ten": 10, "twelve": 12, "fifteen": 15,
          "thirty": 30, "half a": 0.5, "half an": 0.5}
_UNIT = {"w": 604800, "d": 86400, "h": 3600, "m": 60}
_STOP = re.compile(r"^\s*(none|no(?:thing)?|stop|never|no reminders?|"
                   r"unsubscribe|don'?t)\b", re.IGNORECASE)
_AT_START = re.compile(r"\b(at the start|when it starts|as it starts)\b",
                       re.IGNORECASE)


def parse_offsets(text: str) -> tuple[Optional[list[int]], Optional[str]]:
    """``(offsets, problem)``. ``([], None)`` means "no reminders";
    ``(None, None)`` means this isn't an answer at all."""
    raw = text or ""
    if _STOP.match(raw):
        return [], None
    found = []
    for m in _DURATION.finditer(raw):
        n = m.group(1).lower()
        amount = _WORDS.get(n) if n in _WORDS else float(n)
        unit = _UNIT[m.group(2).lower()[0]]
        found.append(int(round(amount * unit)))
    if _AT_START.search(raw):
        found.append(0)
    if not found:
        return None, None
    bad = [f for f in found if f and not MIN_OFFSET <= f <= MAX_OFFSET]
    if bad:
        return None, ("reminders can be from 1 minute to 30 days before "
                      "an event")
    found = sorted(set(found), reverse=True)
    if len(found) > MAX_OFFSETS:
        return None, f"that's more than {MAX_OFFSETS} reminders per event"
    # "At the start" is a reminder at 0; the job needs it before start.
    return [f or 30 for f in found], None


QUESTION = (
    "You turned on event reminders for **{guild}**. How long before each "
    "event should I DM you? For example “1 hour”, “1 day and 30 "
    "minutes”, or “none”. Until you answer, it's {default} before.")


# -- reading a mod's message -----------------------------------------------------

#: "Add these events to the calendar:", "add Lab 6e, 12/10 4:30 PM to the
#: calendar", "<name>, add an event: ...". The first line only: it's the
#: instruction, and the list under it can say anything. Live 2026-11-18
#: a 15-event list matched events-add below the confidence floor, went
#: to chat, and the chat model said it had added them.
_ADD_REQUEST = re.compile(
    r"^\s*(?:@?[\w .'-]{1,32}[,:]\s*)?(?:please\s+|can you\s+|could you\s+)?"
    r"(?:add|put|schedule)\b[^\n]*\b(?:calendar|events?)\b", re.IGNORECASE)


def match_add(text: str) -> bool:
    """A plain request to add events, which skips the classifier."""
    first = (text or "").strip().split("\n", 1)[0]
    return bool(_ADD_REQUEST.match(first))



EXTRACT_PROMPT = """You read a moderator's message that adds events to a \
server calendar, and list every event in it as JSON.

Today is {today}. The server's default timezone is {tz}.

Reply with ONLY a JSON array, one object per event:
[{{"title": "short name", "date": "YYYY-MM-DD", "time": "HH:MM" (24-hour) \
or null, "timezone": "the timezone written for it, e.g. PT, EST, \
Europe/London" or null, "description": "anything else said about it" \
or null}}]

Rules:
- Work weekday and relative dates ("Friday", "next Saturday", "tomorrow", \
"the 5th") out from today. A weekday means the next one after today.
- Numeric dates are month/day/year ("12/10/2026" is December 10), unless \
the first number can't be a month ("13/10" is October 13).
- If no time is written for an event, time is null. Never invent one.
- Only a timezone written in the message; otherwise null.
- The title is what the event is, not the date.
- No events in it: reply [].

Message:
{message}"""


def extract_prompt(message: str, *, now: datetime, tz: Optional[str]) -> str:
    return EXTRACT_PROMPT.format(
        today=now.strftime("%A %d %B %Y"),
        tz=tz or "not set", message=message.strip()[:4000])


def _json_array(text: str) -> Optional[list]:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def check_extracted(text: str, *, now: float, server_tz: Optional[str],
                    mod_tz: Optional[str]
                    ) -> tuple[list[dict], list[str], bool, list[dict]]:
    """``(events, problems, readable, need_zone)`` from the model's JSON.

    Each event gets ``start`` (unix) and ``tz`` (an IANA name). The zone
    is the one written for it, else the mod's own (owner, 2026-11-18:
    "use the timezone of the mod who sent it"), else the server's
    default. With none of those it is never read as UTC: it comes back
    in ``need_zone`` and the mod is asked (:func:`check_items` then)."""
    items = _json_array(text)
    if items is None:
        return [], [], False, []
    fallback = resolve_tz(mod_tz) or resolve_tz(server_tz)
    events, problems, need_zone = check_items(items, now=now,
                                              fallback=fallback)
    return events, problems, True, need_zone


def check_items(items: list, *, now: float, fallback: Any = None
                ) -> tuple[list[dict], list[str], list[dict]]:
    """``(events, problems, need_zone)``. ``fallback`` is the zone for
    an item with none written; without one, such items are returned in
    ``need_zone`` untouched, to be checked again once the mod says."""
    events, problems, need_zone = [], [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:MAX_TITLE]
        label = f"“{title}”" if title else "an event with no name"
        if not title:
            problems.append("an event had no name")
            continue
        date, clock = item.get("date"), item.get("time")
        if not clock:
            problems.append(f"{label} needs a time")
            continue
        written = item.get("timezone")
        zone = resolve_tz(written) if written else None
        if written and zone is None:
            problems.append(f"{label}: “{written}” isn't a timezone I know")
            continue
        zone = zone or fallback
        try:
            local = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            problems.append(f"{label}: I couldn't read the date")
            continue
        if zone is None:
            need_zone.append(item)
            continue
        start = local.replace(tzinfo=zone).timestamp()
        if start <= now:
            problems.append(f"{label} is in the past "
                            f"({local.strftime('%a %d %b %Y %H:%M')})")
            continue
        if start - now > MAX_AHEAD.total_seconds():
            problems.append(f"{label} is more than a year away")
            continue
        desc = " ".join(str(item.get("description") or "").split())
        events.append({"title": title, "start": int(start),
                       "tz": tz_name(zone),
                       "description": desc[:MAX_DESCRIPTION]})
    return events, problems, need_zone


def zone_in(text: str) -> Any:
    """A timezone a reply names ("PT", "it's Europe/London", "pacific
    time"), or None."""
    from .reminders import split_zone

    raw = (text or "").strip().strip(".!")
    zone = resolve_tz(raw)
    if zone is None:
        zone, _rest = split_zone(raw)
    if zone is None:
        words = re.sub(r"\b(?:it'?s|in|time|zone|timezone|the|please)\b",
                       " ", raw, flags=re.IGNORECASE).split()
        if len(words) == 1:
            zone = resolve_tz(words[0])
    return zone
