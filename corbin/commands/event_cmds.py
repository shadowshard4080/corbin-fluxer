"""Server events: the calendar channel and the commands around it.

The rules live in :mod:`corbin.core.events`. Mods (Manage Messages) add
and remove events; setting the calendar up and its default timezone
needs Manage Server; anyone can list them. Subscribing is the 🔔 on the
calendar card, handled in ``bot.py`` (``_on_reaction``), and the DM
question it asks is answered through :func:`answer_reminder_question`.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ..core import events as E
from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..core.weather_alerts import resolve_tz, tz_name

logger = logging.getLogger("corbin.event_cmds")

TOPIC = r"\bevents?\b|\bcalendars?\b"
#: What @everyone gets in a #events he makes: read, and react (the 🔔),
#: but not post; the calendar is his card.
EVERYONE_ALLOW = (Perm.VIEW_CHANNEL | Perm.READ_MESSAGE_HISTORY
                  | Perm.ADD_REACTIONS)
EVERYONE_DENY = Perm.SEND_MESSAGES
MAX_TOKENS = 2000


def _store(extras: Any) -> E.EventStore:
    return extras.get("events") or E.EventStore(extras["settings"])


def _resolve_channel(ctx: CommandContext, value: Optional[str]
                     ) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    if text.startswith("<#") and text.endswith(">"):
        return text[2:-1]
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


# -- the calendar card ----------------------------------------------------------

async def refresh_calendar(adapter: Any, store: E.EventStore, guild_id: str,
                           guild_name: str = "") -> Optional[str]:
    """Edit the calendar card to match the events; if it's gone, post a
    new one (with its 🔔) and remember it. Returns the card's id, or
    None when it couldn't be shown (logged)."""
    cfg = store.get(guild_id)
    channel_id = cfg.get("channel_id")
    if not channel_id:
        return None
    embed = E.calendar_embed(store.upcoming(guild_id), guild_name=guild_name)
    message_id = cfg.get("message_id")
    if message_id and await adapter.edit_message(
            channel_id, message_id, Response(embed=embed)):
        return str(message_id)
    try:
        new_id = await adapter.send(channel_id, Response(embed=embed))
    except Exception:
        logger.warning("events: couldn't post the calendar in %s for %s",
                       channel_id, guild_id, exc_info=True)
        return None
    if not new_id:
        logger.warning("events: the calendar didn't post in %s for %s",
                       channel_id, guild_id)
        return None
    cfg = store.get(guild_id)
    cfg["message_id"] = str(new_id)
    store.put(guild_id, cfg)
    if not await adapter.add_reaction(channel_id, new_id, E.BELL):
        logger.warning("events: couldn't add %s to the calendar in %s",
                       E.BELL, channel_id)
    if message_id:
        logger.info("events: calendar for %s reposted in %s (the old card "
                    "%s couldn't be edited)", guild_id, channel_id,
                    message_id)
    return str(new_id)


async def ensure_events_channel(adapter: Any, guild_id: str,
                                near: Optional[str]
                                ) -> tuple[Optional[str], bool, list[str]]:
    """Create or adopt #events: ``(id, created, problems)``. An existing
    one is used as it is; a new one goes in the asker's category."""
    from ..core.channel_access import grant_self
    from ..core.reaction_roles import BOT_ALLOW

    http = adapter._rest()
    try:
        channels = list(await http.get_guild_channels(guild_id) or [])
    except Exception as exc:
        return None, False, [f"couldn't list the channels ({exc})"]
    for raw in channels:
        if str(raw.get("name") or "").lower() == E.CHANNEL_NAME \
                and int(raw.get("type") or 0) == 0:
            return str(raw["id"]), False, []
    parent = next((str(c["parent_id"]) for c in channels
                   if str(c.get("id")) == str(near) and c.get("parent_id")),
                  None)
    try:
        made = await http.create_guild_channel(
            guild_id, name=E.CHANNEL_NAME, type=0, topic=E.CHANNEL_TOPIC,
            parent_id=parent)
        channel_id = str(made.get("id"))
    except Exception as exc:
        return None, False, [str(exc)]
    problems: list[str] = []
    # His own access first: once @everyone is denied posting, a failed
    # grant would leave him unable to post the calendar.
    if not await grant_self(adapter, http, guild_id, channel_id, BOT_ALLOW):
        problems.append("couldn't give myself permission to post there; "
                        "give my role Send Messages and Add Reactions")
    try:
        await http.edit_channel_permissions(
            channel_id, guild_id, allow=str(int(EVERYONE_ALLOW)),
            deny=str(int(EVERYONE_DENY)), type=0)
    except Exception as exc:
        problems.append(f"couldn't stop people posting in it ({exc})")
    logger.info("events: made #%s (%s) in %s", E.CHANNEL_NAME, channel_id,
                guild_id)
    return channel_id, True, problems


