"""Offline suite for the local video library (corbin/core/library.py,
corbin/core/video_player.py, corbin/commands/video_cmds.py).

The real ffmpeg command was checked by hand against a dual-audio MKV
with two ASS subtitle tracks (2026-10-20): Japanese audio picked the
"Full" English subtitles over "Signs & Songs", and after seeking to 5 s
the line timed for 5-9 s was the one burned in. These tests pin the
logic around it."""
from __future__ import annotations
import asyncio, logging, os, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import library as L
from corbin.core import video_player as V
from corbin.core.video import QUALITIES


def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def _tree(root: Path) -> None:
    for n in (10, 2, 1):
        _touch(root, f"Tom's Tavern/[Grp] Tom's Tavern - {n:02d} [1080p].mkv")
    _touch(root, "Tom's Tavern/notes.txt")
    _touch(root, "Tom's Tavern/.hidden.mkv")
    _touch(root, "Other Show/Season 2/S02E01 Return.mkv")
    _touch(root, "Other Show/Season 1/S01E02 Second.mkv")
    _touch(root, "Other Show/Season 1/S01E01 Pilot.mkv")
    _touch(root, "Other Show/Specials/Behind the scenes.mp4")
    _touch(root, "Other Show/Season 1/Extras/deep/S01E03 Hidden away.mkv")
    (root / "Empty Show").mkdir()


