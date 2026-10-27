"""Offline suite for watching live TV (corbin/core/tv.py, Track T2)."""
from __future__ import annotations
import asyncio, logging, os, tempfile
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import iptv as I
from corbin.core import tv as T
from corbin.core.tv import TV, PlayProbe, TVError
from corbin.core.video import FRAME_BYTES
from test_iptv import T0, _raw


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


class _Proc:
    """An ffmpeg stand-in: ``frames`` frames on stdout, then EOF."""
    def __init__(self, frames=3):
        self.stdout = asyncio.StreamReader()
        for _ in range(frames):
            self.stdout.feed_data(b"\x00" * FRAME_BYTES)
        self.stdout.feed_eof()
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def communicate(self):
        return b"", b""


class _Client:
    def __init__(self):
        self.played = []
        self.is_playing = False

    async def play(self, source, after=None):
        self.played.append(source)
        self.after = after
        self.is_playing = True


class _Session:
    def __init__(self, cid):
        self.channel_id, self.channel_name = cid, "Lounge"
        self.client = _Client()
        self.source_url, self.label = "", ""
        self.closers = []

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


class _Active:
    def __init__(self, gid, kind, cid="vc1", quality=None):
        from corbin.core.video import DEFAULT_QUALITY
        self.guild_id, self.kind, self.channel_id = gid, kind, cid
        self.quality = quality or DEFAULT_QUALITY


class _Streams:
    def __init__(self, fail=None):
        self.fail, self.started, self.stops, self.active = fail, [], [], None
        self.swaps = []

    async def start(self, gid, cid, frames, **kw):
        if self.fail:
            raise self.fail
        self.started.append(kw)
        self.frames = frames
        self.active = _Active(gid, kw["kind"], cid, kw.get("quality"))
        return self.active, []

    async def swap(self, gid, frames, **kw):
        if self.active is None or self.active.guild_id != gid:
            return False
        self.swaps.append(kw)
        self.frames = frames
        return True

    async def stop(self, reason="stopped"):
        self.stops.append(reason)
        self.active = None


class _Source:
    def __init__(self, path, **kw):
        self.path, self.kw = path, kw


def _catalogue(tmp):
    svc = I.IptvCatalogue(tmp, clock=_Clock(T0))
    svc.catalogue = I.build_catalogue(_raw(), now=T0)
    return svc


def _tv(cat, streams, voice, probes, *, player=None, notes=None,
        rules=None):
    spawned = []
    probes = list(probes)
    picked = []

    async def probe(url, s):
        return probes.pop(0) if probes else PlayProbe(False, "no more")

    async def pick(s, max_height=720):
        picked.append(max_height)
        return s.url

    async def card(title, subtitle, q):
        return None

    async def spawn(args, **kw):
        spawned.append(args)
        return _Proc()

    async def logo(channel, folder):
        return os.path.join(folder, "logo.png")

    async def notify(cid, text):
        (notes if notes is not None else []).append((cid, text))

    tv = TV(cat, streams, voice, player=player, notify=notify, probe=probe,
            pick=pick, spawn=spawn, audio_source=_Source, logo=logo,
            card=card, rules=rules)
    tv.spawned, tv.picked = spawned, picked
    return tv


OK_AV = PlayProbe(True, has_video=True, has_audio=True, height=720,
                  decoded=4.0)


def suite_args(r: Results) -> None:
    s = I.Stream(url="http://x/a.m3u8", channel="c", user_agent="UA/1",
                 referrer="https://ref/")
    a = T.av_args("http://x/v.m3u8", s, fifo="/tmp/p.pcm")
    r.ok("one ffmpeg: the pcm pipe, then raw frames on stdout",
         a.index("/tmp/p.pcm") < a.index("pipe:1")
         and a[a.index("/tmp/p.pcm") - 1] == "-y")
    r.ok("the stream's own agent and referrer, as the check sent them",
         a[a.index("-user_agent") + 1] == "UA/1"
         and "Referer: https://ref/\r\n" in a)
    r.ok("the chosen input url", a[a.index("-i") + 1] == "http://x/v.m3u8")
    plain = T.av_args("u", I.Stream(url="u", channel="c"), fifo=None)
    r.ok("no pipe without sound; the default agent otherwise",
         "s16le" not in plain and plain[plain.index("-user_agent") + 1]
         == I.DEFAULT_USER_AGENT)
    before, opts = T.sound_options("u", s)
    r.ok("sound-only: reconnect flags plus the headers", "-reconnect 1"
         in before and "-user_agent UA/1" in before and opts == "-vn")
    lg = " ".join(T.logo_args("/tmp/l.png"))
    r.ok("logo: bounces in x and y, hue turns per bounce",
         "mod(t*150" in lg and "mod(t*110" in lg and "hue=h=" in lg)