# -- commands -------------------------------------------------------------------

@command(
    name="events_setup",
    description="Set up the server events calendar channel",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("setup_events", "events_calendar"),
    topic=TOPIC,
    params=(
        Param("channel", "where the calendar goes; default a new #events",
              kind="channel", required=False),
        Param("timezone", "the server's default timezone for events, if "
              "named, e.g. PT, Europe/London", required=False),
    ),
    confirm_phrase=lambda a: ("set up the events calendar"
                              + (f" in {a['channel']}" if a.get("channel")
                                 else " in a new #events channel")),
    # The first three are what the classifier sees; the owner's two
    # phrasings lead (2026-11-18).
    examples=("set up the calendar", "set up events",
              "set up an events calendar in #announcements",
              "make an events channel with a calendar"),
)
async def events_setup(ctx: CommandContext, channel: Optional[str] = None,
                       timezone: Optional[str] = None) -> str:
    adapter = ctx.extras["adapter"]
    store = _store(ctx.extras)
    gid = str(ctx.guild_id)
    zone = None
    if timezone:
        zone = resolve_tz(timezone)
        if zone is None:
            return (f"“{timezone}” isn't a timezone I know. Try PT, EST or "
                    "a name like Europe/London.")
    note = ""
    if channel:
        target = _resolve_channel(ctx, channel)
        if not target:
            return f"I can't find a channel called {channel}."
    else:
        target, created, problems = await ensure_events_channel(
            adapter, gid, str(ctx.channel.id))
        if target is None:
            return (f"I couldn't make #{E.CHANNEL_NAME}: {problems[0]}. I "
                    "need Manage Channels, or name a channel to use.")
        note = (f"Made <#{target}>: everyone can read it and react, only I "
                "post there." if created else f"Using the existing "
                f"<#{target}>.")
        if problems:
            note += " (" + "; ".join(problems) + ")"
    cfg = store.get(gid)
    moved = cfg.get("channel_id") and str(cfg["channel_id"]) != str(target)
    cfg["channel_id"] = str(target)
    if moved:
        cfg.pop("message_id", None)
    if zone is not None:
        cfg["tz"] = tz_name(zone)
    store.put(gid, cfg)
    card = await refresh_calendar(adapter, store, gid,
                                  ctx.guild.name if ctx.guild else "")
    if card is None:
        return (f"{note} I couldn't post the calendar in <#{target}>; I "
                "need Send Messages and Embed Links there.").strip()
    tz_line = (f" An event with no timezone written uses the mod's own, "
               f"else {store.get(gid)['tz']}."
               if store.get(gid).get("tz") else
               " An event with no timezone written uses the mod's own if I "
               "know it; otherwise I'll ask.")
    return (f"{note} The calendar is up in <#{target}>. Mods can say “add "
            "an event: Movie night Friday at 8pm”, several at once is "
            f"fine, and anyone can react {E.BELL} on it for DM reminders."
            f"{tz_line}").strip()


