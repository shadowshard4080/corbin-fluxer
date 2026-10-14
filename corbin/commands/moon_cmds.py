"""Moon phase commands."""

from __future__ import annotations

from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.moon import moon_name
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

COLOUR = 0x2C3E50


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


@command(
    name="moon",
    description="The current phase of the moon",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("moonphase", "moon_phase"),
    examples=("what phase is the moon", "show me the moon",
              "when's the next full moon"),
)
async def moon(ctx: CommandContext) -> Any:
    calc = ctx.extras.get("moon")
    if calc is None or not calc.available:
        return ("I can't work out the moon right now — the ephemeris file "
                "is missing.")
    now = calc.now()
    if now is None:
        return "The moon calculation failed, oddly enough."

    embed = Embed(title=f"{now.emoji}  {now.name}", colour=COLOUR)
    embed.add_field("Illuminated", f"{now.percent}%", inline=True)
    embed.add_field("Trend", "waxing" if now.waxing else "waning",
                    inline=True)
    if now.next_full:
        embed.add_field(
            "Next full moon",
            f"{now.next_full:%d %B %Y at %H:%M} UTC "
            f"— the {moon_name(now.next_full)}")
    if now.next_new:
        embed.add_field("Next new moon",
                        f"{now.next_new:%d %B %Y at %H:%M} UTC")
    return embed


@command(
    name="moon_calendar",
    description="Upcoming moon phases",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("moon_upcoming", "lunar_calendar"),
    params=(Param("days", "how far ahead to look", kind="integer",
                  required=False, default=60, min_value=1, max_value=365),),
    examples=("show the moon calendar", "moon phases for the next 90 days"),
)
async def moon_calendar(ctx: CommandContext, days: int = 60) -> Any:
    calc = ctx.extras.get("moon")
    if calc is None or not calc.available:
        return "The ephemeris file is missing, so I can't calculate phases."
    events = calc.upcoming(days=days)
    if not events:
        return "Nothing to report in that window."

    lines = []
    for when, kind in events:
        suffix = f" ({moon_name(when)})" if kind == "Full moon" else ""
        lines.append(f"`{when:%d %b}` {kind}{suffix}")
    embed = Embed(title=f"Moon phases, next {days} days", colour=COLOUR)
    embed.add_list_field("Upcoming", lines)
    return embed


@command(
    name="setup_fullmoon",
    description="Announce in a channel when the moon is full",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("fullmoon", "full_moon_alerts"),
    params=(
        Param("enabled", "on or off", kind="boolean"),
        Param("channel", "where to announce", kind="channel",
              required=False),
    ),
    confirm_phrase=lambda a: ("announce full moons" if a.get("enabled")
                              else "stop announcing full moons"),
    examples=("announce full moons in #general",
              "tell us when the moon is full",
              "stop the full moon announcements"),
)
async def setup_fullmoon(ctx: CommandContext, enabled: bool,
                         channel: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    if not enabled:
        settings.set_fullmoon(ctx.guild_id, None)
        return "I'll stop announcing full moons."
    target = _resolve_channel(ctx, channel) or ctx.channel.id
    settings.set_fullmoon(ctx.guild_id, target)

    calc = ctx.extras.get("moon")
    when = calc.next_full_moon() if calc and calc.available else None
    if when:
        return (f"I'll announce full moons in <#{target}>. The next one is "
                f"{when:%d %B} — the {moon_name(when)}.")
    return f"I'll announce full moons in <#{target}>."
