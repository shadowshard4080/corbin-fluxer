"""Offline suite for the music video: the audio clock, the video format,
the ffmpeg command, clock-paced frames, swapping between songs, and the
player hooks. LiveKit and yt-dlp are stubbed; no ffmpeg runs."""
from __future__ import annotations
import asyncio, logging, types
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

import test_video  # noqa: F401  (installs the LiveKit stub)
from test_video import _world, frames

from corbin.core import video as vid
from corbin.core.audio import VIDEO_FORMAT, pick_video_format
from corbin.core.music_video import (MusicVideo, NoVideo, is_video_page,
                                     video_args)
from corbin.core.voice import audio_position
import corbin.core.music_video as _mv

# No measured start-up offline: the video starts where the clock says.
_mv.STARTUP_GUESS = 0.0


def suite_clock(r: Results) -> None:
    r.check("no clock, no position", audio_position(object()), None)
    source = types.SimpleNamespace(queued_duration=0.5)
    client = types.SimpleNamespace(_corbin_audio_clock={
        "source": source, "rate": 48000, "captured": 48000 * 10})
    r.check("captured less what's still queued", audio_position(client),
            9.5)
    source.queued_duration = 0.0
    r.check("an empty queue is the full count", audio_position(client),
            10.0)
    client._corbin_audio_clock["captured"] = 0
    source.queued_duration = 1.0
    r.check("never negative", audio_position(client), 0.0)


def suite_format(r: Results) -> None:
    r.ok("1080p at most", "height<=1080" in VIDEO_FORMAT
         and "height<=720" not in VIDEO_FORMAT)
    r.ok("H.264 first (cheapest to decode)",
         VIDEO_FORMAT.startswith("bestvideo[height<=1080][fps<=30]"
                                 "[vcodec^=avc1]"))
    merged = {"requested_formats": [
        {"vcodec": "none", "acodec": "opus", "url": "a"},
        {"vcodec": "avc1", "url": "v", "http_headers": {"X": "1"}}]}
    r.check("the video half of a merged format", pick_video_format(merged),
            ("v", {"X": "1"}))
    r.check("a single video format",
            pick_video_format({"vcodec": "vp9", "url": "u"}), ("u", {}))
    r.check("audio only: no video",
            pick_video_format({"vcodec": "none", "url": "u"}), None)
    r.check("junk", pick_video_format(None), None)

    r.ok("youtube pages have video",
         is_video_page("https://www.youtube.com/watch?v=x"))
    r.ok("short links too", is_video_page("https://youtu.be/x"))
    r.ok("soundcloud doesn't",
         not is_video_page("https://soundcloud.com/a/b"))

    args = video_args("http://v", {"User-Agent": "UA"}, 42.5)
    r.ok("seeks to the song's position, before -i",
         args.index("-ss") < args.index("-i") and "42.500" in args)
    r.ok("no audio from the video", "-an" in args)
    vf = args[args.index("-vf") + 1]
    r.ok("scaled and padded to 1280x720",
         "scale=1280:720" in vf and "pad=1280:720" in vf)
    r.ok("30 fps, I420", "fps=30" in vf and "yuv420p" in vf)
    r.ok("headers passed", "User-Agent: UA\r\n" in args)
    r.ok("no seek from the start",
         "-ss" not in video_args("http://v", {}, 0.0))
    r.ok("or for a live stream", "-ss" not in video_args("http://v", {},
                                                          None))


async def pts_frames(points):
    for p in points:
        yield p, b"\0" * 10
        await asyncio.sleep(0)


