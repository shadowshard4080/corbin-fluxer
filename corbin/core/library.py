"""The local video library: shows in a folder on the server (owner,
2026-10-20).

Layout, as the owner set it up::

    data/video/                       (CORBIN_VIDEO_ROOT)
        Bob's Burgers/
            Bob's Burgers - 01.mkv          <- episodes straight in the show
            ...
        Some Other Show/
            Season 1/                     <- or in season folders
                S01E01 Pilot.mkv
            Season 2/
            Specials/

* A **show** is a top-level folder. Video files directly in it are its
  episodes; subfolders are **seasons**, numbered from their names
  ("Season 2", "S02", "Series 3"; "Specials"/"Extras" sort first as
  season 0). Deeper folders are folded into the season they sit in.
* **Episodes sort naturally** ("Episode 2" before "Episode 10"), with the
  episode number taken from the name when it has one (S01E05, "Episode 5",
  " - 05 ", "05.mkv"); "episode 3" means the third in the season, or that
  number when the files are numbered.
* Re-read on every lookup (a few hundred ``scandir`` calls): new files
  show up without a restart.
* **Nothing outside the root is ever opened**: every path is resolved and
  checked against it (``resolve``), and episodes are only ever chosen
  from the scan, never from what someone typed.

Platform neutral; no SDK, no network.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".webm", ".mov", ".ts",
              ".wmv", ".flv", ".mpg", ".mpeg"}
#: Season folder names that mean "extras": sorted first, as season 0.
SPECIALS = ("special", "specials", "extra", "extras", "ova", "ovas",
            "bonus", "season 0", "season 00")
#: Name similarity needed to match a show from a misspelling.
FUZZY_CUTOFF = 0.72


def _norm(text: str) -> str:
    """Lowercase, accents and punctuation off, single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def natural_key(name: str) -> list:
    """"Episode 2" < "Episode 10"; case-insensitive."""
    return [int(t) if t.isdigit() else t
            for t in re.split(r"(\d+)", (name or "").lower())]


_SXXEXX = re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})")
_EPWORD = re.compile(r"(?:episode|ep|e)[ ._-]*(\d{1,3})(?!\d)", re.I)
_DASHNUM = re.compile(r"(?:^|[ ._])-[ ._]*(\d{1,3})(?:v\d)?(?=[ ._\[(]|$)")
_LEADNUM = re.compile(r"^(\d{1,3})(?=[ ._\-\]])")
_TAGS = re.compile(r"\[[^\]]*\]|\((?:[^)]*(?:\d{3,4}p|x26[45]|hevc|aac|flac|"
                   r"bd|web|dual|10.?bit)[^)]*)\)", re.I)
_JUNK = re.compile(r"\b(?:\d{3,4}p|x26[45]|h\.?26[45]|hevc|avc|aac\d?(?:\.\d)?|"
                   r"flac|ac3|dts|bd(?:rip)?|blu.?ray|web.?(?:dl|rip)?|"
                   r"10.?bit|8.?bit|dual.?audio|multi.?subs?)\b", re.I)


def episode_number(filename: str) -> Optional[int]:
    """The episode number in a file name, or None."""
    stem = _TAGS.sub(" ", Path(filename).stem)
    for pat in (_SXXEXX, _EPWORD):
        m = pat.search(stem)
        if m:
            return int(m.group(m.lastindex))
    m = _DASHNUM.search(stem)
    if m:
        return int(m.group(1))
    m = _LEADNUM.match(stem.strip())
    if m:
        return int(m.group(1))
    return None


def season_number(folder: str) -> Optional[int]:
    """The season a folder name means: 0 for specials, None for an
    unnumbered folder."""
    low = _norm(folder)
    if low in SPECIALS or low.split(" ")[0] in SPECIALS:
        return 0
    m = re.search(r"(?:season|series|s)\s*0*(\d{1,3})\b", low) or \
        re.fullmatch(r"0*(\d{1,3})", low)
    return int(m.group(1)) if m else None


