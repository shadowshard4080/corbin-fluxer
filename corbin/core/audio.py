"""Audio sourcing: yt-dlp, PO tokens, cookies, and the retry ladder.

This is a faithful port of the hard-won parts of ``audioplayer_cog.py``.
The constants and client orderings below are **measured**, not guessed, and
the reasoning is preserved in the comments because it is not re-derivable:

* A PO token provider running beside the bot is the durable fix for
  YouTube's "Sign in to confirm you're not a bot". Client rotation does not
  fix it.
* Which player clients work depends on whether that provider is up, and the
  wrong order makes extraction *succeed* and then FFmpeg 403 on the stream
  -- which yt-dlp never raises, so it looks like a track that plays for
  zero seconds. That is what ``IMMEDIATE_FAIL_SECS`` detects.
* With a cookiefile set, yt-dlp skips ``tv_simply``/``android_vr`` entirely
  ("does not support cookies"), so only the web family is really in play.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("corbin.audio")

POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL",
                                  "http://127.0.0.1:4416").rstrip("/")
POT_DISABLE = os.environ.get("POT_DISABLE", "").strip().lower() in (
    "1", "true", "yes")
POT_SCRIPT_HOME = os.environ.get("POT_SCRIPT_HOME", "").strip()
#: Re-probe no more often than this after a bot check (seconds).
POT_PROBE_THROTTLE = 60.0

#: Provider up: measured as the only clients still returning real audio.
#: web/web_safari get SABR-only streaming that yt-dlp discards.
POT_PLAYER_CLIENTS = ["mweb", "web_embedded", "web_safari", "web"]
#: Provider down: lead with the clients that do not need a token.
NO_POT_PLAYER_CLIENTS = ["tv_simply", "android_vr", "mweb", "web"]

#: Retries after the first attempt, so up to three tries.
MAX_PLAY_RETRIES = 2
#: A track ending sooner than this is a 403, not real playback.
IMMEDIATE_FAIL_SECS = 4.0
MIN_RETRYABLE_DURATION = 8
#: Each retry forces a different client so a fresh extraction mints a new
#: stream URL. Only the two clients that actually serve audio, measured.
RETRY_CLIENT_SETS = [["web_embedded", "mweb"], ["mweb", "web_embedded"]]

SEARCH_RESULTS = 5
#: Tracks taken from one playlist link. Matches spotify.MAX_COLLECTION;
#: every one costs an extraction, so a 2,000-song playlist is capped.
MAX_PLAYLIST = 50
#: A YouTube playlist page, as opposed to a video that happens to sit in
#: one (watch?v=X&list=Y). That second shape plays the one video, which is
#: what someone sharing a song from a mix means.
_YT_PLAYLIST = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?youtube\.com/playlist\?"
    r"(?:[^#]*&)?list=[\w-]+", re.IGNORECASE)


def is_youtube_playlist(url: str) -> bool:
    return bool(_YT_PLAYLIST.match((url or "").strip()))
EXTRACT_TIMEOUT = 60.0


def find_cookiefile() -> Optional[str]:
    """Locate cookies.txt. yt-dlp rewrites it in place, so keep a pristine
    copy elsewhere."""
    for candidate in (os.environ.get("YTDL_COOKIEFILE"),
                      "cookies.txt",
                      str(Path.home() / "cookies.txt")):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def base_opts() -> dict:
    opts = {
        "format": "bestaudio[acodec!=none]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
        "noplaylist": True,
        "extract_flat": False,
        "cachedir": False,
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/134.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },
        "remote_components": ["ejs:github", "ejs:npm"],
        "cookiefile": find_cookiefile(),
        "extractor_args": {
            "youtube": {"player_client": list(NO_POT_PLAYER_CLIENTS)},
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]},
        },
    }
    if POT_SCRIPT_HOME:
        opts["extractor_args"]["youtubepot-bgutilscript"] = {
            "server_home": [POT_SCRIPT_HOME]}
    return opts


def is_bot_check_error(message: str) -> bool:
    """YouTube's anti-bot gate. Not fixable by client rotation."""
    if not message:
        return False
    low = message.lower()
    return ("confirm you" in low and "not a bot" in low) or \
        "sign in to confirm" in low


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "live"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Track:
    title: str
    url: str           # stream url for ffmpeg
    webpage_url: str = ""
    duration: Optional[float] = None
    uploader: str = ""
    thumbnail: str = ""
    requested_by: str = ""
    requester_name: str = ""

    @property
    def pretty_duration(self) -> str:
        return format_duration(self.duration)

    @property
    def is_live(self) -> bool:
        return not self.duration