def suite_pacing(r: Results) -> None:
    async def main():
        mgr, voice, ids, sent = _world()
        now = [5.0]
        # The first frame of a feed always goes out (a still), so the
        # late one comes second here.
        stream, _ = await mgr.start("g1", "v1", pts_frames(
            [5.0, 4.0, 5.02, 5.1]), label="m", clock=lambda: now[0],
            hold=True, kind="music")
        await asyncio.sleep(0.05)
        r.check("a frame far behind the song is dropped", stream.dropped, 1)
        r.check("on-time frames are shown", stream.frames, 1)
        now[0] = 5.2
        await asyncio.sleep(0.2)
        r.check("the rest show as the clock reaches them", stream.frames,
                3)
        r.ok("held when the video runs out (between songs)",
             mgr.active is stream)

        # swap: new frames, same publication
        published = len(voice._sessions["g1"].client._room
                        .local_participant.published)
        now[0] = 0.0
        ok = await mgr.swap("g1", pts_frames([0.0]), label="next song")
        await asyncio.sleep(0.1)
        r.ok("swap feeds the running stream", ok and stream.frames == 4)
        r.check("without republishing", len(voice._sessions["g1"].client
                                             ._room.local_participant
                                             .published), published)
        r.check("and relabels it", stream.label, "next song")
        r.ok("no swap where nothing streams",
             not await mgr.swap("g2", pts_frames([0.0])))
        await mgr.stop()

        # A paused song (frozen clock) freezes the picture.
        mgr, *_ = _world()
        stream, _ = await mgr.start("g1", "v1", pts_frames([1.0, 1.1]),
                                    label="m", clock=lambda: 0.5, hold=True)
        await asyncio.sleep(0.15)
        # The first frame goes out at once as a still (no grey tile while
        # the song catches up); the next waits for the clock.
        r.check("paused: a still, then the next frame waits",
                stream.frames, 1)
        await mgr.stop()

    asyncio.run(main())


class Player:
    def __init__(self, track=None):
        self.p = types.SimpleNamespace(current=track)

    def get(self, gid):
        return self.p


class Extractor:
    def __init__(self, result=("http://video", {}), fail=False):
        self.result, self.fail, self.calls = result, fail, 0

    async def video_stream(self, url):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bot check")
        return self.result


def _track(title="Song", url="https://www.youtube.com/watch?v=x"):
    return types.SimpleNamespace(title=title, webpage_url=url,
                                 is_live=False)


def suite_music_video(r: Results) -> None:
    import corbin.core.music_video as mvmod

    async def fake_frames(url, headers, start, **_kw):
        yield (start or 0.0), b"\0" * 10

    mvmod.video_frames = fake_frames

    async def main():
        mgr, voice, ids, sent = _world()
        await voice.connect("g1", "v1")      # the music's session
        told = []

        async def notify(cid, text):
            told.append(text)

        mv = MusicVideo(mgr, Player(_track()), voice, Extractor(),
                        notify=notify)
        stream, _ = await mv.start("g1")
        r.ok("on for what's playing", mv.active("g1"))
        r.check("as a music stream", stream.kind, "music")
        r.ok("held between songs", stream.hold)
        r.ok("synced to the audio clock", stream.clock is not None)
        r.check("named after the song", stream.label, "music video: Song")
        try:
            await mv.start("g1")
            r.ok("twice is refused", False)
        except RuntimeError:
            r.ok("twice is refused", True)

        await mv.track_started("g1", _track("Next"))
        r.check("the next track's video swaps in", mgr.active.label,
                "music video: Next")
        await mv.track_started("g1", _track("Sc", "https://soundcloud.com/x"))
        r.ok("a track with no video ends it", not mv.active("g1"))
        r.ok("and says why", told and "no video" in told[-1].lower()
             or "video off" in told[-1].lower())

        await mv.start("g1")
        await mv.queue_ended("g1")
        r.ok("the music ending ends the video", not mv.active("g1"))
        r.ok("with a notice", "music's over" in told[-1])

        nothing = MusicVideo(mgr, Player(None), voice, Extractor())
        try:
            await nothing.start("g1")
            r.ok("nothing playing: refused", False)
        except NoVideo:
            r.ok("nothing playing: refused", True)

        audio_only = MusicVideo(mgr, Player(_track(
            url="https://soundcloud.com/x")), voice, Extractor())
        try:
            await audio_only.start("g1")
            r.ok("no video page: refused", False)
        except NoVideo:
            r.ok("no video page: refused", True)

        broken = MusicVideo(mgr, Player(_track()), voice,
                            Extractor(fail=True))
        try:
            await broken.start("g1")
            r.ok("extraction failure: refused, said plainly", False)
        except NoVideo as exc:
            r.ok("extraction failure: refused, said plainly",
                 "couldn't get the video" in str(exc))

        # Busy in another server: refused before extracting anything.
        await mgr.start("g2", "v9", frames(), label="t")
        ext = Extractor()
        other = MusicVideo(mgr, Player(_track()), voice, ext)
        from corbin.core.video import StreamBusy
        try:
            await other.start("g1")
            r.ok("busy elsewhere: refused", False)
        except StreamBusy:
            r.ok("busy elsewhere: refused", True)
        r.check("without a wasted extraction", ext.calls, 0)
        await mgr.stop()

    asyncio.run(main())


