"""Bible verse lookup.

Parsing lives in ``verse_refs``; this module is the I/O: fetching from
bolls.life and reading the optional John Gill SQLite. Fluxer has no
buttons, so the translation is a command parameter.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger("corbin.verses")

TIMEOUT = 10.0
MAX_VERSES = 12

_vf: Any = None


def load(*_args: Any) -> Any:
    """Load the parser. Kept as a call so a broken import is caught at boot."""
    global _vf
    if _vf is None:
        from . import verse_refs as module

        _vf = module
    return _vf


#: translation slug -> human name, filled on load().
def translations() -> dict[str, str]:
    vf = _vf
    return {vf.KJV: "King James Version (1769)",
            vf.NASB: "New American Standard Bible (1995)",
            vf.NET: "New English Translation"}


def parse(text: str) -> list[dict]:
    return list(_vf.parse_references(text or []) or []) if _vf else []


def ref_title(ref: dict) -> str:
    return _vf._ref_title(ref)


def original_code(book: int) -> str:
    """Hebrew for the OT, Greek for the NT."""
    return _vf._original_code(book)


async def fetch_passage(ref: dict, translation: str) -> list[tuple[int, str]]:
    """[(verse_number, text)] for a reference. Empty on failure."""
    import httpx

    book, chapter = ref["book"], ref["chapter"]
    start = ref["verse"]
    end = ref.get("end_verse") or start
    url = f"{_vf.BOLLS_BASE}/get-text/{translation}/{book}/{chapter}/"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.info("bolls fetch failed for %s %s", translation,
                    ref_title(ref), exc_info=True)
        return []

    out: list[tuple[int, str]] = []
    for row in data if isinstance(data, list) else []:
        num = row.get("verse")
        if not isinstance(num, int) or not (start <= num <= end):
            continue
        cleaned = _vf._clean_verse_text(row.get("text") or "")
        if cleaned:
            out.append((num, cleaned))
        if len(out) >= MAX_VERSES:
            break
    return out


class GillCommentary:
    """Read-only view over the bundled John Gill SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        if not self.path.exists():
            logger.info("Gill commentary not found at %s; NET notes will be "
                        "used instead", self.path)
            return
        try:
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True,
                check_same_thread=False)
        except sqlite3.Error:
            logger.warning("could not open Gill commentary", exc_info=True)

    @property
    def available(self) -> bool:
        return self._conn is not None

    def lookup(self, ref: dict) -> list[tuple[int, str]]:
        if self._conn is None:
            return []
        start = ref["verse"]
        end = ref.get("end_verse") or start
        try:
            rows = self._conn.execute(
                "SELECT verse, text FROM commentary "
                "WHERE book=? AND chapter=? AND verse BETWEEN ? AND ? "
                "ORDER BY verse",
                (ref["book"], ref["chapter"], start, end)).fetchall()
        except sqlite3.Error:
            logger.warning("Gill query failed", exc_info=True)
            return []
        return [(int(v), str(t)) for v, t in rows if t]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


async def fetch_net_notes(ref: dict) -> list[tuple[int, str]]:
    """Fallback commentary: NET translators' notes via bolls."""
    import httpx

    url = (f"{_vf.BOLLS_BASE}/get-chapter/{_vf.NET}/"
           f"{ref['book']}/{ref['chapter']}/")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    start = ref["verse"]
    end = ref.get("end_verse") or start
    out = []
    for row in data if isinstance(data, list) else []:
        num = row.get("verse")
        if not isinstance(num, int) or not (start <= num <= end):
            continue
        note = _vf._clean_commentary(row.get("comment") or "")
        if note:
            out.append((num, note))
    return out