@command(
    name="events_add",
    description="Add one or more events to the server calendar",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_MESSAGES,
    aliases=("add_event", "add_events", "new_event", "schedule_event"),
    topic=TOPIC,
    params=(Param("events", "the event(s) as written: names, dates, times",
                  required=False),),
    examples=("add an event: Movie night Friday at 8pm PT",
              "add these events: game night Saturday 7pm, karaoke Sunday "
              "9pm",
              "put the Christmas party on the events calendar for Dec 20 "
              "at 6pm"),
)
async def events_add(ctx: CommandContext, events: Optional[str] = None
                     ) -> str:
    store = _store(ctx.extras)
    gid = str(ctx.guild_id)
    cfg = store.get(gid)
    if not cfg.get("channel_id"):
        return ("There's no events calendar here yet. Someone with Manage "
                "Server can say “set up an events calendar”.")
    # The whole message, not the classifier's copy: several events in
    # one message are the point, and the classifier's argument is one
    # tidied string at best.
    text = str(ctx.extras.get("raw_text") or events or "").strip()
    if not text:
        return "Which events? Give me a name, a date and a time for each."
    llm = ctx.extras.get("llm")
    if llm is None:
        raise RuntimeError("events_add needs the llm in extras")
    server_tz = cfg.get("tz")
    zone = resolve_tz(server_tz) or _learned_tz(ctx) or timezone.utc
    prompt = E.extract_prompt(text, now=datetime.now(zone), tz=server_tz)
    constants = llm.constants
    result = await llm.call(
        model=constants.MODEL_FAST,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS, temperature=0.0, structured=True)
    mod_tz = _learned_tz(ctx)
    found, problems, readable, need_zone = E.check_extracted(
        result.text or "", now=time.time(), server_tz=server_tz,
        mod_tz=tz_name(mod_tz) if mod_tz else None)
    if not readable:
        logger.warning("events: couldn't read the event list for %s (%d "
                       "chars back)", gid, len(result.text or ""))
        return ("I couldn't make out the events in that. Try one per line: "
                "name, date, time.")
    reply = ""
    if found:
        try:
            added = store.add(gid, found, by=str(ctx.invoker.id))
        except ValueError as exc:
            return f"Nothing added: {exc}."
        logger.info("events: %d added in %s by %s (%d refused)",
                    len(added), gid, ctx.invoker.id, len(problems))
        await refresh_calendar(ctx.extras["adapter"], store, gid,
                               ctx.guild.name if ctx.guild else "")
        reply = added_text(added, cfg["channel_id"])
    if need_zone:
        # No zone written, none known for this mod, no server default:
        # ask rather than read it as UTC (owner, 2026-11-18).
        questions = ctx.extras.get("event_tz_questions")
        if questions is None:
            raise RuntimeError("event_tz_questions is missing from extras")
        questions[(str(ctx.channel.id), str(ctx.invoker.id))] = {
            "guild": gid, "items": need_zone,
            "deadline": time.monotonic() + TZ_QUESTION_TTL}
        names = ", ".join(f"“{i.get('title')}”" for i in need_zone)
        reply += (("\n\n" if reply else "") + f"What timezone is {names} "
                  "in? Say something like “PT” or “Europe/London” (I'll "
                  "remember yours), or cancel.")
        logger.info("events: asked %s for a timezone for %d event(s) in %s",
                    ctx.invoker.id, len(need_zone), gid)
    if problems:
        reply += ("\n\n" if reply else "") + "Not added:\n- " + \
            "\n- ".join(problems)
    if not reply:
        return "Nothing added: I didn't find any events in that."
    return reply


#: How long "what timezone?" stays answerable.
TZ_QUESTION_TTL = 600.0


def added_text(added: list[dict], channel_id: str) -> str:
    """Every date spelled out: the model read them, and a misread has
    to be obvious right here, not when a reminder fires on the wrong
    day."""
    lines = [f"**#{e['id']} · {e['title']}**: {E.when_text(e)}"
             for e in added]
    return (f"Added {len(added)} event{'s' if len(added) != 1 else ''} to "
            f"<#{channel_id}>:\n" + "\n".join(lines)
            + "\n-# Wrong? “remove event 3” and add it again.")


