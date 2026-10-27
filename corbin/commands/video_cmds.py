"""The local video library: list it, watch it, drive it (owner,
2026-10-20). See :mod:`corbin.core.library` and
:mod:`corbin.core.video_player`.

Gated like the rest of streaming (the owner's ``streaming`` allowlist).
Lists are numbered ``video_list`` panels (⬅️ / ➡️ turn the page, "watch
3" picks), and the Now Playing card has ⏯️ ⏭️ ⏹️ for whoever's in the
voice channel.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..core import library as L
from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.reactions import Controls, legend
from ..core.registry import Param, Risk, command

COLOUR = 0x6A1B9A
PER_PAGE = 15
PANEL_KIND = "video_list"
CARD_KIND = "video"
PAGE_BUTTONS = (("⬅️", "previous page"), ("➡️", "next page"))
CARD_BUTTONS = (("⏯️", "pause/resume"), ("⏭️", "next episode"),
                ("⏹️", "stop"))


def _videos(ctx: CommandContext) -> Any:
    return ctx.extras.get("videos")


def _fmt(seconds: float) -> str:
    from ..core.video_player import fmt_time

    return fmt_time(seconds)


NOT_SET_UP = "The video library isn't set up on this bot."


def _empty(videos: Any) -> str:
    return (f"There's nothing in the video folder yet. Put each show in its "
            f"own folder inside `{videos.root}` (seasons in subfolders if "
            f"it has them) and ask again.")


# ---------------------------------------------------------------- listing

def episode_rows(show: L.Show, season: Optional[int]) -> list:
    """The episodes a list covers, in order."""
    if season is not None:
        s = show.season(season)
        return list(s.episodes) if s else []
    return show.episodes()


def render(videos: Any, state: dict) -> Embed:
    """A ``video_list`` page: ``{"show", "season", "page", "guild_id"}``.
    Clamps the page in place."""
    shows = videos.shows()
    show = L.find_show(shows, state.get("show") or "")
    if show is None:
        return Embed(title="🎬 Videos", description="That show is gone.",
                     colour=COLOUR)
    rows = episode_rows(show, state.get("season"))
    pages = max(1, -(-len(rows) // PER_PAGE))
    page = min(max(1, int(state.get("page") or 1)), pages)
    state["page"] = page
    first = (page - 1) * PER_PAGE
    multi = len(show.seasons) > 1 and state.get("season") is None
    prog = videos.progress(state.get("guild_id") or "", show.name) \
        if state.get("guild_id") else None
    lines = []
    for i, e in enumerate(rows[first:first + PER_PAGE], first + 1):
        mark = " ▶️" if prog and prog.get("episode") == e.rel else ""
        label = e.label if multi or e.season is not None else \
            f"Episode {e.number or e.index}"
        title = f" · {e.nice_title}" if e.nice_title else ""
        lines.append(f"**{i}.** {label}{title}{mark}")
    season = show.season(state.get("season"))
    heading = show.name + (f": {season.label}" if season else "")
    embed = Embed(title=f"🎬 {heading} ({len(rows)})",
                  description="\n".join(lines) or "No episodes.",
                  colour=COLOUR)
    turn = f"React {legend(PAGE_BUTTONS)}" if pages > 1 else ""
    embed.footer = " · ".join(x for x in (
        f"Page {page} of {pages}", turn, "Say “watch 3” to put one on")
        if x)
    embed.controls = Controls(kind=PANEL_KIND,
                              buttons=PAGE_BUTTONS if pages > 1 else (),
                              state=dict(state))
    return embed


def shows_embed(videos: Any, guild_id: Optional[str]) -> Embed:
    shows = videos.shows()
    lines = []
    for s in shows:
        n = len(s.episodes())
        seasons = [x for x in s.seasons if x.number not in (None, 0)]
        bits = [f"{n} episode{'s' if n != 1 else ''}"]
        if len(seasons) > 1:
            bits.insert(0, f"{len(seasons)} seasons")
        prog = videos.progress(guild_id, s.name) if guild_id else None
        if prog:
            found = L.find_episode([s], prog.get("episode", ""))
            if found:
                ep = found[1]
                bits.append("finished " + ep.label if prog.get("finished")
                            else f"on {ep.label} at "
                                 f"{_fmt(prog.get('position') or 0)}")
        lines.append(f"**{s.name}** · " + " · ".join(bits))
    embed = Embed(title="🎬 Videos", description="\n".join(lines),
                  colour=COLOUR)
    embed.footer = ("“show Bob's Burgers episodes” · “watch Bob's Burgers "
                    "episode 1” · “continue Bob's Burgers”")
    return embed


@command(
    name="videos",
    description="List the shows in the video library, or a show's episodes",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("video_library", "list_videos", "list_episodes"),
    params=(
        Param("show", "which show; all of them if not given",
              required=False),
        Param("season", "which season", kind="integer", required=False),
    ),
    examples=("what videos do you have?", "list Bob's Burgers episodes",
              "what shows can you play?", "show season 2 of that show"),
)
async def videos(ctx: CommandContext, show: Optional[str] = None,
                 season: Optional[int] = None) -> Any:
    v = _videos(ctx)
    if v is None:
        return NOT_SET_UP
    shows = v.shows()
    if not shows:
        return _empty(v)
    if not show:
        if len(shows) == 1 and season is None:
            return shows_embed(v, ctx.guild_id)
        return shows_embed(v, ctx.guild_id)
    found = L.find_show(shows, show)
    if found is None:
        return (f"I don't have a show called “{show}”. I have: "
                f"{', '.join(s.name for s in shows)}.")
    if season is not None and found.season(season) is None:
        return (f"{found.name} has no season {season}. It has: "
                f"{', '.join(s.label for s in found.seasons)}.")
    return render(v, {"show": found.name, "season": season, "page": 1,
                      "guild_id": str(ctx.guild_id) if ctx.guild_id
                      else None})


@command(
    name="video_page",
    description="Next or previous page of a video library list",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("more_episodes",),
    params=(Param("direction", "next, previous, or a page number",
                  required=False),),
    examples=("next page of episodes", "more episodes"),
)
async def video_page(ctx: CommandContext,
                     direction: Optional[str] = None) -> Any:
    v = _videos(ctx)
    panels = ctx.extras.get("panels")
    found = panels.latest(PANEL_KIND, ctx.guild_id) if panels else None
    if v is None or found is None or \
            found[1].channel_id != str(ctx.channel.id):
        return "There's no episode list here. Ask “list Bob's Burgers " \
               "episodes” first."
    state = dict(found[1].state)
    d = (direction or "next").strip().lower()
    cur = int(state.get("page") or 1)
    state["page"] = int(d) if d.isdigit() else (
        cur - 1 if d.startswith(("prev", "back")) else cur + 1)
    return render(v, state)


# ---------------------------------------------------------------- watching

_NUMBER = re.compile(r"^(?:number|no\.?|#|episode|ep)?\s*(\d{1,4})$", re.I)


def parse_request(text: str) -> tuple:
    """``(season, episode)`` from words like "s02e03", "season 2 episode
    3", "episode 3", "ep 3". Either may be None."""
    t = (text or "").lower()
    m = re.search(r"s(\d{1,2})\s*e(\d{1,3})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    season = re.search(r"season\s*(\d{1,2})", t)
    ep = re.search(r"(?:episode|ep\.?)\s*(\d{1,3})", t)
    return (int(season.group(1)) if season else None,
            int(ep.group(1)) if ep else None)


def _from_list(ctx: CommandContext, v: Any, number: int) -> tuple:
    """``(show, episode, error)`` for "watch 3" against the newest list."""
    panels = ctx.extras.get("panels")
    found = panels.latest(PANEL_KIND, ctx.guild_id) if panels else None
    if found is None or found[1].channel_id != str(ctx.channel.id):
        return None, None, None
    state = found[1].state
    show = L.find_show(v.shows(), state.get("show") or "")
    if show is None:
        return None, None, "That list's show isn't in the folder any more."
    rows = episode_rows(show, state.get("season"))
    if not 1 <= number <= len(rows):
        return None, None, f"That list only goes up to {len(rows)}."
    return show, rows[number - 1], None


def now_playing(p: Any, where: str) -> Embed:
    from .stream_cmds import WATCH_HINT

    e = p.episode
    embed = Embed(title=f"🎬 {p.show.name}" + ("" if e.film
                                               else f": {e.label}"),
                  description=((f"**{e.nice_title}**\n" if e.nice_title
                                else "") + f"In **{where}**. {WATCH_HINT}"),
                  colour=COLOUR)
    total = p.info.duration
    embed.add_field("Position", f"{_fmt(p.position())}"
                    + (f" / {_fmt(total)}" if total else ""), inline=True)
    embed.add_field("Audio", p.audio.name if p.audio else "none",
                    inline=True)
    embed.add_field("Subtitles", p.sub.name if p.sub else "off",
                    inline=True)
    embed.add_field("Picture", f"{p.quality.name}30", inline=True)
    embed.footer = (f"React {legend(CARD_BUTTONS)} · “skip to 12:30”, "
                    f"“switch to Japanese with subtitles”")
    embed.controls = Controls(kind=CARD_KIND, buttons=CARD_BUTTONS,
                              state={"guild_id": p.guild_id},
                              exclusive=True)
    return embed


def _where(ctx: CommandContext) -> str:
    voice = ctx.extras.get("voice")
    session = voice.get(ctx.guild_id) if voice is not None else None
    return getattr(session, "channel_name", "") or "the voice channel"


@command(
    name="video_play",
    description="Watch an episode from the video library in voice",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("watch_video", "watch_episode", "play_episode",
             "continue_show"),
    params=(
        Param("show", "the show's name", required=False),
        Param("episode", "the episode: a number, s02e03, or its number "
                         "in the last list", required=False),
        Param("season", "the season number", kind="integer",
              required=False),
        Param("language", "audio language, e.g. Japanese or English",
              required=False),
        Param("subtitles", "subtitles on or off", kind="boolean",
              required=False),
        Param("from_start", "start the episode over instead of resuming",
              kind="boolean", required=False),
        Param("voice_channel", "which voice channel", kind="channel",
              required=False),
    ),
    examples=("watch Bob's Burgers episode 1", "continue Bob's Burgers",
              "put on Bob's Burgers season 1 episode 3", "watch 3",
              "watch Bob's Burgers in Japanese with subtitles",
              "start Bob's Burgers from the beginning"),
)
async def video_play(ctx: CommandContext, show: Optional[str] = None,
                     episode: Optional[str] = None,
                     season: Optional[int] = None,
                     language: Optional[str] = None,
                     subtitles: Optional[bool] = None,
                     from_start: Optional[bool] = None,
                     voice_channel: Optional[str] = None) -> Any:
    from ..core.video import StreamBusy
    from ..core.video_player import VideoError
    from .radio_cmds import _voice_channel

    v = _videos(ctx)
    if v is None:
        return NOT_SET_UP
    shows = v.shows()
    if not shows:
        return _empty(v)
    target = _voice_channel(ctx, voice_channel)
    if not target:
        if voice_channel:
            return f"I couldn't find a voice channel called “{voice_channel}”."
        return ("You're not in a voice channel. Join one and ask again, or "
                "tell me which.")
    ep_text = (episode or "").strip()
    chosen_show = chosen = None
    start = 0.0
    m = _NUMBER.match(ep_text) if ep_text and not show else None
    if m:
        chosen_show, chosen, err = _from_list(ctx, v, int(m.group(1)))
        if err:
            return err
    if chosen is None:
        if show:
            chosen_show = L.find_show(shows, show)
            if chosen_show is None:
                return (f"I don't have a show called “{show}”. I have: "
                        f"{', '.join(s.name for s in shows)}.")
        elif len(shows) == 1:
            chosen_show = shows[0]
        else:
            playing = v.playing(ctx.guild_id)
            if playing is not None:
                chosen_show = playing.show
            else:
                return ("Which show? I have: "
                        f"{', '.join(s.name for s in shows)}.")
        s_num, e_num = parse_request(ep_text)
        if season is not None:
            s_num = season
        if e_num is None and ep_text.isdigit():
            e_num = int(ep_text)
        if e_num is None and s_num is None:
            if from_start:
                chosen = L.pick(chosen_show)
            else:
                chosen, start = v.resume_point(str(ctx.guild_id),
                                               chosen_show)
        else:
            chosen = L.pick(chosen_show, season=s_num,
                            episode=e_num if e_num is not None else None)
            if chosen is None:
                what = (f"season {s_num} episode {e_num}" if s_num is not None
                        and e_num is not None else f"episode {e_num}"
                        if e_num is not None else f"season {s_num}")
                return f"{chosen_show.name} has no {what}."
    if chosen is None:
        return f"{chosen_show.name} has no episodes."
    try:
        p = await v.play(ctx.guild_id, target, chosen_show, chosen,
                         start=start, notify_to=target, language=language,
                         subtitles=subtitles)
    except (VideoError, StreamBusy) as exc:
        return str(exc)
    except PermissionError as exc:
        return f"{exc}."
    card = now_playing(p, _where(ctx))
    if start > 1:
        card.description = (f"Picking up at {_fmt(start)}. "
                            + (card.description or ""))
    return card


def _p(ctx: CommandContext) -> tuple:
    v = _videos(ctx)
    p = v.playing(ctx.guild_id) if v is not None else None
    return v, p


@command(
    name="video_next",
    description="Next or previous episode of the show that's on",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("next_episode", "previous_episode"),
    params=(Param("direction", "next or previous",
                  choices=("next", "previous"), required=False),),
    examples=("next episode", "play the next episode",
              "go back an episode", "previous episode"),
)
async def video_next(ctx: CommandContext,
                     direction: Optional[str] = None) -> Any:
    from ..core.video_player import VideoError

    v, p = _p(ctx)
    if p is None:
        return "Nothing from the video library is on."
    step = -1 if (direction or "next") == "previous" else 1
    try:
        nxt = await v.step(ctx.guild_id, step)
    except VideoError as exc:
        return str(exc)
    if nxt is None:
        return ("That's the last episode." if step > 0 else
                "That's the first episode.")
    return now_playing(nxt, _where(ctx))


@command(
    name="video_pause",
    description="Pause or resume the episode that's on",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("pause_video", "resume_video", "pause_episode"),
    params=(Param("action", "pause or resume",
                  choices=("pause", "resume"), required=False),),
    examples=("pause the episode", "pause the show", "resume the episode",
              "unpause the video"),
)
async def video_pause(ctx: CommandContext,
                      action: Optional[str] = None) -> str:
    v, p = _p(ctx)
    if p is None:
        return "Nothing from the video library is on."
    want = action or ("resume" if p.paused else "pause")
    if (want == "pause") == p.paused:
        return "It's already paused." if p.paused else "It isn't paused."
    v.pause(ctx.guild_id)
    if p.paused:
        return f"Paused at {_fmt(p.position())}."
    return f"Resumed {p.show.name} {p.episode.label}."


@command(
    name="video_seek",
    description="Jump to a time in the episode that's on, or skip "
                "forward/back",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("seek_video", "skip_to"),
    params=(Param("position", "a time like 12:30, or “forward 30 seconds”, "
                              "“back 2 minutes”"),),
    examples=("skip to 12:30", "go back 30 seconds", "skip ahead 2 minutes",
              "jump to 5 minutes in"),
)
async def video_seek(ctx: CommandContext, position: str) -> Any:
    from ..core.video_player import VideoError, parse_time

    v, p = _p(ctx)
    if p is None:
        return "Nothing from the video library is on."
    text = re.sub(r"\b(in|into it|minutes in)$", "", position.strip(),
                  flags=re.I).strip() or position
    target = parse_time(text, now=p.position())
    if target is None:
        return (f"I don't follow “{position}”. Try “12:30”, “forward 30 "
                f"seconds” or “back 2 minutes”.")
    try:
        p = await v.seek(ctx.guild_id, target)
    except VideoError as exc:
        return str(exc)
    return f"Jumped to {_fmt(p.start)}."


@command(
    name="video_tracks",
    description="Switch the audio language or subtitles of the episode "
                "that's on",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("video_audio", "video_subtitles"),
    params=(
        Param("language", "audio language, e.g. Japanese", required=False),
        Param("subtitles", "subtitles on or off", kind="boolean",
              required=False),
    ),
    examples=("switch to Japanese with subtitles", "turn subtitles on",
              "turn the subtitles off", "switch the audio to English"),
)
async def video_tracks(ctx: CommandContext, language: Optional[str] = None,
                       subtitles: Optional[bool] = None) -> Any:
    from ..core.video_player import VideoError

    v, p = _p(ctx)
    if p is None:
        return "Nothing from the video library is on."
    if language is None and subtitles is None:
        audios = ", ".join(t.name for t in p.info.audio) or "none"
        subs = ", ".join(t.name for t in p.info.subs) or "none"
        return (f"Audio tracks: {audios}. Subtitles: {subs}. Say “switch to "
                f"Japanese with subtitles”.")
    try:
        p = await v.change_tracks(ctx.guild_id, language=language,
                                  subtitles=subtitles)
    except VideoError as exc:
        return str(exc)
    if language and p.audio and not p.audio.name.lower().startswith(
            language.lower()[:3]):
        return (f"There's no {language} audio on this one; it's "
                f"{p.audio.name}. Subtitles: "
                f"{p.sub.name if p.sub else 'off'}.")
    return (f"Audio: {p.audio.name if p.audio else 'none'}. Subtitles: "
            f"{p.sub.name if p.sub else 'off'}. Picking up at "
            f"{_fmt(p.start)}.")


@command(
    name="video_stop",
    description="Stop the episode that's on",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("stop_video", "stop_episode", "stop_watching"),
    examples=("stop the episode", "stop the show", "stop watching"),
)
async def video_stop(ctx: CommandContext) -> str:
    v, p = _p(ctx)
    if p is None:
        return "Nothing from the video library is on."
    where = p.position()
    await v.stop(ctx.guild_id, f"stopped by {ctx.invoker.id}")
    note = f"Stopped {p.episode.full_name} at {_fmt(where)}. " \
           f"Say “continue {p.show.name}” to pick up there."
    voice = ctx.extras.get("voice")
    session = voice.get(ctx.guild_id) if voice is not None else None
    if session is not None and not session.playing:
        await voice.disconnect(ctx.guild_id)
        note += " I've left the channel."
    return note


@command(
    name="video_now",
    description="What episode is on, and how far in",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("what_episode", "video_status"),
    examples=("what episode is this?", "how far into the episode are we?",
              "how long is left in this episode?"),
)
async def video_now(ctx: CommandContext) -> Any:
    v, p = _p(ctx)
    if p is None:
        return "Nothing from the video library is on."
    card = now_playing(p, _where(ctx))
    if p.info.duration:
        left = max(0.0, p.info.duration - p.position())
        card.description = (card.description or "") + \
            f"\n{_fmt(left)} left." + (" (Paused.)" if p.paused else "")
    return card


@command(
    name="video_autoplay",
    description="Turn autoplay of the next episode on or off",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("autoplay_episodes",),
    params=(Param("enabled", "on or off", kind="boolean"),),
    examples=("turn autoplay off", "keep playing the next episode",
              "stop after this episode"),
)
async def video_autoplay(ctx: CommandContext, enabled: bool) -> str:
    v = _videos(ctx)
    if v is None:
        return NOT_SET_UP
    v.set_autoplay(str(ctx.guild_id), enabled)
    return ("Autoplay on: the next episode starts when one ends."
            if enabled else
            "Autoplay off: I'll stop at the end of this episode.")
