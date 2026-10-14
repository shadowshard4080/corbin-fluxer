"""Ranking commands: rank/profile, leaderboard, setup.

Pagination is a parameter, not a button. Fluxer has no message components,
so "leaderboard page 2" is the interaction -- which the AI router handles
naturally, and which also works as `!leaderboard 2`.
"""

from __future__ import annotations

import os

from typing import Any, Optional

from ..identity import NAME
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.ranking import (ROLE_TIERS, next_role,
                            role_for_level, xp_for_level)
from ..core.registry import Param, Risk, command

PAGE_SIZE = 10

#: An image for level-up announcements (LEVEL_UP_IMAGE in .env). A fixed
#: image at a fixed URL, so it can be linked rather than uploaded. Unset:
#: the announcement has no picture.
LEVEL_UP_GIF = os.environ.get("LEVEL_UP_IMAGE", "").strip() or None
LEVEL_UP_COLOUR = 0xF1C40F   # gold


def average_colour(image_bytes: Optional[bytes]) -> Optional[int]:
    """The avatar's average colour as 0xRRGGBB, or None.

    Blocking (Pillow): call through ``asyncio.to_thread``. Same idea as
    the Discord build's ``_choose_embed_color``: the embed takes on the
    member's own colour. Near-black is rejected, since it disappears
    against a dark theme.
    """
    if not image_bytes:
        return None
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            r, g, b = img.convert("RGB").resize((1, 1),
                                                Image.Resampling.BOX
                                                ).getpixel((0, 0))
    except Exception:
        return None
    if max(r, g, b) < 40:
        return None
    return (r << 16) | (g << 8) | b


def level_up_embed(name: str, entry: Any, *,
                   avatar_url: Optional[str] = None,
                   colour: Optional[int] = None,
                   new_title: bool = False) -> Embed:
    """The rank-up card, field for field with the Discord build."""
    embed = Embed(title=f"🏆 {name} has ranked up.",
                  description="Congratulations on your achievement!",
                  colour=colour if colour is not None else LEVEL_UP_COLOUR,
                  thumbnail_url=avatar_url or None,
                  image_url=LEVEL_UP_GIF)
    embed.add_field("Level:", str(entry.level))
    role = entry.role_name or "None yet"
    embed.add_field("New role:" if new_title else "Current role:", role)
    embed.add_field("Total XP:", f"{entry.chat_xp:,}")
    embed.add_field("Total messages sent:", f"{entry.total_messages:,}")
    minutes = int(getattr(entry, "vc_seconds", 0) or 0) // 60
    if minutes:
        embed.add_field("Total VC time:", f"{minutes // 60}h {minutes % 60}m")
    return embed


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    """Turn a channel id or written name into an id, or None."""
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    index = ctx.extras.get("channel_index") or {}
    return index.get(text.lower())


