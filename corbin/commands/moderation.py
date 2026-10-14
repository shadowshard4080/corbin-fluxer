"""Moderation commands.

Handlers assume authorization has already happened -- ``core.risk`` runs
every hard gate before a handler is reached, on every surface. A handler
must never re-derive permission from model output.
"""

from __future__ import annotations

import logging

from ..identity import NAME
from ..core.context import CommandContext
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ._notify import delivery_note, notify_target

logger = logging.getLogger("corbin.moderation")


@command(
    name="ban",
    description="Ban a member from the server",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.BAN_MEMBERS,
    aliases=("banish",),
    params=(
        Param("user", "the member to ban", kind="user"),
        Param("reason", "why they are being banned", required=False,
              max_length=400),
        Param("delete_days", "days of their messages to delete",
              kind="integer", required=False, default=0,
              min_value=0, max_value=7),
        Param("duration", "how long, e.g. 2h or 7d; permanent if omitted",
              required=False),
    ),
    examples=(
        "ban @spammer for raiding",
        "ban @troll for 7 days for posting scam links",
        "get rid of @troll, they're posting scam links",
    ),
)
async def ban(ctx: CommandContext, user: str, reason: str | None = None,
              delete_days: int = 0, duration: str | None = None) -> str:
    from ..core.bans import humanise, parse_duration

    adapter = ctx.extras["adapter"]
    server = ctx.guild.name if ctx.guild and ctx.guild.name else "the server"

    span = parse_duration(duration) if duration else None
    if duration and span is None:
        return (f"I couldn't read “{duration}” as a length. Try "
                "something like 30m, 2h or 7d.")

    # DM FIRST. A ban removes the mutual guild, after which the user is
    # unreachable and would never learn why.
    delivered = await notify_target(
        adapter, user, action="banned", server_name=server, reason=reason,
        moderator=ctx.invoker.display_name,
        duration=humanise(span) if span else None)

    await adapter.ban(ctx.guild_id, user, reason=reason,
                      delete_message_seconds=int(delete_days or 0) * 86400)

    length = ""
    bans = ctx.extras.get("bans")
    if span and bans is not None:
        # Persisted, so the unban still happens across a restart.
        bans.add(ctx.guild_id, user, span, reason=reason,
                 banner_id=ctx.invoker.id, channel_id=ctx.channel.id)
        length = f" for {humanise(span)}"

    tail = f" Reason: {reason}." if reason else "."
    return (f"Banned <@{user}>{length}{tail} "
            f"{delivery_note(delivered, 'banned')}")


@command(
    name="kick",
    description="Kick a member from the server",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.KICK_MEMBERS,
    params=(
        Param("user", "the member to kick", kind="user"),
        Param("reason", "why", required=False, max_length=400),
    ),
    examples=("kick @noisy for spamming",),
)
async def kick(ctx: CommandContext, user: str, reason: str | None = None) -> str:
    adapter = ctx.extras["adapter"]
    server = ctx.guild.name if ctx.guild and ctx.guild.name else "the server"

    # Same ordering rule as ban.
    delivered = await notify_target(
        adapter, user, action="kicked", server_name=server, reason=reason,
        moderator=ctx.invoker.display_name)

    await adapter.kick(ctx.guild_id, user, reason=reason)
    tail = f" Reason: {reason}." if reason else "."
    return f"Kicked <@{user}>{tail} {delivery_note(delivered, 'kicked')}"


@command(
    name="unban",
    description="Lift a ban",
    risk=Risk.WRITE,  # reversing a ban is a restoration, not a destruction
    permission=Perm.BAN_MEMBERS,
    params=(Param("user", "the user id to unban", kind="user"),),
    examples=("unban 12345",),
)
async def unban(ctx: CommandContext, user: str) -> str:
    adapter = ctx.extras["adapter"]
    try:
        await adapter.unban(ctx.guild_id, user)
    except Exception as exc:
        return f"I couldn't unban them: {type(exc).__name__}: {exc}"
    bans = ctx.extras.get("bans")
    if bans is not None:
        bans.remove(ctx.guild_id, user)
    return f"Unbanned <@{user}>."


