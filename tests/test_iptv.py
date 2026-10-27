"""Offline suite for the live TV catalogue (corbin/core/iptv.py, Track T1)."""
from __future__ import annotations
import asyncio, json, logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core import iptv as I
from corbin.core.iptv import (Catalogue, HealthBook, IptvCatalogue, Probe,
                              build_catalogue, category_index, due_streams,
                              is_listed, is_playable, page, parse_master,
                              parse_media, pick_variant, resolve_category,
                              resolve_country, resolve_language, search)

T0 = 1_800_000_000.0


def _raw() -> dict:
    """A small slice of iptv-org's API, one case per filter rule."""
    return {
        "channels": [
            {"id": "BBCNews.uk", "name": "BBC News", "alt_names": ["BBC"],
             "country": "GB", "categories": ["news"], "is_nsfw": False},
            {"id": "CBC.ca", "name": "CBC News Network", "country": "CA",
             "categories": ["news"]},
            {"id": "Old.us", "name": "Old Channel", "country": "US",
             "categories": ["movies"], "closed": "2024-01-01",
             "replaced_by": "New.us@SD"},
            {"id": "New.us", "name": "New Channel", "country": "US",
             "categories": ["movies"]},
            {"id": "Dead.us", "name": "Dead Channel", "country": "US",
             "categories": ["movies"], "closed": "2023-01-01"},
            {"id": "Adult.us", "name": "Adult", "country": "US",
             "categories": ["xxx"], "is_nsfw": True},
            {"id": "Blocked.us", "name": "Blocked", "country": "US",
             "categories": ["sports"]},
            {"id": "Geo.es", "name": "Geo Only", "country": "ES",
             "categories": ["sports"]},
            {"id": "Telemundo.es", "name": "Telemundo Kids", "country": "ES",
             "categories": ["kids"]},
            {"id": "Loose.fr", "name": "Chaîne Libre", "country": "FR",
             "categories": []},
        ],
        "streams": [
            {"channel": "BBCNews.uk", "feed": "SD", "url": "http://b/1080",
             "quality": "1080p", "labels": []},
            {"channel": "BBCNews.uk", "feed": "SD", "url": "http://b/720",
             "quality": "720p", "labels": []},
            {"channel": "BBCNews.uk", "url": "http://b/live",
             "quality": "720p", "labels": ["Not 24/7"]},
            {"channel": "CBC.ca", "url": "https://c/x", "quality": None,
             "referrer": "https://cbc.ca/", "user_agent": "UA/1"},
            {"channel": "Old.us", "url": "http://old/x"},
            {"channel": "Dead.us", "url": "http://dead/x"},
            {"channel": "Adult.us", "url": "http://adult/x"},
            {"channel": "Blocked.us", "url": "http://blocked/x"},
            {"channel": "Geo.es", "url": "http://geo/x",
             "labels": ["Geo-blocked"]},
            {"channel": "Telemundo.es", "url": "http://t/x",
             "labels": ["Not 24/7"]},
            {"channel": "Loose.fr", "url": "http://l/x"},
            {"channel": None, "url": "http://orphan/x"},
            {"channel": "CBC.ca", "url": "https://c/x"},
            {"channel": "CBC.ca", "url": "rtmp://c/x"},
        ],
        "blocklist": [{"channel": "Blocked.us", "reason": "dmca"}],
        "feeds": [
            {"channel": "BBCNews.uk", "id": "SD", "is_main": True,
             "languages": ["eng"]},
            {"channel": "Telemundo.es", "id": "Main", "is_main": True,
             "languages": ["spa"]},
        ],
        "logos": [
            {"channel": "BBCNews.uk", "feed": "SD", "width": 1000,
             "url": "http://logo/feed.png"},
            {"channel": "BBCNews.uk", "feed": None, "width": 100,
             "url": "http://logo/small.png"},
            {"channel": "BBCNews.uk", "feed": None, "width": 512,
             "url": "http://logo/big.png"},
        ],
        "countries": [{"code": "GB", "name": "United Kingdom"},
                      {"code": "CA", "name": "Canada"},
                      {"code": "US", "name": "United States"},
                      {"code": "ES", "name": "Spain"},
                      {"code": "FR", "name": "France"},
                      {"code": "NL", "name": "Netherlands"}],
        "categories": [{"id": "news", "name": "News"},
                       {"id": "movies", "name": "Movies"},
                       {"id": "kids", "name": "Kids"},
                       {"id": "documentary", "name": "Documentary"},
                       {"id": "sports", "name": "Sports"}],
        "languages": [{"code": "eng", "name": "English"},
                      {"code": "spa", "name": "Spanish"}],
    }