def _medal(position: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position:>2}.`")


@command(
    name="rank",
    description="Show your level and XP in this server",
    risk=Risk.SAFE,
    aliases=("profile", "level", "xp"),
    params=(Param("user", "whose rank to show", kind="user", required=False),),
    examples=("what's my level", "show my rank", "what level is @sam"),
)
async def rank(ctx: CommandContext, user: Optional[str] = None) -> Any:
    ranking = ctx.extras.get("ranking")
    settings = ctx.extras.get("settings")
    if ranking is None or ctx.guild_id is None:
        return "Ranking only works inside a server."
    if settings is not None and not settings.ranking_enabled(ctx.guild_id):
        return ("Ranking is switched off in this server. An admin can turn it "
                f"on with “{NAME}, enable ranking”.")

    target = str(user or ctx.invoker.id)
    entry = ranking.get(ctx.guild_id, target)
    if entry.chat_xp == 0 and entry.total_messages == 0:
        who = "You have" if target == ctx.invoker.id else "They have"
        return f"{who} no XP here yet. Say something and I'll start counting."

    position = ranking.position(ctx.guild_id, target)
    into, span = entry.progress
    upcoming = next_role(entry.level)

    name = (ctx.invoker.display_name if target == ctx.invoker.id
            else f"<@{target}>")
    embed = Embed(title=f"{name} — level {entry.level}")
    embed.add_field("Rank", f"#{position}" if position else "unranked",
                    inline=True)
    embed.add_field("XP", f"{entry.chat_xp:,}", inline=True)
    embed.add_field("Messages", f"{entry.total_messages:,}", inline=True)
    if entry.role_name:
        embed.add_field("Title", entry.role_name, inline=True)
    embed.add_field(
        f"Progress to level {entry.level + 1}",
        f"`{entry.bar()}`  {into:,} / {span:,} XP",
    )
    if upcoming:
        level_needed, role_name = upcoming
        embed.add_field(
            "Next title",
            f"**{role_name}** at level {level_needed} "
            f"({xp_for_level(level_needed) - entry.chat_xp:,} XP away)",
        )
    return embed


@command(
    name="sync_ranks",
    description="Re-apply rank roles to everyone who has earned one",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("sync_ranking", "fix_ranks"),
    confirm_phrase=lambda a: "re-apply everyone's rank roles",
    examples=("sync the rank roles", "give everyone their rank roles"),
)
async def sync_ranks(ctx: CommandContext) -> str:
    """Backfill. Level-ups apply roles as they happen, but anyone who
    earned XP before roles existed needs a one-off pass."""
    ranking = ctx.extras.get("ranking")
    adapter = ctx.extras["adapter"]
    if ranking is None or ctx.guild_id is None:
        return "Ranking only works inside a server."

    created, problems = await adapter.ensure_rank_roles(ctx.guild_id,
                                                        ROLE_TIERS)
    if problems:
        return (f"I couldn't set the roles up: {problems[0]}. I need Manage "
                "Roles, and my role has to be above the ones I create.")

    names = [n for n, _c in ROLE_TIERS.values()]
    rows, total = ranking.leaderboard(ctx.guild_id, limit=200)
    applied, failed = 0, 0
    for entry in rows:
        role = role_for_level(entry.level)
        if not role:
            continue
        err = await adapter.apply_rank_role(ctx.guild_id, entry.user_id,
                                            role[0], names)
        if err:
            failed += 1
        else:
            applied += 1
    if not applied and not failed:
        return ("Nobody has reached level 1 yet, so there are no roles to "
                "hand out. Level 1 starts at 100 XP.")
    tail = f" {failed} failed." if failed else ""
    return f"Applied rank roles to {applied} member(s).{tail}"


@command(
    name="leaderboard",
    description="Top members by XP in this server",
    risk=Risk.SAFE,
    aliases=("top", "ranks"),
    params=(Param("page", "which page", kind="integer", required=False,
                  default=1, min_value=1, max_value=100),),
    examples=("show the leaderboard", "who's top of the leaderboard",
              "leaderboard page 2"),
)
async def leaderboard(ctx: CommandContext, page: int = 1) -> Any:
    ranking = ctx.extras.get("ranking")
    settings = ctx.extras.get("settings")
    if ranking is None or ctx.guild_id is None:
        return "Ranking only works inside a server."
    if settings is not None and not settings.ranking_enabled(ctx.guild_id):
        return "Ranking is switched off in this server."

    page = max(1, int(page or 1))
    offset = (page - 1) * PAGE_SIZE
    rows, total = ranking.leaderboard(ctx.guild_id, limit=PAGE_SIZE,
                                      offset=offset)
    if not rows:
        if total:
            return f"That's past the end. There are only {total} ranked members."
        return "Nobody has any XP here yet."

    lines = []
    for index, entry in enumerate(rows, start=offset + 1):
        title = f" · {entry.role_name}" if entry.role_name else ""
        lines.append(f"{_medal(index)} <@{entry.user_id}> — level "
                     f"**{entry.level}** ({entry.chat_xp:,} XP){title}")

    pages = max(1, -(-total // PAGE_SIZE))
    embed = Embed(
        title=f"{ctx.guild.name if ctx.guild else 'Server'} — leaderboard",
        description="\n".join(lines),
        footer=f"Page {page} of {pages} · {total} ranked members",
    )
    return embed


def _ranking_phrase(args) -> str:
    on = args.get("enabled")
    where = args.get("channel")
    base = "turn ranking on" if on else "turn ranking off"
    if on and where:
        return f"{base} and announce level-ups in <#{where}>"
    return f"{base} in this server"


@command(
    name="setup_ranking",
    description="Turn the XP and levelling system on or off, and optionally "
                "pick a channel for level-up announcements",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("ranking",),
    params=(
        Param("enabled", "on or off", kind="boolean"),
        Param("channel", "where to announce level-ups", kind="channel",
              required=False),
    ),
    confirm_phrase=_ranking_phrase,
    examples=("enable ranking",
              "set up the ranking system in this channel",
              "turn off the levelling system"),
)
async def setup_ranking(ctx: CommandContext, enabled: bool,
                        channel: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    settings.set_ranking(ctx.guild_id, enabled)
    if not enabled:
        return "Ranking is off. Existing XP is kept, I just won't add to it."

    # "in this channel" is the common phrasing, so an unresolved reference
    # means here rather than nowhere. A name like "ranking" is resolved
    # through the guild's channel index; anything unrecognised falls back to
    # the current channel rather than writing a broken id.
    target = _resolve_channel(ctx, channel) or ctx.channel.id
    settings.set_ranking_channel(ctx.guild_id, target)

    adapter = ctx.extras["adapter"]
    created, problems = await adapter.ensure_rank_roles(ctx.guild_id,
                                                        ROLE_TIERS)
    note = f" {len(created)} rank roles are ready."
    if problems:
        note = (f" I could only set up {len(created)} of {len(ROLE_TIERS)} "
                f"rank roles: {problems[0]}. I probably need Manage Roles, "
                "and my own role has to sit above the ones I create.")
    return (f"Ranking is on. I'll count XP from messages and post level-ups "
            f"in <#{target}>.{note} Level 1 starts at 100 XP.")