@command(
    name="assignrole",
    description="Give a role to a member",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_ROLES,
    params=(
        Param("user", "the member", kind="user"),
        Param("role", "the role to give", kind="role"),
    ),
    examples=("give @sam the Artist role",),
)
async def assignrole(ctx: CommandContext, user: str, role: str) -> str:
    adapter = ctx.extras["adapter"]

    # "assign <role id> to <user id>" is two bare snowflakes in the
    # order role-then-user, while the parameters are declared
    # user-then-role, and the classifier fills them the way they are
    # written. Rather than hope it guesses, check against the guild's
    # actual roles: if what we were handed as the user is a real role
    # here and what we were handed as the role is not, they are the
    # wrong way round.
    if ctx.guild_id and str(user).isdigit() and str(role).isdigit():
        try:
            known = await adapter.role_index(ctx.guild_id)
        except Exception:
            known = {}
        if known and str(user) in known and str(role) not in known:
            user, role = role, user
            logger.info("assignrole: arguments were swapped; corrected "
                        "against the guild's role list")

    # The executor resolves a written role name to an id before we get
    # here; anything still non-numeric did not match a real role.
    if not str(role).isdigit():
        return (f"There's no role called “{role}” here. Check the "
                "spelling, or give me the role id.")
    try:
        await adapter.add_role(ctx.guild_id, user, role)
    except Exception as exc:
        return (f"I couldn't give them that role: {type(exc).__name__}: "
                f"{exc}. I need Manage Roles, and my own role has to sit "
                "above it.")
    return f"Gave <@{user}> the <@&{role}> role."


@command(
    name="clear",
    description="Bulk-delete recent messages in this channel",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.MANAGE_MESSAGES,
    aliases=("purge",),
    params=(
        Param("count", "how many messages", kind="integer",
              min_value=1, max_value=500),
    ),
    # Blast radius is the count itself, so a big purge always confirms.
    blast_radius=lambda args: int(args.get("count") or 1),
    examples=("clear the last 20 messages",),
)
async def clear(ctx: CommandContext, count: int) -> str:
    adapter = ctx.extras["adapter"]
    deleted = await adapter.purge(ctx.channel.id, count)
    return f"Cleared {deleted} messages."


@command(
    name="listbans",
    description="List timed bans and how long is left on each",
    risk=Risk.SAFE,
    permission=Perm.BAN_MEMBERS,
    aliases=("bans", "timed_bans"),
    examples=("list the bans", "who's banned", "show timed bans"),
)
async def listbans(ctx: CommandContext):
    from ..core.bans import humanise
    from ..core.context import Embed

    bans = ctx.extras.get("bans")
    if bans is None or ctx.guild_id is None:
        return "Ban tracking isn't available here."
    entries = sorted(bans.for_guild(ctx.guild_id), key=lambda b: b.unban_at)
    if not entries:
        return ("No timed bans here. Permanent bans aren't tracked by me, "
                "they're just in the server's ban list.")

    embed = Embed(title=f"{len(entries)} timed ban(s)", colour=0xD9534F)
    embed.add_list_field("Active", [
        f"<@{b.user_id}> — {humanise(b.remaining)} left"
        + (f"\n{b.reason}" if b.reason else "") for b in entries[:20]])
    return embed


@command(
    name="chatban",
    description=f"Stop {NAME} conversing with someone (they keep commands)",
    risk=Risk.DESTRUCTIVE,
    owner_only=True,
    aliases=("mute_corbin", "ignore_user"),
    params=(Param("user", "who", kind="user"),),
    confirm_phrase=lambda a: f"stop talking to {a.get('user')}",
    examples=("stop talking to @someone", "chatban @someone"),
)
async def chatban(ctx: CommandContext, user: str) -> str:
    chatbans = ctx.extras.get("chatbans")
    if chatbans is None:
        return "Chatban tracking isn't available."
    now_banned = chatbans.toggle(user)
    if now_banned:
        return (f"I'll stop replying to <@{user}> in chat. Commands still "
                "work for them.")
    return f"I'll talk to <@{user}> again."
