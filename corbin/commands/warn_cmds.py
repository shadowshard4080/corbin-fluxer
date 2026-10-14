"""Warnings, escalation, and appeals.

The invoker-hierarchy gate comes free: every command here declares a
``user`` param, so ``core.risk`` runs its hard gates (no self-target, no
bots, no server owner, nobody ranked at or above you) before the handler is
reached. Fluxer, like Discord, only enforces hierarchy for the *bot*.

Failures are reported. The Discord build wrapped the whole action block in
one ``except Exception`` and replied "Added warning..." regardless, so a ban
blocked by role hierarchy read as success.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..core.warnings import clamp_timeout, describe_duration
from ._notify import delivery_note, notify_target

ACTIONS = ("none", "timeout", "kick", "ban")


async def _modlog(ctx: CommandContext, title: str, body: str) -> None:
    """Mirror a moderation action into the audit channel, if one is set."""
    settings = ctx.extras.get("settings")
    adapter = ctx.extras.get("adapter")
    if not settings or not adapter or not ctx.guild_id:
        return
    channel = settings.audit_channel(ctx.guild_id)
    if not channel or not settings.audit(ctx.guild_id).get("enabled"):
        return
    try:
        await adapter.send(channel, Response(
            embed=Embed(title=title, description=body, colour=0xE8A33D)))
    except Exception:
        pass


@command(
    name="warn",
    description="Warn a member, optionally timing them out, kicking or banning",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.MODERATE_MEMBERS,
    params=(
        Param("user", "who to warn", kind="user"),
        Param("reason", "why", max_length=900),
        Param("action", "none, timeout, kick or ban", required=False,
              default="none", choices=ACTIONS),
        Param("timeout_minutes", "how long, if timing out", kind="integer",
              required=False, default=10, min_value=1, max_value=40320),
    ),
    confirm_phrase=lambda a: (
        f"warn {a.get('user')}"
        + (f" and {a.get('action')} them" if a.get("action") not in
           (None, "none") else "")
        + (f" for “{a.get('reason')}”" if a.get("reason") else "")),
    examples=("warn @spammer for posting scam links",
              "warn @noisy for spam and time them out for an hour"),
)
async def warn(ctx: CommandContext, user: str, reason: str,
               action: str = "none", timeout_minutes: int = 10) -> Any:
    store = ctx.extras["warnings"]
    adapter = ctx.extras["adapter"]

    entry = store.add(ctx.guild_id, user, moderator_id=ctx.invoker.id,
                      reason=reason,
                      action=action if action != "none" else None)

    server = ctx.guild.name if ctx.guild and ctx.guild.name else "the server"
    duration = (describe_duration(clamp_timeout(timeout_minutes))
                if action == "timeout" else None)

    # Tell them BEFORE any removal: a kick or ban leaves no mutual guild,
    # so the DM would bounce.
    told_as = {"kick": "kicked", "ban": "banned",
               "timeout": "timed out"}.get(action, "warned")
    delivered = await notify_target(
        adapter, user, action=told_as, server_name=server, reason=reason,
        moderator=ctx.invoker.display_name, warn_id=entry.id,
        duration=duration)

    failure = None
    if action != "none":
        try:
            if action == "timeout":
                minutes = clamp_timeout(timeout_minutes)  # noqa: F841
                until = ctx.extras.get("now_utc")
                from datetime import datetime, timezone

                until = (until or datetime.now(timezone.utc)) + \
                    timedelta(minutes=minutes)
                await adapter.timeout(ctx.guild_id, user, until)
            elif action == "kick":
                await adapter.kick(ctx.guild_id, user, reason=reason)
            elif action == "ban":
                await adapter.ban(ctx.guild_id, user, reason=reason)
        except Exception as exc:
            # Roll the warning back only if nothing landed? No: the warning
            # is still a true record of the moderator's decision. Report the
            # failure instead of pretending it worked.
            failure = f"{type(exc).__name__}: {exc}"

    total = store.count(ctx.guild_id, user)
    embed = Embed(title="Warning recorded", colour=0xE8A33D)
    embed.add_field("Member", f"<@{user}>", inline=True)
    embed.add_field("Warning ID", f"`{entry.id}`", inline=True)
    embed.add_field("Total warnings", str(total), inline=True)
    embed.add_field("Reason", reason)
    if action != "none":
        detail = action
        if action == "timeout":
            detail += f" ({describe_duration(clamp_timeout(timeout_minutes))})"
        embed.add_field("Action", detail, inline=True)
    if failure:
        embed.add_field("⚠️ Action failed", failure)
    embed.add_field("Notified", delivery_note(delivered, told_as),
                    inline=False)
    embed.footer = f"They can appeal with: appeal {entry.id}"

    await _modlog(ctx, "Member warned",
                  f"<@{user}> warned by <@{ctx.invoker.id}>\n"
                  f"**Reason:** {reason}\n**ID:** `{entry.id}`")
    return embed


@command(
    name="warnings",
    description="List a member's warnings",
    risk=Risk.SAFE,
    permission=Perm.MODERATE_MEMBERS,
    aliases=("list_warnings",),
    params=(Param("user", "whose warnings", kind="user"),),
    examples=("show warnings for @someone", "how many warnings does @x have"),
)
async def warnings_list(ctx: CommandContext, user: str) -> Any:
    store = ctx.extras["warnings"]
    entries = store.for_user(ctx.guild_id, user)
    if not entries:
        return f"<@{user}> has no warnings."

    embed = Embed(title=f"Warnings for {user}",
                  description=f"<@{user}> — {len(entries)} total")
    lines = []
    for w in entries[-20:]:
        when = w.timestamp.strftime("%d %b %Y") if w.timestamp else "unknown"
        by = f" by <@{w.moderator_id}>" if w.moderator_id else ""
        act = f" [{w.action}]" if w.action else ""
        lines.append(f"`{w.id}` — {when}{by}{act}\n{w.reason}")
    embed.add_list_field("Entries", lines)
    return embed


@command(
    name="unwarn",
    description="Delete a warning by its ID",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.MODERATE_MEMBERS,
    aliases=("delete_warning", "remove_warning"),
    params=(Param("warn_id", "the warning id"),),
    confirm_phrase=lambda a: f"delete warning {a.get('warn_id')}",
    examples=("delete warning 4e770a8f",),
)
async def unwarn(ctx: CommandContext, warn_id: str) -> str:
    store = ctx.extras["warnings"]
    found, _owner = store.find(ctx.guild_id, warn_id)
    if not found:
        return f"No warning here with the id `{warn_id}`."
    store.remove(ctx.guild_id, warn_id)
    await _modlog(ctx, "Warning deleted",
                  f"`{warn_id}` removed by <@{ctx.invoker.id}>")
    return f"Deleted warning `{warn_id}`."


@command(
    name="appeal",
    description="Appeal a warning against you",
    risk=Risk.SAFE,
    guild_only=True,
    params=(
        Param("warn_id", "the warning id from your warning message"),
        Param("statement", "why you're appealing", required=False,
              max_length=900),
    ),
    examples=("appeal 4e770a8f", "appeal 4e770a8f, it wasn't me"),
)
async def appeal(ctx: CommandContext, warn_id: str,
                 statement: Optional[str] = None) -> Any:
    store = ctx.extras["warnings"]
    settings = ctx.extras["settings"]
    adapter = ctx.extras["adapter"]

    found, owner = store.find(ctx.guild_id, warn_id)
    if not found:
        return f"I can't find a warning with the id `{warn_id}`."
    if owner != ctx.invoker.id:
        return "You can only appeal your own warnings."

    allowed, next_at = found.can_appeal()
    if not allowed:
        return (f"You've already appealed that one. You can appeal again "
                f"after {next_at:%d %b %Y}.")

    embed = Embed(title="Warning appeal", colour=0x5BC0DE)
    embed.add_field("From", f"<@{ctx.invoker.id}>", inline=True)
    embed.add_field("Warning", f"`{warn_id}`", inline=True)
    embed.add_field("Original reason", found.reason)
    if statement:
        embed.add_field("Their statement", statement)

    # Deliver FIRST. The cooldown is spent on delivery, never on the
    # attempt: stamping it up front once told users "sent to the moderators"
    # while notifying nobody, then locked them out for a week.
    delivered = False
    channel = (settings.audit_channel(ctx.guild_id)
               if settings.audit(ctx.guild_id).get("enabled") else None)
    if channel:
        try:
            await adapter.send(channel, Response(embed=embed))
            delivered = True
        except Exception:
            delivered = False
    if not delivered and ctx.guild and ctx.guild.owner_id:
        try:
            delivered = bool(await adapter.send_dm(ctx.guild.owner_id,
                                                   Response(embed=embed)))
        except Exception:
            delivered = False

    if not delivered:
        return ("I couldn't reach any moderators. Nothing was recorded, so "
                "your appeal is still available once a mod channel is set up.")

    store.stamp_appeal(ctx.guild_id, warn_id)
    return Response(
        content="Appeal sent to the moderators. They'll take it from here.",
        private=True,
        private_notice="Sent your appeal to the moderators.")
