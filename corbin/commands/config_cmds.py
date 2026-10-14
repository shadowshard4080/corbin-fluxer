"""Configuration commands.

These are the first commands with persistent state, so they take the settings
store from ``ctx.extras`` rather than owning a global -- that keeps them
testable and keeps the store's lifetime owned by the bot.
"""

from __future__ import annotations

import re
import time
from typing import Mapping, Optional

from ..identity import NAME
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command


@command(
    name="reply_channel",
    description=f"Toggle whether {NAME} answers every message in this channel",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("response_channel", "aichannel"),
    examples=(
        "set this as your response channel",
        "corbin, always reply in here",
        "stop answering everything in this channel",
    ),
)
async def reply_channel(ctx: CommandContext) -> str:
    settings = ctx.extras["settings"]
    now_on = settings.toggle_ai_channel(ctx.guild_id, ctx.channel.id)
    if now_on:
        return ("Got it — I'll answer everything in here now, no mention "
                "needed. Say the same thing again to turn it back off.")
    return "Back to mention-only in here."


@command(
    name="silence",
    description=f"Stop {NAME} responding in this channel at all",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("shush", "quiet"),
    examples=("be quiet in this channel", "stop responding here"),
)
async def silence(ctx: CommandContext) -> str:
    settings = ctx.extras["settings"]
    now_silent = settings.toggle_silence(ctx.guild_id, ctx.channel.id)
    return ("Silenced here. I won't respond in this channel until you undo it."
            if now_silent else "Unsilenced — I'll answer in here again.")


#: How long "which channel is your general chat?" stays answerable.
GENERAL_QUESTION_TTL = 300.0

_HERE = re.compile(r"^\s*(here|this( one| channel)?|this is it|in here)\s*[.!]*\s*$",
                   re.IGNORECASE)


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    """Turn a channel id or written name into an id, or None."""
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


def _ask_for_general(ctx: CommandContext) -> None:
    """Let the asker's next message in this channel answer the question.

    The classifier sees one message at a time, so a bare "#lounge" sent
    back would never reach a command. ``bot.py`` checks this map before
    dispatch; see :func:`match_general_answer`.
    """
    waiting = ctx.extras.get("general_questions")
    if waiting is not None:
        waiting[(str(ctx.channel.id), str(ctx.invoker.id))] = (
            str(ctx.guild_id), time.monotonic() + GENERAL_QUESTION_TTL)


def match_general_answer(text: str, mentioned: Mapping[str, str],
                         index: Mapping[str, str],
                         here: str) -> Optional[str]:
    """The channel id a reply to "which channel?" names, or None.

    ``mentioned`` is ``{id: name}`` for channels the message links,
    ``index`` is the guild's ``{name: id}``. Only a clear answer counts:
    one linked channel, "here", or a message that is little more than a
    channel name. Anything else falls through to normal handling, so an
    unrelated message is never eaten by the question.
    """
    if len(mentioned) == 1:
        return next(iter(mentioned))
    if mentioned:
        return None
    if _HERE.match(text or ""):
        return here
    whole = (text or "").strip().strip(".!").lstrip("#").lower()
    if whole in index:
        return index[whole]
    words = re.sub(r"[^\w\s'-]", " ", (text or "").lower()).split()
    filler = {"it's", "its", "it", "is", "the", "our", "that", "one",
              "channel", "chat", "general", "main", "corbin", "in", "there"}
    hits = {index[w] for w in words if w in index}
    if len(hits) == 1 and all(w in index or w in filler for w in words):
        return hits.pop()
    return None


@command(
    name="chat_mode",
    description=f"Set how {NAME} decides when to speak server-wide",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    params=(
        Param("mode", "mention, global, or off", choices=("mention", "global", "off")),
        Param("channel", "the general chat, if named", kind="channel",
              required=False),
    ),
    examples=("set your mode to global", "only respond when mentioned"),
)
async def chat_mode(ctx: CommandContext, mode: str,
                         channel: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    settings.set_mode(ctx.guild_id, mode)
    if mode != "global":
        return {
            "mention": "Mention-only now — ping me, reply to me, or use an AI channel.",
            "off": "Off. I'll stay quiet server-wide until you switch me back on.",
        }[mode]

    base = "Global mode. I'll chime in when something looks worth answering."
    target = _resolve_channel(ctx, channel)
    if target:
        settings.set_general_channel(ctx.guild_id, target)
        return (f"{base} When I've got something on my mind I'll bring it "
                f"up in <#{target}>.")
    current = settings.general_channel(ctx.guild_id)
    if current:
        return (f"{base} Anything I bring up on my own still goes in "
                f"<#{current}>.")
    _ask_for_general(ctx)
    return (f"{base} Which channel is your general chat? That's where I'll "
            "bring stuff up on my own.")


@command(
    name="general_channel",
    description=f"Set the general chat, where {NAME} posts his own thoughts",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("musings_channel",),
    params=(
        Param("channel", "the general chat channel", kind="channel"),
    ),
    examples=("#lounge is the general channel",
              "post your random thoughts in #general"),
)
async def general_channel(ctx: CommandContext, channel: str) -> str:
    settings = ctx.extras["settings"]
    target = _resolve_channel(ctx, channel)
    if not target:
        return f"I couldn't find a channel called {channel}."
    settings.set_general_channel(ctx.guild_id, target)
    if settings.mode(ctx.guild_id) != "global":
        return (f"Noted, <#{target}> is the general chat. I only bring "
                "things up on my own in global mode, though.")
    return f"Got it, <#{target}> it is."


@command(
    name="chat_status",
    description=f"Show {NAME}'s current configuration for this server",
    risk=Risk.SAFE,
    aliases=("status",),
    examples=("what's your config", "show your settings"),
)
async def chat_status(ctx: CommandContext) -> Embed:
    settings = ctx.extras["settings"]
    gid = ctx.guild_id
    ai = sorted(settings.ai_channels(gid)) if gid else []
    silenced = sorted(settings.silenced(gid)) if gid else []

    embed = Embed(title=f"{NAME} — configuration",
                  description=(ctx.guild.name if ctx.guild else "Direct messages"))
    embed.add_field("Mode", settings.mode(gid), inline=True)
    general = settings.general_channel(gid) if gid else None
    embed.add_field("General chat", f"<#{general}>" if general else "not set",
                    inline=True)
    embed.add_field("This channel",
                    "silenced" if settings.is_silenced(gid, ctx.channel.id)
                    else ("always-on" if settings.is_ai_channel(gid, ctx.channel.id)
                          else "mention-only"),
                    inline=True)
    embed.add_field(
        "Always-on channels",
        "\n".join(f"<#{c}>" for c in ai[:15]) + (
            f"\n…and {len(ai) - 15} more" if len(ai) > 15 else "")
        if ai else "none",
    )
    if silenced:
        embed.add_field(
            "Silenced",
            "\n".join(f"<#{c}>" for c in silenced[:15]) + (
                f"\n…and {len(silenced) - 15} more" if len(silenced) > 15 else ""))
    return embed