def suite_player_hooks(r: Results) -> None:
    from corbin.core.player import PlayerManager

    async def main():
        fired = []

        async def started(gid, track):
            fired.append(("track", gid, track))

        async def ended(gid):
            fired.append(("end", gid))

        voice = types.SimpleNamespace(get=lambda gid: types.SimpleNamespace(
            channel_id="v1"))
        pm = PlayerManager(voice, None)
        pm.on_queue_end.append(ended)
        pm.on_track_started.append(started)
        await pm.advance("g1")           # empty queue
        await asyncio.sleep(0)
        r.check("an empty queue fires the end hook", fired, [("end", "g1")])
        pm._fire(pm.on_track_started, "g1", "t")
        await asyncio.sleep(0)
        r.check("track hooks get guild and track", fired[-1],
                ("track", "g1", "t"))

    asyncio.run(main())


def suite_card(r: Results) -> None:
    from corbin.commands.music_cmds import track_embed, video_ok

    class Features:
        def __init__(self, on):
            self.on = on

        def allowed(self, gid, feature):
            return self.on and feature == "streaming"

    t = _track()
    r.ok("📺 offered where streaming's allowed",
         video_ok(Features(True), "g1", t))
    r.ok("not where it isn't", not video_ok(Features(False), "g1", t))
    r.ok("not for a track with no video", not video_ok(
        Features(True), "g1", _track(url="https://soundcloud.com/x")))
    track = types.SimpleNamespace(title="S", webpage_url=t.webpage_url,
                                  uploader="", pretty_duration="3:00",
                                  requester_name="", thumbnail="")
    card = track_embed(track, guild_id="g1", video=True)
    r.ok("the card carries 📺", "📺" in card.controls.emojis())
    plain = track_embed(track, guild_id="g1")
    r.ok("and doesn't otherwise", "📺" not in plain.controls.emojis())


def suite_restart(r: Results) -> None:
    """Live 2026-10-17: the video's connection died mid-song, the music
    carried on, and the picture froze, because a held stream treated any
    end of frames as the song finishing."""
    import corbin.core.music_video as mvmod

    async def short_frames(url, headers, start, **_kw):
        # Dies after one frame, like a dropped connection.
        yield (start or 0.0), b"\0" * 10

    mvmod.video_frames = short_frames
    # The fake clock stands still, so a frame START_LEAD ahead would wait
    # for it forever.
    mvmod.START_LEAD = 0.0

    async def main():
        mgr, voice, ids, sent = _world()
        session = await voice.connect("g1", "v1")
        told = []

        async def notify(cid, text):
            told.append(text)

        track = _track()
        track.duration = 240.0
        player = Player(track)
        ext = Extractor()
        mv = MusicVideo(mgr, player, voice, ext, notify=notify)
        position = [60.0]
        mv._clock = lambda gid: (lambda: position[0])
        await mv.start("g1")
        await asyncio.sleep(0.2)
        r.ok("a video that dies mid-song is fetched again",
             ext.calls >= 2)
        for _ in range(40):
            if not mv.active("g1"):
                break
            await asyncio.sleep(0.05)
        r.check("restarted MAX_RESTARTS times (each a fresh link)",
                ext.calls, 1 + mvmod.MAX_RESTARTS)
        r.ok("then gives up", not mv.active("g1"))
        r.ok("saying the music carries on",
             told and "carries on" in told[-1])

        ext2 = Extractor()
        mv2 = MusicVideo(mgr, Player(track), voice, ext2, notify=notify)
        position[0] = 238.0
        mv2._clock = lambda gid: (lambda: position[0])
        await mv2.start("g1")
        await asyncio.sleep(0.2)
        r.check("near the song's end it isn't restarted", ext2.calls, 1)
        r.ok("it holds for the next song", mv2.active("g1"))
        await mgr.stop()

    asyncio.run(main())


