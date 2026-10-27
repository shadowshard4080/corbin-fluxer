"""Ranking commands: rank/profile, leaderboard, setup.

Pagination is a parameter, not a button. Fluxer has no message components,
so "leaderboard page 2" is the interaction -- which the AI router handles
naturally, and which also works as `!leaderboard 2`.
"""

from __future__ import annotations

import os

from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.ranking import (MAX_TIER_LEVEL, RankRoles, next_role,
                            xp_for_level)
from ..core.registry import Param, Risk, command
from ..identity import NAME

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
                   new_title: bool = False,
                   title: Optional[str] = None) -> Embed:
    """The rank-up card, field for field with the Discord build."""
    embed = Embed(title=f"🏆 {name} has ranked up.",
                  description="Congratulations on your achievement!",
                  colour=colour if colour is not None else LEVEL_UP_COLOUR,
                  thumbnail_url=avatar_url or None,
                  image_url=LEVEL_UP_GIF)
    embed.add_field("Level:", str(entry.level))
    # ``title``: the server's own tier name (RankRoles); the default
    # tiers' name only for a caller that doesn't pass one.
    role = (title if title is not None else entry.role_name) or "None yet"
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


def _rank_roles(ctx: CommandContext) -> RankRoles:
    return ctx.extras.get("rank_roles") or RankRoles(ctx.extras["settings"])


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
    roles = _rank_roles(ctx)
    tiers = roles.tiers(ctx.guild_id)
    upcoming = next_role(entry.level, tiers)
    title = roles.role_for(ctx.guild_id, entry.level)

    name = (ctx.invoker.display_name if target == ctx.invoker.id
            else f"<@{target}>")
    embed = Embed(title=f"{name} — level {entry.level}")
    embed.add_field("Rank", f"#{position}" if position else "unranked",
                    inline=True)
    embed.add_field("XP", f"{entry.chat_xp:,}", inline=True)
    embed.add_field("Messages", f"{entry.total_messages:,}", inline=True)
    if title:
        embed.add_field("Title", title, inline=True)
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

    roles = _rank_roles(ctx)
    created, problems = await adapter.ensure_rank_roles(
        ctx.guild_id, roles.tiers(ctx.guild_id))
    if problems:
        return (f"I couldn't set the roles up: {problems[0]}. I need Manage "
                "Roles, and my role has to be above the ones I create.")

    # Retired names too, so a renamed or removed tier comes off everyone.
    names = roles.all_names(ctx.guild_id)
    rows, total = ranking.leaderboard(ctx.guild_id, limit=200)
    applied, failed = 0, 0
    for entry in rows:
        role = roles.role_for(ctx.guild_id, entry.level)
        if not role:
            continue
        err = await adapter.apply_rank_role(ctx.guild_id, entry.user_id,
                                            role, names)
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

    roles = _rank_roles(ctx)
    lines = []
    for index, entry in enumerate(rows, start=offset + 1):
        name = roles.role_for(ctx.guild_id, entry.level)
        title = f" · {name}" if name else ""
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
    tiers = _rank_roles(ctx).tiers(ctx.guild_id)
    created, problems = await adapter.ensure_rank_roles(ctx.guild_id, tiers)
    note = f" {len(created)} rank roles are ready."
    if problems:
        note = (f" I could only set up {len(created)} of {len(tiers)} "
                f"rank roles: {problems[0]}. I probably need Manage Roles, "
                "and my own role has to sit above the ones I create.")
    return (f"Ranking is on. I'll count XP from messages and post level-ups "
            f"in <#{target}>.{note} Level 1 starts at 100 XP.")


# -- customising the rank roles ----------------------------------------------

def _colour(text: Optional[str]) -> Optional[int]:
    """"#ffaa00", "ffaa00", "0xffaa00" -> int; None if not given.
    Raises ValueError on something that isn't a hex colour."""
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip().lower().removeprefix("#").removeprefix("0x")
    if len(raw) != 6 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError(f"“{text}” isn't a colour I can read; use a hex "
                         "code like #ffaa00")
    return int(raw, 16)