def track_from_info(info: dict, *, requested_by: str = "",
                    requester_name: str = "") -> Optional[Track]:
    if not isinstance(info, dict):
        return None
    stream = info.get("url")
    if not stream:
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("acodec") not in (None, "none") and fmt.get("url"):
                stream = fmt["url"]
                break
    if not stream:
        return None
    thumb = info.get("thumbnail") or ""
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url", "")
    return Track(
        title=str(info.get("title") or "Unknown"),
        url=str(stream),
        webpage_url=str(info.get("webpage_url") or info.get("original_url")
                        or ""),
        duration=info.get("duration"),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        thumbnail=str(thumb or ""),
        requested_by=requested_by, requester_name=requester_name,
    )


class Extractor:
    """Wraps yt-dlp, owning PO-token state and the client ordering."""

    def __init__(self, spotify: Any = None) -> None:
        self.pot_ready = False
        self._last_probe = 0.0
        self._client_order = list(NO_POT_PLAYER_CLIENTS)
        #: Optional :class:`~corbin.core.spotify.Spotify`. Spotify streams
        #: are DRM'd, so a link is resolved to "artist - title" and looked
        #: up on YouTube instead.
        self.spotify = spotify

    async def probe_pot(self, force: bool = False) -> bool:
        """Ping the PO token provider and swap the client order to match.

        Throttled, so a burst of bot-check failures does not hammer it. A
        provider that came up late is picked up without a bot restart.
        """
        if POT_DISABLE:
            self.pot_ready = False
            self._client_order = list(NO_POT_PLAYER_CLIENTS)
            return False
        now = time.monotonic()
        if not force and (now - self._last_probe) < POT_PROBE_THROTTLE:
            return self.pot_ready
        self._last_probe = now

        ready = False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{POT_PROVIDER_URL}/ping")
            ready = resp.status_code == 200
        except Exception:
            ready = False

        if ready != self.pot_ready:
            logger.info("PO token provider is %s; using clients %s",
                        "up" if ready else "down",
                        POT_PLAYER_CLIENTS if ready else NO_POT_PLAYER_CLIENTS)
        self.pot_ready = ready
        self._client_order = list(
            POT_PLAYER_CLIENTS if ready else NO_POT_PLAYER_CLIENTS)
        return ready

    def opts(self, *, clients: Optional[Sequence[str]] = None,
             flat: bool = False, playlist: bool = False) -> dict:
        opts = base_opts()
        opts["extractor_args"]["youtube"]["player_client"] = list(
            clients or self._client_order)
        if playlist:
            opts["noplaylist"] = False
        if flat:
            opts["extract_flat"] = "in_playlist"
        return opts

    async def extract(self, query: str, *,
                      clients: Optional[Sequence[str]] = None,
                      playlist: bool = False, flat: bool = False) -> dict:
        """Run yt-dlp off the event loop. Raises on failure."""
        import yt_dlp

        def _run() -> dict:
            with yt_dlp.YoutubeDL(self.opts(clients=clients,
                                            playlist=playlist,
                                            flat=flat)) as ydl:
                return ydl.extract_info(query, download=False)

        return await asyncio.wait_for(asyncio.to_thread(_run),
                                      timeout=EXTRACT_TIMEOUT)

    async def resolve(self, query: str, *, requested_by: str = "",
                      requester_name: str = "",
                      allow_playlist: bool = False
                      ) -> tuple[list[Track], Optional[str]]:
        """Turn a URL or search phrase into tracks. Returns (tracks, error)."""
        if not query.strip():
            return [], "Give me something to play."
        target = query.strip()

        # Spotify first: yt-dlp cannot play a DRM'd Spotify stream, so
        # handing it the link just fails. Turn it into the song's name
        # and search YouTube for that.
        if self.spotify is not None:
            from .spotify import is_spotify_link

            if is_spotify_link(target):
                wanted = await self.spotify.resolve(target)
                if not wanted:
                    if not self.spotify.configured:
                        return [], ("I can't read Spotify links — no "
                                    "Spotify credentials configured. Give "
                                    "me the song name instead.")
                    return [], ("Spotify wouldn't tell me what that link "
                                "is. Give me the song name instead.")
                found, failed = [], 0
                for item in wanted[:1] if not allow_playlist else wanted:
                    tracks, _err = await self.resolve(
                        item.query, requested_by=requested_by,
                        requester_name=requester_name)
                    if tracks:
                        found.append(tracks[0])
                    else:
                        failed += 1
                if not found:
                    return [], (f"Found \"{wanted[0].query}\" on Spotify "
                                f"but couldn't get it off YouTube.")
                if failed:
                    logger.info("%d of %d Spotify tracks had no YouTube "
                                "match", failed, len(wanted))
                return found, None

        if not target.startswith(("http://", "https://")):
            target = f"ytsearch1:{target}"

        try:
            info = await self.extract(target, playlist=allow_playlist)
        except asyncio.TimeoutError:
            return [], "That took too long to look up."
        except Exception as exc:
            message = str(exc)
            if is_bot_check_error(message):
                await self.probe_pot(force=True)
                return [], (
                    "YouTube is asking me to prove I'm not a bot. That needs "
                    "a PO token provider or fresh cookies on the host; "
                    "switching clients won't fix it.")
            logger.info("extraction failed for %r: %s", query, message)
            return [], "I couldn't find or load that."

        entries = info.get("entries") if isinstance(info, dict) else None
        raw = list(entries) if entries else [info]
        tracks = []
        for item in raw:
            track = track_from_info(item, requested_by=requested_by,
                                    requester_name=requester_name)
            if track:
                tracks.append(track)
        if not tracks:
            return [], "Nothing playable came back for that."
        return tracks, None

    async def collection(self, query: str) -> Optional[list[str]]:
        """Per-track queries for a playlist or album link.

        None when ``query`` is not a collection at all, so the caller
        resolves it as one track. ``[]`` when it is one but could not be
        read. Nothing here fetches a stream: a YouTube playlist is listed
        flat (titles and page URLs only), because extracting 50 streams up
        front took minutes before the first note played, and stream URLs
        expire while they sit in a queue anyway. Each entry is resolved
        when the importer reaches it.
        """
        text = (query or "").strip()
        if self.spotify is not None:
            from .spotify import parse_link

            parsed = parse_link(text)
            if parsed and parsed[0] in ("playlist", "album"):
                wanted = await self.spotify.resolve(text)
                return [w.query for w in wanted][:MAX_PLAYLIST]
        if is_youtube_playlist(text):
            try:
                info = await self.extract(text, playlist=True, flat=True)
            except Exception as exc:
                logger.info("playlist listing failed for %r: %s", text, exc)
                return []
            out = []
            for entry in (info or {}).get("entries") or ():
                if not isinstance(entry, dict):
                    continue
                url = entry.get("url") or entry.get("webpage_url")
                if not url and entry.get("id"):
                    url = f"https://www.youtube.com/watch?v={entry['id']}"
                if url:
                    out.append(url)
            return out[:MAX_PLAYLIST]
        return None

    async def search(self, query: str,
                     limit: int = SEARCH_RESULTS) -> list[dict]:
        """Titles for a search, without resolving streams."""
        try:
            info = await self.extract(f"ytsearch{limit}:{query}")
        except Exception:
            return []
        return [e for e in (info.get("entries") or []) if isinstance(e, dict)]
