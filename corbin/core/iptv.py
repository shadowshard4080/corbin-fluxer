"""Live TV catalogue from iptv-org: Track T1 of ``VOICE_ROADMAP.md``.

What this module owns:

* **The catalogue.** iptv-org's JSON API, downloaded once a day, joined
  (streams to channels, feeds for languages, logos) and filtered the way
  the owner decided: closed channels (following ``replaced_by``), NSFW,
  the blocklist, and every stream labelled ``Geo-blocked`` go. Saved to
  ``fluxer_iptv_cache.json``. A failed or suspiciously small refresh keeps
  the old catalogue; it never empties it.
* **The health check.** 0f measured 54% of streams playing from the
  server, and the ``Geo-blocked`` label predicted none of the 403s, so a
  channel is *listed* only when one of its streams passed recently.
  Results live in ``fluxer_iptv_health.json``, separate from the
  catalogue so a probe doesn't rewrite megabytes.
* **Search and browsing**, as pure functions: by name (exact, prefix,
  substring, words, fuzzy), by country, category and language, and the
  category index T1b answers "what can you put on?" with.

The check is deliberately not ffprobe for HLS. ffprobe reads seconds of
video per stream -- several MB, which over ~15,000 streams a day is tens
of GB. For HLS (every stream that played in 0f) it fetches the playlist,
the variant playlist and the first 4 KB of a segment, about 10 KB. That
catches what 0f saw fail (timeouts, 403/404, DNS, TLS, 5xx, DRM). It
can't see a stream that opens and then can't keep up; T2's probe before
announcing does that. Anything that isn't HLS goes to a small ffprobe.

Platform-neutral: no SDK imports. httpx is imported lazily, so the
offline tests never need it.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urljoin

logger = logging.getLogger("corbin.iptv")

#: Set while a health probe runs. The bot keeps httpx's failed requests
#: in the log, and half the catalogue is dead, so every probe of a dead
#: stream logged a line: thousands a day saying what the health file and
#: the hourly count already say. httpx logs inside the probe's own task,
#: so a context variable picks out exactly those lines.
_PROBING: contextvars.ContextVar = contextvars.ContextVar(
    "iptv_probing", default=False)


class _QuietProbes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PROBING.get()


logging.getLogger("httpx").addFilter(_QuietProbes())

API_BASE = "https://iptv-org.github.io/api"
#: Every API file the catalogue is built from.
API_FILES = ("channels", "streams", "blocklist", "feeds", "logos",
             "countries", "categories", "languages")
CATALOGUE_FILE = "fluxer_iptv_cache.json"
HEALTH_FILE = "fluxer_iptv_health.json"

HOUR = 3600.0
DAY = 24 * HOUR
REFRESH_EVERY = DAY
#: A pass this recent lists the channel. Longer than RECHECK_OK, so a
#: working channel never drops out of the list while waiting its turn.
OK_FRESH = 36 * HOUR
RECHECK_OK = DAY
#: A failed stream waits longer each time it fails again.
RECHECK_FAILED = (DAY, 2 * DAY, 4 * DAY, 7 * DAY)
#: A failure seen during *playback* (a play probe, a drop on air) counts
#: for this long, not a day: a live stream that hiccups once is not dead.
#: Live, 2026-10-20: one drop at a programme boundary hid and refused
#: Pluto Classic Movie Westerns (its only stream) for 24 h.
SOFT_FAIL_FOR = 30 * 60
#: Reasons recorded by playback, recognised so marks made before
#: ``soft`` existed are treated the same.
SOFT_REASONS = ("play probe", "dropped on air")
#: 0f: dead streams gave up by 10 s at p90, live ones opened by 6 s.
PROBE_TIMEOUT = 10.0
#: Streams checked at once. The host is one core; this is network-bound.
CONCURRENCY = 2
#: How much smaller a refresh may be before it's treated as broken data.
MIN_REFRESH_RATIO = 0.3
STARTUP_DELAY = 60.0
IDLE_SLEEP = 600.0
BUSY_SLEEP = 30.0
LOG_EVERY = HOUR
SAVE_EVERY = 60.0

#: Sent when a stream doesn't name its own. T2's ffmpeg must send the
#: same, or the check and the playback are talking to different servers.
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36")
GEO_LABEL = "Geo-blocked"
NOT_247_LABEL = "Not 24/7"
OTHER_CATEGORY = "other"
#: Checked first on a cold cache, so the list fills with what people are
#: likely to ask for. The rest follow in catalogue order.
CATEGORY_PRIORITY = {
    "news": 6, "sports": 5, "movies": 5, "kids": 5, "animation": 4,
    "entertainment": 4, "documentary": 4, "music": 4, "comedy": 4,
    "science": 3, "series": 3, "general": 3, "weather": 3, "classic": 3,
    "outdoor": 2, "travel": 2, "cooking": 2, "family": 2, "lifestyle": 2,
    "relax": 2, "auto": 2, "business": 2, "culture": 2,
}
ENGLISH = "eng"
#: Words people use that the API spells differently.
COUNTRY_ALIASES = {
    "uk": "GB", "britain": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "usa": "US", "america": "US",
    "united states of america": "US", "the us": "US", "holland": "NL",
    "south korea": "KR", "korea": "KR", "russia": "RU", "uae": "AE",
}
FUZZY_CUTOFF = 0.82


# ------------------------------------------------------------------ models

@dataclass
class Stream:
    url: str
    channel: str
    feed: Optional[str] = None
    title: str = ""
    quality: Optional[str] = None
    labels: list = field(default_factory=list)
    referrer: Optional[str] = None
    user_agent: Optional[str] = None
    languages: list = field(default_factory=list)

    @property
    def height(self) -> Optional[int]:
        return quality_height(self.quality)

    @property
    def part_time(self) -> bool:
        return NOT_247_LABEL in self.labels


@dataclass
class Channel:
    id: str
    name: str
    alt_names: list = field(default_factory=list)
    network: Optional[str] = None
    country: str = ""
    categories: list = field(default_factory=list)
    languages: list = field(default_factory=list)
    logo: Optional[str] = None
    website: Optional[str] = None
    #: Best first for T2: a 540-720p source, then unknown, then larger
    #: (needs a downscale), then smaller; part-time ones last in each.
    streams: list = field(default_factory=list)

    @property
    def main_category(self) -> str:
        return self.categories[0] if self.categories else OTHER_CATEGORY

    @property
    def part_time(self) -> bool:
        """Every stream is Not 24/7: say so in a listing."""
        return bool(self.streams) and all(s.part_time for s in self.streams)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        streams = [Stream(**s) for s in d.get("streams") or []]
        return cls(**{**d, "streams": streams})


def quality_height(quality: Optional[str]) -> Optional[int]:
    m = re.match(r"\s*(\d{3,4})\s*[pi]?", quality or "")
    return int(m.group(1)) if m else None


def _stream_rank(s: Stream) -> tuple:
    h = s.height
    if h is None:
        band, near = 1, 0
    elif 540 <= h <= 720:
        band, near = 0, -h
    elif h > 720:
        band, near = 2, h
    else:
        band, near = 3, -h
    return (s.part_time, band, near)


# --------------------------------------------------------------- catalogue

@dataclass
class Catalogue:
    channels: dict = field(default_factory=dict)        # id -> Channel
    fetched_at: float = 0.0
    country_names: dict = field(default_factory=dict)   # code -> name
    category_names: dict = field(default_factory=dict)  # id -> name
    language_names: dict = field(default_factory=dict)  # code -> name
    dropped: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.channels)

    def stream_count(self) -> int:
        return sum(len(c.streams) for c in self.channels.values())

    def urls(self) -> set:
        return {s.url for c in self.channels.values() for s in c.streams}

    def to_json(self) -> dict:
        return {"fetched_at": self.fetched_at,
                "country_names": self.country_names,
                "category_names": self.category_names,
                "language_names": self.language_names,
                "dropped": self.dropped,
                "channels": [c.to_dict() for c in self.channels.values()]}

    @classmethod
    def from_json(cls, d: dict) -> "Catalogue":
        chans = [Channel.from_dict(c) for c in d.get("channels") or []]
        return cls(channels={c.id: c for c in chans},
                   fetched_at=float(d.get("fetched_at") or 0),
                   country_names=d.get("country_names") or {},
                   category_names=d.get("category_names") or {},
                   language_names=d.get("language_names") or {},
                   dropped=d.get("dropped") or {})


def _resolve_closed(chan: dict, channels: dict) -> Optional[dict]:
    """The channel itself, its replacement if closed, or None. One hop:
    a chain of replacements is rare and a loop would be worse."""
    if not chan.get("closed"):
        return chan
    repl = str(chan.get("replaced_by") or "").split("@")[0]
    target = channels.get(repl) if repl else None
    if target and not target.get("closed"):
        return target
    return None


def build_catalogue(raw: dict, *, now: float = 0.0) -> Catalogue:
    """Join and filter iptv-org's API files (``raw[name]`` is the parsed
    ``<name>.json``). Pure: the tests feed it a fixture."""
    channels = {c["id"]: c for c in raw.get("channels") or [] if c.get("id")}
    blocked = {b.get("channel") for b in raw.get("blocklist") or []}
    feeds = {}
    main_feed_langs: dict = {}
    for f in raw.get("feeds") or []:
        feeds[(f.get("channel"), f.get("id"))] = f
        if f.get("is_main"):
            main_feed_langs[f.get("channel")] = f.get("languages") or []
    logos: dict = {}
    for lg in raw.get("logos") or []:
        cid = lg.get("channel")
        # The channel's own logo (no feed) over a feed's, then the widest.
        rank = (lg.get("feed") is None, lg.get("width") or 0)
        if cid and lg.get("url") and (cid not in logos
                                      or rank > logos[cid][0]):
            logos[cid] = (rank, lg["url"])

    dropped: Counter = Counter()
    out: dict = {}
    seen_urls: set = set()
    for s in raw.get("streams") or []:
        cid = s.get("channel")
        chan = channels.get(cid) if cid else None
        labels = list(s.get("labels") or [])
        url = (s.get("url") or "").strip()
        if chan is None:
            dropped["no channel record"] += 1
            continue
        chan = _resolve_closed(chan, channels)
        if chan is None:
            dropped["channel closed"] += 1
        elif chan.get("is_nsfw"):
            dropped["nsfw"] += 1
        elif chan["id"] in blocked:
            dropped["blocklisted"] += 1
        elif GEO_LABEL in labels:
            dropped["labelled Geo-blocked"] += 1
        elif not url.startswith(("http://", "https://")):
            dropped["no usable url"] += 1
        elif url in seen_urls:
            dropped["duplicate url"] += 1
        else:
            seen_urls.add(url)
            feed = feeds.get((cid, s.get("feed")))
            langs = list((feed or {}).get("languages")
                         or main_feed_langs.get(chan["id"]) or [])
            c = out.get(chan["id"])
            if c is None:
                c = out[chan["id"]] = Channel(
                    id=chan["id"], name=chan.get("name") or chan["id"],
                    alt_names=list(chan.get("alt_names") or []),
                    network=chan.get("network"),
                    country=(chan.get("country") or "").upper(),
                    categories=list(chan.get("categories") or []),
                    logo=logos.get(chan["id"], (None, None))[1],
                    website=chan.get("website"))
            c.streams.append(Stream(
                url=url, channel=c.id, feed=s.get("feed"),
                title=s.get("title") or "", quality=s.get("quality"),
                labels=labels, referrer=s.get("referrer") or None,
                user_agent=s.get("user_agent") or None, languages=langs))
            for lang in langs:
                if lang not in c.languages:
                    c.languages.append(lang)
    for c in out.values():
        c.streams.sort(key=_stream_rank)

    # Every country and category name is kept, used or not, so "sports"
    # resolves even when no sports channel is up, and he can say there are
    # none rather than not knowing the word. Languages number in the
    # thousands, so only those in use.
    used_langs = {k for c in out.values() for k in c.languages}
    return Catalogue(
        channels=out, fetched_at=now, dropped=dict(dropped),
        country_names={x["code"]: x["name"]
                       for x in raw.get("countries") or [] if x.get("code")},
        category_names={x["id"]: x["name"]
                        for x in raw.get("categories") or [] if x.get("id")},
        language_names={x["code"]: x["name"]
                        for x in raw.get("languages") or []
                        if x.get("code") in used_langs})


# ------------------------------------------------------------------ health

OK, FAILED, UNCHECKED = "ok", "failed", "unchecked"


class HealthBook:
    """Per-stream check results, keyed by URL: ``{"last": "ok"|"failed",
    "ok": ts, "failed": ts, "fails": n, "why": str, "height": int}``."""

    def __init__(self, records: Optional[dict] = None) -> None:
        self.records: dict = records or {}

    @staticmethod
    def _soft(r: dict) -> bool:
        return bool(r.get("soft")) or str(r.get("why") or "").startswith(
            SOFT_REASONS)

    def status(self, url: str, now: float) -> str:
        r = self.records.get(url)
        if not r:
            return UNCHECKED
        ok = r.get("ok") or 0
        if r.get("last") == FAILED:
            # A playback failure expires on its own: the health check is
            # paused while anything streams, so it can't be left to clear
            # it.
            # Expired, it reads as whatever the stream was before the
            # hiccup: listed again if it had passed recently.
            if not (self._soft(r) and now - (r.get("failed") or 0)
                    > SOFT_FAIL_FOR):
                return FAILED
        if ok and now - ok <= OK_FRESH:
            return OK
        return UNCHECKED

    def next_check(self, url: str) -> float:
        """When this stream is due again (0 = now: never checked)."""
        r = self.records.get(url)
        if not r:
            return 0.0
        if r.get("last") == FAILED:
            if self._soft(r):
                return (r.get("failed") or 0) + SOFT_FAIL_FOR
            n = max(1, int(r.get("fails") or 1))
            return (r.get("failed") or 0) + \
                RECHECK_FAILED[min(n, len(RECHECK_FAILED)) - 1]
        return (r.get("ok") or 0) + RECHECK_OK

    def record(self, url: str, ok: bool, now: float, *, why: str = "",
               height: Optional[int] = None, soft: bool = False) -> None:
        """``soft``: a failure seen during playback (``SOFT_FAIL_FOR``),
        not by the health check."""
        r = self.records.setdefault(url, {})
        r["soft"] = bool(soft) and not ok
        # The latest result wins, even within the same second (a stream
        # that passed its check and then died on air).
        r["last"] = OK if ok else FAILED
        if ok:
            r.update(ok=now, fails=0, why="")
            if height:
                r["height"] = height
        else:
            r.update(failed=now, fails=int(r.get("fails") or 0) + 1,
                     why=why[:120])

    def prune(self, keep: set) -> int:
        gone = [u for u in self.records if u not in keep]
        for u in gone:
            del self.records[u]
        return len(gone)


def channel_priority(c: Channel) -> float:
    best = max((CATEGORY_PRIORITY.get(k, 1) for k in c.categories),
               default=1)
    return best + (2 if ENGLISH in c.languages else 0) \
        + 0.5 * min(len(c.streams), 3)


def is_listed(c: Channel, health: HealthBook, now: float) -> bool:
    return any(health.status(s.url, now) == OK for s in c.streams)


def is_playable(c: Channel, health: HealthBook, now: float) -> bool:
    """Worth trying on request: listed, or not yet known to be dead. T2
    probes before announcing anyway, so an unchecked channel is safe to
    try; it just isn't *offered* until it has passed."""
    return any(health.status(s.url, now) != FAILED for s in c.streams)