def suite_build(r: Results) -> None:
    cat = build_catalogue(_raw(), now=T0)
    r.check("kept channels", sorted(cat.channels),
            ["BBCNews.uk", "CBC.ca", "Loose.fr", "New.us", "Telemundo.es"])
    d = cat.dropped
    r.check("closed with a live replacement follows it", "New.us" in
            cat.channels and "Old.us" not in cat.channels, True)
    r.check("closed with none is dropped", d.get("channel closed"), 1)
    r.check("nsfw dropped", d.get("nsfw"), 1)
    r.check("blocklist dropped", d.get("blocklisted"), 1)
    r.check("geo-blocked label dropped", d.get("labelled Geo-blocked"), 1)
    r.check("no channel record dropped", d.get("no channel record"), 1)
    r.check("duplicate url dropped", d.get("duplicate url"), 1)
    r.check("non-http url dropped", d.get("no usable url"), 1)
    r.ok("a channel whose only stream is geo-blocked disappears",
         "Geo.es" not in cat.channels)

    bbc = cat.channels["BBCNews.uk"]
    r.check("streams best first: 720p, then 1080p, part-time last",
            [s.url for s in bbc.streams],
            ["http://b/720", "http://b/1080", "http://b/live"])
    r.check("languages from the feed, or the main feed",
            bbc.languages, ["eng"])
    r.check("channel logo beats a feed logo, widest wins",
            bbc.logo, "http://logo/big.png")
    cbc = cat.channels["CBC.ca"]
    r.check("referrer and user agent kept",
            (cbc.streams[0].referrer, cbc.streams[0].user_agent),
            ("https://cbc.ca/", "UA/1"))
    r.ok("all streams Not 24/7 marks the channel",
         cat.channels["Telemundo.es"].part_time and not bbc.part_time)
    r.check("every country and category name is kept, used or not",
            (len(cat.country_names), "documentary" in cat.category_names),
            (6, True))
    r.check("only languages in use are kept", sorted(cat.language_names),
            ["eng", "spa"])

    back = Catalogue.from_json(json.loads(json.dumps(cat.to_json())))
    r.check("json round trip", (len(back), back.stream_count(),
                                back.channels["BBCNews.uk"].streams[0].url),
            (len(cat), cat.stream_count(), "http://b/720"))
    r.check("quality heights", [I.quality_height(q) for q in
                                ("720p", "1080i", "576", None, "HD")],
            [720, 1080, 576, None, None])


def suite_health(r: Results) -> None:
    h = HealthBook()
    u = "http://x"
    r.check("unknown is unchecked", h.status(u, T0), I.UNCHECKED)
    r.check("...and due now", h.next_check(u), 0.0)
    h.record(u, True, T0, height=720)
    r.check("a pass is ok", h.status(u, T0 + 3600), I.OK)
    r.check("re-check a day later", h.next_check(u), T0 + I.RECHECK_OK)
    r.check("a stale pass is unchecked again",
            h.status(u, T0 + I.OK_FRESH + 1), I.UNCHECKED)
    h.record(u, False, T0 + 10, why="http 403")
    r.check("a later failure wins", h.status(u, T0 + 20), I.FAILED)
    r.check("first failure: back in a day", h.next_check(u),
            T0 + 10 + I.RECHECK_FAILED[0])
    for i in range(6):
        h.record(u, False, T0 + 100 + i)
    r.check("repeat failures back off to a week", h.next_check(u),
            T0 + 105 + I.RECHECK_FAILED[-1])
    h.record(u, True, T0 + 200)
    r.check("a pass resets the failures", h.records[u]["fails"], 0)
    r.check("prune drops urls no longer listed",
            (h.prune({"http://other"}), h.records), (1, {}))