def suite_restart_and_prefetch(r: Results) -> None:
    import sys, time
    import corbin.core.music_video as mvmod
    import corbin.core.player as playermod
    from corbin.core.player import PlayerManager

    # play_now imports FFmpegPCMAudio from fluxer.voice.
    if "fluxer.voice" not in sys.modules:
        fl = sys.modules.setdefault("fluxer", types.ModuleType("fluxer"))
        fv = types.ModuleType("fluxer.voice")
        fv.FFmpegPCMAudio = lambda *a, **k: ("src", a, k)
        fl.voice = fv
        sys.modules["fluxer.voice"] = fv
    playermod.IMMEDIATE_FAIL_SECS = 0.05

    class Client:
        is_paused = False

        def __init__(self):
            self.plays = 0

        async def play(self, source, after=None):
            self.plays += 1

        def resume(self):
            pass

    session = types.SimpleNamespace(channel_id="v1", client=Client(),
                                    playing=True)

    class Voice:
        async def connect(self, gid, cid):
            return session

        def get(self, gid):
            return session

    async def main():
        pm = PlayerManager(Voice(), None)
        fired = []

        async def hook(gid, track):
            fired.append((time.monotonic(), track))

        pm.on_track_started.append(hook)
        track = types.SimpleNamespace(title="Song", url="u", duration=200,
                                      webpage_url="https://youtu.be/x")
        began = time.monotonic()
        await pm.play_now("g1", "v1", track)
        r.ok("the hook fires as the audio starts, not after the check",
             fired and fired[0][0] - began < playermod.IMMEDIATE_FAIL_SECS)
        r.check("restarting nothing does nothing",
                await PlayerManager(Voice(), None).restart("g9"), None)
        plays = session.client.plays
        again = await pm.restart("g1")
        r.ok("restart replays the current track",
             again is track and session.client.plays == plays + 1)
        await asyncio.sleep(0)
        r.check("and fires the hook so the video restarts too", len(fired),
                2)

    asyncio.run(main())

    async def fake_frames(url, headers, start, **_kw):
        yield (start or 0.0), b"\0" * 10

    mvmod.video_frames = fake_frames
    mvmod.START_LEAD = 0.0

    async def links():
        mgr, voice, *_ = _world()
        await voice.connect("g1", "v1")
        nxt = _track("Next", "https://youtu.be/next")
        player = Player(_track())
        player.p.queue = [nxt]
        ext = Extractor()
        mv = MusicVideo(mgr, player, voice, ext)
        await mv.start("g1")
        await asyncio.sleep(0.05)
        r.check("the next song's video is looked up early", ext.calls, 2)
        await mv.track_started("g1", nxt)
        r.check("so its swap needs no lookup", ext.calls, 2)

        # A stale clock (the old song's) must not set the new start.
        mv._clock = lambda gid: (lambda: 120.0)
        starts = []

        async def record(url, headers, start, **_kw):
            starts.append(start)
            yield (start or 0.0), b"\0" * 10

        mvmod.video_frames = record
        await mv.track_started("g1", _track("Third", "https://youtu.be/3"))
        await asyncio.sleep(0.05)   # the feeder pulls the first frame
        r.ok("a track change starts the video near 0, not where the old "
             # [0]: this one-frame fake then ends at once and the
             # mid-song restart (stale clock) appends its own start.
             "song was", starts and starts[0] < 5)
        await mgr.stop()

    asyncio.run(links())


def suite_footer(r: Results) -> None:
    import corbin.core.music_video as mvmod
    from corbin.commands.music_cmds import player_status, track_embed
    from corbin.core.reactions import Controls, ReactionPanels

    async def endless(url, headers, start, **_kw):
        # Keeps going, like a real video: one that ends would set off
        # the mid-song restart and flip the state more than this checks.
        while True:
            yield (start or 0.0), b"\0" * 10
            await asyncio.sleep(0.01)

    mvmod.video_frames = endless
    mvmod.START_LEAD = 0.0

    async def main():
        mgr, voice, *_ = _world()
        await voice.connect("g1", "v1")
        seen = []
        mv = MusicVideo(mgr, Player(_track()), voice, Extractor())
        mv._clock = lambda gid: (lambda: 100.0)

        async def refreshed(gid):
            seen.append(mv.status_line(gid))

        mv.on_status = refreshed
        r.check("off: no video line", mv.status_line("g1"), "")
        await mv.start("g1")
        await asyncio.sleep(0.1)
        r.ok("starting is shown first", seen and "Starting" in seen[0])
        r.ok("then on, once a frame is out",
             "Video on" in mv.status_line("g1") and "Video on" in seen[-1])
        await mv.stop("g1")
        r.check("off again after stop", mv.status_line("g1"), "")
        r.check("and the card was told each time", len(seen), 3)

    asyncio.run(main())

    playing = types.SimpleNamespace(paused=False, current=object(),
                                    loop_track=False, loop_queue=False)
    paused = types.SimpleNamespace(paused=True, current=object(),
                                   loop_track=False, loop_queue=False)
    r.ok("playing says so", player_status(playing).startswith("▶️"))
    r.ok("paused says so", player_status(paused).startswith("⏸️"))

    track = types.SimpleNamespace(title="S", webpage_url="", uploader="",
                                  pretty_duration="3:00", requester_name="",
                                  thumbnail="")
    card = track_embed(track, guild_id="g1", player=playing,
                       status="📺 Video on", note="Resumed by Lux")
    lines = card.footer.splitlines()
    r.ok("footer: state, video, who, then the legend",
         lines[0].startswith("▶️") and lines[1] == "📺 Video on"
         and lines[2] == "Resumed by Lux" and lines[-1].startswith("React"))
    busy = track_embed(track, guild_id="g1", player=playing,
                       activity="⏮️ Starting from the beginning…")
    r.ok("an action in progress replaces the state line",
         busy.footer.startswith("⏮️ Starting"))

    panels = ReactionPanels()

    async def attach():
        await panels.attach("m1", "c", "g1", Controls("music", [("⏯️", "")],
                                                      exclusive=False))
        await panels.attach("m2", "c", "g1", Controls("music", [("⏯️", "")],
                                                      exclusive=False))
        await panels.attach("m3", "c", "g2", Controls("music", [("⏯️", "")]))

    asyncio.run(attach())
    r.check("the newest card in the guild", panels.latest("music", "g1")[0],
            "m2")
    r.check("none of another kind", panels.latest("verse", "g1"), None)


