"""Copying the Discord server's emojis and stickers over.

One command. It is re-runnable by design — anything already present by
name is skipped — so an interrupted copy is finished by saying it again
rather than by working out where it stopped.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..core.context import CommandContext, Embed, Response
from ..core.expressions import copy_to_fluxer, fetch_discord
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

logger = logging.getLogger("corbin.expressions")


@command(
    name="copy_emojis",
    description="Copy the Discord server's emojis and stickers into this "
                "one",
    # Bulk creation. Additive rather than destructive, but a hundred
    # unwanted emojis is a long afternoon of clicking to undo, so it goes
    # through the confirm gate unless the request is unambiguous.
    risk=Risk.DESTRUCTIVE,
    permission=Perm.MANAGE_EXPRESSIONS,
    aliases=("copy_stickers", "import_emojis", "copy_expressions"),
    params=(Param("kind", "what to copy", required=False, default="both",
                  choices=("both", "emojis", "stickers")),),
    confirm_phrase=lambda a: ("copy the Discord server's "
                              f"{a.get('kind') or 'both'} into this one"),
    examples=("copy the emojis from discord",
              "import all the discord emojis and stickers",
              "copy over the discord stickers"),
)
async def copy_emojis(ctx: CommandContext,
                      kind: str = "both") -> Any:
    import config

    adapter = ctx.extras["adapter"]
    if ctx.guild_id is None:
        return "Ask me this in the server you want them copied into."

    token = getattr(config, "DISCORD_BOT_TOKEN", "")
    source = getattr(config, "DISCORD_GUILD_ID", "")
    if not token or not source:
        return ("I don't have the Discord bot's token or source server id, "
                "so I can't read the emojis. That's a setup problem on my "
                "end — DISCORD_TOKEN and CORBIN_DISCORD_GUILD.")

    try:
        found = await fetch_discord(token, source)
    except RuntimeError as exc:
        return str(exc)
    except Exception:
        logger.exception("reading Discord expressions failed")
        return "Couldn't read the Discord server's emojis. Check my logs."

    if kind == "emojis":
        found = [e for e in found if e.kind == "emoji"]
    elif kind == "stickers":
        found = [e for e in found if e.kind == "sticker"]
    if not found:
        return f"The Discord server has no {kind} to copy."

    try:
        existing = await adapter.guild_expressions(ctx.guild_id)
    except Exception as exc:
        logger.exception("listing this server's expressions failed")
        return (f"I couldn't read what's already in this server, so I've "
                f"not copied anything — otherwise I'd re-upload "
                f"duplicates of everything. ({exc})")
    todo = [e for e in found
            if f"{e.kind}:{e.name.lower()}" not in existing]
    if not todo:
        return (f"All {len(found)} are already here. Nothing to do.")

    progress_id = await ctx.respond(
        f"Copying {len(todo)} of {len(found)}… this takes about "
        f"{max(1, round(len(todo) * 1.5 / 60))} min, I have to go slowly "
        f"or we get rate limited.")

    async def tick(done: int, total: int) -> None:
        if progress_id:
            await adapter.edit_message(
                ctx.channel.id, progress_id,
                Response(content=f"Copying… {done} of {total}."))

    report = await copy_to_fluxer(found, adapter=adapter,
                                  guild_id=ctx.guild_id,
                                  existing=existing, progress=tick)
    if progress_id:
        await adapter.delete_message(ctx.channel.id, progress_id)

    embed = Embed(
        title="Copied from Discord",
        colour=0x57F287 if not report.failed else 0xE8A33D)
    embed.add_field("Added", str(report.copied), inline=True)
    if report.skipped_existing:
        embed.add_field("Already here", str(report.skipped_existing),
                        inline=True)
    if report.skipped_lottie:
        embed.add_field(
            "Animated stickers skipped", str(report.skipped_lottie),
            inline=True)
    if report.failed:
        embed.add_field("Failed", str(report.failed), inline=True)
        embed.add_list_field("What went wrong",
                             [f"• {e}" for e in report.errors[:10]])
    if report.skipped_lottie:
        embed.footer = ("Discord's animated stickers are Lottie vector "
                        "files, not images — there's nothing to upload.")
    if report.stopped_early:
        embed.add_field("Stopped early",
                        "Too many failures in a row. Say it again later "
                        "and I'll pick up where I left off.")
    return Response(embed=embed)
