"""Offline suite for Spotify link resolution."""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.spotify import (MAX_COLLECTION, Spotify, SpotifyTrack,
                                 is_spotify_link, parse_link)


class FakeSpotipy:
    def __init__(self, *, fail=False, count=1):
        self.fail, self.count = fail, count

    def track(self, sid):
        if self.fail:
            raise RuntimeError("404")
        return {"name": "The Raven", "artists": [{"name": "Rotting Christ"}]}

    def playlist_items(self, sid, limit=MAX_COLLECTION):
        self.limit = limit
        return {"items": [{"track": {"name": f"T{i}",
                                     "artists": [{"name": "A"}]}}
                          for i in range(self.count)] + [{"track": None}]}

    def album_tracks(self, sid, limit=MAX_COLLECTION):
        return {"items": [{"name": "B",
                           "artists": [{"name": "Y"}, {"name": "Z"}]}]}


def connected(client):
    sp = Spotify("id", "secret")
    sp._client, sp._tried = client, True
    return sp


def suite_links(r: Results) -> None:
    r.check("plain track link",
            parse_link("https://open.spotify.com/track/2jF1RlFrNtdCDbbVV051CG"),
            ("track", "2jF1RlFrNtdCDbbVV051CG"))
    # Share sheets emit a regional prefix; the old shape would miss it.
    r.check("regional prefix",
            parse_link("https://open.spotify.com/intl-de/track/ABC123?si=x"),
            ("track", "ABC123"))
    r.check("uri form", parse_link("spotify:track:XYZ"), ("track", "XYZ"))
    r.check("playlist", parse_link("https://open.spotify.com/playlist/P1"),
            ("playlist", "P1"))
    r.check("album", parse_link("https://open.spotify.com/album/A1"),
            ("album", "A1"))
    r.check("found mid-sentence",
            parse_link("play spotify:track:Q please"), ("track", "Q"))
    r.check("a youtube link is not spotify",
            parse_link("https://youtube.com/watch?v=x"), None)
    r.check("an artist page is not a track",
            parse_link("https://open.spotify.com/artist/A1"), None)
    r.ok("predicate agrees", is_spotify_link("spotify:track:Q")
         and not is_spotify_link("nothing here"))


def suite_query(r: Results) -> None:
    r.check("artist leads the search query",
            SpotifyTrack("The Raven", "Rotting Christ").query,
            "Rotting Christ - The Raven")
    r.check("no artist degrades to the title",
            SpotifyTrack("Untitled", "").query, "Untitled")


def suite_resolve(r: Results) -> None:
    sp = connected(FakeSpotipy())
    got = asyncio.run(sp.resolve("https://open.spotify.com/track/X"))
    r.check("track resolves to one search",
            [t.query for t in got], ["Rotting Christ - The Raven"])

    got = asyncio.run(sp.resolve("https://open.spotify.com/album/A"))
    r.check("multiple artists joined", [t.query for t in got], ["Y, Z - B"])

    sp = connected(FakeSpotipy(count=3))
    got = asyncio.run(sp.resolve("https://open.spotify.com/playlist/P"))
    r.check("playlist yields each track", len(got), 3)
    r.ok("a null playlist row is dropped, not crashed",
         all(t.title for t in got))
    r.check("collection fetch is capped", sp._client.limit, MAX_COLLECTION)

    r.check("a failing lookup is empty, not an exception",
            asyncio.run(connected(FakeSpotipy(fail=True)).resolve(
                "spotify:track:X")), [])
    r.check("a non-spotify string resolves to nothing",
            asyncio.run(sp.resolve("just a song name")), [])


def suite_unconfigured(r: Results) -> None:
    sp = Spotify("", "")
    r.ok("reports itself unconfigured", not sp.configured)
    r.check("...and resolves nothing rather than erroring",
            asyncio.run(sp.resolve("spotify:track:X")), [])
    r.ok("configured when both halves present",
         Spotify("a", "b").configured)
    r.ok("an id without a secret is not configured",
         not Spotify("a", "").configured)


SUITES = [
    ("spotify link parsing", suite_links),
    ("spotify search queries", suite_query),
    ("spotify resolution", suite_resolve),
    ("spotify without credentials", suite_unconfigured),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
