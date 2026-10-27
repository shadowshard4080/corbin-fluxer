"""Offline suite for audio sourcing and the queue."""
from __future__ import annotations
import logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.audio import (NO_POT_PLAYER_CLIENTS, POT_PLAYER_CLIENTS,
                               RETRY_CLIENT_SETS, Extractor, Track,
                               base_opts, format_duration,
                               is_bot_check_error, track_from_info)
from corbin.core.player import GuildPlayer, MAX_QUEUE


def suite_sourcing(r: Results) -> None:
    # The anti-bot gate is NOT fixable by client rotation, so it has to be
    # recognised and reported rather than retried forever.
    for msg in ("ERROR: Sign in to confirm you're not a bot",
                "Please confirm you are not a bot to continue"):
        r.ok(f"bot check detected: {msg[:28]!r}", is_bot_check_error(msg))
    for msg in ("HTTP Error 404", "", "video unavailable"):
        r.ok(f"not a bot check: {msg[:20]!r}", not is_bot_check_error(msg))

    r.check("no duration means live", format_duration(None), "live")
    r.check("zero means live", format_duration(0), "live")
    r.check("minutes", format_duration(65), "1:05")
    r.check("hours", format_duration(3725), "1:02:05")

    opts = base_opts()
    r.check("defaults to the token-free clients",
            opts["extractor_args"]["youtube"]["player_client"],
            NO_POT_PLAYER_CLIENTS)
    r.ok("pot base url passed to the plugin",
         opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"])
    r.ok("audio-only format preferred", "bestaudio" in opts["format"])
    r.ok("playlists off by default", opts["noplaylist"])

    e = Extractor()
    r.check("extractor starts assuming no provider",
            e.opts()["extractor_args"]["youtube"]["player_client"],
            NO_POT_PLAYER_CLIENTS)
    r.check("clients can be forced",
            e.opts(clients=["mweb"])["extractor_args"]["youtube"]
            ["player_client"], ["mweb"])
    r.ok("playlist mode flips noplaylist",
         not e.opts(playlist=True)["noplaylist"])
    # Retry sets stick to clients that actually serve audio; with a
    # cookiefile yt-dlp skips tv_simply/android_vr entirely.
    for group in RETRY_CLIENT_SETS:
        r.ok(f"retry set {group} avoids cookie-incompatible clients",
             not ({"tv_simply", "android_vr"} & set(group)))
    r.ok("pot and no-pot orders differ",
         POT_PLAYER_CLIENTS != NO_POT_PLAYER_CLIENTS)


def suite_track(r: Results) -> None:
    t = track_from_info({"title": "Song", "url": "http://s",
                         "duration": 200, "uploader": "Artist",
                         "thumbnail": "http://t"},
                        requested_by="u1", requester_name="Lux")
    r.check("title", t.title, "Song")
    r.check("duration formatted", t.pretty_duration, "3:20")
    r.check("requester kept", t.requester_name, "Lux")
    r.ok("not live", not t.is_live)

    # A stream url is often only inside formats.
    f = track_from_info({"title": "S", "formats": [
        {"acodec": "none", "url": "http://video"},
        {"acodec": "opus", "url": "http://audio"}]})
    r.check("falls back to an audio format", f.url, "http://audio")

    r.check("no playable url is dropped",
            track_from_info({"title": "S"}), None)
    r.check("non-dict is dropped", track_from_info("nope"), None)
    r.ok("missing duration reads as live",
         track_from_info({"title": "S", "url": "u"}).is_live)


def suite_queue(r: Results) -> None:
    p = GuildPlayer("g")
    r.ok("starts idle", not p.playing)
    for i in range(3):
        p.add(Track(title=f"T{i}", url="u"))
    r.check("queued in order", [t.title for t in p.queue],
            ["T0", "T1", "T2"])

    r.check("next pops the front", p.next_track().title, "T0")
    r.check("queue shrinks", len(p.queue), 2)

    p.loop_queue = True
    taken = p.next_track()
    r.check("loop_queue requeues at the back", p.queue[-1].title, taken.title)
    p.loop_queue = False

    p.current = Track(title="CUR", url="u")
    p.loop_track = True
    r.check("loop_track replays the current", p.next_track().title, "CUR")
    r.check("loop_track does not consume the queue", len(p.queue), 2)
    p.loop_track = False

    expected_front = p.queue[0].title
    r.check("remove returns the item at that index",
            p.remove(0).title, expected_front)
    r.check("bad index is None", p.remove(99), None)
    r.check("negative index is None", p.remove(-1), None)

    p.clear()
    r.check("clear empties", len(p.queue), 0)
    r.check("empty queue yields nothing", p.next_track(), None)

    full = GuildPlayer("g2")
    for i in range(MAX_QUEUE):
        full.add(Track(title=str(i), url="u"))
    r.ok("queue cap enforced", not full.add(Track(title="over", url="u")))
    r.check("cap respected", len(full.queue), MAX_QUEUE)

    s = GuildPlayer("g3")
    for i in range(20):
        s.add(Track(title=str(i), url="u"))
    before = [t.title for t in s.queue]
    s.shuffle()
    r.check("shuffle keeps every track", sorted(t.title for t in s.queue),
            sorted(before))


def suite_playlists(r: Results) -> None:
    """A playlist link used to queue its first song and drop the rest."""
    import asyncio
    from types import SimpleNamespace

    from corbin.core.audio import (MAX_PLAYLIST, Extractor as AudioExtractor,
                                   is_youtube_playlist)
    from corbin.core.player import PlayerManager

    r.ok("a playlist page is a playlist", is_youtube_playlist(
        "https://www.youtube.com/playlist?list=PLabc123"))
    r.ok("...on YouTube Music too", is_youtube_playlist(
        "https://music.youtube.com/playlist?foo=1&list=OLAK5uy_x"))
    r.ok("a video inside a mix is ONE song", not is_youtube_playlist(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ"))

    ex = AudioExtractor.__new__(AudioExtractor)
    ex.spotify = None

    async def flat(query, **kw):
        assert kw.get("flat") and kw.get("playlist"), kw
        return {"entries": [{"url": "https://youtu.be/a"}, {"id": "b"},
                            "junk"] + [{"id": str(i)} for i in range(80)]}
    ex.extract = flat
    listed = asyncio.run(ex.collection(
        "https://www.youtube.com/playlist?list=PL1"))
    r.check("entries become page URLs, flat",
            listed[:2], ["https://youtu.be/a",
                         "https://www.youtube.com/watch?v=b"])
    r.check("...capped", len(listed), MAX_PLAYLIST)
    r.check("a plain search is not a collection",
            asyncio.run(ex.collection("bohemian rhapsody")), None)

    ex.spotify = SimpleNamespace(resolve=lambda text: _done([
        SimpleNamespace(query="Queen - Bohemian Rhapsody"),
        SimpleNamespace(query="Queen - Radio Ga Ga")]))
    r.check("a Spotify album lists its songs as searches",
            asyncio.run(ex.collection(
                "https://open.spotify.com/album/4g1ZRSobMefqF6nelkgibi")),
            ["Queen - Bohemian Rhapsody", "Queen - Radio Ga Ga"])
    r.check("a Spotify single track is not a collection",
            asyncio.run(ex.collection(
                "https://open.spotify.com/track/4u7EnebtmKWzUH433cf5Qv")),
            None)

    class Resolver:
        def __init__(self, missing=()):
            self.seen, self.missing = [], set(missing)

        async def resolve(self, q, **kw):
            self.seen.append(q)
            await asyncio.sleep(0)
            if q in self.missing:
                return [], "nope"
            return [Track(title=q, url="s")], None

    class Voice:
        async def disconnect(self, gid):
            pass

    async def importing():
        res = Resolver(missing={"b"})
        mgr = PlayerManager(Voice(), res)
        player = mgr.player("g")
        player.current = Track(title="first", url="s")
        mgr.start_import("g", "vc", ["a", "b", "c"])
        r.ok("the import runs in the background", mgr.importing("g"))
        while mgr.importing("g"):
            await asyncio.sleep(0)
        r.check("the rest are queued, in order, skipping misses",
                [t.title for t in player.queue], ["a", "c"])

        # The first song ended before the importer caught up: nothing
        # would advance on its own, so the importer must start playback.
        started = []
        mgr2 = PlayerManager(Voice(), Resolver())

        async def play_now(gid, ch, track, **kw):
            started.append(track.title)
            mgr2.player(gid).current = track
        mgr2.play_now = play_now
        mgr2.start_import("g", "vc", ["x", "y"])
        while mgr2.importing("g"):
            await asyncio.sleep(0)
        r.check("a dry queue restarts playback, then queues the rest",
                (started, [t.title for t in mgr2.player("g").queue]),
                (["x"], ["y"]))

        slow = Resolver()
        mgr3 = PlayerManager(Voice(), slow)
        mgr3.player("g").current = Track(title="first", url="s")
        mgr3.start_import("g", "vc", [str(i) for i in range(50)])
        await asyncio.sleep(0)
        await mgr3.stop("g")
        await asyncio.sleep(0)
        r.ok("stop cancels the import", not mgr3.importing("g"))
        r.ok("...and nothing more is queued after it",
             len(mgr3.player("g").queue) == 0 and len(slow.seen) < 50)

    asyncio.run(importing())


async def _done(value):
    return value


SUITES = [
    ("audio sourcing", suite_sourcing),
    ("track parsing", suite_track),
    ("queue behaviour", suite_queue),
    ("playlists", suite_playlists),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