async def _role_name(ctx: CommandContext, role: str) -> str:
    """A role mention or id becomes that role's name, so linking an
    existing role ("make @Pup the level 5 role") adopts it by name."""
    text = str(role or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and (text.startswith("<@&") or text.isdigit()):
        adapter = ctx.extras.get("adapter")
        try:
            index = await adapter.role_index(ctx.guild_id) if adapter else {}
        except Exception:
            index = {}
        found = index.get(digits)
        if found is not None and getattr(found, "name", None):
            return found.name
    return text.lstrip("@")


def _tier_list(tiers: dict) -> str:
    return "\n".join(f"• Level **{lvl}**: {name} (`#{colour:06x}`)"
                      for lvl, (name, colour) in sorted(tiers.items())) \
        or "None: nobody gets a rank role here."


@command(
    name="rank_roles",
    description="List this server's rank roles and the level each one "
                "is earned at",
    risk=Risk.SAFE,
    aliases=("list_rank_roles", "rank_titles", "level_roles"),
    topic=r"rank|level|role|title",
    examples=("what are the rank roles?", "which roles do levels give?"),
)
async def rank_roles(ctx: CommandContext) -> Any:
    roles = _rank_roles(ctx)
    tiers = roles.tiers(ctx.guild_id)
    return Embed(
        title="Rank roles",
        description=_tier_list(tiers),
        footer=("Customised for this server" if roles.custom(ctx.guild_id)
                else "The default set. Admins can change them: "
                     "“make level 10 give the role Pup”"))


@command(
    name="rank_role_set",
    description="Add or change a rank role: the role a level earns, its "
                "name and colour. Naming an existing role uses that role",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("set_rank_role", "rename_rank_role", "add_rank_role"),
    params=(
        Param("level", "the level that earns it", kind="integer",
              min_value=1, max_value=MAX_TIER_LEVEL),
        Param("role", "the role's name, or @ an existing role"),
        Param("colour", "a hex colour like #ffaa00", required=False),
    ),
    topic=r"rank|level|role|title",
    confirm_phrase=lambda a: (f"make level {a.get('level')} give the role "
                              f"{a.get('role')}"),
    examples=("make level 10 give the role Pup",
              "rename the level 25 rank role to Moon Child",
              "set the level 50 rank role to @Elder with colour #c0c0c0"),
)
async def rank_role_set(ctx: CommandContext, level: int, role: str,
                        colour: Optional[str] = None) -> str:
    roles = _rank_roles(ctx)
    try:
        name = await _role_name(ctx, role)
        replaced = roles.set(ctx.guild_id, level, name, _colour(colour))
    except ValueError as exc:
        return f"That won't work: {exc}."
    adapter = ctx.extras.get("adapter")
    note = ""
    if adapter is not None:
        tier = {level: roles.tiers(ctx.guild_id)[level]}
        _made, problems = await adapter.ensure_rank_roles(ctx.guild_id, tier)
        if problems:
            note = (f" I couldn't create the role yet ({problems[0]}): I "
                    "need Manage Roles, and my role above it.")
    was = f", replacing **{replaced}**" if replaced else ""
    return (f"Level {level} now earns **{name}**{was}.{note} Say “sync the "
            "rank roles” to update everyone who's already past it.")


@command(
    name="rank_role_remove",
    description="Stop a level from giving a rank role",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("remove_rank_role", "delete_rank_role"),
    params=(Param("level", "the level whose role to drop", kind="integer",
                  min_value=1, max_value=MAX_TIER_LEVEL),),
    topic=r"rank|level|role|title",
    confirm_phrase=lambda a: f"remove the level {a.get('level')} rank role",
    examples=("remove the level 500 rank role",
              "stop level 175 giving a role"),
)
async def rank_role_remove(ctx: CommandContext, level: int) -> str:
    name = _rank_roles(ctx).remove(ctx.guild_id, level)
    if name is None:
        return f"No rank role is set for level {level}."
    return (f"Level {level} no longer gives **{name}**. The role itself "
            "is still in the server; say “sync the rank roles” to take it "
            "off members, or delete it in the server settings.")


@command(
    name="rank_roles_reset",
    description="Put this server's rank roles back to the defaults",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    always_confirm=True,
    aliases=("reset_rank_roles",),
    topic=r"rank|level|role|title",
    confirm_phrase=lambda a: "put the rank roles back to the defaults",
    examples=("reset the rank roles to default",),
)
async def rank_roles_reset(ctx: CommandContext) -> str:
    roles = _rank_roles(ctx)
    if not roles.custom(ctx.guild_id):
        return "They're already the defaults."
    roles.reset(ctx.guild_id)
    return ("Rank roles are back to the defaults. Say “sync the rank "
            "roles” to swap everyone over.")
