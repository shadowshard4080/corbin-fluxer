"""Spoken conversation in a voice channel. See :mod:`corbin.core.voicechat`.

Gated per server by the owner's allowlist (``feature="voice"``), except
stopping and opting out, which always work: leaving and privacy must never
depend on a setting.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ..core.context import CommandContext
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from .radio_cmds import _voice_channel
from ..identity import NAME

#: What members are told when VoiceStudio can't be reached.
SPEECH_UNAVAILABLE = ("I can't talk in voice right now: my speech service "
                      "isn't reachable.")

#: The listening notice. Posted in the voice channel's chat on joining
#: (with how to opt out); not spoken any more -- see JOIN_LINES.
LISTENING_NOTICE = ("I'm listening. Say my name if it's for me, or tell me "
                    "\"don't listen to me\" and I won't hear you at all.")

#: Spoken on joining when no fresh greeting arrives in time. He used to
#: say LISTENING_NOTICE every time, word for word in an identical voice,
#: and the owner found it repetitive; the instructions are in the chat
#: message already. Never the same line twice running.
JOIN_LINES = (
    "Evening. Mind the feathers.",
    "Right, I'm here. What did I miss?",
    "*caw* Sorry, habit. Hello.",
    "Found a good perch. Carry on.",
    "Hello, all. I've brought no snacks, as usual.",
    "Landed. Who's talking?",
)
_last_join_line = [""]


def pick_join_line(choice=None) -> str:
    """A fallback greeting, never the previous one."""
    import random

    pool = [line for line in JOIN_LINES if line != _last_join_line[0]]
    line = (choice or random.choice)(pool)
    _last_join_line[0] = line
    return line


#: Said out loud when he's told to leave, before he goes. He used to just
#: vanish: the reply ("Stopped and left the channel.") was generated after
#: the listener had closed, so nothing was ever spoken.
GOODBYE_LINES = (
    "Alright, I'm off. Bye, all.",
    "Heading out. See you around.",
    "Off I fly. Later.",
    "Bye for now.",
    "Right, I'll leave you to it. Take care.",
)
_last_goodbye = [""]
#: A goodbye that can't be said in this long isn't worth holding the
#: leave for.
GOODBYE_TIMEOUT = 10.0


def pick_goodbye(choice=None) -> str:
    """A goodbye line, never the previous one."""
    import random

    pool = [line for line in GOODBYE_LINES if line != _last_goodbye[0]]
    line = (choice or random.choice)(pool)
    _last_goodbye[0] = line
    return line


async def say_goodbye(ctx: CommandContext) -> bool:
    """Say goodbye out loud, if he's talking in voice here. Call before
    leaving: once the listener closes there's no voice to say it with.
    True if a goodbye was attempted."""
    chat = ctx.extras.get("voicechat")
    listener = chat.listener(ctx.guild_id) if chat is not None else None
    if listener is None:
        return False
    try:
        await asyncio.wait_for(listener.say(pick_goodbye()),
                               timeout=GOODBYE_TIMEOUT)
    except Exception:
        pass
    return True


def _channel_name(ctx: CommandContext, channel_id: str) -> str:
    """The channel's name before joining (the session has it only after),
    from the channel index; lower-cased there, which is fine for a
    prompt."""
    for name, cid in (ctx.extras.get("channel_index") or {}).items():
        if str(cid) == str(channel_id):
            return name
    return "the channel"


def _cancel(task: Optional[asyncio.Task]) -> None:
    if task is not None:
        task.cancel()


def _start_greeting(ctx: CommandContext, where: str
                    ) -> Optional[asyncio.Task]:
    """Start writing the hello before joining, so the model call overlaps
    the probe and connect instead of following them (it was most of the
    ~4 s between him appearing and speaking)."""
    greeter = ctx.extras.get("voice_greeter")
    if greeter is None:
        return None
    task = asyncio.create_task(greeter(where))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


async def _greet(listener, pending: Optional[asyncio.Task]) -> None:
    """Say hello on joining: the fresh line if it comes quickly, else a
    stock one. Runs in the background, so joining isn't held up."""
    line = None
    if pending is not None:
        try:
            line = await asyncio.wait_for(pending, timeout=5.0)
        except Exception:
            line = None
    await listener.say(line or pick_join_line())


