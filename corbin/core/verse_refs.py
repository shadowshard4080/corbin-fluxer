"""Bible reference parsing and text cleaning, with no network or I/O.

Detects references in ordinary chat ("Romans 12:2", "1 Corinthians
13:4-7", "Ps 23:1"). Two-letter abbreviations that are also English words
(``is``, ``am``, ``re``) deliberately never match, or "is 3:30 ok?" would
become a verse lookup. Verse text comes from the keyless bolls.life API
(``corbin/core/verses.py``); bolls embeds Strong's tags and margin notes
that are stripped here.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List, Optional, Tuple

BOLLS_BASE = "https://bolls.life"


# Translation slugs on bolls.life.
KJV = "KJV"      # King James Version 1769 (public domain; has Strong's tags)
NASB = "NASB"    # New American Standard Bible (1995)
WLC = "WLC"      # Westminster Leningrad Codex (Hebrew OT)
TR = "TR"        # Elzevir Textus Receptus 1624 (Greek NT)
NET = "NET"      # New English Translation — carries per-verse translators' notes (commentary)

FULL_NAMES = {
    KJV: "King James Version (1769)",
    NASB: "New American Standard Bible (1995)",
    WLC: "Westminster Leningrad Codex · Hebrew",
    TR: "Textus Receptus · Greek",
}
COLORS = {
    KJV: 0xC9A227,   # aged gold
    NASB: 0x2E6DB4,  # blue
    WLC: 0x6B4FA0,   # purple (Hebrew)
    TR: 0x3A7D6E,    # teal (Greek)
}
# ------------------------------------------------------------------------------
# Book table — Protestant 66-book canonical numbering (confirmed against bolls,
# e.g. John == 43). Books 1-39 are the Old Testament (Hebrew original), 40-66 the
# New Testament (Greek original).
# ------------------------------------------------------------------------------
DISPLAY = {
    1: "Genesis", 2: "Exodus", 3: "Leviticus", 4: "Numbers", 5: "Deuteronomy",
    6: "Joshua", 7: "Judges", 8: "Ruth", 9: "1 Samuel", 10: "2 Samuel",
    11: "1 Kings", 12: "2 Kings", 13: "1 Chronicles", 14: "2 Chronicles",
    15: "Ezra", 16: "Nehemiah", 17: "Esther", 18: "Job", 19: "Psalms",
    20: "Proverbs", 21: "Ecclesiastes", 22: "Song of Solomon", 23: "Isaiah",
    24: "Jeremiah", 25: "Lamentations", 26: "Ezekiel", 27: "Daniel", 28: "Hosea",
    29: "Joel", 30: "Amos", 31: "Obadiah", 32: "Jonah", 33: "Micah", 34: "Nahum",
    35: "Habakkuk", 36: "Zephaniah", 37: "Haggai", 38: "Zechariah", 39: "Malachi",
    40: "Matthew", 41: "Mark", 42: "Luke", 43: "John", 44: "Acts", 45: "Romans",
    46: "1 Corinthians", 47: "2 Corinthians", 48: "Galatians", 49: "Ephesians",
    50: "Philippians", 51: "Colossians", 52: "1 Thessalonians",
    53: "2 Thessalonians", 54: "1 Timothy", 55: "2 Timothy", 56: "Titus",
    57: "Philemon", 58: "Hebrews", 59: "James", 60: "1 Peter", 61: "2 Peter",
    62: "1 John", 63: "2 John", 64: "3 John", 65: "Jude", 66: "Revelation",
}

# name/abbreviation -> canonical number. Abbreviations are lowercase, no dots.
_BOOK_ALIASES: Dict[int, List[str]] = {
    1: ["genesis", "gen", "gn"],
    2: ["exodus", "exod", "exo", "ex"],
    3: ["leviticus", "lev", "lv"],
    4: ["numbers", "num", "nm", "nb"],
    5: ["deuteronomy", "deut", "deu", "dt"],
    6: ["joshua", "josh", "jos", "jsh"],
    7: ["judges", "judg", "jdg", "jg"],
    8: ["ruth", "rut", "rth"],
    9: ["1 samuel", "1 sam", "1sa", "1 sm", "first samuel"],
    10: ["2 samuel", "2 sam", "2sa", "2 sm", "second samuel"],
    11: ["1 kings", "1 kgs", "1ki", "1 kin", "first kings"],
    12: ["2 kings", "2 kgs", "2ki", "2 kin", "second kings"],
    13: ["1 chronicles", "1 chron", "1 chr", "1ch", "first chronicles"],
    14: ["2 chronicles", "2 chron", "2 chr", "2ch", "second chronicles"],
    15: ["ezra", "ezr"],
    16: ["nehemiah", "neh"],
    17: ["esther", "esth", "est"],
    18: ["job"],
    19: ["psalms", "psalm", "pslm", "psa", "psm", "pss", "ps"],
    20: ["proverbs", "prov", "prv", "pro"],
    21: ["ecclesiastes", "eccles", "eccl", "ecc", "qoh"],
    22: ["song of solomon", "song of songs", "song", "sos", "sng", "canticles", "cant"],
    23: ["isaiah", "isa", "isai"],
    24: ["jeremiah", "jer", "jere"],
    25: ["lamentations", "lam", "lame"],
    26: ["ezekiel", "ezek", "eze", "ezk"],
    27: ["daniel", "dan", "dni"],
    28: ["hosea", "hos"],
    29: ["joel", "joe"],
    30: ["amos", "amo"],
    31: ["obadiah", "obad", "oba"],
    32: ["jonah", "jon", "jnh"],
    33: ["micah", "mic"],
    34: ["nahum", "nah"],
    35: ["habakkuk", "hab", "hbk"],
    36: ["zephaniah", "zeph", "zep"],
    37: ["haggai", "hag", "hagg"],
    38: ["zechariah", "zech", "zec"],
    39: ["malachi", "mal"],
    40: ["matthew", "matt", "mat", "mt"],
    41: ["mark", "mrk", "mar", "mk"],
    42: ["luke", "luk", "lk"],
    43: ["john", "joh", "jhn", "jn"],
    44: ["acts", "act"],
    45: ["romans", "rom", "rmn"],
    46: ["1 corinthians", "1 cor", "1co", "first corinthians"],
    47: ["2 corinthians", "2 cor", "2co", "second corinthians"],
    48: ["galatians", "gal", "gala"],
    49: ["ephesians", "eph", "ephes"],
    50: ["philippians", "phil", "php", "phi"],
    51: ["colossians", "col", "colo"],
    52: ["1 thessalonians", "1 thess", "1 thes", "1th", "first thessalonians"],
    53: ["2 thessalonians", "2 thess", "2 thes", "2th", "second thessalonians"],
    54: ["1 timothy", "1 tim", "1ti", "first timothy"],
    55: ["2 timothy", "2 tim", "2ti", "second timothy"],
    56: ["titus", "tit", "tts"],
    57: ["philemon", "philem", "phm", "phlm"],
    58: ["hebrews", "heb", "hebr"],
    59: ["james", "jas", "jam"],
    60: ["1 peter", "1 pet", "1pe", "1pt", "first peter"],
    61: ["2 peter", "2 pet", "2pe", "2pt", "second peter"],
    62: ["1 john", "1 jn", "1jn", "1jo", "first john"],
    63: ["2 john", "2 jn", "2jn", "2jo", "second john"],
    64: ["3 john", "3 jn", "3jn", "3jo", "third john"],
    65: ["jude", "jud"],
    66: ["revelation", "revelations", "rev", "reve", "apocalypse"],
}

# Two-letter tokens that are also common English words (is=Isaiah, am=Amos,
# re=Revelation, ...) are DELIBERATELY excluded above — matching them would let a
# message like "is 3:30 ok?" hijack the chatbot. Only these unambiguous 2-letter
# abbreviations are honored; everything else must be >= 3 characters or a full name.
_SAFE_SHORT = {"ps", "jn", "mt", "mk", "lk"}

_NAME_TO_NUM: Dict[str, int] = {}
for _num, _names in _BOOK_ALIASES.items():
    for _n in _names:
        _NAME_TO_NUM[_n] = _num

# book name/abbrev  (chapter) : (verse)  optional -(end verse)
_REF_RE = re.compile(
    r"\b"
    r"((?:[1-3]|i{1,3}|first|second|third)\s+)?"           # optional book ordinal
    r"([a-z]+(?:\s+of\s+[a-z]+(?:\s+[a-z]+)?)?)"            # book name (incl. "song of solomon")
    r"\.?\s*"
    r"(\d{1,3})"                                            # chapter
    r"[:.](\d{1,3})"                                        # verse
    r"(?:\s*[-–]\s*(\d{1,3}))?"                        # optional end verse
    r"\b",
    re.IGNORECASE,
)

_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _superscript(n: int) -> str:
    return str(n).translate(_SUP)


def _normalize_book(raw: str) -> str:
    """Lowercase, collapse whitespace, and turn roman/ordinal prefixes into digits."""
    s = raw.strip().lower().replace(".", "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^(i{1,3})\s+", lambda m: {"i": "1 ", "ii": "2 ", "iii": "3 "}[m.group(1)], s)
    s = re.sub(r"^first\s+", "1 ", s)
    s = re.sub(r"^second\s+", "2 ", s)
    s = re.sub(r"^third\s+", "3 ", s)
    return s


def parse_references(text: str) -> List[Dict[str, Any]]:
    """Return every bible reference found in ``text`` (order preserved)."""
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    for m in _REF_RE.finditer(text):
        ordinal, bookname, chap, verse, endverse = m.groups()
        raw_book = ((ordinal or "") + bookname).strip()
        norm = _normalize_book(raw_book)
        num = _NAME_TO_NUM.get(norm)
        if num is None:
            # ordinal may have been spurious (e.g. "2 Genesis 1:1"); retry on the name alone
            num = _NAME_TO_NUM.get(_normalize_book(bookname))
        if num is None:
            continue
        # guard against short common-word collisions (see _SAFE_SHORT note)
        token = norm.replace(" ", "")
        if len(token) < 3 and token not in _SAFE_SHORT:
            continue
        chapter = int(chap)
        start = int(verse)
        end = int(endverse) if endverse else None
        if end is not None and end <= start:
            end = None  # ignore malformed / reversed ranges
        out.append({
            "book": num,
            "display": DISPLAY[num],
            "chapter": chapter,
            "verse": start,
            "end_verse": end,
        })
    return out


def _clean_verse_text(text: str) -> str:
    """Strip Strong's number tags and any other markup bolls embeds in verse text."""
    if not text:
        return ""
    text = re.sub(r"<S>\d+</S>", "", text)   # Strong's numbers
    # Remove translator/marginal footnotes entirely. bolls wraps these in
    # <sup>…</sup> (e.g. KJV margin notes) or <f>…</f>; stripping only the tag
    # would leave the note text dangling mid-verse ("firmament: Heb. expansion"),
    # which reads like the verse was interrupted.
    text = re.sub(r"<sup\b[^>]*>.*?</sup>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<f\b[^>]*>.*?</f>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)       # any remaining tags (<i>, <pb/>, ...)
    text = html.unescape(text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


# NET note-type codes -> human labels (sn = study note, tn = translator note, ...)
_NET_CODE_PREFIX = [
    (re.compile(r"^sn\b"), "Study note:"),
    (re.compile(r"^tn\b"), "Translator note:"),
    (re.compile(r"^tc\b"), "Text note:"),
    (re.compile(r"^map\b", re.IGNORECASE), "Map:"),
]


def _clean_commentary(raw: str) -> str:
    """Turn a bolls NET ``comment`` (HTML) into readable text for an embed."""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<S>\d+</S>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        for pat, label in _NET_CODE_PREFIX:
            new = pat.sub(label, ln, count=1)
            if new != ln:
                ln = new
                break
        lines.append(ln)
    return "\n".join(lines).strip()


def _original_code(book: int) -> str:
    """Hebrew (WLC) for the Old Testament, Greek (TR) for the New."""
    return WLC if book <= 39 else TR


def _ref_key(ref: Dict[str, Any]) -> Tuple[int, int, int, Optional[int]]:
    return (ref["book"], ref["chapter"], ref["verse"], ref["end_verse"])


def _ref_title(ref: Dict[str, Any]) -> str:
    title = f"{ref['display']} {ref['chapter']}:{ref['verse']}"
    if ref["end_verse"]:
        title += f"-{ref['end_verse']}"
    return title


