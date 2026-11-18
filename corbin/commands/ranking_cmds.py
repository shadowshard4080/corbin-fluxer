"""Ranking commands: rank/profile, leaderboard, setup.

Pagination is a parameter, not a button. Fluxer has no message components,
so "leaderboard page 2" is the interaction -- which the AI router handles
naturally, and which also works as `!leaderboard 2`.
"""

from __future__ import annotations

import logging
import os
import re
import time

from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.ranking import (MAX_TIER_LEVEL, RankRoles, next_role,
                            xp_for_level)
from ..core.registry import Param, Risk, command
from ..identity import NAME

logger = logging.getLogger("corbin.ranking_cmds")

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
    if not enabled:
        settings.set_ranking(ctx.guild_id, enabled)
        return "Ranking is off. Existing XP is kept, I just won't add to it."

    gid = str(ctx.guild_id)
    roles = _rank_roles(ctx)
    # "in this channel" is the common phrasing, so a named channel that
    # can't be resolved means here rather than nowhere.
    target = (_resolve_channel(ctx, channel) or ctx.channel.id) \
        if channel else None
    stored = settings.ranking_channel(gid)
    # Owner, 2026-11-17: ask where level-ups go (an existing channel or
    # a new one) and which roles, before making anything. A server that
    # already had ranking keeps its channel and roles: re-enabling it
    # used to move level-ups to wherever it was typed.
    ask_channel = target is None and not stored
    ask_roles = roles.never_set_up(gid)
    if not ask_channel and not ask_roles:
        return await finish_setup(ctx.extras, gid, target or stored)
    questions = ctx.extras.get("ranking_questions")
    if questions is None:
        raise RuntimeError("ranking_questions is missing from extras; "
                           "the setup questions can't be answered")
    questions[(str(ctx.channel.id), str(ctx.invoker.id))] = {
        "guild": gid, "stage": "channel" if ask_channel else "roles",
        "channel": target, "make": False, "here": str(ctx.channel.id),
        "deadline": time.monotonic() + SETUP_QUESTION_TTL}
    logger.info("ranking setup in %s: asking about the %s", gid,
                "channel" if ask_channel else "roles")
    return CHANNEL_QUESTION if ask_channel else roles_question()


# -- the setup questions -------------------------------------------------------

#: How long the setup questions stay answerable.
SETUP_QUESTION_TTL = 600.0
LEVEL_UP_CHANNEL = "level-ups"
LEVEL_UP_TOPIC = f"Level-ups and new rank roles, posted by {NAME}."
#: Seconds between one member's messages in a #level-ups he makes.
LEVEL_UP_SLOWMODE = 30
#: What @everyone gets there: read, post, react with any emoji.
MEMBER_ALLOW = (Perm.VIEW_CHANNEL | Perm.READ_MESSAGE_HISTORY
                | Perm.SEND_MESSAGES | Perm.ADD_REACTIONS
                | Perm.USE_EXTERNAL_EMOJIS)

CHANNEL_QUESTION = (
    "Where should I post level-ups? Name a channel that's already here "
    "(like #general), say **here** for this one, or say **make one** and "
    f"I'll create #{LEVEL_UP_CHANNEL}. Say cancel to stop.")


def roles_question() -> str:
    from ..core.ranking import STANDARD_TIERS
    listing = "\n".join(f"• Level {lvl}: {name}"
                        for lvl, (name, _c) in sorted(STANDARD_TIERS.items()))
    return ("Which rank roles should I make? Say **default** for these:\n"
            f"{listing}\n"
            "Or list your own, like `1 Rookie, 10 Regular, 50 Veteran "
            "#c0c0c0` (a colour is optional), or say **no roles** for XP "
            "and levels without roles. Say cancel to stop.")


#: "<name>, make one" / "@<name> default": the bot's name in front
#: isn't part of the answer.
_ADDRESS = re.compile(rf"^\s*(?:@?{re.escape(NAME)}\b[\s,:!-]*"
                      r"|<@!?\d+>[\s,:]*)", re.IGNORECASE)