def suite_library(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _tree(root)
        shows = L.scan(root)
        r.check("shows are top-level folders with videos, by name",
                [s.name for s in shows], ["Other Show", "Tom's Tavern"])
        show = L.find_show(shows, "toms tavern")
        r.check("episodes sort naturally, not as text (2 before 10)",
                [e.number for e in show.episodes()], [1, 2, 10])
        r.ok("only video files, never hidden ones",
             all(e.rel.endswith(".mkv") and "/." not in e.rel
                 for e in show.episodes()))
        other = L.find_show(shows, "Other Show")
        r.check("seasons in order, specials first as season 0",
                [s.label for s in other.seasons],
                ["Specials", "Season 1", "Season 2"])
        r.check("a deeper folder is folded into its season",
                [e.number for e in other.season(1).episodes], [1, 2, 3])
        r.check("episode 2 of season 1",
                L.pick(other, season=1, episode=2).title, "S01E02 Second")
        r.check("no season given: the first numbered season, not specials",
                L.pick(other, episode=1).label, "S01E01")
        r.check("the next episode crosses into the next season",
                L.neighbour(other, other.season(1).episodes[-1].rel).label,
                "S02E01")
        r.check("no next after the last", L.neighbour(
            show, show.episodes()[-1].rel), None)
        r.check("fuzzy show names", L.find_show(shows, "tom tavern").name,
                "Tom's Tavern")
        r.check("nothing close: None", L.find_show(shows, "Cowboy Bebop"),
                None)
        r.check("a show named in a sentence",
                L.show_in_text(shows, "play Tom's Tavern episode 3").name,
                "Tom's Tavern")
        r.check("...not a song that merely shares a word",
                L.show_in_text(shows, "play rain by the beatles"), None)
        r.check("a missing root is no shows, not an error",
                L.scan(root / "nope"), [])

        r.ok("ids resolve inside the root",
             L.resolve(root, show.episodes()[0].rel).is_file())
        for bad in ("../etc/passwd", "/etc/passwd"):
            try:
                L.resolve(root, bad)
                ok = False
            except ValueError:
                ok = True
            r.ok(f"{bad!r} is refused", ok)
        outside = Path(tmp).parent / "corbin-outside.mkv"
        outside.write_bytes(b"x")
        try:
            os.symlink(outside, root / "Tom's Tavern" / "sneaky.mkv")
            try:
                L.resolve(root, "Tom's Tavern/sneaky.mkv")
                ok = False
            except ValueError:
                ok = True
            r.ok("a symlink pointing out of the root is refused", ok)
        finally:
            outside.unlink()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _touch(root, "Tom's Tavern/Tom's Tavern Episode 1 - City of Howls.mp4")
        _touch(root, "Tom's Tavern/Tom's Tavern Episode 12 - DON'T MAKE ME "
                     "BLUE.mp4")
        _touch(root, "Happy Trails.mp4")
        shows = L.scan(root)
        r.check("a video straight in the folder is a film of its own "
                "(the owner's real layout)", [(x.name, len(x.episodes()))
                                              for x in shows],
                [("Happy Trails", 1), ("Tom's Tavern", 2)])
        film = L.find_show(shows, "happy trails").episodes()[0]
        r.ok("...worded as a film", film.film and film.full_name ==
             "Happy Trails")
        show = L.find_show(shows, "Tom's Tavern")
        r.check("real titles, without the show or the number",
                [e.nice_title for e in show.episodes()],
                ["City of Howls", "DON'T MAKE ME BLUE"])
        r.check("episode 12 by its number, not its position",
                L.pick(show, episode=12).nice_title, "DON'T MAKE ME BLUE")
    for t, want in (("01", ""), ("Episode 01 - Leader of the Pack",
                                 "Leader of the Pack"),
                    ("S01E02 Second", "Second"), ("26v2", "")):
        r.check(f"display title for {t!r}", L.Episode(
            rel="x", show="s", season=None, season_name="", number=1,
            title=t).nice_title, want)

    for name, want in (("[Grp] Tom's Tavern - 05 [1080p].mkv", 5),
                       ("Toms.Tavern.S01E07.1080p.mkv", 7),
                       ("Episode 12 - Title.mkv", 12),
                       ("03 - Gray Wolf.mkv", 3),
                       ("Tom's Tavern - 26v2.mkv", 26),
                       ("Behind the scenes.mp4", None)):
        r.check(f"episode number in {name!r}", L.episode_number(name), want)
    r.check("season folders", [L.season_number(x) for x in
                               ("Season 3", "S02", "Specials", "OVA",
                                "Misc")], [3, 2, 0, 0, None])


PROBE = {"format": {"duration": "1440.5"}, "streams": [
    {"codec_type": "video", "width": 1920, "height": 1080},
    {"codec_type": "audio", "codec_name": "flac",
     "tags": {"language": "jpn"}, "disposition": {"default": 1}},
    {"codec_type": "audio", "codec_name": "aac",
     "tags": {"language": "eng"}},
    {"codec_type": "subtitle", "codec_name": "ass",
     "tags": {"language": "eng", "title": "Signs & Songs"}},
    {"codec_type": "subtitle", "codec_name": "ass",
     "tags": {"language": "eng", "title": "Full Subtitles"}}]}


def suite_tracks(r: Results) -> None:
    info = V.parse_probe(PROBE)
    r.check("probe: duration and size", (info.duration, info.width,
                                         info.height), (1440.5, 1920, 1080))
    r.check("audio indexes count audio streams only",
            [(t.index, t.language) for t in info.audio],
            [(0, "jpn"), (1, "eng")])
    a, s = V.choose_tracks(info)
    r.check("unasked: English audio, no subtitles needed",
            (a.language, s), ("eng", None))
    a, s = V.choose_tracks(info, language="Japanese")
    r.check("Japanese: subtitles on by themselves, the full ones",
            (a.language, s.title), ("jpn", "Full Subtitles"))
    a, s = V.choose_tracks(info, language="Japanese", subtitles=False)
    r.check("...unless asked not to", s, None)
    jp_only = V.parse_probe({"streams": [
        {"codec_type": "audio", "tags": {"language": "jpn"}},
        {"codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng"}}]})
    a, s = V.choose_tracks(jp_only)
    r.ok("Japanese-only release: never played without subtitles",
         a.language == "jpn" and s is not None)
    a, s = V.choose_tracks(info, language="klingon")
    r.ok("a language it doesn't have: the default track, not silence",
         a is not None and a.language == "jpn")

    q = QUALITIES["1080p"]
    args = V.av_args("/v/Tom's Tavern/ep.mkv", start=300.0, fifo="/t/a.pcm",
                     q=q, audio=info.audio[0], sub=info.subs[1],
                     sub_path="/t/input.mkv")
    vf = args[args.index("-vf") + 1]
    r.ok("seeks before the input (fast)",
         args.index("-ss") < args.index("-i")
         and args[args.index("-ss") + 1] == "300.000")
    r.ok("the chosen audio track, padded across gaps",
         "0:a:0" in args and "aresample=async=1" in args)
    r.ok("subtitles from the plain-named copy, timed for the seek",
         vf.startswith("setpts=PTS+300.000/TB,subtitles=/t/input.mkv:si=1,"
                       "setpts=PTS-STARTPTS,scale=1920:1080"))
    r.ok("the real path (with its apostrophe) never goes into a filter",
         "Tom" not in vf)
    pgs = V.Track(0, "eng", "", "hdmv_pgs_subtitle")
    args = V.av_args("/v/x.mkv", start=0, fifo="/t/a.pcm", q=q,
                     audio=info.audio[0], sub=pgs)
    r.ok("picture subtitles are overlaid", "-filter_complex" in args
         and "[0:v:0][0:s:0]overlay" in args[args.index("-filter_complex")
                                             + 1])
    r.check("a 23.976 fps episode streams at its own rate, not 30",
            round(V.out_fps(V.MediaInfo(fps=24000 / 1001)), 3), 23.976)
    r.check("60 fps comes down to 30", V.out_fps(V.MediaInfo(fps=59.94)),
            30.0)
    r.check("unknown: 30", V.out_fps(V.MediaInfo()), 30.0)
    film_args = V.av_args("/v/x.mp4", start=0, fifo="/t/a.pcm", q=q,
                          audio=info.audio[0], sub=None, fps=24000 / 1001)
    r.ok("...and the filter says so", "fps=23.976" in
         film_args[film_args.index("-vf") + 1])
    r.check("the probe reads the frame rate", round(V.parse_probe(
        {"streams": [{"codec_type": "video", "width": 1440,
                      "height": 1080, "r_frame_rate": "24000/1001"}]})
        .fps, 3), 23.976)
    silent = V.av_args("/v/x.mkv", start=0, fifo=None, q=q, audio=None,
                       sub=None)
    r.ok("no audio track: picture only", "s16le" not in silent)

    for text, now, want in (("12:30", 0, 750), ("1:02:03", 0, 3723),
                            ("90 seconds", 0, 90), ("5 minutes", 0, 300),
                            ("1h 5m", 0, 3900), ("forward 30 seconds",
                                                 100, 130),
                            ("back 2 minutes", 300, 180), ("+45", 10, 55),
                            ("back 10 minutes", 60, 0), ("soon", 0, None)):
        r.check(f"time {text!r}", V.parse_time(text, now), want)
    r.check("times shown", [V.fmt_time(x) for x in (65, 3723)],
            ["1:05", "1:02:03"])