def suite_scheduling(r: Results) -> None:
    cat = build_catalogue(_raw(), now=T0)
    h = HealthBook()
    first = due_streams(cat, h, T0, 100)
    unlisted_first = [s for s in first[:len(cat)]]
    r.check("cold cache: one stream per channel first",
            len({s.channel for s in unlisted_first}), len(cat))
    r.check("...news in English first", first[0].url, "http://b/720")
    r.check("...each channel's best stream first", first[0].url,
            cat.channels["BBCNews.uk"].streams[0].url)
    r.check("everything is due on a cold cache", len(first),
            cat.stream_count())

    h.record("http://b/720", True, T0)
    nxt = due_streams(cat, h, T0 + 1, 100)
    r.ok("a passed stream isn't due again yet",
         "http://b/720" not in [s.url for s in nxt])
    r.ok("a listed channel's spare streams wait behind unlisted ones",
         [s.channel for s in nxt].index("BBCNews.uk") >= len(cat) - 1)
    r.ok("listed after one pass", is_listed(cat.channels["BBCNews.uk"],
                                            h, T0 + 1))
    for s in cat.channels["CBC.ca"].streams:
        h.record(s.url, False, T0, why="timeout")
    r.ok("all streams failed: not playable",
         not is_playable(cat.channels["CBC.ca"], h, T0 + 1))
    r.ok("unchecked is playable on request, though not listed",
         is_playable(cat.channels["New.us"], h, T0 + 1)
         and not is_listed(cat.channels["New.us"], h, T0 + 1))
    r.ok("a failed stream isn't due before its back-off",
         "https://c/x" not in [s.url for s in due_streams(cat, h, T0 + 5,
                                                          100)])