def due_streams(cat: Catalogue, health: HealthBook, now: float,
                limit: int) -> list:
    """The next ``limit`` streams to check, most useful first:

    0. channels not listed yet: one never-checked stream each (a channel
       needs only one pass to be listed), highest priority first
    1. channels not listed yet: their other due streams
    2. listed channels: due re-checks, so they stay listed
    3. listed channels: spare streams never checked (failover for T2)
    """
    picks: list = []
    for c in cat.channels.values():
        listed = is_listed(c, health, now)
        prio = channel_priority(c)
        first_new = True
        for i, s in enumerate(c.streams):
            if health.next_check(s.url) > now:
                continue
            new = s.url not in health.records
            if not listed:
                bucket = 0 if (new and first_new) else 1
            else:
                bucket = 3 if new else 2
            if new:
                first_new = False
            picks.append(((bucket, -prio, i, c.id), s))
    picks.sort(key=lambda p: p[0])
    return [s for _, s in picks[:limit]]


def health_counts(cat: Catalogue, health: HealthBook,
                  now: float) -> dict:
    counts = Counter(health.status(s.url, now)
                     for c in cat.channels.values() for s in c.streams)
    return {OK: counts[OK], FAILED: counts[FAILED],
            UNCHECKED: counts[UNCHECKED],
            "listed": sum(is_listed(c, health, now)
                          for c in cat.channels.values()),
            "channels": len(cat.channels)}


