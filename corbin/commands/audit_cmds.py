"""Audit-log configuration.

The Discord build used a 21-toggle component panel. Fluxer has no
components, so this is conversational: "log message deletes and joins in
#mod-log". That reads better than 21 buttons anyway.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.audit import CATEGORIES, normalise_categories
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


@command(
    name="audit_logs",
    description="Turn server event logging on or off and pick a channel",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("serverlog", "modlog", "setup_logs"),
    params=(
        Param("enabled", "on or off", kind="boolean"),
        Param("channel", "where to post log entries", kind="channel",
              required=False),
    ),
    confirm_phrase=lambda a: ("turn server event logging on" if a.get("enabled")
                              else "turn server event logging off"),
    examples=("turn on server logging in #mod-log",
              "start logging server events here",
              "stop logging server events"),
)
async def audit_logs(ctx: CommandContext, enabled: bool,
                     channel: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    if not enabled:
        settings.set_audit(ctx.guild_id, enabled=False)
        return "Server event logging is off."
    target = _resolve_channel(ctx, channel) or ctx.channel.id
    cfg = settings.set_audit(ctx.guild_id, enabled=True, channel_id=target)
    on = [CATEGORIES[k] for k, v in cfg["categories"].items() if v]
    return (f"Logging to <#{target}>. Currently on: {', '.join(on)}. "
            "Say “also log voice activity” or “stop logging edits” to change "
            "what I watch.")


@command(
    name="audit_category",
    description="Turn one kind of server event log on or off",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("log_category",),
    params=(
        Param("category", "which events", choices=tuple(CATEGORIES)),
        Param("enabled", "on or off", kind="boolean"),
    ),
    confirm_phrase=lambda a: (
        f"{'start' if a.get('enabled') else 'stop'} logging "
        f"{CATEGORIES.get(str(a.get('category')), a.get('category'))}"),
    examples=("also log voice activity", "stop logging message edits",
              "log when people leave"),
)
async def audit_category(ctx: CommandContext, category: str,
                         enabled: bool) -> str:
    settings = ctx.extras["settings"]
    try:
        settings.set_audit_category(ctx.guild_id, category, enabled)
    except ValueError:
        return (f"I don't log “{category}”. I can log: "
                f"{', '.join(CATEGORIES)}.")
    label = CATEGORIES[category]
    if not settings.audit(ctx.guild_id).get("enabled"):
        return (f"{label} is {'on' if enabled else 'off'}, but server logging "
                "itself is switched off, so nothing will post yet.")
    return f"{label}: {'on' if enabled else 'off'}."


@command(
    name="audit_status",
    description="Show what server events are being logged",
    risk=Risk.SAFE,
    aliases=("log_status",),
    examples=("what are you logging", "show the log settings"),
)
async def audit_status(ctx: CommandContext) -> Any:
    settings = ctx.extras["settings"]
    if ctx.guild_id is None:
        return "That only works inside a server."
    cfg = settings.audit(ctx.guild_id)
    cats = normalise_categories(cfg.get("categories"))

    embed = Embed(title="Server event logging")
    embed.add_field("Status", "on" if cfg.get("enabled") else "off",
                    inline=True)
    if cfg.get("channel_id"):
        embed.add_field("Channel", f"<#{cfg['channel_id']}>", inline=True)
    embed.add_field(
        "Watching",
        "\n".join(f"{'✅' if v else '⬜'} {CATEGORIES[k]}"
                  for k, v in cats.items()))
    embed.footer = ("Fluxer emits no audit-log events, so I can log what "
                    "happened but not always who did it.")
    return embed