@command(
    name="voice_chat",
    description="Talk out loud in a voice channel",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="voice",
    aliases=("talk_in_voice", "join_voice_chat", "voicechat"),
    params=(Param("channel", "which voice channel", kind="channel",
                  required=False),),
    examples=("talk with me in voice", "join voice chat in Lounge"),
)
async def voice_chat(ctx: CommandContext,
                     channel: Optional[str] = None) -> str:
    chat = ctx.extras.get("voicechat")
    if chat is None:
        return "Voice chat isn't set up on this bot."
    target = _voice_channel(ctx, channel)
    if not target:
        if channel:
            return (f"I couldn't find a voice channel called “{channel}”. "
                    "Join one and just say “talk with me in voice”.")
        return ("You're not in a voice channel. Join one and ask again, "
                "or tell me which, like “voice chat in Lounge”.")
    if chat.listener(ctx.guild_id) is not None:
        session = ctx.extras["voice"].get(ctx.guild_id)
        if session is not None and session.channel_id == str(target):
            return "I'm already listening in there."
    pending = _start_greeting(ctx, _channel_name(ctx, target))
    try:
        session = await chat.start(ctx.guild_id, target)
    except PermissionError as exc:
        _cancel(pending)
        return f"{exc}."
    except Exception as exc:
        _cancel(pending)
        if not chat.studio.available:
            # Same words whether the PC is off or VoiceStudio is closed;
            # the technical reason is in the log, not shown to members.
            return SPEECH_UNAVAILABLE
        return f"I couldn't join voice: {type(exc).__name__}: {exc}"

    where = session.channel_name or "the channel"
    listener = session.listener
    if listener is not None:
        # Spoken in the background so the command's reply isn't held up
        # by speech generation.
        asyncio.create_task(_greet(listener, pending))
    else:
        _cancel(pending)
    note = ""
    if session.playing:
        note = " Something's playing, so I'll pause it while I talk."
    return (f"🎙️ Listening in **{where}**. Say my name if it's for me. "
            f"Anyone who'd rather I didn't hear them can say “{NAME}, don't "
            "listen to me”, here or out loud. No audio is kept." + note)


@command(
    name="stop_voice_chat",
    description="Stop listening in voice",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("stop_listening", "end_voice_chat"),
    examples=("stop listening", "stop the voice chat"),
)
async def stop_voice_chat(ctx: CommandContext) -> str:
    chat = ctx.extras.get("voicechat")
    voice = ctx.extras.get("voice")
    session = voice.get(ctx.guild_id) if voice else None
    leaving = session is not None and not session.playing
    if leaving and chat is not None and chat.active(ctx.guild_id):
        await say_goodbye(ctx)
    if chat is None or not await chat.stop(ctx.guild_id):
        if session is not None:
            # In voice but not listening: "leave the vc" was routed here
            # and he answered "I'm not listening" and stayed (live,
            # 2026-10-17). Whatever the wording, they want him out.
            from .radio_cmds import stop_radio

            return await stop_radio(ctx)
        return "I'm not listening in voice here."
    if leaving:
        # Nothing else is using the channel; don't sit there silently.
        await voice.disconnect(ctx.guild_id)
        return "Stopped listening and left the channel."
    return "Stopped listening. The music stays on."


@command(
    name="voice_optout",
    description=f"Stop or let {NAME} hear you in voice",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("dont_listen_to_me", "voice_opt_out", "voice_optin"),
    params=(Param("out", "true: don't hear me", kind="boolean",
                  required=False, default=True),),
    examples=("don't listen to me", "you can listen to me again"),
)
async def voice_optout(ctx: CommandContext, out: bool = True) -> str:
    optout = ctx.extras.get("voice_optout")
    if optout is None:
        return "Voice chat isn't set up on this bot."
    optout.set(ctx.invoker.id, out)
    chat = ctx.extras.get("voicechat")
    if not out:
        if chat is not None:
            chat.rescan_everywhere()
        return "Okay, I can hear you again in voice chat."
    if chat is not None:
        chat.forget_everywhere(ctx.invoker.id)
    return ("Got it. I won't hear your voice in any voice chat, anywhere. "
            "Say “you can listen to me again” to undo it.")