# ------------------------------------------------------- search & browsing

def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _forms(word: str) -> set:
    """The word and its plausible singulars: "movies" -> movie,
    "documentaries" -> documentary, "news" stays news. Over-generating is
    harmless: forms are only compared against real category names."""
    out = {word}
    if word.endswith("s") and not word.endswith("ss"):
        out.add(word[:-1])
    if word.endswith("ies"):
        out.add(word[:-3] + "y")
    return out


def resolve_country(text: str, cat: Catalogue) -> Optional[str]:
    """"UK", "gb", "Canada", "the Netherlands" -> an ISO code the
    catalogue uses, or None."""
    t = normalise(text)
    t = re.sub(r"^the ", "", t)
    if not t:
        return None
    if t in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[t]
    if len(t) == 2 and t.upper() in cat.country_names:
        return t.upper()
    names = {normalise(n): code for code, n in cat.country_names.items()}
    names.update({re.sub(r"^the ", "", k): v for k, v in names.items()})
    if t in names:
        return names[t]
    best = max(names, key=lambda n: SequenceMatcher(None, t, n).ratio(),
               default=None)
    if best and SequenceMatcher(None, t, best).ratio() >= FUZZY_CUTOFF:
        return names[best]
    return None


def resolve_category(text: str, cat: Catalogue) -> Optional[str]:
    """"news", "Sports", "documentaries", "the kids ones" -> a category
    id, or None. ``other`` is the catch-all for uncategorised channels."""
    t = normalise(text)
    t = re.sub(r"^the |( ones?| channels?| tv)$", "", t).strip()
    if not t:
        return None
    if t in (OTHER_CATEGORY, "uncategorised", "uncategorized", "misc"):
        return OTHER_CATEGORY
    forms = {}
    for cid, name in cat.category_names.items():
        for word in (cid, normalise(name)):
            for form in _forms(word):
                forms.setdefault(form, cid)
    for candidate in _forms(t):
        if candidate in forms:
            return forms[candidate]
    return None