# ---------------------------------------------------------------- player

class _Proc:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed, self.returncode = True, -9

    async def communicate(self):
        return b"", b""


class _Client:
    def __init__(self):
        self.is_playing, self.paused = False, False

    async def play(self, source, after=None):
        self.source, self.after, self.is_playing = source, after, True

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class _Session:
    def __init__(self, cid):
        self.channel_id, self.channel_name = cid, "Movie Night"
        self.client = _Client()
        self.closers = []
        self.source_url = self.label = ""

    @property
    def playing(self):
        return self.client.is_playing

    def cancel_watcher(self):
        pass


class _Voice:
    def __init__(self):
        self.sessions, self.stopped = {}, []

    async def connect(self, gid, cid):
        return self.sessions.setdefault(gid, _Session(cid))

    def get(self, gid):
        return self.sessions.get(gid)

    async def stop(self, gid):
        self.stopped.append(gid)
        self.sessions[gid].client.is_playing = False

    async def disconnect(self, gid):
        self.sessions.pop(gid, None)
        return True


class _Active:
    def __init__(self, gid, cid, kind, quality):
        self.guild_id, self.channel_id, self.kind = gid, cid, kind
        self.quality, self.frames = quality, 0


class _Streams:
    def __init__(self):
        self.active, self.started, self.swaps, self.stops = None, [], [], []

    async def start(self, gid, cid, frames, **kw):
        self.started.append(kw)
        self.active = _Active(gid, cid, kw["kind"], kw.get("quality"))
        return self.active, []

    async def swap(self, gid, frames, **kw):
        if self.active is None:
            return False
        self.swaps.append(kw)
        return True

    async def stop(self, reason="stopped"):
        self.stops.append(reason)
        self.active = None


class _Source:
    def __init__(self, path, **kw):
        self.path = path