_CANCEL = re.compile(r"^\s*(cancel|never\s*mind|nevermind|stop|forget it)\b",
                     re.IGNORECASE)
_MAKE = re.compile(
    r"^\s*(?:please\s+)?(?:make|create|new)\b(?:\s+(?:a|one|it|me))?"
    r"(?:\s+(?:new\s+)?(?:one|channel|#?level[\s-]?ups?))?\s*(?:please)?"
    r"[.!]*\s*$"
    r"|^\s*(?:a\s+)?new\s+(?:one|channel)\b", re.IGNORECASE)
_DEFAULT = re.compile(
    r"^\s*(?:use\s+)?(?:the\s+)?(?:defaults?|standard)(?:\s+(?:ones|roles|"
    r"list|please))?[.!]*\s*$"
    r"|^\s*(?:yes|yeah|yep|sure|ok(?:ay)?|those|sounds good|go with "
    r"(?:those|the defaults?))[.!]*\s*$", re.IGNORECASE)
_NO_ROLES = re.compile(r"^\s*(?:no(?:ne)?|without)\s*(?:rank\s+)?roles?"
                       r"[.!]*\s*$|^\s*none[.!]*\s*$", re.IGNORECASE)
_OWN = re.compile(r"\b(?:my|our)\s+own\b|\bcustom\b", re.IGNORECASE)
_TIER = re.compile(
    r"(?:level\s*)?(\d{1,4})\s*[:=.)\-–]?\s*([^,;\n#]+?)\s*"
    r"(#[0-9a-fA-F]{6})?\s*(?=[,;\n]|$)", re.IGNORECASE)


def parse_tiers(text: str) -> tuple[dict, list[str]]:
    """``{level: (name, colour)}`` and problems, from "1 Rookie, 10 Pup
    #ffaa00". Each comma, semicolon or line is one tier."""
    from ..core.ranking import DEFAULT_COLOUR
    body = _OWN.sub(" ", text or "")
    body = re.sub(r"^\s*[:\-–]+|\b(?:rank\s+)?roles?\s*:", " ", body)
    tiers, problems = {}, []
    for part in re.split(r"[,;\n]+", body):
        part = part.strip().strip(".")
        if not part:
            continue
        m = _TIER.fullmatch(part)
        if not m:
            problems.append(f"“{part}” needs a level and a name, like "
                            "`10 Regular`")
            continue
        level, name, colour = int(m.group(1)), m.group(2).strip(), m.group(3)
        if level in tiers:
            problems.append(f"level {level} is in the list twice")
            continue
        tiers[level] = (name, int(colour[1:], 16) if colour
                        else DEFAULT_COLOUR)
    return tiers, problems


def _can_make_channels(member: Any) -> bool:
    if getattr(member, "is_guild_owner", False):
        return True
    from ..core.permissions import resolve
    perms = resolve((getattr(r, "permissions", 0)
                     for r in getattr(member, "roles", ()) or ()))
    return bool(perms & (Perm.MANAGE_CHANNELS | Perm.ADMINISTRATOR))


