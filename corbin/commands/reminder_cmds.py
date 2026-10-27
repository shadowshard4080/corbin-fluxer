"""Reminders."""

from __future__ import annotations

import logging
from datetime import datetime, timezone as _utc_mod
from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.registry import Param, Risk, command
from ..core.reminders import MAX_PER_USER, is_relative, parse_when, split_zone
from ..core.weather_alerts import resolve_tz, tz_name

logger = logging.getLogger("corbin.commands.reminders")
UTC = _utc_mod.utc


def _learned_tz(ctx: CommandContext):
    """The zone the memory extractor learned for the asker, or None.

    None, not UTC: guessing UTC is what made "remind me at 9pm" fire at
    2 pm for someone on Pacific time.
    """
    memory = ctx.extras.get("memory")
    if memory is None:
        return None
    try:
        return resolve_tz(memory.get_user(ctx.invoker.id).timezone)
    except Exception:
        return None


def _remember_tz(ctx: CommandContext, zone) -> None:
    """Keep a zone the user just stated, if we didn't know theirs yet."""
    memory = ctx.extras.get("memory")
    name = tz_name(zone)
    if memory is None or "/" not in name:
        return
    try:
        entry = memory.get_user(ctx.invoker.id)
        if not entry.timezone:
            entry.preferences["timezone"] = name
            memory.save_user(entry)
    except Exception:
        logger.exception("could not store timezone for %s", ctx.invoker.id)


@command(
    name="remind",
    description="Set a reminder",
    risk=Risk.WRITE,
    guild_only=False,
    aliases=("reminder", "remind_me"),
    params=(
        Param("when", "when, e.g. in 20 minutes, tomorrow at 9am"),
        Param("text", "what to remind you about", max_length=500),
        Param("timezone", "only if the user named one, e.g. PST, "
              "America/New_York", required=False),
    ),
    examples=("remind me in 20 minutes to check the oven",
              "remind me tomorrow at 9am about the meeting",
              "remind me at 9pm PST to take my meds",
              "set a reminder for 2026-12-25 08:00 to call home"),
)
async def remind(ctx: CommandContext, when: str, text: str,
                 timezone: Optional[str] = None) -> Any:
    store = ctx.extras.get("reminders")
    if store is None:
        return "Reminders aren't available right now."

    # Zone priority: the one they named, then the one we remember. A clock
    # time with neither is asked about, never assumed to be UTC.
    stated, when_text = split_zone(when)
    if timezone and stated is None:
        stated = resolve_tz(timezone)
        if stated is None:
            return (f"“{timezone}” isn't a timezone I know. Use PST/EST or "
                    "a name like America/Los_Angeles.")
    tz = stated or _learned_tz(ctx)
    if tz is None:
        if not is_relative(when_text):
            return ("What timezone are you in? Say it with the time, like "
                    f"“{when_text} PST”, or give me a time from now, like "
                    "“in 2 hours”.")
        tz = UTC

    fire_at = parse_when(when_text, tz=tz)
    if fire_at is None:
        return (f"I couldn't read “{when}” as a time. Try "
                "“in 20 minutes”, “tomorrow at 9am PST”, or "
                "a date like 2026-12-25 08:00.")
    if fire_at <= datetime.now(UTC):
        return "That's in the past. When did you actually mean?"
    if stated is not None:
        _remember_tz(ctx, stated)

    entry = store.add(user_id=ctx.invoker.id, channel_id=ctx.channel.id,
                      guild_id=ctx.guild_id, text=text, fire_at=fire_at)
    if entry is None:
        return (f"You already have {MAX_PER_USER} reminders pending. "
                "Cancel one first.")

    local = fire_at.astimezone(tz)
    return (f"Noted. I'll remind you at {local:%H:%M %Z on %d %b}: "
            f"“{text}”  `{entry.id}`")


@command(
    name="reminders",
    description="List your pending reminders",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("my_reminders", "list_reminders"),
    examples=("what are my reminders", "list my reminders"),
)
async def reminders(ctx: CommandContext) -> Any:
    store = ctx.extras.get("reminders")
    if store is None:
        return "Reminders aren't available right now."
    items = store.for_user(ctx.invoker.id)
    if not items:
        return "You have no reminders set."

    tz = _learned_tz(ctx) or UTC
    embed = Embed(title=f"Your reminders ({len(items)})", colour=0x5BC0DE)
    embed.add_list_field("Pending", [
        f"`{r.id}` {r.fire_at.astimezone(tz):%d %b %H:%M %Z} — {r.text}"
        for r in items])
    embed.footer = "Cancel one with: cancel reminder <id>"
    return embed


@command(
    name="cancel_reminder",
    description="Cancel one of your reminders",
    risk=Risk.WRITE,
    guild_only=False,
    aliases=("forget_reminder", "delete_reminder"),
    params=(Param("reminder_id", "the reminder id"),),
    examples=("cancel reminder a1b2c3d4",),
)
async def cancel_reminder(ctx: CommandContext, reminder_id: str) -> str:
    store = ctx.extras.get("reminders")
    if store is None:
        return "Reminders aren't available right now."
    # Scoped to the caller, so nobody can cancel someone else's.
    removed = store.remove(reminder_id, user_id=ctx.invoker.id)
    if removed is None:
        return f"You don't have a reminder called `{reminder_id}`."
    return f"Cancelled: “{removed.text}”"