def _player(root, *, info=None, settings=None):
    from corbin.core.settings import ChatSettings

    settings = settings or ChatSettings(Path(root) / "chat.json")
    notes, spawned = [], []

    async def probe(path):
        return info or V.parse_probe(PROBE)

    async def spawn(args, **kw):
        spawned.append(args)
        return _Proc()

    async def card(*a):
        return None

    async def notify(cid, text):
        notes.append(text)

    vp = V.VideoPlayer(Path(root) / "video", _Streams(), _Voice(),
                       settings=settings, probe_fn=probe, spawn=spawn,
                       audio_source=_Source, card=card, notify=notify)
    vp.notes, vp.spawned = notes, spawned
    return vp


def suite_player(r: Results) -> None:
    V.ENDED_SETTLE = 0
    with tempfile.TemporaryDirectory() as tmp:
        _tree(Path(tmp) / "video")
        vp = _player(tmp)
        show = L.find_show(vp.shows(), "Tom's Tavern")

        async def main():
            p = await vp.play("g1", "vc1", show, L.pick(show, episode=1),
                              notify_to="vc1")
            out = {"started": vp.streams.started[0], "p": p}
            out["prog0"] = vp.progress("g1", "Tom's Tavern")
            out["pause"] = (vp.pause("g1"), p.client.paused,
                            vp.pause("g1"), p.client.paused)
            # The episode finishes: the sound ends near the end.
            p.start = p.info.duration - 10
            p.client.is_playing = False
            p.client.after(None)
            await asyncio.sleep(0.05)
            out["after_end"] = vp.playing("g1")
            out["notes"] = list(vp.notes)
            # Stopped part way in: progress kept for "continue".
            q = vp.playing("g1")
            q.start = 600
            await vp.stop("g1", "test")
            out["resume"] = vp.resume_point("g1", show)
            return out
        out = asyncio.run(main())
        p = out["p"]
        r.ok("plays as a held video stream on the sound's clock, at 1080p "
             "for a 1080p file", out["started"]["kind"] == "video"
             and out["started"]["clock"] is not None
             and out["started"]["hold"] and p.audio.language == "eng")
        r.ok("progress is saved from the start",
             out["prog0"]["episode"] == p.episode.rel)
        r.check("pause and resume hold the player, no restart",
                out["pause"], (True, True, False, False))
        r.ok("episode over, autoplay on: the next one starts, and the "
             "channel's told", out["after_end"] is not None
             and out["after_end"].episode.number == 2
             and "Up next" in out["notes"][-1])
        ep, pos = out["resume"]
        r.ok("stopped 10 minutes in: 'continue' picks up there",
             ep.number == 2 and abs(pos - 600) < 1)

    with tempfile.TemporaryDirectory() as tmp:
        _tree(Path(tmp) / "video")
        vp = _player(tmp)
        show = L.find_show(vp.shows(), "Tom's Tavern")

        async def early():
            p = await vp.play("g1", "vc1", show, L.pick(show, episode=1))
            p.start = 300                    # dies 5 minutes in
            p.client.is_playing = False
            p.client.after(None)
            await asyncio.sleep(0.05)
            return p, vp.playing("g1")
        p, again = asyncio.run(early())
        r.ok("dying part way through: restarted where it was, same "
             "episode, swapped into the same picture",
             again is not None and again is not p
             and again.episode.rel == p.episode.rel
             and abs(again.start - 298) < 1 and vp.streams.swaps)

    with tempfile.TemporaryDirectory() as tmp:
        _tree(Path(tmp) / "video")
        vp = _player(tmp)
        show = L.find_show(vp.shows(), "Tom's Tavern")

        async def replaced():
            p = await vp.play("g1", "vc1", show, L.pick(show, episode=1))
            p.client.after(None)            # a song took over the sound
            await asyncio.sleep(0.05)
            return vp.playing("g1")
        r.check("a song starting replaces the video quietly",
                asyncio.run(replaced()), None)

    with tempfile.TemporaryDirectory() as tmp:
        _tree(Path(tmp) / "video")
        vp = _player(tmp, info=V.parse_probe({
            "format": {"duration": "1400"}, "streams": [
                {"codec_type": "video", "width": 1280, "height": 720},
                {"codec_type": "audio", "tags": {"language": "eng"}}]}))
        show = L.find_show(vp.shows(), "Tom's Tavern")

        class Rules:
            def quality(self, gid):
                return QUALITIES["1080p"]
        vp.rules = Rules()
        p = asyncio.run(vp.play("g1", "vc1", show, L.pick(show, episode=1)))
        r.check("a 720p file on a 1080p server isn't upscaled",
                p.quality.name, "720p")

    with tempfile.TemporaryDirectory() as tmp:
        _touch(Path(tmp) / "video", "Happy Trails.mp4")
        _touch(Path(tmp) / "video", "Another Film.mp4")
        vp = _player(tmp)
        film = L.find_show(vp.shows(), "Happy Trails")

        async def film_ends():
            p = await vp.play("g1", "vc1", film, film.episodes()[0],
                              notify_to="vc1")
            p.start = p.info.duration - 5
            p.client.is_playing = False
            p.client.after(None)
            await asyncio.sleep(0.05)
            return vp.playing("g1"), list(vp.notes)
        now, notes = asyncio.run(film_ends())
        r.ok("a film ends: stops, never on to another loose file, and "
             "says the film's over (not 'last episode')", now is None
             and notes[-1] == "🎬 That's the end of Happy Trails.")
    V.ENDED_SETTLE = 1.0