def resolve_language(text: str, cat: Catalogue) -> Optional[str]:
    """"Spanish", "spa" -> a language code, or None."""
    t = normalise(text)
    if t in cat.language_names:
        return t
    for code, name in cat.language_names.items():
        if normalise(name) == t:
            return code
    return None


#: Words people add around a name that are never part of it: "Pluto TV
#: channels", "the BBC ones". "tv" is not here: it is in real names.
_FILLER = re.compile(r"^(the|any|all)\s+|\s+(channels?|ones?|stations?)$")


def clean_query(text: Optional[str]) -> str:
    q = normalise(text or "")
    while True:
        stripped = _FILLER.sub("", q).strip()
        if stripped == q:
            return q
        q = stripped


def _name_tier(q: str, c: Channel) -> Optional[int]:
    """0 exact, 1 prefix, 2 whole-word substring or the network, 3 every
    word, 4 a typo. Names are also compared with the spaces taken out, so
    "PlutoTV" finds "Pluto TV FBI"."""
    names = [normalise(n) for n in [c.name, *c.alt_names] if n]
    flat = q.replace(" ", "")
    flats = [n.replace(" ", "") for n in names]
    if q in names or flat in flats:
        return 0
    if any(n.startswith(q + " ") for n in names) or (
            len(flat) >= 4 and any(f.startswith(flat) for f in flats)):
        return 1
    if any(f" {q} " in f" {n} " for n in names):
        return 2
    # The network: 524 Pluto TV channels don't say "Pluto" in their name
    # ("Investiga", "Star Trek"), only in this field.
    network = normalise(c.network or "")
    if network and (q == network or flat == network.replace(" ", "")):
        return 2
    words = q.split()
    if len(words) > 1 and any(all(w in n.split() for w in words)
                              for n in names):
        return 3
    if len(q) >= 4 and any(SequenceMatcher(None, q, n).ratio()
                           >= FUZZY_CUTOFF for n in names):
        return 4
    return None


