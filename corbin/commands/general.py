"""Low-risk everyday commands."""

from __future__ import annotations

from ..core.context import CommandContext, Embed
from ..core.registry import Param, Risk, command


@command(
    name="ping",
    description="Check that the bot is responding",
    risk=Risk.SAFE,
    guild_only=False,
    examples=("are you alive?", "ping"),
)
async def ping(ctx: CommandContext) -> str:
    return "Still here."


@command(
    name="whois",
    description="Show information about a member",
    risk=Risk.SAFE,
    params=(Param("user", "the member to look up", kind="user"),),
    examples=("who is @sam", "tell me about @alex"),
)
async def whois(ctx: CommandContext, user: str) -> Embed:
    adapter = ctx.extras["adapter"]
    profile = await adapter.profile(ctx.guild_id, user)
    if profile is None:
        return Embed(title="Not found",
                     description="I couldn't find that member here.")
    embed = Embed(title=profile.display_name or profile.user_id)
    if profile.tenure_days is not None:
        embed.add_field("In this server", f"{profile.tenure_days:.0f} days",
                        inline=True)
    if profile.account_age_days is not None:
        embed.add_field("Account age", f"{profile.account_age_days:.0f} days",
                        inline=True)
    embed.add_field("Roles", str(profile.role_count), inline=True)
    embed.add_field("Messages", str(profile.message_count), inline=True)
    if profile.warning_count:
        embed.add_field("Warnings", str(profile.warning_count), inline=True)
    return embed