def suite_monitor(r: Results) -> None:
    saved = V.FROZEN_AFTER, V.MONITOR_EVERY, V.MONITOR_GRACE, V.ENDED_SETTLE
    V.FROZEN_AFTER, V.MONITOR_EVERY, V.MONITOR_GRACE = 0.2, 0.05, 0.0
    V.ENDED_SETTLE = 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _tree(Path(tmp) / "video")
            vp = _player(tmp)
            show = L.find_show(vp.shows(), "Tom's Tavern")

            async def paused():
                p = await vp.play("g1", "vc1", show, L.pick(show, episode=1))
                vp.pause("g1")
                await asyncio.sleep(0.5)    # frames don't move: it's paused
                return p, vp.playing("g1")
            p, still = asyncio.run(paused())
            r.ok("a pause is not a freeze", still is p and not p.proc.killed)

        with tempfile.TemporaryDirectory() as tmp:
            _tree(Path(tmp) / "video")
            vp = _player(tmp)
            show = L.find_show(vp.shows(), "Tom's Tavern")

            async def frozen():
                p = await vp.play("g1", "vc1", show, L.pick(show, episode=1))
                p.start = 120

                async def flow_after_restart():
                    while True:
                        if vp.streams.swaps:
                            vp.streams.active.frames += 3
                        await asyncio.sleep(0.03)
                t = asyncio.ensure_future(flow_after_restart())
                await asyncio.sleep(0.6)
                t.cancel()
                return p, vp.playing("g1")
            p, back = asyncio.run(frozen())
            r.ok("frozen while playing: ffmpeg killed, restarted at the same "
                 "spot", p.proc.killed and back is not None
                 and back is not p and abs(back.start - 118) < 1)
    finally:
        V.FROZEN_AFTER, V.MONITOR_EVERY, V.MONITOR_GRACE, V.ENDED_SETTLE = \
            saved


# --------------------------------------------------------------- commands