def search(cat: Catalogue, query: Optional[str] = None, *,
           country: Optional[str] = None, category: Optional[str] = None,
           language: Optional[str] = None,
           include: Optional[Callable[[Channel], bool]] = None) -> list:
    """Channels matching the name (if given) and every filter, best
    first. ``include`` is the listing gate (``is_listed`` for "what can
    you put on", ``is_playable`` for "put on X"). Filters are already
    resolved codes/ids."""
    q = clean_query(query) if query else ""
    found = []
    for c in cat.channels.values():
        if country and c.country != country:
            continue
        if category:
            if category == OTHER_CATEGORY:
                if c.categories:
                    continue
            elif category not in c.categories:
                continue
        if language and language not in c.languages:
            continue
        if include and not include(c):
            continue
        tier = _name_tier(q, c) if q else 0
        if tier is None:
            continue
        found.append(((tier, -channel_priority(c) if not q else 0,
                       len(c.name), c.name.lower()), c))
    found.sort(key=lambda x: x[0])
    # Typo matches only when nothing matched properly: "bbc news" is one
    # letter from "ABC News", a different channel.
    if found and found[0][0][0] < 4:
        found = [x for x in found if x[0][0] < 4]
    return [c for _, c in found]


def category_index(channels: Iterable[Channel],
                   names: Optional[dict] = None) -> list:
    """``[(category_id, display_name, count)]``, biggest first. A channel
    in two categories counts in both; uncategorised ones are "Other" so
    nothing is unreachable from the index."""
    counts: Counter = Counter()
    for c in channels:
        for k in c.categories or [OTHER_CATEGORY]:
            counts[k] += 1
    names = names or {}
    out = [(k, "Other" if k == OTHER_CATEGORY
            else names.get(k) or k.title(), n) for k, n in counts.items()]
    # "Other" sorts last whatever its size: it's the leftovers.
    out.sort(key=lambda x: (x[0] == OTHER_CATEGORY, -x[2], x[1]))
    return out


def _index(counts: Counter, names: dict) -> list:
    out = [(k, names.get(k) or k, n) for k, n in counts.items() if k]
    out.sort(key=lambda x: (-x[2], x[1]))
    return out


def country_index(channels: Iterable[Channel],
                  names: Optional[dict] = None) -> list:
    """``[(code, display_name, count)]``, most channels first."""
    return _index(Counter(c.country for c in channels), names or {})


def language_index(channels: Iterable[Channel],
                   names: Optional[dict] = None) -> list:
    """``[(code, display_name, count)]``, most channels first. A channel
    broadcasting in two languages counts in both."""
    return _index(Counter(k for c in channels for k in c.languages),
                  names or {})


def overview(channels: list, cat: Catalogue) -> dict:
    """What "what channels are available?" answers with (owner,
    2026-10-17): how many are available (and indexed), the categories, countries
    and languages there are to filter by, each with counts. The caller
    passes the *listed* channels, so every number is something he can
    actually put on."""
    return {"total": len(channels), "indexed": len(cat.channels),
            "categories": category_index(channels, cat.category_names),
            "countries": country_index(channels, cat.country_names),
            "languages": language_index(channels, cat.language_names)}


