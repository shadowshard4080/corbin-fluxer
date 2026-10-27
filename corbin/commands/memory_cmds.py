"""Memory commands: what the bot remembers, and forgetting it."""

from __future__ import annotations

from typing import Any

from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..identity import NAME


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
    embed.footer = ("Say “forget me” to delete all of it, or “don't "
                    "remember me” to stop me saving anything.")
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
    _drop_history(ctx)
    if store.forget_user(ctx.invoker.id):
        return Response(content="Done. Wiped what I had about you.",
                        private=True,
                        private_notice="Wiped it. Details in your DMs.")
    return Response(content="There was nothing stored about you to begin "
                            "with.", private=True)


def _drop_history(ctx: CommandContext) -> None:
    """The recent back-and-forth he holds in memory goes too: "forget me"
    used to leave it, so he could still quote you until a restart."""
    conversation = ctx.extras.get("conversation")
    if conversation is not None:
        conversation.clear_user(str(ctx.invoker.id))


@command(
    name="memory_optout",
    description=f"Stop or let {NAME} remember things about you, everywhere. "
                "Opting out deletes what he has",
    # Opting out deletes the file, so it carries forget-me's risk: it
    # confirms unless the phrasing is unambiguous.
    risk=Risk.DESTRUCTIVE,
    guild_only=False,
    aliases=("dont_remember_me", "memory_opt_out", "memory_optin",
             "stop_remembering_me"),
    params=(Param("out", "true: don't remember me", kind="boolean",
                  required=False, default=True),),
    confirm_phrase=lambda a: (
        "stop remembering you and delete what I have"
        if a.get("out", True) not in (False, "false", "no") else
        "start remembering you again"),
    examples=("don't remember me", "stop remembering things about me",
              "you can remember me again"),
)
async def memory_optout(ctx: CommandContext, out: bool = True) -> Any:
    optout = ctx.extras.get("memory_optout")
    if optout is None:
        return "Memory opt-out isn't set up on this bot."
    if not out:
        optout.set(ctx.invoker.id, False)
        return Response(content="Okay, I'll remember things you tell me "
                                "again.", private=True,
                        private_notice="Done. Details in your DMs.")
    optout.set(ctx.invoker.id, True)
    ctx.extras["memory"].forget_user(ctx.invoker.id)
    _drop_history(ctx)
    return Response(
        content=("Done. I've deleted what I had about you and I won't save "
                 "anything new, in any server or DM. I'll still answer you, "
                 "I just won't remember. Say “you can remember me again” "
                 "to undo it."),
        private=True, private_notice="Done. Details in your DMs.")


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