async def answer_setup(questions: dict, extras: Any, *, guild_id: Optional[str],
                       channel_id: str, member: Any, content: str,
                       mentioned: Any, index: Any) -> Optional[str]:
    """The reply to an answer to a setup question, or None when this
    message isn't one (it's then handled as normal and the question
    stays open until it expires)."""
    key = (str(channel_id), str(member.id))
    asked = questions.get(key)
    if asked is None:
        return None
    if time.monotonic() > asked["deadline"] or \
            str(asked["guild"]) != str(guild_id):
        questions.pop(key, None)
        return None
    content = _ADDRESS.sub("", content or "")
    if _CANCEL.match(content):
        questions.pop(key, None)
        return "Okay, dropped it. Ranking is still off and nothing changed."

    gid = str(guild_id)
    roles = extras.get("rank_roles") or RankRoles(extras["settings"])
    if asked["stage"] == "channel":
        if _MAKE.match(content or ""):
            if not _can_make_channels(member):
                return ("Making a channel needs Manage Channels, which you "
                        "don't have. Name a channel that's already here "
                        "instead, or say cancel.")
            asked["make"] = True
        else:
            from .config_cmds import match_general_answer
            target = match_general_answer(content, mentioned or {},
                                          index or {}, asked["here"])
            if not target:
                return None
            asked["channel"] = target
        if roles.never_set_up(gid):
            asked["stage"] = "roles"
            asked["deadline"] = time.monotonic() + SETUP_QUESTION_TTL
            return roles_question()
        return await _finish_and_close(questions, key, extras, gid, asked)

    # stage == "roles"
    if _DEFAULT.match(content or ""):
        choice: Any = "default"
    elif _NO_ROLES.match(content or ""):
        choice = {}
    else:
        tiers, problems = parse_tiers(content)
        if not tiers and not problems:
            if _OWN.search(content or ""):
                return ("Go ahead: list them like `1 Rookie, 10 Regular, "
                        "50 Veteran #c0c0c0`, or say cancel.")
            return None
        if not tiers or problems:
            # A message that's mostly not tiers is chat, not an answer.
            if not tiers and not _OWN.search(content or ""):
                return None
            return ("I couldn't use that:\n- " + "\n- ".join(problems)
                    + "\n\nTry the whole list again, or say cancel.")
        try:
            roles.replace_all(gid, tiers)
        except ValueError as exc:
            return f"That won't work: {exc}. Try again, or say cancel."
        choice = "own"
    if choice == "default":
        roles.use_defaults(gid)
    elif choice == {}:
        roles.replace_all(gid, {})
    return await _finish_and_close(questions, key, extras, gid, asked)


async def _finish_and_close(questions: dict, key: tuple, extras: Any,
                            gid: str, asked: dict) -> str:
    """Finish, and close the question only once that worked: a crash
    in between (live 2026-11-18, ``KeyError: 'adapter'``) used to eat
    the question, so answering again did nothing."""
    reply = await finish_setup(extras, gid, asked.get("channel"),
                               make=asked["make"], near=asked["here"])
    questions.pop(key, None)
    return reply


async def finish_setup(extras: Any, gid: str, channel_id: Optional[str], *,
                       make: bool = False, near: Optional[str] = None) -> str:
    """Make the channel if asked, switch ranking on, create the roles."""
    settings = extras["settings"]
    adapter = extras["adapter"]
    roles = extras.get("rank_roles") or RankRoles(settings)
    note = ""
    if make:
        channel_id, created, problems = await ensure_level_up_channel(
            adapter, gid, near)
        if channel_id is None:
            return (f"I couldn't make #{LEVEL_UP_CHANNEL}: {problems[0]}. I "
                    "need Manage Channels. Ranking is still off; name an "
                    "existing channel with “enable ranking in #channel”.")
        if not created:
            note += f" #{LEVEL_UP_CHANNEL} was already here, so I used it."
        elif problems:
            note += f" (#{LEVEL_UP_CHANNEL} is made, but I {problems[0]}.)"
    settings.set_ranking(gid, True)
    settings.set_ranking_channel(gid, channel_id)
    tiers = roles.tiers(gid)
    if tiers:
        created_roles, problems = await adapter.ensure_rank_roles(gid, tiers)
        if problems:
            note += (f" I could only set up {len(created_roles)} of "
                     f"{len(tiers)} rank roles: {problems[0]}. I probably "
                     "need Manage Roles, and my own role has to sit above "
                     "the ones I create.")
        else:
            note += f" {len(created_roles)} rank roles are ready."
    else:
        note += (" No rank roles: say “make level 10 give the role X” to "
                 "add one.")
    logger.info("ranking on in %s: level-ups in %s, %d rank role(s)", gid,
                channel_id, len(tiers))
    return (f"Ranking is on. I'll count XP from messages and post level-ups "
            f"in <#{channel_id}>.{note} Level 1 starts at 100 XP.")


