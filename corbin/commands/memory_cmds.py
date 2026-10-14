"""Memory commands: what the bot remembers, and forgetting it."""

from __future__ import annotations

from typing import Any

from ..identity import NAME
from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command


@command(
    name="my_memory",
    description=f"Show what {NAME} remembers about you",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("memory", "what_do_you_remember"),
    examples=("what do you remember about me", "show my memory"),
)
async def my_memory(ctx: CommandContext) -> Any:
    """Always answered privately.

    These are personal facts, and on Discord this was an ephemeral reply.
    Fluxer has no ephemeral messages, so ``private=True`` degrades to a DM,
    then to post-and-delete for anyone with DMs closed. Printing somebody's
    accumulated facts into a public channel is the one outcome to avoid.
    """
    store = ctx.extras["memory"]
    entry = store.get_user(ctx.invoker.id)
    if not entry.facts and not entry.preferences:
        return Response(
            content=("Nothing yet. I pick things up as we talk, and I only "
                     "keep what you say about yourself."),
            private=True)

    embed = Embed(title=f"What I remember about {ctx.invoker.display_name}")
    if entry.facts:
        # 21 facts is ~1700 chars, well past the 1024 field limit, which
        # rejects the whole message. Split rather than truncate.
        embed.add_list_field("Facts", [f"• {f}" for f in entry.facts[-25:]])
    if entry.timezone:
        embed.add_field("Timezone", entry.timezone, inline=True)
    if entry.interaction_count:
        embed.add_field("Conversations", str(entry.interaction_count),
                        inline=True)
    embed.footer = "Say “forget me” to delete all of it."
    return Response(embed=embed, private=True,
                    private_notice="Sent what I've got to your DMs.")


@command(
    name="forget_me",
    description=f"Delete everything {NAME} remembers about you",
    # Destructive: the data is unrecoverable, so this confirms unless the
    # phrasing is unambiguous.
    risk=Risk.DESTRUCTIVE,
    guild_only=False,
    aliases=("forget_about_me",),
    confirm_phrase=lambda a: "delete everything I remember about you",
    examples=("forget me", "delete what you know about me"),
)
async def forget_me(ctx: CommandContext) -> Any:
    store = ctx.extras["memory"]
    if store.forget_user(ctx.invoker.id):
        return Response(content="Done. Wiped what I had about you.",
                        private=True,
                        private_notice="Wiped it. Details in your DMs.")
    return Response(content="There was nothing stored about you to begin "
                            "with.", private=True)


@command(
    name="server_memories",
    description=f"Show what {NAME} remembers about this server",
    risk=Risk.SAFE,
    aliases=("server_memory",),
    examples=("what do you remember about this server",),
)
async def server_memories(ctx: CommandContext) -> Any:
    store = ctx.extras["memory"]
    if ctx.guild_id is None:
        return "That only works inside a server."
    entry = store.get_server(ctx.guild_id)
    if not entry.facts:
        return "Nothing about this server yet."
    embed = Embed(
        title=f"What I remember about {ctx.guild.name if ctx.guild else 'here'}")
    embed.add_list_field("Facts", [f"• {f}" for f in entry.facts[-25:]])
    return embed


@command(
    name="forget_server",
    description=f"Delete everything {NAME} remembers about this server",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.MANAGE_GUILD,
    confirm_phrase=lambda a: "delete everything I remember about this server",
    examples=("forget everything about this server",),
)
async def forget_server(ctx: CommandContext) -> str:
    store = ctx.extras["memory"]
    if ctx.guild_id is None:
        return "That only works inside a server."
    if store.forget_server(ctx.guild_id):
        return "Cleared what I had about this server."
    return "There was nothing stored about this server."