def page(items: list, number: int, per_page: int = 15) -> tuple:
    """``(slice, page, pages, first_index)`` for 1-based page ``number``,
    clamped. ``first_index`` is the 1-based number of the slice's first
    item, so numbering runs on across pages ("play 23" from page 2)."""
    pages = max(1, -(-len(items) // per_page))
    number = min(max(1, number), pages)
    start = (number - 1) * per_page
    return items[start:start + per_page], number, pages, start + 1


# ---------------------------------------------------------------- probing

@dataclass
class Probe:
    ok: bool
    why: str = ""
    height: Optional[int] = None


_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line: str) -> dict:
    return {k: v.strip('"') for k, v in _ATTR.findall(line.split(":", 1)[-1])}


def parse_master(text: str, base: str) -> list:
    """Variants of an HLS master playlist: ``[{url, height, bandwidth}]``.
    Empty when ``text`` is a media playlist."""
    variants = []
    pending: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            a = _attrs(line)
            m = re.match(r"(\d+)x(\d+)", a.get("RESOLUTION", ""))
            pending = {"height": int(m.group(2)) if m else None,
                       "bandwidth": int(a["BANDWIDTH"])
                       if a.get("BANDWIDTH", "").isdigit() else None}
        elif pending is not None and line and not line.startswith("#"):
            pending["url"] = urljoin(base, line)
            variants.append(pending)
            pending = None
    return variants


def pick_variant(variants: list, max_height: int = 720) -> Optional[dict]:
    """The variant T2 would play: closest to ``max_height`` (the stream
    size) without going over, else the smallest above it, else whatever
    has no resolution."""
    if not variants:
        return None
    sized = [v for v in variants if v.get("height")]
    under = [v for v in sized if v["height"] <= max_height]
    if under:
        return max(under, key=lambda v: v["height"])
    if sized:
        return min(sized, key=lambda v: v["height"])
    return variants[0]


def parse_media(text: str, base: str) -> tuple:
    """``(segment_url or None, drm)`` from an HLS media playlist. The
    newest segment, since on a live stream the oldest may already have
    expired. ``drm`` is a key method ffmpeg can't decrypt."""
    drm = False
    last = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY") or \
                line.startswith("#EXT-X-SESSION-KEY"):
            a = _attrs(line)
            method = a.get("METHOD", "").upper()
            keyformat = a.get("KEYFORMAT", "identity").lower()
            if method not in ("", "NONE", "AES-128") or \
                    keyformat != "identity":
                drm = True
        elif line and not line.startswith("#"):
            last = urljoin(base, line)
    return last, drm


def _headers(s: Stream) -> dict:
    h = {"User-Agent": s.user_agent or DEFAULT_USER_AGENT}
    if s.referrer:
        h["Referer"] = s.referrer
    return h


def _http_why(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "name or service" in low or "resolve" in low or \
            "nodename" in low or "getaddrinfo" in low:
        return "dns"
    if "ssl" in low or "certificate" in low or "tls" in low:
        return "tls"
    if "refused" in low:
        return "refused"
    if "no route" in low or "unreachable" in low:
        return "unreachable"
    return text[:120]


async def probe_stream(s: Stream, timeout: float = PROBE_TIMEOUT) -> Probe:
    """Can this stream be opened from here? HLS by playlists and the
    first 4 KB of a segment; anything else by a small ffprobe."""
    import httpx

    _PROBING.set(True)      # this task only; see _QuietProbes
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers=_headers(s)) as client:
            async def get(url: str, limit: int, **kw) -> tuple:
                async with client.stream("GET", url, **kw) as resp:
                    if resp.status_code >= 400:
                        return resp.status_code, b"", str(resp.url)
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) >= limit:
                            break
                    return resp.status_code, body[:limit], str(resp.url)

            async def chain() -> Probe:
                code, body, final = await get(s.url, 256_000)
                if code >= 400:
                    return Probe(False, f"http {code}")
                text = body.decode("utf-8", "replace")
                if not text.lstrip("﻿").lstrip().startswith("#EXTM3U"):
                    return await ffprobe_stream(s, timeout)
                height = None
                variant = pick_variant(parse_master(text, final))
                if variant:
                    height = variant.get("height")
                    code, body, final = await get(variant["url"], 256_000)
                    if code >= 400:
                        return Probe(False, f"variant http {code}")
                    text = body.decode("utf-8", "replace")
                segment, drm = parse_media(text, final)
                if drm:
                    return Probe(False, "drm")
                if not segment:
                    return Probe(False, "empty playlist")
                code, _, _ = await get(segment, 4096,
                                       headers={"Range": "bytes=0-4095"})
                if code >= 400:
                    return Probe(False, f"segment http {code}")
                return Probe(True, height=height)

            return await asyncio.wait_for(chain(), timeout * 3)
    except asyncio.TimeoutError:
        return Probe(False, "timeout")
    except Exception as exc:  # httpx's errors, bad URLs, decoding
        return Probe(False, _http_why(exc))