async def _set_slowmode(http: Any, channel_id: str, seconds: int) -> bool:
    """``rate_limit_per_user`` on a channel. Through the client's own
    ``request`` first (a plain PATCH, nothing for the SDK to drop), then
    its ``modify_channel`` binding."""
    request = getattr(http, "request", None)
    route = getattr(http, "_route", None)
    if request is not None and route is not None:
        try:
            await request(route("PATCH", "/channels/{channel_id}",
                                channel_id=channel_id),
                          json={"rate_limit_per_user": int(seconds)})
            return True
        except Exception:
            logger.info("slowmode PATCH refused for %s; trying the binding",
                        channel_id, exc_info=True)
    try:
        await http.modify_channel(channel_id, rate_limit_per_user=int(seconds))
        return True
    except Exception:
        logger.warning("ranking: couldn't set slowmode on %s", channel_id,
                       exc_info=True)
        return False


async def ensure_level_up_channel(adapter: Any, guild_id: str,
                                  near: Optional[str]
                                  ) -> tuple[Optional[str], bool, list[str]]:
    """Create or adopt #level-ups: ``(id, created, problems)``. An
    existing one is used as it is. A new one goes in the same category
    as the channel the setup was asked in; everyone may post (30-second
    slowmode) and react."""
    http = adapter._rest()
    try:
        channels = list(await http.get_guild_channels(guild_id) or [])
    except Exception as exc:
        return None, False, [f"couldn't list the channels ({exc})"]
    for raw in channels:
        if str(raw.get("name") or "").lower() == LEVEL_UP_CHANNEL \
                and int(raw.get("type") or 0) == 0:
            return str(raw["id"]), False, []
    parent = next((str(c["parent_id"]) for c in channels
                   if str(c.get("id")) == str(near) and c.get("parent_id")),
                  None)
    try:
        made = await http.create_guild_channel(
            guild_id, name=LEVEL_UP_CHANNEL, type=0, topic=LEVEL_UP_TOPIC,
            parent_id=parent)
        channel_id = str(made.get("id"))
    except Exception as exc:
        return None, False, [str(exc)]
    problems: list[str] = []
    # Owner, 2026-11-18: members post here too (congratulations), with a
    # 30-second slowmode, and can react with any emoji.
    try:
        await http.edit_channel_permissions(
            channel_id, guild_id, allow=str(int(MEMBER_ALLOW)), deny="0",
            type=0)
    except Exception as exc:
        problems.append(f"couldn't open it to everyone ({exc})")
    if not await _set_slowmode(http, channel_id, LEVEL_UP_SLOWMODE):
        problems.append(f"couldn't set the {LEVEL_UP_SLOWMODE}-second "
                        "slowmode; set it in the channel's settings")
    # His own access. Live 2026-11-18 he made the channel and then
    # couldn't post in it: the member overwrite alone didn't take, and
    # its failure only reached an INFO line. Now the member overwrite
    # AND one on each of his roles (role overwrites are what the
    # @everyone deny above proves work), and a reply that says so if
    # neither went through.
    from ..core.channel_access import grant_self
    from ..core.reaction_roles import BOT_ALLOW

    # Manage Messages: what lets a bot past slowmode on Discord, so two
    # level-ups in 30 s both post. Unverified on Fluxer.
    granted = await grant_self(adapter, http, guild_id, channel_id,
                               BOT_ALLOW | Perm.MANAGE_MESSAGES)
    if not granted:
        problems.append("couldn't give myself permission to post in it; "
                        "give my role Send Messages there")
    logger.info("ranking: made #%s (%s) in %s", LEVEL_UP_CHANNEL,
                channel_id, guild_id)
    return channel_id, True, problems


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