def clean_title(filename: str, show: str = "") -> str:
    """A readable episode title from a file name: tags, codecs and the
    show's own name taken off. Never empty."""
    stem = Path(filename).stem
    text = _TAGS.sub(" ", stem)
    text = _JUNK.sub(" ", text)
    text = re.sub(r"[._]+", " ", text)
    if show:
        text = re.sub(re.escape(show), " ", text, flags=re.I)
    text = re.sub(r"\s+-\s+(?=-|$)", " ", text)
    text = " ".join(text.split()).strip(" -_.")
    return text or stem


@dataclass
class Episode:
    #: Path relative to the root, with forward slashes: the stable id
    #: progress is stored under.
    rel: str
    show: str
    season: Optional[int]
    season_name: str
    number: Optional[int]
    title: str
    #: 1-based position in its season, as listed.
    index: int = 0
    #: A film: a video straight in the library folder, not in a show's.
    film: bool = False

    @property
    def nice_title(self) -> str:
        """The title if it says anything ("Leader of the Pack"), "" when
        it's only a number ("01") that the label already gives."""
        t = re.sub(r"^(?:s\d{1,2}\s*e\d{1,3}|(?:episode|ep|e)?\s*\d+)"
                   r"(?:v\d)?\s*[-:.]?\s*", "", self.title,
                   flags=re.I).strip(" -_.")
        return t if re.search(r"[a-zA-Z]", t) else ""

    @property
    def full_name(self) -> str:
        """"Bob's Burgers Episode 5", or just the film's name."""
        return self.show if self.film else f"{self.show} {self.label}"

    @property
    def label(self) -> str:
        """"S02E05" / "Episode 5" / "Special 1" for messages."""
        if self.film:
            return "the film"
        n = self.number if self.number is not None else self.index
        if self.season == 0:
            return f"Special {n}"
        if self.season is not None:
            return f"S{self.season:02d}E{n:02d}"
        return f"Episode {n}"


@dataclass
class Season:
    name: str
    number: Optional[int]
    episodes: list = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.number == 0:
            return "Specials"
        if self.number is not None:
            return f"Season {self.number}"
        return self.name


@dataclass
class Show:
    name: str
    seasons: list = field(default_factory=list)

    def episodes(self) -> list:
        return [e for s in self.seasons for e in s.episodes]

    def season(self, number: Optional[int]) -> Optional[Season]:
        if number is None:
            return None
        return next((s for s in self.seasons if s.number == number), None)


def _videos(folder: Path) -> list:
    """Video files under ``folder``, any depth, naturally sorted by their
    path relative to it."""
    found = []
    for dirpath, dirnames, files in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in files:
            if not f.startswith(".") and Path(f).suffix.lower() in VIDEO_EXTS:
                found.append(Path(dirpath) / f)
    found.sort(key=lambda p: natural_key(str(p.relative_to(folder))))
    return found


def scan(root: str | os.PathLike) -> list:
    """Every show under ``root``, by name. A missing root is no shows
    (the command says why), not an error."""
    base = Path(root)
    if not base.is_dir():
        return []
    shows = []
    for entry in sorted(base.iterdir(), key=lambda p: natural_key(p.name)):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        show = Show(name=entry.name)
        loose = [p for p in entry.iterdir()
                 if p.is_file() and not p.name.startswith(".")
                 and p.suffix.lower() in VIDEO_EXTS]
        loose.sort(key=lambda p: natural_key(p.name))
        groups: list = []
        if loose:
            groups.append((entry.name, None, loose))
        subs = [d for d in entry.iterdir()
                if d.is_dir() and not d.name.startswith(".")]
        numbered = []
        for d in subs:
            files = _videos(d)
            if files:
                numbered.append((d.name, season_number(d.name), files))
        numbered.sort(key=lambda g: (g[1] is None,
                                     g[1] if g[1] is not None else 0,
                                     natural_key(g[0])))
        groups += numbered
        for name, number, files in groups:
            season = Season(name=name, number=number)
            # By episode number where the names have one, then by name:
            # a folder path ("Extras/deep/S01E03") mustn't sort an episode
            # ahead of S01E01.
            files = sorted(files, key=lambda f: (
                episode_number(f.name) is None,
                episode_number(f.name) or 0, natural_key(f.name)))
            for i, f in enumerate(files, 1):
                season.episodes.append(Episode(
                    rel=f.relative_to(base).as_posix(), show=show.name,
                    season=number, season_name=name,
                    number=episode_number(f.name), index=i,
                    title=clean_title(f.name, show.name)))
            show.seasons.append(season)
        if show.seasons:
            shows.append(show)
    # A video straight in the library folder (owner, 2026-10-20: "Happy
    # Trails.mp4") is a film: its own entry, one "episode".
    for f in sorted((p for p in base.iterdir() if p.is_file()
                     and not p.name.startswith(".")
                     and p.suffix.lower() in VIDEO_EXTS),
                    key=lambda p: natural_key(p.name)):
        name = clean_title(f.name)
        film = Show(name=name, seasons=[Season(name=name, number=None)])
        film.seasons[0].episodes.append(Episode(
            rel=f.relative_to(base).as_posix(), show=name, season=None,
            season_name=name, number=None, title=name, index=1, film=True))
        shows.append(film)
    shows.sort(key=lambda s: natural_key(s.name))
    return shows


