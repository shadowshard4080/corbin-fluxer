"""Resolving Spotify links to something playable.

Spotify streams are DRM'd and yt-dlp cannot touch them, so a Spotify
link is treated as a *description of a song*: fetch the title and
artists from Spotify's API, then find that song on YouTube. This is what
the Discord bot has always done; the Fluxer port simply never had it,
so pasting a track link got "I couldn't find or load that."

Credentials are the client-credentials flow -- no user login, no
refresh, just an app id and secret. Without them link resolution is off
and the caller says so rather than failing as a bad search.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("corbin.spotify")

#: open.spotify.com/track/<id>, spotify:track:<id>, and the regional
#: form open.spotify.com/intl-de/track/<id> that share sheets emit.
_LINK = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|playlist|album)/"
    r"|spotify:(track|playlist|album):)([A-Za-z0-9]+)", re.I)
#: Tracks pulled from a playlist or album in one go.
MAX_COLLECTION = 50


@dataclass
class SpotifyTrack:
    title: str
    artists: str

    @property
    def query(self) -> str:
        """What to search YouTube for."""
        return f"{self.artists} - {self.title}" if self.artists else self.title


def parse_link(text: str) -> Optional[tuple[str, str]]:
    """``(kind, id)`` for the first Spotify link in ``text``, else None."""
    match = _LINK.search(text or "")
    if not match:
        return None
    kind = (match.group(1) or match.group(2) or "").lower()
    return (kind, match.group(3)) if kind else None


def is_spotify_link(text: str) -> bool:
    return parse_link(text) is not None


class Spotify:
    """Thin spotipy wrapper. Every call is blocking, so all are threaded."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._id, self._secret = client_id, client_secret
        self._client: Any = None
        self._tried = False

    @property
    def configured(self) -> bool:
        return bool(self._id and self._secret)

    def _connect(self) -> Any:
        if self._client is not None or self._tried:
            return self._client
        self._tried = True
        if not self.configured:
            return None
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyClientCredentials

            self._client = spotipy.Spotify(
                client_credentials_manager=SpotifyClientCredentials(
                    client_id=self._id, client_secret=self._secret))
        except Exception:
            logger.warning("spotipy unavailable; Spotify links will not "
                           "resolve", exc_info=True)
            self._client = None
        return self._client

    async def resolve(self, text: str) -> list[SpotifyTrack]:
        """Tracks described by a Spotify link. Empty if it cannot resolve.

        A playlist or album yields several, capped at
        ``MAX_COLLECTION`` -- each one costs a YouTube search later, so
        an 800-track playlist must not be taken literally.
        """
        parsed = parse_link(text)
        if parsed is None:
            return []
        kind, sid = parsed
        client = await asyncio.to_thread(self._connect)
        if client is None:
            return []
        try:
            return await asyncio.to_thread(self._fetch, client, kind, sid)
        except Exception:
            logger.info("Spotify lookup failed for %s %s", kind, sid,
                        exc_info=True)
            return []

    @staticmethod
    def _names(item: dict) -> str:
        return ", ".join(
            a.get("name", "") for a in (item.get("artists") or [])
            if a.get("name"))

    def _fetch(self, client: Any, kind: str, sid: str) -> list[SpotifyTrack]:
        if kind == "track":
            data = client.track(sid) or {}
            title = data.get("name") or ""
            return [SpotifyTrack(title, self._names(data))] if title else []

        if kind == "album":
            data = client.album_tracks(sid, limit=MAX_COLLECTION) or {}
            items = data.get("items") or []
        else:
            data = client.playlist_items(sid, limit=MAX_COLLECTION) or {}
            # Playlist rows wrap the track; album rows are the track.
            items = [row.get("track") or {} for row in (data.get("items") or [])]

        found = []
        for item in items:
            title = (item or {}).get("name") or ""
            if title:
                found.append(SpotifyTrack(title, self._names(item)))
        return found
