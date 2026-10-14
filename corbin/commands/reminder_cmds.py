"""Reminders."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.registry import Param, Risk, command
from ..core.reminders import MAX_PER_USER, parse_when, user_timezone


def _tz_for(ctx: CommandContext):
    """Use the timezone the memory extractor learned, if any."""
    memory = ctx.extras.get("memory")
    if memory is None:
        return timezone.utc
    try:
        return user_timezone(memory.get_user(ctx.invoker.id).timezone)
    except Exception:
        return timezone.utc


@command(
    name="remind",
    description="Set a reminder",
    risk=Risk.WRITE,
    guild_only=False,
    aliases=("reminder", "remind_me"),
    params=(
        Param("when", "when, e.g. in 20 minutes, tomorrow at 9am"),
        Param("text", "what to remind you about", max_length=500),
    ),
    examples=("remind me in 20 minutes to check the oven",
              "remind me tomorrow at 9am about the meeting",
              "set a reminder for 2026-12-25 08:00 to call home"),
)
async def remind(ctx: CommandContext, when: str, text: str) -> Any:
    store = ctx.extras.get("reminders")
    if store is None:
        return "Reminders aren't available right now."

    tz = _tz_for(ctx)
    fire_at = parse_when(when, tz=tz)
    if fire_at is None:
        return (f"I couldn't read “{when}” as a time. Try "
                "“in 20 minutes”, “tomorrow at 9am”, or "
                "a date like 2026-12-25 08:00.")
    if fire_at <= datetime.now(timezone.utc):
        return "That's in the past. When did you actually mean?"

    entry = store.add(user_id=ctx.invoker.id, channel_id=ctx.channel.id,
                      guild_id=ctx.guild_id, text=text, fire_at=fire_at)
    if entry is None:
        return (f"You already have {MAX_PER_USER} reminders pending. "
                "Cancel one first.")

    local = fire_at.astimezone(tz)
    suffix = "" if tz is timezone.utc else f" ({local:%Z})"
    return (f"Noted. I'll remind you at {local:%H:%M on %d %b}{suffix}: "
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

    tz = _tz_for(ctx)
    embed = Embed(title=f"Your reminders ({len(items)})", colour=0x5BC0DE)
    embed.add_list_field("Pending", [
        f"`{r.id}` {r.fire_at.astimezone(tz):%d %b %H:%M} — {r.text}"
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