async def answer_timezone_question(questions: dict, extras: Any, *,
                                   guild_id: Optional[str], guild_name: str,
                                   channel_id: str, user_id: str,
                                   content: str) -> Optional[str]:
    """The reply to "what timezone is that in?", or None when this
    message isn't an answer (handled as normal; the question stays open
    until it expires)."""
    key = (str(channel_id), str(user_id))
    asked = questions.get(key)
    if asked is None:
        return None
    if time.monotonic() > asked["deadline"] or \
            str(asked["guild"]) != str(guild_id):
        questions.pop(key, None)
        return None
    if re.match(r"^\s*(cancel|never\s*mind|nevermind|forget it)\b",
                content or "", re.IGNORECASE):
        questions.pop(key, None)
        return "Okay, I left those off the calendar."
    zone = E.zone_in(content)
    if zone is None:
        return None
    questions.pop(key, None)
    store = _store(extras)
    gid = str(guild_id)
    found, problems, _left = E.check_items(asked["items"], now=time.time(),
                                           fallback=zone)
    _remember_zone(extras.get("memory"), user_id, zone)
    reply = ""
    if found:
        try:
            added = store.add(gid, found, by=str(user_id))
        except ValueError as exc:
            return f"Nothing added: {exc}."
        logger.info("events: %d added in %s by %s after asking the "
                    "timezone", len(added), gid, user_id)
        await refresh_calendar(extras["adapter"], store, gid, guild_name)
        reply = added_text(added, store.get(gid)["channel_id"])
    if problems:
        reply += ("\n\n" if reply else "") + "Not added:\n- " + \
            "\n- ".join(problems)
    return reply or "Nothing to add."


def _remember_zone(memory: Any, user_id: str, zone: Any) -> None:
    """Keep a mod's stated zone, like reminders do, so they aren't asked
    again. Only when none was known, and only a real IANA name."""
    name = tz_name(zone)
    if memory is None or "/" not in name:
        return
    try:
        entry = memory.get_user(str(user_id))
        if not entry.timezone:
            entry.preferences["timezone"] = name
            memory.save_user(entry)
    except Exception:
        logger.exception("events: couldn't store the timezone for %s",
                         user_id)


def _learned_tz(ctx: CommandContext):
    memory = ctx.extras.get("memory")
    if memory is None:
        return None
    try:
        return resolve_tz(memory.get_user(ctx.invoker.id).timezone)
    except Exception:
        return None


@command(
    name="events_remove",
    description="Remove an event from the server calendar",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_MESSAGES,
    aliases=("remove_event", "delete_event", "cancel_event"),
    topic=TOPIC,
    params=(Param("event", "its number (#3) or name"),),
    examples=("remove event 3", "cancel the movie night event",
              "delete event #2 from the calendar"),
)
async def events_remove(ctx: CommandContext, event: str) -> str:
    store = _store(ctx.extras)
    gid = str(ctx.guild_id)
    if not store.get(gid).get("channel_id"):
        return "There's no events calendar here."
    gone = store.remove(gid, event)
    if not gone:
        return (f"No event matches “{event}”. “what events are coming up?” "
                "lists them with their numbers.")
    await refresh_calendar(ctx.extras["adapter"], store, gid,
                           ctx.guild.name if ctx.guild else "")
    names = ", ".join(f"#{e['id']} {e['title']}" for e in gone)
    logger.info("events: %d removed in %s by %s", len(gone), gid,
                ctx.invoker.id)
    return f"Removed {names}."


