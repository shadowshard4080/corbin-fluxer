"""Music playback commands.

The Discord build drove this with NowPlayingView, QueueView, SearchView,
ConfirmPlaylistView and a modal. Fluxer has no components, so each is a
command instead -- which the AI router handles naturally ("skip this",
"what's in the queue", "play the next one twice").
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.audio import format_duration
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.reactions import Controls, legend
from ..core.registry import Param, Risk, command

COLOUR = 0x1DB954
QUEUE_PAGE = 10

#: Reaction controls on the Now Playing card. Only people in the bot's
#: voice channel can use them (see ``bot.py``'s music panel handler).
MUSIC_BUTTONS = (("⏯️", "pause/resume"), ("⏮️", "restart"),
                 ("⏭️", "skip"), ("⏹️", "stop"),
                 ("🔁", "loop"), ("🔀", "shuffle"))
#: Added to the card where the music video can be streamed (the server is
#: on the owner's ``streaming`` allowlist and the track has a video page).
VIDEO_BUTTON = ("📺", "video")
#: 🔁 cycles through these.
LOOP_CYCLE = ("off", "track", "queue")


def video_status(extras: Any, guild_id: Optional[str]) -> str:
    """The card's video line from a command's extras ("" if off)."""
    mv = (extras or {}).get("music_video") if extras else None
    return mv.status_line(guild_id) if mv is not None else ""


def video_ok(features: Any, guild_id: Optional[str], track: Any) -> bool:
    """Should this card offer 📺? Streaming allowed here, and a video."""
    from ..core.music_video import is_video_page

    if features is None or not guild_id or track is None:
        return False
    try:
        allowed = features.allowed(str(guild_id), "streaming")
    except Exception:
        return False
    return bool(allowed) and is_video_page(getattr(track, "webpage_url", ""))


def loop_mode(player: Any) -> str:
    if getattr(player, "loop_track", False):
        return "track"
    if getattr(player, "loop_queue", False):
        return "queue"
    return "off"


def player_status(player: Any) -> str:
    """"▶️ Playing" / "⏸️ Paused", plus the loop mode if any."""
    bits = []
    if player is not None and getattr(player, "paused", False):
        bits.append("⏸️ Paused")
    elif player is not None and getattr(player, "current", None) is not None:
        bits.append("▶️ Playing")
    mode = loop_mode(player) if player is not None else "off"
    if mode != "off":
        bits.append(f"🔁 Looping {mode}")
    return " · ".join(bits)


def _voice_channel(ctx: CommandContext, name: Optional[str]) -> Optional[str]:
    if name:
        text = str(name).strip().lstrip("#")
        if text.isdigit():
            return text
        found = (ctx.extras.get("channel_index") or {}).get(text.lower())
        if found:
            return found
    return (ctx.extras.get("voice_states") or {}).get(str(ctx.invoker.id))


def track_embed(track: Any, *, title: str = "Now playing",
                position: Optional[int] = None,
                guild_id: Optional[str] = None, player: Any = None,
                note: str = "", video: bool = False,
                status: str = "", activity: str = "") -> Embed:
    """The track card. With ``guild_id`` it is a Now Playing panel: it
    carries the reaction controls and says so in the footer.

    The footer is live (``bot.py`` re-renders it as things change): an
    ``activity`` in progress ("⏮️ Starting from the beginning…") in place
    of the playing/paused line, then the video's ``status`` line, then
    ``note`` (who did what last)."""
    embed = Embed(title=title, colour=COLOUR,
                  description=(f"**[{track.title}]({track.webpage_url})**"
                               if track.webpage_url else f"**{track.title}**"))
    if track.uploader:
        embed.add_field("Uploader", track.uploader, inline=True)
    embed.add_field("Length", track.pretty_duration, inline=True)
    if position is not None:
        embed.add_field("Position in queue", str(position), inline=True)
    if track.requester_name:
        embed.add_field("Requested by", track.requester_name, inline=True)
    if track.thumbnail:
        embed.thumbnail_url = track.thumbnail
    if guild_id:
        # 📺 first (owner, 2026-10-20): reactions go on in this order,
        # one at a time, so the first is clickable as soon as the card
        # loads; the video is what people reach for.
        buttons = ((VIDEO_BUTTON,) if video else ()) + MUSIC_BUTTONS
        lines = [x for x in (activity or player_status(player), status,
                             note) if x]
        lines.append("React: " + legend(buttons))
        embed.footer = "\n".join(lines)
        embed.controls = Controls(kind="music", buttons=buttons,
                                  state={"guild_id": str(guild_id)},
                                  exclusive=True)
    return embed


@command(
    name="play",
    description=("Play a song, or a whole YouTube or Spotify playlist or "
                 "album, or add it to the queue"),
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("p", "queue_song"),
    params=(
        Param("query", "a search phrase or a link", max_length=500),
        Param("channel", "which voice channel", kind="channel",
              required=False),
    ),
    examples=("play bohemian rhapsody", "play that song in Music",
              "queue up some lofi", "play this playlist <link>"),
)
async def play(ctx: CommandContext, query: str,
               channel: Optional[str] = None) -> Any:
    player_mgr = ctx.extras.get("player")
    extractor = ctx.extras.get("extractor")
    # "Play Bob's Burgers episode 3" is the video library, not a YouTube
    # search for a song: a show named by its whole folder name goes there.
    videos = ctx.extras.get("videos")
    if videos is not None:
        from ..core.library import show_in_text

        show = show_in_text(videos.shows(), query)
        if show is not None:
            from .video_cmds import parse_request, video_play

            season, episode = parse_request(query)
            return await video_play(
                ctx, show=show.name,
                episode=str(episode) if episode is not None else None,
                season=season, voice_channel=channel)
    if player_mgr is None or extractor is None:
        return "Music support isn't loaded."
    target = _voice_channel(ctx, channel)
    if not target:
        return ("You're not in a voice channel, so I don't know where to "
                "play. Join one, or tell me which.")

    await extractor.probe_pot()
    who = dict(requested_by=ctx.invoker.id,
               requester_name=ctx.invoker.display_name)

    # A playlist or album used to queue only its first song. Now the first
    # playable one starts straight away and the rest are imported in the
    # background, as on Discord.
    rest: list = []
    listed = await extractor.collection(query)
    if listed:
        tracks = []
        for i, item in enumerate(listed[:3]):
            tracks, _err = await extractor.resolve(item, **who)
            if tracks:
                rest = listed[i + 1:]
                break
        if not tracks:
            return "I couldn't load anything from that playlist."
    else:
        # None: not a collection. []: one we couldn't read -- resolving it
        # as a single link gives the specific reason (e.g. no Spotify
        # credentials) instead of a generic failure.
        tracks, error = await extractor.resolve(query, **who)
        if error:
            return error

    player = player_mgr.player(ctx.guild_id)
    player.announce_to = target
    track = tracks[0]

    if player.playing:
        if not player.add(track):
            return "The queue is full."
        embed = track_embed(track, title="Added to the queue",
                            position=len(player.queue))
    else:
        # announce=False: the embed returned below IS the announcement.
        failure = await player_mgr.play_now(ctx.guild_id, target, track,
                                            announce=False)
        if failure:
            return failure
        embed = track_embed(track, guild_id=ctx.guild_id, player=player,
                            video=video_ok(ctx.extras.get("features"),
                                           ctx.guild_id, track),
                            status=video_status(ctx.extras, ctx.guild_id))
        # This reply is the card. If the player's own announcement fires
        # for the same track in this channel, it skips.
        player.last_card = (id(track), str(ctx.channel.id))

    if rest:
        player_mgr.start_import(ctx.guild_id, target, rest, **who)
        embed.add_field("Playlist", f"Adding {len(rest)} more to the queue "
                                    f"in the background.")
    return embed


@command(
    name="skip",
    description="Skip the current track",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("next",),
    examples=("skip this", "next song"),
)
async def skip(ctx: CommandContext) -> str:
    player_mgr = ctx.extras.get("player")
    if player_mgr is None:
        return "Music support isn't loaded."
    player = player_mgr.get(ctx.guild_id)
    if not player or not player.playing:
        return "Nothing is playing."
    skipped = await player_mgr.skip(ctx.guild_id)
    if skipped is None:
        # The check above passed, then another skip got there first --
        # "skip this" and "next song" a second apart is a normal thing to
        # say. Reporting the track the other call skipped would post the
        # same message twice.
        return "Already skipped that one."
    nxt = player.current
    if nxt and nxt is not skipped:
        return f"Skipped **{skipped.title}**. Now playing **{nxt.title}**."
    return f"Skipped **{skipped.title}**. Queue's empty."


@command(
    name="restart_song",
    description="Play the current track again from the start",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("replay", "restart_track", "start_over"),
    examples=("restart the song", "play this from the beginning",
              "start the song over"),
)
async def restart_song(ctx: CommandContext) -> str:
    player_mgr = ctx.extras.get("player")
    if player_mgr is None:
        return "Music support isn't loaded."
    player = player_mgr.get(ctx.guild_id)
    if not player or not player.playing:
        return "Nothing is playing."
    track = await player_mgr.restart(ctx.guild_id)
    if track is None:
        return "I couldn't restart that one."
    return f"Back to the start of **{track.title}**."


@command(
    name="stop_music",
    description="Stop playback, clear the queue and leave",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("stop_playback", "clear_queue"),
    confirm_phrase=lambda a: "stop the music and clear the queue",
    examples=("stop the music", "clear the queue and leave"),
)
async def stop_music(ctx: CommandContext) -> str:
    player_mgr = ctx.extras.get("player")
    if player_mgr is None:
        return "Music support isn't loaded."
    player = player_mgr.get(ctx.guild_id)
    if not player or not (player.playing or player.queue):
        return "Nothing to stop."
    await player_mgr.stop(ctx.guild_id)
    return "Stopped, queue cleared, and I've left the channel."


@command(
    name="queue",
    description="Show what's queued up",
    risk=Risk.SAFE,
    aliases=("q", "playlist"),
    params=(Param("page", "which page", kind="integer", required=False,
                  default=1, min_value=1, max_value=50),),
    examples=("what's in the queue", "show the queue", "queue page 2"),
)
async def queue(ctx: CommandContext, page: int = 1) -> Any:
    player_mgr = ctx.extras.get("player")
    player = player_mgr.get(ctx.guild_id) if player_mgr else None
    if not player or not (player.playing or player.queue):
        return "Nothing playing and nothing queued."

    embed = Embed(title="Queue", colour=COLOUR)
    if player.current:
        elapsed = format_duration(player.elapsed)
        embed.add_field(
            "Now playing",
            f"**{player.current.title}** "
            f"({elapsed} / {player.current.pretty_duration})")
    if player.queue:
        offset = (max(1, page) - 1) * QUEUE_PAGE
        rows = player.queue[offset:offset + QUEUE_PAGE]
        if not rows:
            return f"That's past the end. {len(player.queue)} tracks queued."
        embed.add_list_field("Up next", [
            f"`{offset + i}.` {t.title} ({t.pretty_duration})"
            for i, t in enumerate(rows, 1)])
        pages = max(1, -(-len(player.queue) // QUEUE_PAGE))
        total = sum(t.duration or 0 for t in player.queue)
        embed.footer = (f"Page {page} of {pages} · {len(player.queue)} "
                        f"tracks · {format_duration(total)} total")
    else:
        embed.footer = "Nothing queued after this."
    modes = [m for m, on in (("looping track", player.loop_track),
                             ("looping queue", player.loop_queue)) if on]
    if modes:
        embed.add_field("Mode", ", ".join(modes), inline=True)
    return embed


@command(
    name="nowplaying_music",
    description="Show the current track",
    risk=Risk.SAFE,
    aliases=("current_track", "whats_playing"),
    examples=("what song is this", "what's currently playing"),
)
async def nowplaying_music(ctx: CommandContext) -> Any:
    player_mgr = ctx.extras.get("player")
    player = player_mgr.get(ctx.guild_id) if player_mgr else None
    if not player or not player.current:
        return "Nothing is playing."
    embed = track_embed(player.current, guild_id=ctx.guild_id, player=player,
                        video=video_ok(ctx.extras.get("features"),
                                       ctx.guild_id, player.current),
                        status=video_status(ctx.extras, ctx.guild_id))
    embed.add_field("Elapsed", format_duration(player.elapsed), inline=True)
    return embed


@command(
    name="pause_music",
    description="Pause the current track",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("pause",),
    examples=("pause the music", "pause this song"),
)
async def pause_music(ctx: CommandContext) -> str:
    player_mgr = ctx.extras.get("player")
    player = player_mgr.get(ctx.guild_id) if player_mgr else None
    if not player or not player.current:
        # "Pause" with an episode on means the episode.
        videos = ctx.extras.get("videos")
        if videos is not None and videos.playing(ctx.guild_id) is not None:
            from .video_cmds import video_pause

            return await video_pause(ctx, action="pause")
        return "Nothing is playing."
    if player.paused:
        return "Already paused. Say “resume” to carry on."
    player_mgr.toggle_pause(ctx.guild_id)
    return f"Paused **{player.current.title}**."


@command(
    name="resume_music",
    description="Resume the paused track",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("resume", "unpause"),
    examples=("resume the music", "unpause", "keep playing"),
)
async def resume_music(ctx: CommandContext) -> str:
    player_mgr = ctx.extras.get("player")
    player = player_mgr.get(ctx.guild_id) if player_mgr else None
    if not player or not player.current:
        videos = ctx.extras.get("videos")
        if videos is not None and videos.playing(ctx.guild_id) is not None:
            from .video_cmds import video_pause

            return await video_pause(ctx, action="resume")
        return "Nothing is playing."
    if not player.paused:
        return "It isn't paused."
    player_mgr.toggle_pause(ctx.guild_id)
    return f"Resumed **{player.current.title}**."


@command(
    name="loop",
    description="Loop the current track or the whole queue",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    params=(Param("mode", "track, queue or off",
                  choices=("track", "queue", "off")),),
    examples=("loop this song", "loop the queue", "stop looping"),
)
async def loop(ctx: CommandContext, mode: str) -> str:
    player_mgr = ctx.extras.get("player")
    if player_mgr is None:
        return "Music support isn't loaded."
    player = player_mgr.player(ctx.guild_id)
    player.loop_track = mode == "track"
    player.loop_queue = mode == "queue"
    return {"track": "Looping this track.",
            "queue": "Looping the whole queue.",
            "off": "Looping off."}[mode]


@command(
    name="shuffle",
    description="Shuffle the queue",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    examples=("shuffle the queue", "mix up the songs"),
)
async def shuffle(ctx: CommandContext) -> str:
    player_mgr = ctx.extras.get("player")
    player = player_mgr.get(ctx.guild_id) if player_mgr else None
    if not player or len(player.queue) < 2:
        return "Not enough queued to shuffle."
    player.shuffle()
    return f"Shuffled {len(player.queue)} tracks."


@command(
    name="remove_track",
    description="Remove one track from the queue by its position",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("unqueue",),
    params=(Param("position", "its number in the queue", kind="integer",
                  min_value=1, max_value=500),),
    examples=("remove track 3 from the queue",),
)
async def remove_track(ctx: CommandContext, position: int) -> str:
    player_mgr = ctx.extras.get("player")
    player = player_mgr.get(ctx.guild_id) if player_mgr else None
    if not player or not player.queue:
        return "The queue is empty."
    removed = player.remove(position - 1)
    if removed is None:
        return f"There's no track {position}. The queue has {len(player.queue)}."
    return f"Removed **{removed.title}**."
