"""Internet radio playback.

The station is set in .env: RADIO_NAME, RADIO_STREAM_URL, and optionally
RADIO_NOW_PLAYING_URL for the profile status. The default is 975 KZLF.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

STREAM_URL = (os.environ.get("RADIO_STREAM_URL")
              or "https://listen.kzlf.net/stream")
#: Plain text or a small HTML snippet naming the current track. Optional:
#: without it the status just shows the station.
NOW_PLAYING_URL = (os.environ.get("RADIO_NOW_PLAYING_URL")
                   or ("" if os.environ.get("RADIO_STREAM_URL")
                       else "https://kzlf.net/NowPlaying.txt"))
STATION = os.environ.get("RADIO_NAME") or "975 KZLF"


#: KZLF's NowPlaying.txt is HTML despite the extension: a styled header
#: span followed by a div holding the actual track. Plain text passes
#: through the same cleaning unchanged.
_TAG = __import__("re").compile(r"<[^>]+>")
_HEADER = __import__("re").compile(r"now playing on[^<]*", __import__("re").I)


def parse_now_playing(raw: str) -> Optional[str]:
    """Pull the track out of a now-playing snippet (HTML or plain text)."""
    import html as _html
    import re

    if not raw:
        return None
    # The track lives in the last div; fall back to stripping everything.
    divs = re.findall(r"<div[^>]*>(.*?)</div>", raw, re.S | re.I)
    text = divs[-1] if divs else raw
    text = _TAG.sub(" ", text)
    text = _HEADER.sub(" ", text)
    text = _html.unescape(text)
    text = " ".join(text.split())
    return text[:200] or None


async def fetch_now_playing() -> Optional[str]:
    import httpx

    if not NOW_PLAYING_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(NOW_PLAYING_URL)
        resp.raise_for_status()
        return parse_now_playing(resp.text)
    except Exception:
        return None


def _voice_channel(ctx: CommandContext, name: Optional[str]) -> Optional[str]:
    """Resolve the target channel: named, or wherever the caller already is.

    Voice state is tracked from VOICE_STATE_UPDATE and seeded from the
    GUILD_CREATE snapshot, so someone who joined before the bot started is
    still found.
    """
    if name:
        text = str(name).strip().lstrip("#")
        if text.isdigit():
            return text
        found = (ctx.extras.get("channel_index") or {}).get(text.lower())
        if found:
            return found
    return (ctx.extras.get("voice_states") or {}).get(str(ctx.invoker.id))


#: How often to check for a track change. The station updates on song
#: boundaries, so polling faster just costs requests.
WATCH_INTERVAL = 20.0


def render_track(track: str, where: str = "") -> Embed:
    embed = Embed(title=f"📻 {STATION}", description=track, colour=0x1DB954)
    if where:
        embed.footer = f"Playing in {where}"
    return embed


async def _watch_tracks(voice: Any, adapter: Any, guild_id: str,
                        post_to: str) -> None:
    """Announce each new song in the voice channel's own text chat.

    Posts only on change, so a three-minute song produces one message
    rather than one per poll.
    """
    import asyncio

    try:
        while True:
            await asyncio.sleep(WATCH_INTERVAL)
            session = voice.get(guild_id)
            if session is None or not session.playing:
                return
            track = await fetch_now_playing()
            if not track or track == session.last_track:
                continue
            session.last_track = track
            try:
                from ..core.context import Response

                await adapter.send(post_to, Response(
                    embed=render_track(track, session.channel_name)))
            except Exception:
                return
    except asyncio.CancelledError:
        return


@command(
    name="radio",
    description=f"Play {STATION} in a voice channel",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("play_radio",),
    params=(Param("channel", "which voice channel", kind="channel",
                  required=False),),
    examples=("play the radio in Music", "start the radio",
              "put the radio on in the Gaming channel"),
)
async def radio(ctx: CommandContext, channel: Optional[str] = None) -> Any:
    voice = ctx.extras.get("voice")
    if voice is None:
        return "Voice support isn't loaded."
    target = _voice_channel(ctx, channel)
    if not target:
        if channel:
            return (f"I couldn't find a voice channel called “{channel}”. "
                    "Join one and just say “play the radio”.")
        return ("You're not in a voice channel, so I don't know where to "
                "play. Join one, or tell me which, like “play the radio in "
                "Music”.")
    try:
        session = await voice.play_stream(ctx.guild_id, target, STREAM_URL,
                                          label=STATION)
    except Exception as exc:
        return f"I couldn't start the radio: {type(exc).__name__}: {exc}"

    track = await fetch_now_playing()
    session.last_track = track or ""

    # Fluxer voice channels have their own text chat (verified), so track
    # announcements belong there rather than wherever the command was typed.
    adapter = ctx.extras.get("adapter")
    if adapter is not None:
        import asyncio

        voice.watch(ctx.guild_id, asyncio.create_task(
            _watch_tracks(voice, adapter, ctx.guild_id, target)))
        if track:
            try:
                from ..core.context import Response

                await adapter.send(target, Response(
                    embed=render_track(track, session.channel_name)))
            except Exception:
                pass

    embed = Embed(title=f"📻 {STATION}", colour=0x1DB954,
                  description=f"Playing in **{session.channel_name or target}**"
                              ". I'll post each new track there.")
    if track:
        embed.add_field("Now playing", track)
    return embed


@command(
    name="stop_radio",
    description="Stop playback and leave the voice channel",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("leave_voice", "stop_playing", "disconnect_voice"),
    examples=("stop the radio", "leave the voice channel"),
)
async def stop_radio(ctx: CommandContext) -> str:
    voice = ctx.extras.get("voice")
    if voice is None:
        return "Voice support isn't loaded."
    if await voice.disconnect(ctx.guild_id):
        failed = voice.teardown_failures(ctx.guild_id)
        if "leave voice channel" in failed or "disconnect" in failed:
            return ("Stopped the audio, but Fluxer didn't confirm I left "
                    "the channel. If I'm still showing in there, kick me "
                    "from voice.")
        return "Stopped and left the channel."
    return "I'm not in a voice channel here."


@command(
    name="nowplaying",
    description=f"What's on {STATION} right now",
    risk=Risk.SAFE,
    aliases=("np", "now_playing"),
    examples=("what's playing on the radio", "now playing"),
)
async def nowplaying(ctx: CommandContext) -> Any:
    track = await fetch_now_playing()
    voice = ctx.extras.get("voice")
    session = voice.get(ctx.guild_id) if voice else None

    if not track:
        return f"Couldn't reach {STATION}'s track listing just now."
    embed = Embed(title=f"📻 {STATION}", description=track, colour=0x1DB954)
    if session and session.playing:
        embed.footer = f"Playing in {session.channel_name}"
    return embed
