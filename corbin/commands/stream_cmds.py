"""Streaming video into voice. See :mod:`corbin.core.video`.

Gated per server by the owner's allowlist (``feature="streaming"``),
except stopping, which always works: ending a stream must never depend on
a setting. Two things stream: the music video of what's playing
(:mod:`corbin.core.music_video`; also 📺 on the Now Playing card), and a
test pattern (owner only) that checks the streaming path live without
stopping the bot. Live TV comes next (VOICE_ROADMAP.md, T).
"""

from __future__ import annotations

from typing import Optional

from ..core.context import CommandContext
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from .radio_cmds import _voice_channel
from ..identity import NAME

#: Said with every stream start: a grey tile is Fluxer's "not watching
#: yet", and the first live test read it as a broken stream.
WATCH_HINT = "Click my stream tile in the voice channel to watch."


@command(
    name="stream_test",
    description="Stream a moving test pattern into voice (owner check)",
    risk=Risk.WRITE,
    owner_only=True,
    feature="streaming",
    aliases=("test_stream", "stream_test_pattern"),
    params=(Param("channel", "which voice channel", kind="channel",
                  required=False),),
    examples=("stream a test pattern", "test streaming in Lounge"),
)
async def stream_test(ctx: CommandContext,
                      channel: Optional[str] = None) -> str:
    from ..core.video import StreamBusy, test_pattern

    streams = ctx.extras.get("streams")
    if streams is None:
        return "Streaming isn't set up on this bot."
    target = _voice_channel(ctx, channel)
    if not target:
        if channel:
            return f"I couldn't find a voice channel called “{channel}”."
        return ("You're not in a voice channel. Join one and ask again, "
                "or tell me which.")
    rules = ctx.extras.get("tv_rules")
    q = rules.quality(str(ctx.guild_id)) if rules is not None else None
    try:
        stream, ended = await streams.start(
            ctx.guild_id, target, test_pattern(q), label="test pattern",
            quality=q)
    except StreamBusy as exc:
        return str(exc)
    except PermissionError as exc:
        return f"{exc}."
    except Exception as exc:
        return f"I couldn't start the stream: {exc}"
    note = ""
    if ended:
        note = (" Voice chat is off while I stream, so I've stopped "
                "listening.")
    return (f"📺 Streaming a test pattern in **{stream.where}** at "
            f"{stream.quality.name}30. "
            f"{WATCH_HINT} Say “stop the stream” to end it." + note)


@command(
    name="stream_video",
    description="Stream the video of the song that's playing",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("music_video", "show_the_video", "video_on"),
    examples=("stream the video", "show us the music video",
              "turn the video on"),
)
async def stream_video(ctx: CommandContext) -> str:
    from ..core.music_video import NoVideo
    from ..core.video import StreamBusy

    mv = ctx.extras.get("music_video")
    if mv is None:
        return "Music video isn't set up on this bot."
    if mv.active(ctx.guild_id):
        return f"The video's already on. {WATCH_HINT}"
    try:
        stream, ended = await mv.start(ctx.guild_id)
    except NoVideo as exc:
        return f"No video: {exc}."
    except StreamBusy as exc:
        return str(exc)
    except PermissionError as exc:
        return f"{exc}."
    except Exception as exc:
        return f"I couldn't start the video: {exc}"
    note = (" Voice chat is off while I stream, so I've stopped "
            "listening." if ended else "")
    return (f"📺 Video on in **{stream.where}**, following the queue. "
            f"{WATCH_HINT} Say “stop the video” to go back to just audio."
            + note)


@command(
    name="stop_stream",
    description="Stop streaming video in voice",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("end_stream", "stop_streaming", "stop_the_video"),
    examples=("stop the stream", "end the stream"),
)
async def stop_stream(ctx: CommandContext) -> str:
    streams = ctx.extras.get("streams")
    active = streams.active if streams is not None else None
    if active is None:
        return "I'm not streaming anything."
    if active.guild_id != str(ctx.guild_id):
        # One stream across every server: say where, but don't let
        # another server end it.
        return f"I'm not streaming here. (I'm streaming in {active.where}.)"
    music = active.kind == "music"
    mv = ctx.extras.get("music_video")
    tv = ctx.extras.get("tv")
    videos = ctx.extras.get("videos")
    if active.kind == "video" and videos is not None:
        # Through the player, so progress is saved and the sound stops.
        await videos.stop(ctx.guild_id, f"stopped by {ctx.invoker.id}")
    elif active.kind == "tv" and tv is not None:
        # Through the TV, so its sound stops with the picture.
        await tv.stop(ctx.guild_id, f"stopped by {ctx.invoker.id}")
    elif music and mv is not None:
        # Through MusicVideo, so the Now Playing footer updates too.
        await mv.stop(ctx.guild_id, f"stopped by {ctx.invoker.id}")
    else:
        await streams.stop(f"stopped by {ctx.invoker.id}")
    if music:
        # Back to just audio; the music carries on.
        return "Video off. The music keeps playing."
    # Nothing else using the channel: leave rather than sit there alone
    # (he stayed after the first live stop, and "leave the vc" then
    # misfired). Music or voice chat keeps him in.
    voice = ctx.extras.get("voice")
    chat = ctx.extras.get("voicechat")
    session = voice.get(ctx.guild_id) if voice is not None else None
    if session is not None and not session.playing and not (
            chat is not None and chat.active(ctx.guild_id)):
        await voice.disconnect(ctx.guild_id)
        return "Stream stopped, and I've left the channel."
    return "Stream stopped."


@command(
    name="stream_quality",
    description=f"Set the video quality {NAME} streams at in this server "
                "(720p or 1080p)",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    feature="streaming",
    aliases=("set_stream_quality", "video_quality"),
    params=(Param("quality", "720p or 1080p", required=False),),
    confirm_phrase=lambda a: f"stream at {a.get('quality')} here",
    examples=("stream in 1080p", "set the stream quality to 720p",
              "what quality do you stream at?"),
)
async def stream_quality(ctx: CommandContext,
                         quality: Optional[str] = None) -> str:
    from ..core.tv_rules import RuleError

    rules = ctx.extras.get("tv_rules")
    if rules is None:
        return "Streaming isn't set up on this bot."
    gid = str(ctx.guild_id)
    if not quality:
        q = rules.quality(gid)
        note = rules.quality_note(gid)
        return f"I stream at {q.name}30 here." + (f" {note}" if note else "")
    try:
        q = rules.set_quality(gid, quality)
    except RuleError as exc:
        return str(exc)
    tail = ""
    streams = ctx.extras.get("streams")
    active = streams.active if streams is not None else None
    if active is not None and active.guild_id == gid \
            and getattr(active, "quality", None) != q:
        tail = " The stream that's on keeps its size until it's restarted."
    return f"I'll stream at {q.name}30 here from now on.{tail}"