async def run_process(args: list, timeout: float) -> tuple:
    """``(returncode or None on timeout, stdout, stderr)``. Both pipes are
    drained the whole time and the process is killed in ``finally``, so no
    path leaves it running; every wait after the kill is bounded. (Reading
    stderr in a task and then calling communicate() read it twice and
    hung ``tools/check_iptv.py`` on the server.)"""
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out_task = asyncio.ensure_future(proc.stdout.read())
    err_task = asyncio.ensure_future(proc.stderr.read())
    timed_out = False
    try:
        done, _ = await asyncio.wait({out_task, err_task}, timeout=timeout)
        timed_out = len(done) < 2
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait({out_task, err_task}, timeout=5)
        for t in (out_task, err_task):
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass

    def text(t: asyncio.Task) -> str:
        if t.done() and not t.cancelled() and t.exception() is None:
            return t.result().decode(errors="replace")
        return ""
    return (None if timed_out else proc.returncode), text(out_task), \
        text(err_task)


async def ffprobe_stream(s: Stream, timeout: float = PROBE_TIMEOUT) -> Probe:
    """For DASH, raw TS and the rest: a short ffprobe with a small probe
    size, so it reads well under a megabyte."""
    args = ["ffprobe", "-v", "error", "-probesize", "500000",
            "-analyzeduration", "1000000",
            "-user_agent", s.user_agent or DEFAULT_USER_AGENT]
    if s.referrer:
        args += ["-headers", f"Referer: {s.referrer}\r\n"]
    args += ["-select_streams", "v:0", "-show_entries", "stream=height",
             "-of", "json", s.url]
    try:
        code, out, err = await run_process(args, timeout)
    except FileNotFoundError:
        return Probe(False, "ffprobe not installed")
    if code is None:
        return Probe(False, "timeout")
    # What ffprobe printed decides, not its exit code. asyncio has been
    # seen reporting a child's exit as 255 when something else reaped it
    # first ("Unknown child process pid N, will report returncode 255",
    # live 2026-10-20), and a working stream failed by that would be
    # hidden for a day.
    try:
        heights = [st.get("height") for st in
                   json.loads(out or "{}").get("streams") or []
                   if st.get("height")]
    except (json.JSONDecodeError, AttributeError):
        heights = []
    if heights:
        return Probe(True, height=int(max(heights)))
    if code != 0:
        last = [ln for ln in err.splitlines() if ln.strip()]
        return Probe(False, (last[-1] if last else f"exit {code}")[-120:])
    return Probe(False, "no video")


# ------------------------------------------------------------- the service

Fetch = Callable[[str], Awaitable[Any]]
ProbeFn = Callable[[Stream], Awaitable[Probe]]


async def fetch_api(name: str) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True,
                                 headers={"User-Agent": "corbin"}) as c:
        resp = await c.get(f"{API_BASE}/{name}.json")
        resp.raise_for_status()
        return resp.json()