PROBE_ERR = """Input #0, hls, from 'http://x':
  Stream #0:0: Video: h264 (High), yuv420p, 1280x720 [SAR 1:1], 25 fps
  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp
"""


def suite_probe(r: Results) -> None:
    p = T.parse_probe(0, "out_time_us=4000000\n", PROBE_ERR)
    r.check("a stream that keeps up", (p.ok, p.has_video, p.has_audio,
                                       p.height), (True, True, True, 720))
    slow = T.parse_probe(None, "out_time_us=1300000\n", PROBE_ERR)
    r.check("opens, then crawls: refused", (slow.ok, slow.why),
            (False, "too slow: 1.3s of 4s"))
    radio = T.parse_probe(0, "out_time_us=4000000\n",
                          "  Stream #0:0: Audio: mp3, 44100 Hz, stereo\n")
    r.check("sound only", (radio.ok, radio.has_video, radio.has_audio),
            (True, False, True))
    dead = T.parse_probe(1, "", "http://x: Server returned 403 Forbidden")
    r.check("dead: ffmpeg's own words",
            (dead.ok, dead.why), (False, "http://x: Server returned 403 "
                                         "Forbidden"))
    r.check("nothing at all before the timeout",
            T.parse_probe(None, "", "").why, "timeout")


def suite_frames(r: Results) -> None:
    async def go():
        proc = _Proc(frames=4)
        ended = []
        got = [item async for item in T.clocked_frames(
            proc, clocked=True, on_end=lambda n, e: ended.append(n))]
        bare = [item async for item in T.clocked_frames(_Proc(2),
                                                        clocked=False)]
        return proc, ended, got, bare
    proc, ended, got, bare = asyncio.run(go())
    r.check("clocked frames carry their time", [round(p, 3) for p, _ in got],
            [0.0, 0.033, 0.067, 0.1])
    r.ok("bare frames for the wall clock", all(isinstance(b, bytes)
                                               for b in bare))
    r.check("ended on its own: told, process reaped", (ended, proc.killed),
            ([4], True))