@command(
    name="events_list",
    description="List the upcoming server events",
    risk=Risk.SAFE,
    aliases=("events", "upcoming_events", "event_calendar"),
    topic=TOPIC,
    examples=("what events are coming up?", "show the events calendar",
              "any events this week?"),
)
async def events_list(ctx: CommandContext) -> Any:
    store = _store(ctx.extras)
    gid = str(ctx.guild_id)
    if not store.get(gid).get("channel_id"):
        return ("There's no events calendar here. Someone with Manage "
                "Server can say “set up an events calendar”.")
    upcoming = store.upcoming(gid)
    embed = E.calendar_embed(upcoming, guild_name=ctx.guild.name
                             if ctx.guild else "", limit=24)
    if len(upcoming) > 24:
        # Every event, not a cut list: a missing one reads as cancelled.
        embed.fields = embed.fields[:-1]
        embed.add_list_field("…and the rest", [
            f"#{e['id']} · {e['title']}: {E.when_text(e)}"
            for e in upcoming[24:]])
    return embed


@command(
    name="events_timezone",
    description="Set the default timezone for the server's event times",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("event_timezone",),
    topic=TOPIC + r"|\btime ?zone\b",
    params=(Param("timezone", "e.g. PT, EST, Europe/London"),),
    examples=("events timezone Pacific", "set the events timezone to EST"),
)
async def events_timezone(ctx: CommandContext, timezone: str) -> str:
    zone = resolve_tz(timezone)
    if zone is None:
        return (f"“{timezone}” isn't a timezone I know. Try PT, EST or a "
                "name like Europe/London.")
    store = _store(ctx.extras)
    gid = str(ctx.guild_id)
    cfg = store.get(gid)
    cfg["tz"] = tz_name(zone)
    store.put(gid, cfg)
    return (f"Event times here default to {cfg['tz']} now. Events already "
            "on the calendar keep the time they were given.")


# -- the 🔔 and the DM question ----------------------------------------------------

async def bell(adapter: Any, store: E.EventStore, questions: dict, *,
               guild_id: str, guild_name: str, user_id: str,
               added: bool) -> None:
    """Someone put their 🔔 on the calendar, or took it off."""
    if not added:
        if store.unsubscribe(guild_id, user_id):
            logger.info("events: %s unsubscribed in %s", user_id, guild_id)
        return
    entry = store.subscribe(guild_id, user_id)
    logger.info("events: %s subscribed in %s", user_id, guild_id)
    text = E.QUESTION.format(guild=guild_name or "this server",
                             default=E.describe_all(entry["offsets"]))
    ref = await adapter.send_dm_ref(user_id, Response(content=text))
    if ref is None:
        # Closed DMs: no question and no reminders would arrive either.
        logger.warning("events: couldn't DM %s about reminders for %s; "
                       "they're subscribed but DMs may be closed",
                       user_id, guild_id)
        return
    questions[(str(ref[0]), str(user_id))] = {
        "guild": guild_id, "guild_name": guild_name,
        "deadline": time.monotonic() + E.QUESTION_TTL}


def answer_reminder_question(questions: dict, store: E.EventStore, *,
                             channel_id: str, user_id: str,
                             content: str) -> Optional[str]:
    """The reply to "how long before?", or None when this DM isn't an
    answer (it's then handled as normal)."""
    key = (str(channel_id), str(user_id))
    asked = questions.get(key)
    if asked is None:
        return None
    if time.monotonic() > asked["deadline"]:
        questions.pop(key, None)
        return None
    offsets, problem = E.parse_offsets(content)
    if problem:
        return f"I can't do that: {problem}. Try again, like “2 hours”."
    if offsets is None:
        return None
    questions.pop(key, None)
    gid, name = asked["guild"], asked.get("guild_name") or "that server"
    if not offsets:
        store.unsubscribe(gid, user_id)
        logger.info("events: %s turned reminders off for %s", user_id, gid)
        return (f"Okay, no event reminders from **{name}**. Your "
                f"{E.BELL} is still on the calendar; take it off and put "
                "it back if you change your mind.")
    if store.offsets(gid, user_id) is None:
        return (f"Your {E.BELL} isn't on **{name}**'s calendar any more, "
                "so there's nothing to set.")
    store.subscribe(gid, user_id, offsets)
    logger.info("events: %s set %d reminder(s) for %s", user_id,
                len(offsets), gid)
    return (f"Got it: I'll DM you {E.describe_all(offsets)} before each "
            f"event in **{name}**.")