def find_show(shows: list, query: str) -> Optional[Show]:
    """The show a name means: exact, then contained either way, then the
    closest spelling. None when nothing is close."""
    q = _norm(query)
    if not q:
        return None
    for s in shows:
        if _norm(s.name) == q:
            return s
    contains = [s for s in shows if q in _norm(s.name)
                or _norm(s.name) in q]
    if contains:
        return min(contains, key=lambda s: abs(len(_norm(s.name)) - len(q)))
    best, score = None, 0.0
    for s in shows:
        r = SequenceMatcher(None, q, _norm(s.name)).ratio()
        if r > score:
            best, score = s, r
    return best if score >= FUZZY_CUTOFF else None


def show_in_text(shows: list, text: str) -> Optional[Show]:
    """A show named anywhere in a sentence ("play Bob's Burgers episode 3"),
    by its whole name. For telling a library request from a song."""
    t = f" {_norm(text)} "
    hits = [s for s in shows if f" {_norm(s.name)} " in t]
    return max(hits, key=lambda s: len(s.name)) if hits else None


def pick(show: Show, *, season: Optional[int] = None,
         episode: Optional[int] = None) -> Optional[Episode]:
    """An episode by season and number. The number is the one in the file
    names when they have them, else the position; no season means the
    first season with that episode (seasons numbered, specials last)."""
    seasons = ([show.season(season)] if season is not None
               else sorted(show.seasons, key=lambda s: (s.number == 0,)))
    seasons = [s for s in seasons if s is not None]
    if episode is None:
        return seasons[0].episodes[0] if seasons and seasons[0].episodes \
            else None
    for s in seasons:
        by_number = [e for e in s.episodes if e.number == episode]
        if by_number:
            return by_number[0]
        if 1 <= episode <= len(s.episodes) and all(
                e.number is None for e in s.episodes):
            return s.episodes[episode - 1]
    return None


def neighbour(show: Show, rel: str, step: int = 1) -> Optional[Episode]:
    """The episode ``step`` after (or before) ``rel`` across seasons, or
    None at either end."""
    eps = show.episodes()
    for i, e in enumerate(eps):
        if e.rel == rel:
            j = i + step
            return eps[j] if 0 <= j < len(eps) else None
    return None


def find_episode(shows: list, rel: str) -> Optional[tuple]:
    """``(show, episode)`` for a stored id, or None if it's gone."""
    for s in shows:
        for e in s.episodes():
            if e.rel == rel:
                return s, e
    return None


def resolve(root: str | os.PathLike, rel: str) -> Path:
    """The file for an episode id, refusing anything outside the root
    (a symlink out, ``..``): raises ValueError."""
    base = Path(root).resolve()
    path = (base / rel).resolve()
    if base != path and base not in path.parents:
        raise ValueError(f"{rel!r} is outside the video folder")
    return path