def suite_watch(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        bbc = cat.catalogue.channels["BBCNews.uk"]
        dead = PlayProbe(False, "http 403")
        tv = _tv(cat, streams, voice, [dead, OK_AV])
        w = asyncio.run(tv.watch("g1", "vc1", bbc, notify_to="vc1"))
        r.check("first stream failed its probe: marked failed",
                cat.health.status(bbc.streams[0].url, T0), I.FAILED)
        r.check("the second plays, and is marked ok",
                (w.stream.url, cat.health.status(w.stream.url, T0)),
                (bbc.streams[1].url, I.OK))
        r.check("picture and sound", w.mode, "picture")
        kw = streams.started[0]
        r.ok("a held tv stream against the audio clock",
             kw["kind"] == "tv" and kw["hold"] and kw["clock"] is not None)
        src = voice.get("g1").client.played[0]
        r.ok("the sound plays from the pipe with the short queue",
             src.path.endswith("audio.pcm") and src.kw["before_options"]
             == T.RAW_PCM_IN and src.corbin_queue_ms == T.TV_AUDIO_QUEUE_MS)
        r.ok("the pipe is a real fifo", os.path.exists(src.path)
             and not os.path.isfile(src.path))
        r.ok("tracked as watching", tv.watching("g1") is w)

        folder = w.folder
        r.ok("stop: picture and sound off, files gone",
             asyncio.run(tv.stop("g1")) and streams.stops
             and voice.stopped == ["g1"] and not os.path.exists(folder)
             and tv.watching("g1") is None)

    with tempfile.TemporaryDirectory() as tmp:
        cat, voice = _catalogue(tmp), _Voice()
        streams = _Streams(fail=RuntimeError("the voice server refused "
                                             "the video"))
        tv = _tv(cat, streams, voice, [OK_AV])
        w = asyncio.run(tv.watch("g1", "vc1",
                                 cat.catalogue.channels["BBCNews.uk"]))
        src = voice.get("g1").client.played[0]
        r.ok("no picture possible: sound only, straight from the url",
             w.mode == "sound" and src.path.startswith("http")
             and "couldn't show the picture" in w.note)

    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        silent = PlayProbe(True, has_video=True, has_audio=False,
                           decoded=4.0)
        tv = _tv(cat, streams, voice, [silent])
        w = asyncio.run(tv.watch("g1", "vc1",
                                 cat.catalogue.channels["CBC.ca"]))
        r.ok("video with no sound: silent, wall-clock paced",
             w.mode == "silent" and streams.started[0]["clock"] is None
             and voice.get("g1") is None)

    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        radio = PlayProbe(True, has_audio=True, decoded=4.0)
        tv = _tv(cat, streams, voice, [radio])
        w = asyncio.run(tv.watch("g1", "vc1",
                                 cat.catalogue.channels["CBC.ca"]))
        r.ok("sound-only channel: its sound and the bouncing logo",
             w.mode == "logo" and streams.started
             and any("hue=h=" in " ".join(a) for a in tv.spawned))

    with tempfile.TemporaryDirectory() as tmp:
        cat = _catalogue(tmp)
        tv = _tv(cat, _Streams(), _Voice(),
                 [PlayProbe(False, "timeout")] * 3)
        bbc = cat.catalogue.channels["BBCNews.uk"]
        try:
            asyncio.run(tv.watch("g1", "vc1", bbc))
            msg = None
        except TVError as exc:
            msg = str(exc)
        r.check("every stream fails: says so", msg,
                "BBC News isn't working right now (3 streams tried).")
        r.ok("...and all are marked failed",
             all(cat.health.status(s.url, T0) == I.FAILED
                 for s in bbc.streams))
        try:
            asyncio.run(tv.watch("g1", "vc1", bbc))
            again = None
        except TVError as exc:
            again = str(exc)
        r.check("asked for again by name: probed again, not refused on "
                "the strength of a mark", again,
                "BBC News isn't working right now (3 streams tried).")


class _Player:
    def __init__(self):
        self.p = type("P", (), {})()
        self.p.generation, self.p.current = 4, "song"
        self.p.loop_track = self.p.loop_queue = True
        self.cleared = False
        self.p.clear = lambda: setattr(self, "cleared", True)

    def cancel_import(self, gid):
        self.import_cancelled = True

    def get(self, gid):
        return self.p


def suite_endings(r: Results) -> None:
    T.ENDED_SETTLE = 0

    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice, player = (_catalogue(tmp), _Streams(), _Voice(),
                                       _Player())
        tv = _tv(cat, streams, voice, [OK_AV], player=player)
        bbc = cat.catalogue.channels["BBCNews.uk"]
        r.ok("a channel replaces the music: queue cleared, stale "
             "callbacks ignored",
             asyncio.run(tv.watch("g1", "vc1", bbc)) and player.cleared
             and player.p.current is None and player.p.generation == 5
             and not player.p.loop_queue)

    async def replaced_or_dropped(playing_after: bool):
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        notes = []
        tv = _tv(cat, streams, voice, [OK_AV], notes=notes)
        w = await tv.watch("g1", "vc1", cat.catalogue.channels["BBCNews.uk"],
                           notify_to="vc1")
        client = voice.get("g1").client
        client.is_playing = playing_after   # a song took over, or nothing
        client.after(None)                  # the TV sound's "finished"
        await asyncio.sleep(0.05)
        return tv, w, cat, voice, notes

    with tempfile.TemporaryDirectory() as tmp:
        tv, w, cat, voice, notes = asyncio.run(replaced_or_dropped(True))
        r.ok("replaced by a song: TV off quietly, the song left alone",
             tv.watching("g1") is None and not notes
             and voice.stopped == []
             and cat.health.status(w.stream.url, T0) == I.OK)

    with tempfile.TemporaryDirectory() as tmp:
        tv, w, cat, voice, notes = asyncio.run(replaced_or_dropped(False))
        r.ok("dropped on air, and no other stream plays: off, marked "
             "failed, the channel told",
             tv.watching("g1") is None
             and cat.health.status(w.stream.url, T0) == I.FAILED
             and notes and "BBC News dropped out" in notes[0][1])

    with tempfile.TemporaryDirectory() as tmp:
        async def comeback():
            cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
            notes = []
            tv = _tv(cat, streams, voice, [OK_AV, OK_AV], notes=notes)
            w = await tv.watch("g1", "vc1",
                               cat.catalogue.channels["BBCNews.uk"],
                               notify_to="vc1")
            client = voice.get("g1").client
            client.is_playing = False
            client.after(None)
            await asyncio.sleep(0.05)
            return tv, w, streams, notes
        tv, w, streams, notes = asyncio.run(comeback())
        back = tv.watching("g1")
        r.ok("dropped on air: the same stream reconnected first (Pluto "
             "hands out a fresh session), nobody told",
             back is not None and back is not w and not notes
             and back.stream.url == w.stream.url and back.retries == 1)
        r.ok("...swapped into the same picture, not republished",
             len(streams.started) == 1 and len(streams.swaps) == 1
             and not streams.stops)

    with tempfile.TemporaryDirectory() as tmp:
        async def leave():
            cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
            tv = _tv(cat, streams, voice, [OK_AV])
            await tv.watch("g1", "vc1", cat.catalogue.channels["BBCNews.uk"])
            for closer in voice.get("g1").closers:
                await closer()
            return tv
        r.ok("leaving voice by any route forgets the TV",
             asyncio.run(leave()).watching("g1") is None)
    T.ENDED_SETTLE = 1.0


def suite_rules(r: Results) -> None:
    """T3/T4: the server's quality, watch limit and last channel."""
    from corbin.core.settings import ChatSettings
    from corbin.core.tv_rules import TVRules
    from corbin.core.video import QUALITIES

    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        rules = TVRules(ChatSettings(os.path.join(tmp, "s.json")),
                        allow_1080p=True)
        rules.set_quality("g1", "1080p")
        rules.set_max_minutes("g1", 1)
        notes = []
        hd = PlayProbe(True, has_video=True, has_audio=True, height=1080,
                       decoded=4.0)
        tv = _tv(cat, streams, voice, [hd], rules=rules, notes=notes)
        bbc = cat.catalogue.channels["BBCNews.uk"]

        async def go():
            w = await tv.watch("g1", "vc1", bbc, notify_to="vc1")
            return w
        w = asyncio.run(go())
        a = tv.spawned[0]
        r.ok("the server's 1080p, a 1080p channel: the variant, the filter "
             "and the stream", tv.picked == [1080]
             and "scale=1920:1080" in " ".join(a)
             and streams.started[0]["quality"] is QUALITIES["1080p"])
        r.check("...with the read-ahead capped by bytes at 1080p",
                T.read_ahead_for(QUALITIES["1080p"]), 32)
        small = _Streams()
        tv720 = _tv(cat, small, _Voice(), [OK_AV], rules=rules)
        w720 = asyncio.run(tv720.watch("g1", "vc1", bbc))
        r.ok("a 720p channel on a 1080p server isn't upscaled",
             w720.quality is QUALITIES["720p"]
             and small.started[0]["quality"] is QUALITIES["720p"])
        r.ok("the watch limit is armed", w.deadline is not None)
        r.check("the last channel is remembered", rules.last_watched("g1"),
                "BBCNews.uk")

        async def limited():
            w.deadline = 0          # already up
            tv2 = _tv(cat, _Streams(), _Voice(), [OK_AV], rules=rules,
                      notes=notes)
            w2 = await tv2.watch("g1", "vc1", bbc, notify_to="vc1",
                                 deadline=0.0)
            await asyncio.sleep(0.05)
            return tv2, w2
        tv2, w2 = asyncio.run(limited())
        r.ok("the limit reached: off, and the channel told why",
             tv2.watching("g1") is None and notes
             and "TV limit of 1 minute" in notes[-1][1])


class _HungProc(_Proc):
    """ffmpeg that sends ``frames`` frames and then hangs: no EOF, no
    exit. What a stalled live source does (live, 2026-10-20)."""
    def __init__(self, frames=3):
        self.stdout = asyncio.StreamReader()
        for _ in range(frames):
            self.stdout.feed_data(b"\x00" * FRAME_BYTES)
        self.returncode = None
        self.killed = False


def suite_stall(r: Results) -> None:
    from corbin.core import video as V
    old = V.FRAME_STALL_TIMEOUT, V.FIRST_FRAME_TIMEOUT
    V.FRAME_STALL_TIMEOUT, V.FIRST_FRAME_TIMEOUT = 0.1, 0.2
    T.ENDED_SETTLE = 0
    try:
        async def frames_then_hang():
            proc = _HungProc(3)
            ended = []
            got = [x async for x in T.clocked_frames(
                proc, clocked=True, on_end=lambda n, e: ended.append((n, e)))]
            return proc, got, ended
        proc, got, ended = asyncio.run(asyncio.wait_for(frames_then_hang(),
                                                        5))
        r.check("a hung ffmpeg: its frames, then the stream ends",
                len(got), 3)
        r.ok("...killed, and the end says it stalled",
             proc.killed and ended and ended[0][0] == 3
             and ended[0][1].startswith("stalled (no frame for"))

        async def never_starts():
            proc = _HungProc(0)
            t = asyncio.get_running_loop().time()
            got = [x async for x in T.clocked_frames(proc, clocked=False)]
            return got, asyncio.get_running_loop().time() - t
        got, took = asyncio.run(never_starts())
        r.ok("the first frame gets the longer allowance",
             got == [] and took >= 0.18)

        # End to end: the channel hangs on air, is retried, and comes
        # back on its next stream, swapped into the same picture.
        with tempfile.TemporaryDirectory() as tmp:
            async def hang_on_air():
                cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
                notes = []
                tv = _tv(cat, streams, voice, [OK_AV, OK_AV], notes=notes)
                procs = []

                async def spawn(args, **kw):
                    procs.append(_HungProc(2) if not procs else _Proc(3))
                    return procs[-1]
                tv._spawn = spawn
                w = await tv.watch("g1", "vc1",
                                   cat.catalogue.channels["BBCNews.uk"],
                                   notify_to="vc1")
                # What the stream manager does: pull frames until they
                # stop, then call the held stream's frames-ended hook.
                async for _ in streams.frames:
                    pass
                voice.get("g1").client.is_playing = False
                await tv._frames_ended(w)
                return tv, w, streams, notes, procs
            tv, w, streams, notes, procs = asyncio.run(
                asyncio.wait_for(hang_on_air(), 10))
            back = tv.watching("g1")
            r.ok("hung on air: killed, reconnected, back on",
                 procs[0].killed and back is not None and back is not w
                 and back.stream.url == w.stream.url and not notes)
            r.ok("...in the same picture (swapped, not republished)",
                 len(streams.started) == 1 and len(streams.swaps) == 1)
    finally:
        V.FRAME_STALL_TIMEOUT, V.FIRST_FRAME_TIMEOUT = old
        T.ENDED_SETTLE = 1.0


def suite_feeds(r: Results) -> None:
    """Live 2026-10-20: "Pluto TV Westerns" dropped, its entry had no
    other stream, and the retry gave up while another "Pluto TV
    Westerns" entry (another country's feed) played when asked again.
    Also: the picture and the sound both ending ran the retry twice."""
    T.ENDED_SETTLE = 0

    def westerns(cat):
        a = I.Channel(id="PlutoTVWesterns.de", name="Pluto TV Westerns",
                      country="DE", categories=["movies"], streams=[
                          I.Stream(url="https://jmp2.uk/plu-a.m3u8",
                                   channel="PlutoTVWesterns.de")])
        b = I.Channel(id="PlutoTVWesterns.us", name="Pluto TV Westerns",
                      country="US", categories=["movies"], streams=[
                          I.Stream(url="https://jmp2.uk/plu-b1.m3u8",
                                   channel="PlutoTVWesterns.us"),
                          I.Stream(url="https://jmp2.uk/plu-b2.m3u8",
                                   channel="PlutoTVWesterns.us")])
        cat.catalogue.channels[a.id] = a
        cat.catalogue.channels[b.id] = b
        return a, b

    with tempfile.TemporaryDirectory() as tmp:
        cat = _catalogue(tmp)
        a, b = westerns(cat)
        tv = _tv(cat, _Streams(), _Voice(), [])
        r.check("same-name feeds, same country first, most streams first",
                [c.id for c in tv.siblings(a)], ["PlutoTVWesterns.us"])
        tv = _tv(cat, _Streams(), _Voice(),
                 [PlayProbe(False, "http 404"), OK_AV])
        w = asyncio.run(tv.watch("g1", "vc1", a))
        r.ok("its own stream dead: another feed of the same channel plays",
             w.channel.id == "PlutoTVWesterns.us")

    with tempfile.TemporaryDirectory() as tmp:
        async def drop_on_air():
            cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
            a, b = westerns(cat)
            notes = []
            # Plays, then on the retry: A's reconnect fails, B plays.
            tv = _tv(cat, streams, voice,
                     [OK_AV, PlayProbe(False, "http 404"), OK_AV],
                     notes=notes)
            w = await tv.watch("g1", "vc1", a, notify_to="vc1")
            # Probing takes seconds live; both recoveries must overlap it,
            # or the second one sees the first one's result and the test
            # proves nothing.
            fast = tv._probe

            async def slow(url, stream):
                await asyncio.sleep(0.05)
                return await fast(url, stream)
            tv._probe = slow
            client = voice.get("g1").client
            client.is_playing = False
            # Both ends of a dead stream, as live: the sound's callback
            # and the picture's frames-ended hook, at the same moment.
            client.after(None)
            await asyncio.gather(tv._frames_ended(w),
                                 asyncio.sleep(0.3))
            await asyncio.sleep(0.1)
            return tv, w, streams, notes
        tv, w, streams, notes = asyncio.run(drop_on_air())
        back = tv.watching("g1")
        r.ok("dropped on air, its only stream won't reconnect: back on "
             "from the other feed, nobody told",
             back is not None and back.channel.id == "PlutoTVWesterns.us"
             and not notes)
        r.check("...recovered once, not twice", len(streams.swaps), 1)
    T.ENDED_SETTLE = 1.0


def suite_drift(r: Results) -> None:
    """Short pauses that came more often the longer a stream ran: the
    picture creeping ahead of the sound at each ad-break timestamp gap,
    until the one ffmpeg blocked and the sound ran dry."""
    a = T.av_args("u", I.Stream(url="u", channel="c"), fifo="/tmp/p.pcm")
    i = a.index("/tmp/p.pcm")
    r.ok("the sound is padded across timestamp gaps, like the picture",
         "-af" in a[:i] and a[a.index("-af") + 1] == "aresample=async=1"
         and a.index("-af") > a.index("-i"))
    r.ok("...on the sound output only", a.count("-af") == 1)

    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        tv = _tv(cat, streams, voice, [OK_AV])
        asyncio.run(tv.watch("g1", "vc1",
                             cat.catalogue.channels["BBCNews.uk"]))
        kw = streams.started[0]
        r.check("picture with sound: the re-anchoring safety net is on",
                kw["max_lead"], T.TV_MAX_LEAD)
        voice.get("g1").client._corbin_audio_clock = {"dry": 3}
        voice.get("g1").client._corbin_audio_clock["wait_max"] = 1.42
        r.check("...and the stats line says how often the sound ran dry, "
                "and the longest wait for it", kw["extra_stats"](),
                ", sound ran dry 3x, longest audio wait 1.42s")
        r.check("...the wait resets after each stats line",
                kw["extra_stats"](),
                ", sound ran dry 3x, longest audio wait 0.00s")
    with tempfile.TemporaryDirectory() as tmp:
        cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
        silent = PlayProbe(True, has_video=True, has_audio=False,
                           decoded=4.0)
        tv = _tv(cat, streams, voice, [silent])
        asyncio.run(tv.watch("g1", "vc1", cat.catalogue.channels["CBC.ca"]))
        r.check("no sound, no clock: nothing to re-anchor to",
                streams.started[0]["max_lead"], None)


def suite_programme_end(r: Results) -> None:
    """Live 2026-10-20: a Pluto stream dropped when the programme ended;
    it was marked failed before the retry, it was the channel's only
    stream, and he gave up ("dropped out") on a channel that reconnected
    fine. And a lifetime budget of 2 retries can't survive a film night."""
    T.ENDED_SETTLE = 0
    import time as _t

    def one_stream(cat):
        c = I.Channel(id="Classic.us", name="Pluto Classic Movie Westerns",
                      country="US", categories=["movies"], streams=[
                          I.Stream(url="https://jmp2.uk/plu-c.m3u8",
                                   channel="Classic.us")])
        cat.catalogue.channels[c.id] = c
        return c

    async def drop(tv, voice, w):
        voice.get("g1").client.is_playing = False
        await tv._frames_ended(w)
        return tv.watching("g1")

    with tempfile.TemporaryDirectory() as tmp:
        async def go():
            cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
            c = one_stream(cat)
            notes = []
            tv = _tv(cat, streams, voice, [OK_AV] * 6, notes=notes)
            w = await tv.watch("g1", "vc1", c, notify_to="vc1")
            backs = []
            for _ in range(4):
                # Each programme played its full length before dropping.
                w.started = _t.monotonic() - T.STABLE_AFTER - 1
                w = await drop(tv, voice, w)
                backs.append(w is not None)
                if w is None:
                    break
            return cat, c, notes, backs
        cat, c, notes, backs = asyncio.run(go())
        r.ok("its only stream drops: reconnected, not given up on",
             backs[0] and not notes)
        r.ok("four programme boundaries in a row: back on every time "
             "(the budget is per incident, not per watch)",
             backs == [True] * 4)
        r.check("a reconnected stream is still marked working",
                cat.health.status(c.streams[0].url, T0), I.OK)

    with tempfile.TemporaryDirectory() as tmp:
        async def flapping():
            cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
            c = one_stream(cat)
            notes = []
            tv = _tv(cat, streams, voice, [OK_AV] * 6, notes=notes)
            w = await tv.watch("g1", "vc1", c, notify_to="vc1")
            n = 0
            while w is not None and n < 6:
                w = await drop(tv, voice, w)    # drops again at once
                n += 1
            return cat, c, notes, n
        cat, c, notes, n = asyncio.run(flapping())
        r.check("dropping again straight away: given up after the budget",
                n, T.ON_AIR_RETRIES + 1)
        r.ok("...then told, and only then marked failed",
             notes and "dropped out" in notes[-1][1]
             and cat.health.status(c.streams[0].url, T0) == I.FAILED)
    T.ENDED_SETTLE = 1.0


def suite_soft_marks(r: Results) -> None:
    """Live 2026-10-20: one drop at a programme boundary marked Pluto
    Classic Movie Westerns' only stream failed for a day, and "put on
    the usual" refused it without trying, minutes after it had played."""
    hb = I.HealthBook()
    hb.record("u", False, 1000.0, why="dropped on air", soft=True)
    r.check("a playback failure hides it for now",
            hb.status("u", 1000.0 + 60), I.FAILED)
    r.check("...for half an hour, not a day",
            hb.status("u", 1000.0 + I.SOFT_FAIL_FOR + 1), I.UNCHECKED)
    r.check("and the health check looks again then",
            hb.next_check("u"), 1000.0 + I.SOFT_FAIL_FOR)
    old = I.HealthBook({"v": {"last": "failed", "failed": 1000.0,
                               "fails": 1, "why": "dropped on air"}})
    r.check("marks made before 'soft' existed are recognised by reason",
            old.status("v", 1000.0 + I.SOFT_FAIL_FOR + 1), I.UNCHECKED)
    probe = I.HealthBook({"w": {"last": "failed", "failed": 1000.0,
                                 "fails": 1, "why": "play probe: timeout"}})
    r.check("...a failed play probe too",
            probe.status("w", 1000.0 + I.SOFT_FAIL_FOR + 1), I.UNCHECKED)
    hb.record("y", True, 900.0)
    hb.record("y", False, 1000.0, why="dropped on air", soft=True)
    r.check("expired, a stream that had passed recently is listed again",
            hb.status("y", 1000.0 + I.SOFT_FAIL_FOR + 1), I.OK)
    hb.record("x", False, 1000.0, why="HTTP 404")
    r.check("a health-check failure still counts for a day",
            hb.status("x", 1000.0 + I.SOFT_FAIL_FOR + 1), I.FAILED)
    hb.record("u", True, 2000.0)
    r.ok("passing again clears the soft flag",
         hb.status("u", 2000.0) == I.OK and not hb.records["u"]["soft"])

    with tempfile.TemporaryDirectory() as tmp:
        cat = _catalogue(tmp)
        cat.mark("http://b/720", False, "dropped on air")
        r.ok("TV's own marks are soft", cat.health.records[
            "http://b/720"]["soft"])
        bbc = cat.catalogue.channels["BBCNews.uk"]
        for st in bbc.streams[:2]:
            cat.health.record(st.url, False, T0, why="HTTP 404")
        tried = []
        tv = _tv(cat, _Streams(), _Voice(), [])

        async def probe(url, s):
            tried.append(url)
            return OK_AV if url == bbc.streams[0].url else \
                PlayProbe(False, "no")
        tv._probe = probe
        w = asyncio.run(tv.watch("g1", "vc1", bbc))
        r.check("an explicit request: healthy streams first, marked ones "
                "last, never skipped", tried,
                [bbc.streams[2].url, bbc.streams[0].url])
        r.ok("...and a marked stream that plays is put on", w is not None
             and w.stream.url == bbc.streams[0].url)


def suite_frozen(r: Results) -> None:
    """Live 2026-10-20: frames stopped at 1,695 with no stall warning and
    nothing ended. The queue was full, so no read was pending for the
    read watchdog to time; the sound froze, and the picture waited on it
    forever. The monitor watches frames reaching viewers instead."""
    saved = T.FROZEN_AFTER, T.MONITOR_EVERY, T.MONITOR_GRACE, T.ENDED_SETTLE
    T.FROZEN_AFTER, T.MONITOR_EVERY, T.MONITOR_GRACE = 0.2, 0.05, 0.0
    T.ENDED_SETTLE = 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            async def frozen():
                cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
                notes = []
                tv = _tv(cat, streams, voice, [OK_AV, OK_AV], notes=notes)
                w = await tv.watch("g1", "vc1",
                                   cat.catalogue.channels["BBCNews.uk"],
                                   notify_to="vc1")
                streams.active.frames = 1695

                async def after_recovery():
                    # Frozen until the channel is swapped back in, then
                    # frames flow again, as a real recovered stream does.
                    while True:
                        if streams.swaps:
                            streams.active.frames += 3
                        await asyncio.sleep(0.03)
                ticker = asyncio.ensure_future(after_recovery())
                # The sound "plays" (its loop is stuck, not finished), the
                # frames never move, and nothing ends on its own.
                await asyncio.sleep(0.8)
                ticker.cancel()
                return tv, w, streams, notes
            tv, w, streams, notes = asyncio.run(frozen())
            back = tv.watching("g1")
            r.ok("a frozen picture with nothing ending: ffmpeg killed and "
                 "the channel recovered", w.proc.killed and back is not None
                 and back is not w and not notes)
            r.check("...swapped into the same picture, once",
                    len(streams.swaps), 1)

        with tempfile.TemporaryDirectory() as tmp:
            async def healthy():
                cat, streams, voice = _catalogue(tmp), _Streams(), _Voice()
                tv = _tv(cat, streams, voice, [OK_AV])
                w = await tv.watch("g1", "vc1",
                                   cat.catalogue.channels["BBCNews.uk"])
                streams.active.frames = 0
                for _ in range(12):
                    streams.active.frames += 3      # frames flowing
                    await asyncio.sleep(0.05)
                return tv, w
            tv, w = asyncio.run(healthy())
            r.ok("frames flowing: left alone", tv.watching("g1") is w
                 and not w.proc.killed)
    finally:
        T.FROZEN_AFTER, T.MONITOR_EVERY, T.MONITOR_GRACE, T.ENDED_SETTLE = \
            saved


SUITES = [
    ("tv ffmpeg commands", suite_args),
    ("tv play probe", suite_probe),
    ("tv frame read-ahead", suite_frames),
    ("tv watching", suite_watch),
    ("tv endings", suite_endings),
    ("tv server rules", suite_rules),
    ("tv stall watchdog", suite_stall),
    ("tv other feeds and one recovery", suite_feeds),
    ("tv picture/sound drift", suite_drift),
    ("tv programme ends: reconnect", suite_programme_end),
    ("tv soft marks, explicit requests try", suite_soft_marks),
    ("tv frozen-picture monitor", suite_frozen),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