def suite_search(r: Results) -> None:
    cat = build_catalogue(_raw(), now=T0)
    names = lambda cs: [c.id for c in cs]
    r.check("exact name", names(search(cat, "bbc news"))[:1], ["BBCNews.uk"])
    r.check("alt name", names(search(cat, "BBC"))[:1], ["BBCNews.uk"])
    r.check("accents and case ignored", names(search(cat, "chaine libre")),
            ["Loose.fr"])
    r.check("words in any order", names(search(cat, "news cbc")),
            ["CBC.ca"])
    r.check("typo tolerated", names(search(cat, "Telemundo Kidz")),
            ["Telemundo.es"])
    r.check("short queries aren't fuzzy", names(search(cat, "bxc")), [])
    r.check("no typo matches beside a real match",
            names(search(cat, "telemundo kids")), ["Telemundo.es"])
    r.check("country filter", names(search(cat, country="CA")), ["CBC.ca"])
    r.check("category filter", sorted(names(search(cat, category="news"))),
            ["BBCNews.uk", "CBC.ca"])
    r.check("'other' is the uncategorised", names(
        search(cat, category=I.OTHER_CATEGORY)), ["Loose.fr"])
    r.check("language filter", names(search(cat, language="spa")),
            ["Telemundo.es"])
    r.check("listing gate applies", names(search(
        cat, category="news", include=lambda c: c.id == "CBC.ca")),
        ["CBC.ca"])

    pluto = Catalogue(channels={
        "A": I.Channel(id="A", name="Investiga", network="Pluto TV",
                       alt_names=["Pluto TV Investiga"]),
        "B": I.Channel(id="B", name="Star Trek", network="Pluto TV"),
        "C": I.Channel(id="C", name="Pluto TV FBI", network="Pluto TV"),
        "D": I.Channel(id="D", name="Plutonium Docs")})
    for q in ("Pluto TV", "PlutoTV", "PlutoTV channels", "the pluto tv ones"):
        r.check(f"network search: {q!r}", sorted(names(search(pluto, q))),
                ["A", "B", "C"])
    r.check("filler words stripped", I.clean_query("the BBC channels"),
            "bbc")
    r.check("'tv' is kept: it's in real names", I.clean_query("Pluto TV"),
            "pluto tv")

    r.check("countries resolve", [resolve_country(x, cat) for x in
                                  ("UK", "gb", "Canada", "the netherlands",
                                   "spain", "Atlantis")],
            ["GB", "GB", "CA", "NL", "ES", None])
    r.check("categories resolve", [resolve_category(x, cat) for x in
                                   ("news", "Sports", "documentaries",
                                    "the kids ones", "movie channels",
                                    "other", "cars")],
            ["news", "sports", "documentary", "kids", "movies",
             I.OTHER_CATEGORY, None])
    r.check("languages resolve", [resolve_language(x, cat) for x in
                                  ("Spanish", "eng", "Klingon")],
            ["spa", "eng", None])

    idx = category_index(cat.channels.values(), cat.category_names)
    r.check("category index: biggest first, Other last",
            [(k, n) for k, _, n in idx],
            [("news", 2), ("kids", 1), ("movies", 1),
             (I.OTHER_CATEGORY, 1)])
    r.check("display names", [name for _, name, _ in idx],
            ["News", "Kids", "Movies", "Other"])

    ov = I.overview(list(cat.channels.values()), cat)
    r.check("overview total", ov["total"], 5)
    r.check("countries: most first, ties by name", ov["countries"][:2],
            [("CA", "Canada", 1), ("FR", "France", 1)])
    r.check("languages", [(k, n) for k, _, n in ov["languages"]],
            [("eng", 1), ("spa", 1)])
    r.check("categories match the category index", ov["categories"], idx)

    items = list(range(1, 41))
    r.check("page 1", page(items, 1)[1:], (1, 3, 1))
    sl, num, pages, first = page(items, 2)
    r.check("page 2 numbering runs on", (sl[0], num, pages, first),
            (16, 2, 3, 16))
    r.check("past the end clamps", page(items, 9)[1], 3)
    r.check("empty list is one page", page([], 1)[1:], (1, 1, 1))


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.640028"
hd/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
https://cdn.example/360.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXTINF:6.0,
seg100.ts
#EXTINF:6.0,
seg101.ts
"""


def suite_playlists(r: Results) -> None:
    base = "https://host.tv/live/master.m3u8"
    vs = parse_master(MASTER, base)
    r.check("variants parsed", [(v["height"], v["url"]) for v in vs],
            [(1080, "https://host.tv/live/hd/index.m3u8"),
             (720, "https://host.tv/live/720/index.m3u8"),
             (360, "https://cdn.example/360.m3u8")])
    r.check("picks 720p", pick_variant(vs)["height"], 720)
    r.check("only bigger: the smallest of them",
            pick_variant([{"height": 2160, "url": "a"},
                          {"height": 1080, "url": "b"}])["url"], "b")
    r.check("no resolutions: the first", pick_variant(
        [{"height": None, "url": "a"}])["url"], "a")
    r.check("a media playlist has no variants", parse_master(MEDIA, base),
            [])
    r.check("newest segment, AES-128 is fine",
            parse_media(MEDIA, base),
            ("https://host.tv/live/seg101.ts", False))
    drm = MEDIA.replace('METHOD=AES-128', 'METHOD=SAMPLE-AES,'
                        'KEYFORMAT="com.widevine"')
    r.check("widevine is DRM", parse_media(drm, base)[1], True)
    r.check("empty media playlist", parse_media("#EXTM3U\n", base),
            (None, False))


class _Fetch:
    def __init__(self, raw):
        self.raw, self.fail, self.calls = raw, False, 0

    async def __call__(self, name):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        return self.raw[name]


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def suite_service(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock(T0)
        fetch = _Fetch(_raw())
        probed = []

        async def probe(s):
            probed.append(s.url)
            return Probe(s.url != "https://c/x", "http 403", 720)

        svc = IptvCatalogue(tmp, fetch=fetch, probe=probe, clock=clock)
        r.ok("starts empty, refresh due", len(svc.catalogue) == 0
             and svc.refresh_due())
        checked = asyncio.run(svc.step())
        r.check("first step refreshes and checks a batch",
                (len(svc.catalogue), checked), (5, I.CONCURRENCY))
        r.ok("catalogue written", (Path(tmp) / I.CATALOGUE_FILE).exists())

        while asyncio.run(svc.step()):
            pass
        r.check("every stream checked once", sorted(probed),
                sorted(svc.catalogue.urls()))
        c = svc.counts()
        r.check("counts", (c[I.OK], c[I.FAILED], c[I.UNCHECKED],
                           c["listed"]), (6, 1, 0, 4))
        r.check("listed excludes the dead channel",
                sorted(ch.id for ch in svc.listed_channels()),
                ["BBCNews.uk", "Loose.fr", "New.us", "Telemundo.es"])
        r.ok("health written", (Path(tmp) / I.HEALTH_FILE).exists())

        svc.mark("http://b/720", False, "stalled on air")
        r.check("playback failure recorded at once",
                svc.health.status("http://b/720", clock()), I.FAILED)

        # A restart reads both files back.
        svc.save(force=True)
        again = IptvCatalogue(tmp, fetch=fetch, probe=probe, clock=clock)
        r.check("reloaded from disk", (len(again.catalogue),
                                       again.counts()["listed"]), (5, 4))
        r.ok("no refresh needed yet", not again.refresh_due())

        # Refresh failures keep yesterday's catalogue.
        clock.t += I.REFRESH_EVERY + 1
        fetch.fail = True
        r.check("failed refresh keeps the old catalogue",
                (asyncio.run(again.refresh()), len(again.catalogue)),
                (False, 5))
        r.ok("...and retries in an hour, not every loop",
             not again.refresh_due())
        clock.t += I.HOUR + 1
        fetch.fail = False
        tiny = _raw()
        tiny["streams"] = tiny["streams"][:1]
        fetch.raw = tiny
        r.check("a refresh far smaller than before is refused",
                (asyncio.run(again.refresh()), len(again.catalogue)),
                (False, 5))
        clock.t += I.HOUR + 1
        fetch.raw = _raw()
        r.ok("a good refresh is taken", asyncio.run(again.refresh()))

        # Paused while streaming, idle when no server allows it.
        busy = IptvCatalogue(tmp, fetch=fetch, probe=probe, clock=clock,
                             busy=lambda: True)
        unwanted = IptvCatalogue(tmp, fetch=fetch, probe=probe, clock=clock,
                                 wanted=lambda: False)
        clock.t += I.OK_FRESH + 1          # everything stale, all due
        n = len(probed)
        r.check("no checks while a stream is on air or unwanted",
                (asyncio.run(busy.step()), asyncio.run(unwanted.step()),
                 len(probed) - n), (0, 0, 0))

        async def boom(s):
            raise RuntimeError("bug")
        crashy = IptvCatalogue(tmp, fetch=fetch, probe=boom, clock=clock)
        res = asyncio.run(crashy.check(crashy.catalogue.channels[
            "BBCNews.uk"].streams[0]))
        r.check("a probe that raises is a failure, not a crash",
                (res.ok, res.why), (False, "probe error: RuntimeError"))


def suite_run_process(r: Results) -> None:
    async def go():
        quick = await I.run_process(["sh", "-c", "echo out; echo err >&2;"
                                     " exit 3"], 5)
        slow = await I.run_process(["sleep", "30"], 0.5)
        return quick, slow
    quick, slow = asyncio.run(go())
    r.check("exit code and both pipes", quick, (3, "out\n", "err\n"))
    r.check("timeout kills and reports None", slow[0], None)


def suite_quiet_probes(r: Results) -> None:
    seen = []

    class _Catch(logging.Handler):
        def emit(self, record): seen.append(record.getMessage())

    log = logging.getLogger("httpx")
    handler, level = _Catch(), log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        async def probe_like():
            I._PROBING.set(True)
            log.info("HTTP Request: GET http://dead/x 404")

        async def elsewhere():
            log.info("HTTP Request: GET http://radio/np 500")

        async def both():
            await asyncio.gather(probe_like(), elsewhere())
        asyncio.run(both())
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    r.check("a probe's httpx lines are dropped, others kept", seen,
            ["HTTP Request: GET http://radio/np 500"])


def suite_exit_codes(r: Results) -> None:
    """A probe that printed its result counts, whatever exit code asyncio
    reports (it reports 255 for a child something else reaped)."""
    import corbin.core.iptv as M
    s = I.Stream(url="http://x/raw.ts", channel="c")
    saved = M.run_process

    def fake(code, out, err=""):
        async def run(args, timeout):
            return code, out, err
        return run
    try:
        M.run_process = fake(255, '{"streams": [{"height": 720}]}')
        p = asyncio.run(M.ffprobe_stream(s))
        r.ok("printed a stream, 'exit 255': still a pass",
             p.ok and p.height == 720)
        M.run_process = fake(1, "", "http://x: 404 Not Found")
        p = asyncio.run(M.ffprobe_stream(s))
        r.check("a real failure keeps ffprobe's words",
                (p.ok, p.why), (False, "http://x: 404 Not Found"))
        M.run_process = fake(None, "")
        r.check("a timeout is a timeout",
                asyncio.run(M.ffprobe_stream(s)).why, "timeout")
    finally:
        M.run_process = saved


def suite_no_refresh_while_streaming(r: Results) -> None:
    """Owner, 2026-10-20: the daily refresh waits while anything streams."""
    calls = []

    async def fetch(name):
        calls.append(name)
        return _raw()[name]
    with tempfile.TemporaryDirectory() as tmp:
        streaming = [True]
        svc = I.IptvCatalogue(tmp, fetch=fetch, busy=lambda: streaming[0])
        asyncio.run(svc.step())
        r.check("due, but a stream is on: no refresh", calls, [])
        streaming[0] = False
        asyncio.run(svc.step())
        r.ok("the stream ends: it refreshes",
             calls and len(svc.catalogue) > 0)


SUITES = [
    ("iptv: no refresh while streaming", suite_no_refresh_while_streaming),
    ("iptv: exit codes don't decide", suite_exit_codes),
    ("iptv catalogue build", suite_build),
    ("iptv health book", suite_health),
    ("iptv check order", suite_scheduling),
    ("iptv search and browsing", suite_search),
    ("iptv HLS playlists", suite_playlists),
    ("iptv service", suite_service),
    ("iptv subprocess", suite_run_process),
    ("iptv quiet probes", suite_quiet_probes),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