def suite_commands(r: Results) -> None:
    from corbin.commands import music_cmds as M
    from corbin.commands import video_cmds as C
    from corbin.core.context import (Channel, CommandContext, Embed, Guild,
                                     Member, Platform, Surface, User)
    from corbin.core.reactions import Panel

    class _R:
        async def send(self, r): return "m"

    class Panels:
        def __init__(self): self.found = None
        def latest(self, kind, gid): return self.found

    V.ENDED_SETTLE = 0
    with tempfile.TemporaryDirectory() as tmp:
        _tree(Path(tmp) / "video")
        vp = _player(tmp)
        panels = Panels()

        def ctx():
            return CommandContext(
                platform=Platform.FLUXER, surface=Surface.AI,
                invoker=Member(user=User(id="u1", name="Quill"),
                               guild_id="g1"),
                channel=Channel(id="c1", name="movies", guild_id="g1"),
                guild=Guild(id="g1", name="Den"), responder=_R(),
                extras={"videos": vp, "panels": panels, "voice": vp.voice,
                        "voice_states": {"u1": "vc1"}})

        def call(fn, **kw):
            return asyncio.run(fn(ctx(), **kw))

        lst = call(C.videos)
        r.ok("the shows, with episode counts",
             "**Tom's Tavern** · 3 episodes" in lst.description
             and "**Other Show** · 2 seasons" in lst.description)
        eps = call(C.videos, show="toms tavern")
        r.ok("a show's episodes, numbered, as a list panel",
             isinstance(eps, Embed) and "**3.**" in eps.description
             and eps.controls.kind == "video_list")
        panels.found = ("m", Panel(kind="video_list", channel_id="c1",
                                   guild_id="g1", emojis=(),
                                   state=dict(eps.controls.state)))
        card = call(C.video_play, episode="2")
        r.ok("'watch 2' plays the second in the list here",
             card.title == "🎬 Tom's Tavern: Episode 2"
             and card.controls.kind == "video")
        r.ok("the card shows the tracks and the size",
             {f.name: f.value for f in card.fields}["Audio"] == "English")
        r.check("pause through the music's own 'pause'",
                call(M.pause_music)[:10], "Paused at ")
        r.check("...and resume", call(M.resume_music),
                "Resumed Tom's Tavern Episode 2.")
        r.check("next episode", call(C.video_next).title,
                "🎬 Tom's Tavern: Episode 10")
        r.check("past the last", call(C.video_next),
                "That's the last episode.")
        r.ok("skip to a time", call(C.video_seek, position="12:30")
             == "Jumped to 12:30.")
        r.ok("not a time", "I don't follow" in call(C.video_seek,
                                                    position="soonish"))
        r.ok("tracks listed when nothing's asked", "Audio tracks: "
             "Japanese, English" in call(C.video_tracks))
        stop = call(C.video_stop)
        r.ok("stop says where, and how to continue",
             stop.startswith("Stopped Tom's Tavern Episode 10 at ")
             and "continue Tom's Tavern" in stop)
        cont = call(C.video_play, show="Tom's Tavern")
        r.ok("'continue Tom's Tavern' picks up where it stopped",
             cont.title == "🎬 Tom's Tavern: Episode 10"
             and "Picking up at" in cont.description)
        call(C.video_stop)
        redirected = call(M.play, query="Tom's Tavern episode 1")
        r.ok("'play Tom's Tavern episode 1' via the music command goes to "
             "the library, not YouTube", isinstance(redirected, Embed)
             and redirected.title == "🎬 Tom's Tavern: Episode 1")
        r.check("an unknown show", call(C.video_play, show="Bebop"),
                "I don't have a show called “Bebop”. I have: Other Show, "
                "Tom's Tavern.")
        r.check("no such episode", call(C.video_play, show="Other Show",
                                        episode="s01e09"),
                "Other Show has no season 1 episode 9.")

    with tempfile.TemporaryDirectory() as tmp:
        vp = _player(tmp)
        r.ok("an empty folder says where to put shows",
             "nothing in the video folder yet" in asyncio.run(
                 C.videos(CommandContext(
                     platform=Platform.FLUXER, surface=Surface.AI,
                     invoker=Member(user=User(id="u1", name="W"),
                                    guild_id="g1"),
                     channel=Channel(id="c1", name="x", guild_id="g1"),
                     guild=Guild(id="g1", name="Den"), responder=_R(),
                     extras={"videos": vp}))))
    V.ENDED_SETTLE = 1.0

    from corbin.core.permissions import Perm
    from corbin.core.registry import registry
    specs = {s.name: s for s in registry.ai_invocable()}
    names = ("videos", "video-play", "video-next", "video-pause",
             "video-seek", "video-tracks", "video-now", "video-autoplay",
             "video-page")
    r.ok("all behind the streaming allowlist",
         all(specs[n].feature == "streaming" for n in names))
    r.ok("controls need Connect, like the music",
         all(specs[n].permission == Perm.CONNECT for n in
             ("video-play", "video-next", "video-pause", "video-seek",
              "video-stop")))


SUITES = [
    ("video library: scanning and picking", suite_library),
    ("video library: tracks, ffmpeg, times", suite_tracks),
    ("video library: playing", suite_player),
    ("video library: freeze monitor", suite_monitor),
    ("video library: commands", suite_commands),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