def suite_own_link(r: Results) -> None:
    """The lookup was most of the wait for 📺: the video is picked from
    the track's own extraction instead."""
    import time
    from corbin.core.audio import pick_video_from_formats, track_from_info

    info = {"title": "S", "url": "audio", "webpage_url":
            "https://youtu.be/x", "formats": [
                {"url": "a", "vcodec": "none", "acodec": "opus"},
                {"url": "sb", "vcodec": "none", "protocol": "mhtml"},
                {"url": "v2160", "vcodec": "avc1", "acodec": "none",
                 "height": 2160, "fps": 30},
                {"url": "v1080", "vcodec": "avc1", "acodec": "none",
                 "height": 1080, "fps": 30},
                {"url": "hls1080", "vcodec": "avc1", "acodec": "none",
                 "height": 1080, "fps": 30, "tbr": 9000,
                 "protocol": "m3u8_native"},
                {"url": "v1080_60", "vcodec": "avc1", "acodec": "none",
                 "height": 1080, "fps": 60},
                {"url": "v720vp9", "vcodec": "vp9", "acodec": "none",
                 "height": 720, "fps": 30, "tbr": 2000},
                {"url": "v720avc", "vcodec": "avc1.4d401f", "acodec": "none",
                 "height": 720, "fps": 30, "tbr": 1500,
                 "http_headers": {"UA": "x"}},
                {"url": "v720_60", "vcodec": "avc1", "acodec": "none",
                 "height": 720, "fps": 60},
                # The one that was picked live: HLS, a bigger tbr figure.
                {"url": "hls720", "vcodec": "avc1.4d401f", "acodec": "none",
                 "height": 720, "fps": 30, "tbr": 4000,
                 "protocol": "m3u8_native"},
                {"url": "dashseg", "vcodec": "avc1", "acodec": "none",
                 "height": 720, "fps": 30, "tbr": 9000,
                 "protocol": "http_dash_segments"},
                {"url": "muxed360", "vcodec": "avc1", "acodec": "mp4a",
                 "height": 360, "fps": 30}]}
    r.check("1080p30 H.264 video-only over https wins (not 4K, not 60 fps, "
            "not HLS)", pick_video_from_formats(info), ("v1080", {}))
    r.check("nothing usable: None",
            pick_video_from_formats({"formats": [{"url": "a",
                                                  "vcodec": "none"}]}),
            None)
    track = track_from_info(info)
    r.check("the track keeps it", track.video_url, "v1080")

    async def main():
        mgr, voice, *_ = _world()
        await voice.connect("g1", "v1")
        ext = Extractor()
        mv = MusicVideo(mgr, Player(track), voice, ext)
        await mv.start("g1")
        r.check("📺 needs no lookup when the track has its video", ext.calls,
                0)
        await mgr.stop()
        track.extracted_at = time.monotonic() - 2 * 3600
        mv2 = MusicVideo(mgr, Player(track), voice, ext)
        await mv2.start("g1")
        r.check("a stale one is looked up again", ext.calls, 1)
        await mgr.stop()

    asyncio.run(main())


