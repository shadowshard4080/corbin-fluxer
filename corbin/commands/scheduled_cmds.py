"""Configuration for the daily quote and the hourly animal."""

from __future__ import annotations

from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..core.scheduled import ANIMALS, resolve_animal


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


@command(
    name="setup_dailyquote",
    description="Post a quote once a day",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("dailyquote", "daily_quote"),
    params=(
        Param("enabled", "on or off", kind="boolean"),
        Param("channel", "where to post", kind="channel", required=False),
        Param("hour", "hour of day, 0-23 UTC", kind="integer",
              required=False, default=8, min_value=0, max_value=23),
        Param("minute", "minute of the hour", kind="integer",
              required=False, default=0, min_value=0, max_value=59),
    ),
    confirm_phrase=lambda a: ("turn the daily quote on" if a.get("enabled")
                              else "turn the daily quote off"),
    examples=("post a daily quote in #general",
              "send a quote every morning at 9",
              "stop the daily quote"),
)
async def setup_dailyquote(ctx: CommandContext, enabled: bool,
                           channel: Optional[str] = None, hour: int = 8,
                           minute: int = 0) -> str:
    settings = ctx.extras["settings"]
    if not enabled:
        settings.set_daily_quote(ctx.guild_id, enabled=False)
        return "Daily quote is off."
    target = _resolve_channel(ctx, channel) or ctx.channel.id
    cfg = settings.set_daily_quote(ctx.guild_id, enabled=True,
                                   channel_id=target, hour=hour,
                                   minute=minute)
    return (f"Daily quote on in <#{target}> at "
            f"{cfg['hour']:02d}:{cfg['minute']:02d} UTC.")


@command(
    name="hourly_animals",
    description="Post an animal picture every hour",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("animals", "hourly_animal"),
    params=(
        Param("enabled", "on or off", kind="boolean"),
        Param("channel", "where to post", kind="channel", required=False),
        Param("animal", "a specific animal, or leave blank for random",
              required=False),
    ),
    confirm_phrase=lambda a: ("turn hourly animals on" if a.get("enabled")
                              else "turn hourly animals off"),
    examples=("post hourly animals in #critters",
              "send an hourly fox to this channel",
              "stop the hourly animals"),
)
async def hourly_animals(ctx: CommandContext, enabled: bool,
                         channel: Optional[str] = None,
                         animal: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    if not enabled:
        settings.set_hourly_animals(ctx.guild_id, enabled=False)
        return "Hourly animals are off."

    key = resolve_animal(animal) if animal else None
    if animal and not key:
        sample = ", ".join(sorted(ANIMALS.values())[:8])
        return (f"I don't have “{animal}”. I can do {len(ANIMALS)} animals, "
                f"including {sample}. Leave it blank for a random one.")

    target = _resolve_channel(ctx, channel) or ctx.channel.id
    settings.set_hourly_animals(ctx.guild_id, enabled=True,
                                channel_id=target, animal=key)
    what = ANIMALS[key] if key else "a random animal"
    return f"Hourly animals on in <#{target}>. I'll post {what} each hour."


@command(
    name="animal",
    description="Post an animal picture right now",
    risk=Risk.SAFE,
    params=(Param("animal", "which animal", required=False),),
    examples=("show me a fox", "post a capybara", "give me an animal"),
)
async def animal_now(ctx: CommandContext,
                     animal: Optional[str] = None) -> Any:
    from ..core.scheduled import animal_image_url, pick_animal

    if animal and not resolve_animal(animal):
        return (f"I don't have “{animal}”. Try one of: "
                f"{', '.join(sorted(ANIMALS.values())[:10])}…")
    key, label = pick_animal(animal)
    return Embed(title=f"Here is your {label}.",
                 image_url=animal_image_url(key), colour=0x5CB85C)


@command(
    name="quote",
    description="Post an inspirational quote right now",
    risk=Risk.SAFE,
    guild_only=False,
    examples=("give me a quote", "post a quote"),
)
async def quote_now(ctx: CommandContext) -> str:
    from ..core.scheduled import fetch_quote

    quote = await fetch_quote()
    if quote is None:
        return "Couldn't reach Zenquotes just now. Try again in a bit."
    return quote.render()
