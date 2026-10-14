"""On-demand usage digest."""

from __future__ import annotations

from typing import Any, Optional

from ..identity import NAME
from ..core.context import CommandContext, Embed
from ..core.digest import Counters
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

SECTIONS = ("overview", "users", "servers")


def render(counters: Counters, section: str = "overview", *,
           title: str = f"{NAME} — usage") -> Embed:
    """One embed per section. Paging was buttons on Discord; here it is a
    parameter, so nothing is unreachable without components."""
    embed = Embed(title=title, colour=0x5865F2)

    if section == "users":
        rows = counters.top_users()
        if not rows:
            embed.description = "No activity recorded yet."
            return embed
        embed.add_list_field("Most active people", [
            f"`{i:>2}.` <@{uid}> — {int(e.get('count', 0))}"
            for i, (uid, e) in enumerate(rows, 1)])
        return embed

    if section == "servers":
        rows = counters.top_guilds()
        if not rows:
            embed.description = "No activity recorded yet."
            return embed
        embed.add_list_field("Most active servers", [
            f"`{i:>2}.` {e.get('name') or gid} — {int(e.get('count', 0))}"
            for i, (gid, e) in enumerate(rows, 1)])
        return embed

    embed.add_field("Messages sent", str(counters.messages_sent), inline=True)
    embed.add_field("Commands run", str(counters.commands_run), inline=True)
    embed.add_field("People", str(len(counters.users)), inline=True)
    embed.add_field("Servers", str(len(counters.guilds)), inline=True)
    top_u = counters.top_users(3)
    if top_u:
        embed.add_field("Top people", "\n".join(
            f"<@{uid}> — {int(e.get('count', 0))}" for uid, e in top_u))
    top_g = counters.top_guilds(3)
    if top_g:
        embed.add_field("Top servers", "\n".join(
            f"{e.get('name') or gid} — {int(e.get('count', 0))}"
            for gid, e in top_g))
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