def suite_stuck_press(r: Results) -> None:
    """A hung ⏮️ handler left every button on the card dead (live)."""
    import corbin.core.reactions as rx
    from corbin.core.reactions import Controls, ReactionPanels

    rx.PRESS_TIMEOUT = 0.1
    panels = ReactionPanels()
    calls = []

    async def handler(panel, mid, emoji, uid):
        calls.append(emoji)
        if emoji == "⏮️":
            await asyncio.sleep(3600)       # wedged

    panels.on("music", handler)

    async def main():
        await panels.attach("m1", "c", "g1", Controls(
            "music", [("⏮️", ""), ("⏯️", "")]))
        await panels.press(message_id="m1", channel_id="c", user_id="u1",
                           emoji="⏮️", added=True)
        await panels.press(message_id="m1", channel_id="c", user_id="u2",
                           emoji="⏯️", added=True)

    asyncio.run(asyncio.wait_for(main(), 5))
    r.check("a stuck press times out and the next button still works",
            calls, ["⏮️", "⏯️"])


def suite_catch_up(r: Results) -> None:
    """Live: ffmpeg took ~7 s to open and seek, so every frame for 19 s
    arrived behind the song and was dropped."""
    import corbin.core.music_video as mvmod
    import corbin.core.video as vmod

    async def main():
        mgr, voice, *_ = _world()
        told = []

        async def behind(stream, secs):
            told.append(secs)

        late = [float(i) / 30 for i in range(100)]
        stream, _ = await mgr.start("g1", "v1", pts_frames(late), label="m",
                                    clock=lambda: 50.0, hold=True,
                                    on_behind=behind)
        await asyncio.sleep(0.3)
        r.ok("the first frame still goes out (a still, not grey)",
             stream.frames >= 1)
        r.check("frames far behind are dropped", stream.dropped, 99)
        r.check("and the owner is told once, with how far behind",
                len(told), 1)
        r.ok("about 48 s behind", told and 47 < told[0] < 50)
        await mgr.stop()

    asyncio.run(main())

    mv = MusicVideo(None, None, None, None)
    mv._startup = 4.0
    mv._measured(8.0)
    r.check("start-up estimate follows what's measured", mv._startup, 6.0)
    mv._measured(100.0)
    r.check("but is capped", mv._startup, mvmod.STARTUP_CAP)


def suite_stall_watchdog(r: Results) -> None:
    """A hung video ffmpeg (no frames, no exit) must end the frames, so
    MusicVideo._frames_ended can restart it (live, 2026-10-20: frozen)."""
    import corbin.core.music_video as MV
    from corbin.core import video as V

    class Hung:
        def __init__(self, frames):
            self.stdout = asyncio.StreamReader()
            for _ in range(frames):
                self.stdout.feed_data(b"\x00" * V.FRAME_BYTES)
            self.returncode = None
            self.killed = False

        def kill(self):
            self.killed, self.returncode = True, -9

        async def communicate(self):
            return b"", b""

    procs = []

    async def fake_exec(*args, **kw):
        procs.append(Hung(3))
        return procs[-1]

    old = (V.FRAME_STALL_TIMEOUT, V.FIRST_FRAME_TIMEOUT,
           MV.asyncio.create_subprocess_exec)
    V.FRAME_STALL_TIMEOUT, V.FIRST_FRAME_TIMEOUT = 0.1, 0.2
    MV.asyncio.create_subprocess_exec = fake_exec
    try:
        async def go():
            return [pts async for pts, _ in MV.video_frames("u", {}, 60.0)]
        got = asyncio.run(asyncio.wait_for(go(), 5))
        r.check("its frames, at the song's position", got[0], 60.0)
        r.ok("then it ends instead of hanging, and ffmpeg is killed",
             len(got) == 3 and procs[0].killed)
    finally:
        (V.FRAME_STALL_TIMEOUT, V.FIRST_FRAME_TIMEOUT,
         MV.asyncio.create_subprocess_exec) = old


SUITES = [
    ("music video stall watchdog", suite_stall_watchdog),
    ("music video: catching up after a slow start", suite_catch_up),
    ("music video: a stuck press frees the card", suite_stuck_press),
    ("music video: the track's own video link", suite_own_link),
    ("music video: live footer", suite_footer),
    ("music video: restart and prefetch", suite_restart_and_prefetch),
    ("music video: dies mid-song", suite_restart),
    ("music video: audio clock", suite_clock),
    ("music video: format and ffmpeg", suite_format),
    ("music video: clock pacing and swap", suite_pacing),
    ("music video: on, off, queue", suite_music_video),
    ("music video: player hooks", suite_player_hooks),
    ("music video: the 📺 button", suite_card),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