def _read(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("unreadable %s; ignoring it", path)
        return None


def _write(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.exception("could not write %s", path)


class IptvCatalogue:
    """The catalogue, its health book, and the background job keeping both
    current. ``busy()`` true pauses checking (a stream is on air);
    ``wanted()`` false idles it (no server has streaming allowed)."""

    def __init__(self, root: str | os.PathLike, *,
                 fetch: Fetch = fetch_api, probe: ProbeFn = probe_stream,
                 clock: Callable[[], float] = time.time,
                 busy: Callable[[], bool] = lambda: False,
                 wanted: Callable[[], bool] = lambda: True,
                 concurrency: int = CONCURRENCY) -> None:
        self.catalogue_path = Path(root) / CATALOGUE_FILE
        self.health_path = Path(root) / HEALTH_FILE
        self.fetch, self.probe, self.clock = fetch, probe, clock
        self.busy, self.wanted = busy, wanted
        self.concurrency = max(1, concurrency)
        data = _read(self.catalogue_path)
        self.catalogue = Catalogue.from_json(data) if isinstance(
            data, dict) else Catalogue()
        records = _read(self.health_path)
        self.health = HealthBook(records if isinstance(records, dict)
                                 else {})
        self._dirty = False
        self._saved_at = 0.0
        self._logged_at = 0.0
        self._task: Optional[asyncio.Task] = None

    # -- queries ------------------------------------------------------

    def listed(self, c: Channel) -> bool:
        return is_listed(c, self.health, self.clock())

    def playable(self, c: Channel) -> bool:
        return is_playable(c, self.health, self.clock())

    def listed_channels(self) -> list:
        now = self.clock()
        return [c for c in self.catalogue.channels.values()
                if is_listed(c, self.health, now)]

    def counts(self) -> dict:
        return health_counts(self.catalogue, self.health, self.clock())

    # -- refresh ------------------------------------------------------

    def refresh_due(self) -> bool:
        return self.clock() - self.catalogue.fetched_at >= REFRESH_EVERY

    async def refresh(self) -> bool:
        """Download and rebuild. On any failure, or a result far smaller
        than what we had, keep the old catalogue and say why."""
        try:
            raw = {}
            for name in API_FILES:
                raw[name] = await self.fetch(name)
            # Off the event loop: tens of thousands of dicts to walk.
            fresh = await asyncio.to_thread(build_catalogue, raw,
                                            now=self.clock())
        except Exception as exc:
            logger.warning("iptv refresh failed, keeping the catalogue "
                           "from before (%d channels): %s: %s",
                           len(self.catalogue), type(exc).__name__, exc)
            self._retry_later()
            return False
        old = len(self.catalogue)
        if not fresh.channels or (old and len(fresh) < old *
                                  MIN_REFRESH_RATIO):
            logger.warning("iptv refresh returned %d channels (had %d); "
                           "looks broken, keeping the old catalogue",
                           len(fresh), old)
            self._retry_later()
            return False
        self.catalogue = fresh
        pruned = self.health.prune(fresh.urls())
        await asyncio.to_thread(_write, self.catalogue_path, fresh.to_json())
        self._dirty = True
        logger.info("iptv catalogue: %d channels, %d streams (dropped: %s); "
                    "%d stale health records pruned", len(fresh),
                    fresh.stream_count(),
                    ", ".join(f"{v} {k}" for k, v in
                              sorted(fresh.dropped.items(),
                                     key=lambda kv: -kv[1])) or "none",
                    pruned)
        return True

    def _retry_later(self) -> None:
        # Try again in an hour rather than on every loop.
        self.catalogue.fetched_at = self.clock() - REFRESH_EVERY + HOUR

    # -- checking -----------------------------------------------------

    async def check(self, s: Stream) -> Probe:
        try:
            result = await self.probe(s)
        except Exception as exc:
            result = Probe(False, f"probe error: {type(exc).__name__}")
            logger.warning("iptv probe of %s raised", s.url, exc_info=True)
        self.health.record(s.url, result.ok, self.clock(), why=result.why,
                           height=result.height)
        self._dirty = True
        return result

    def mark(self, url: str, ok: bool, why: str = "") -> None:
        """For T2: what playback found, recorded at once, so a stream that
        died on air drops out of the list without waiting for a re-check.
        A failure here is soft (``SOFT_FAIL_FOR``): one hiccup on a live
        stream must not hide it for a day."""
        self.health.record(url, ok, self.clock(), why=why, soft=not ok)
        self._dirty = True

    def save(self, force: bool = False) -> None:
        now = self.clock()
        if self._dirty and (force or now - self._saved_at >= SAVE_EVERY):
            _write(self.health_path, self.health.records)
            self._dirty = False
            self._saved_at = now

    def log_counts(self, force: bool = False) -> None:
        now = self.clock()
        if force or now - self._logged_at >= LOG_EVERY:
            self._logged_at = now
            c = self.counts()
            logger.info("iptv health: %d ok / %d failed / %d unchecked; "
                        "%d of %d channels listed", c[OK], c[FAILED],
                        c[UNCHECKED], c["listed"], c["channels"])

    async def step(self) -> int:
        """One round: refresh if due, then check the next batch. Returns
        how many streams were checked (0 = nothing to do or paused)."""
        # Never while streaming (owner, 2026-10-20): the download and
        # rebuild take CPU a stream's encoder and audio need. It waits.
        if self.refresh_due() and not self.busy():
            await self.refresh()
        if not self.wanted() or self.busy():
            return 0
        batch = due_streams(self.catalogue, self.health, self.clock(),
                            self.concurrency)
        if not batch:
            self.log_counts(force=bool(self._dirty))
            self.save(force=True)
            return 0
        await asyncio.gather(*(self.check(s) for s in batch))
        self.save()
        self.log_counts()
        return len(batch)

    # -- the background job --------------------------------------------

    def start(self) -> None:
        """Idempotent: on_ready fires again on every reconnect."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.save(force=True)

    async def _run(self) -> None:
        await asyncio.sleep(STARTUP_DELAY)
        logger.info("iptv: %d channels cached, health check running",
                    len(self.catalogue))
        while True:
            try:
                checked = await self.step()
                if checked:
                    await asyncio.sleep(0)
                elif self.wanted() and self.busy():
                    await asyncio.sleep(BUSY_SLEEP)
                else:
                    await asyncio.sleep(IDLE_SLEEP)
            except asyncio.CancelledError:
                self.save(force=True)
                raise
            except Exception:
                logger.exception("iptv health loop failed; retrying")
                await asyncio.sleep(60)
