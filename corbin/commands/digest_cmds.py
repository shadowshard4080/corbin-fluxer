"""On-demand usage digest."""

from __future__ import annotations

from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.digest import Counters
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..identity import NAME

SECTIONS = ("overview", "users", "servers")


def voice_time(seconds: float) -> str:
    """ "0m", "45m", "3h 05m" -- minutes are the useful grain for a day."""
    minutes = int(round(max(0.0, seconds) / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _ranked(embed: Embed, name: str, rows, label) -> None:
    if rows:
        embed.add_list_field(name, [
            f"`{i:>2}.` {label(key, e)} — {int(e.get('count', 0))}"
            for i, (key, e) in enumerate(rows, 1)])


def render(counters: Counters, section: str = "overview", *,
           title: str = f"{NAME} — usage") -> Embed:
    """One embed per section. Paging was buttons on Discord; here it is a
    parameter, so nothing is unreachable without components. The overview
    alone carries everything the daily post needs."""
    embed = Embed(title=title, colour=0x5865F2)

    if section in ("users", "servers"):
        rows = (counters.top_users() if section == "users"
                else counters.top_guilds())
        if not rows:
            embed.description = "No activity recorded yet."
            return embed
        if section == "users":
            _ranked(embed, "Most active people", rows,
                    lambda uid, e: f"<@{uid}>")
        else:
            _ranked(embed, "Most active servers", rows,
                    lambda gid, e: e.get("name") or gid)
        return embed

    # One embed: the totals, then the full rankings. The scheduled post
    # used to follow this with a second "Most active people" embed that
    # repeated the top three.
    embed.add_field("Messages sent", str(counters.messages_sent), inline=True)
    embed.add_field("Commands run", str(counters.commands_run), inline=True)
    embed.add_field("Time in voice", voice_time(counters.voice_seconds),
                    inline=True)
    embed.add_field("People", str(len(counters.users)), inline=True)
    embed.add_field("Servers", str(len(counters.guilds)), inline=True)
    _ranked(embed, "Most active people", counters.top_users(),
            lambda uid, e: f"<@{uid}>")
    _ranked(embed, "Most active servers", counters.top_guilds(),
            lambda gid, e: e.get("name") or gid)
    if not counters.total:
        embed.description = "Nothing recorded in this window yet."
    return embed


@command(
    name="digest",
    description=f"Show {NAME}'s usage summary for the current window",
    risk=Risk.SAFE,
    permission=Perm.MANAGE_GUILD,
    aliases=("usage", "stats"),
    params=(Param("section", "overview, users or servers", required=False,
                  default="overview", choices=SECTIONS),),
    examples=("show the usage digest", "digest users",
              "which servers are most active"),
)
async def digest(ctx: CommandContext, section: str = "overview") -> Any:
    store = ctx.extras.get("digest")
    if store is None:
        return "Usage tracking isn't running."
    # Reads the live window; unlike the scheduled post this never resets it.
    return render(store.counters, section,
                  title=f"{NAME} — usage (current window)")
