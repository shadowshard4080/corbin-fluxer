"""The #do-not-type honeypot command."""

from __future__ import annotations

from typing import Any

from ..core.context import CommandContext, Embed, Response
from ..core.honeypot import (CHANNEL_NAME, NOTICE_BODY, NOTICE_TITLE,
                             ensure_channel)
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command


@command(
    name="spamblock",
    description="Create the #do-not-type honeypot that auto-bans scam bots",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("honeypot", "setup_honeypot"),
    params=(Param("enabled", "on or off", kind="boolean", required=False,
                  default=True),),
    confirm_phrase=lambda a: ("set up the scam-bot honeypot channel"
                              if a.get("enabled", True)
                              else "turn the honeypot off"),
    examples=("set up the honeypot", "create the spam trap",
              "turn off the honeypot"),
)
async def spamblock(ctx: CommandContext, enabled: bool = True) -> Any:
    settings = ctx.extras["settings"]
    adapter = ctx.extras["adapter"]

    if not enabled:
        settings.set_honeypot(ctx.guild_id, None)
        return (f"Honeypot disabled. I've left #{CHANNEL_NAME} in place — "
                "delete it yourself if you want it gone, so I can't remove "
                "a channel you were using.")

    http = getattr(adapter.client, "_http", None)
    if http is None:
        return "I can't reach the channel API right now."

    channel_id, problems = await ensure_channel(
        http, ctx.guild_id, settings.honeypot(ctx.guild_id))
    if channel_id is None:
        return (f"I couldn't set that up: {problems[0]}. I need Manage "
                "Channels and Manage Roles.")

    settings.set_honeypot(ctx.guild_id, channel_id)

    # Re-post the notice only if it is missing, so re-running is harmless.
    try:
        await adapter.send(channel_id, Response(embed=Embed(
            title=NOTICE_TITLE, description=NOTICE_BODY, colour=0xD9534F)))
    except Exception:
        problems.append("couldn't post the notice")

    embed = Embed(
        title="Honeypot armed", colour=0xD9534F,
        description=(f"<#{channel_id}> is live. Anyone who posts there is "
                     "banned automatically."))
    embed.add_field(
        "How it works",
        "Scam bots sweep writable channels top-down and post in the first "
        "one they find. This one sits at the very top and looks postable, "
        "so they hit it before reaching anywhere real.")
    embed.add_field(
        "Staff are safe",
        "Anyone with Manage Server, Ban, Kick or Timeout gets their message "
        "deleted and a quiet nudge instead of a ban.", inline=False)
    if problems:
        embed.add_field("⚠️ Partly applied", "; ".join(problems))
    return embed


@command(
    name="honeypot_status",
    description="Show whether the scam-bot honeypot is set up",
    risk=Risk.SAFE,
    permission=Perm.MANAGE_GUILD,
    examples=("is the honeypot on", "honeypot status"),
)
async def honeypot_status(ctx: CommandContext) -> str:
    settings = ctx.extras["settings"]
    channel = settings.honeypot(ctx.guild_id)
    if not channel:
        return ("No honeypot here. Say “set up the honeypot” and "
                f"I'll make #{CHANNEL_NAME}.")
    return f"Honeypot armed: <#{channel}>. Posting there is an instant ban."
