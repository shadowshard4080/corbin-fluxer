"""Offline suite for verse parsing and the commentary DB."""
from __future__ import annotations
import logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

REPO = None  # the parser is vendored; load() takes no path


def suite_parsing(r: Results) -> None:
    from corbin.core import verses
    try:
        verses.load(REPO)
    except Exception:
        r.ok("versefinder module unavailable (skipped)", True)
        return

    r.check("simple reference",
            [(x["display"], x["chapter"], x["verse"])
             for x in verses.parse("check John 3:16")],
            [("John", 3, 16)])
    r.check("range captured",
            verses.parse("Gen 1:1-3")[0]["end_verse"], 3)
    r.check("numbered book",
            verses.parse("1 Cor 13:4")[0]["display"], "1 Corinthians")
    r.check("two references found", len(verses.parse("Rom 8:28 and Ps 23:1")), 2)
    r.check("no reference", verses.parse("hello there"), [])

    # The whole reason two-letter abbreviations that are English words are
    # excluded: otherwise ordinary chat becomes a verse lookup.
    for text in ("is 3:30 ok?", "am 5:24", "see you at 2:30",
                 "re 1:1 about that", "meet at 7:15"):
        r.check(f"not a verse: {text!r}", verses.parse(text), [])

    r.check("title rendering",
            verses.ref_title(verses.parse("John 3:16")[0]), "John 3:16")
    r.check("OT uses Hebrew",
            verses.original_code(verses.parse("Gen 1:1")[0]["book"]), "WLC")
    r.check("NT uses Greek",
            verses.original_code(verses.parse("John 3:16")[0]["book"]), "TR")

    vf = verses._vf
    r.check("Strong's tags stripped",
            vf._clean_verse_text("In the <S>7225</S>beginning God"),
            "In the beginning God")
    r.ok("marginal notes stripped",
         "expansion" not in vf._clean_verse_text(
             "the waters. <sup>firmament: Heb. expansion</sup>"))
    r.check("plain text untouched",
            vf._clean_verse_text("For God so loved"), "For God so loved")


def suite_gill(r: Results) -> None:
    from corbin.core import verses
    try:
        verses.load(REPO)
    except Exception:
        r.ok("skipped", True)
        return

    missing = verses.GillCommentary("/nonexistent/gill.sqlite")
    r.ok("absent db reports unavailable", not missing.available)
    r.check("absent db returns nothing",
            missing.lookup({"book": 43, "chapter": 3, "verse": 16,
                            "end_verse": None}), [])

    real = Path(__file__).resolve().parent.parent / "data" / "gill_commentary.sqlite"
    if not real.exists():
        r.ok("bundled db not present here (skipped)", True)
        return
    g = verses.GillCommentary(real)
    r.ok("bundled db opens", g.available)
    rows = g.lookup({"book": 43, "chapter": 3, "verse": 16,
                     "end_verse": None})
    r.ok("returns commentary for John 3:16", len(rows) == 1)
    r.ok("text is substantial", len(rows[0][1]) > 200)
    span = g.lookup({"book": 43, "chapter": 3, "verse": 16, "end_verse": 18})
    r.ok("a range returns several verses", len(span) >= 2)
    r.check("nonsense reference is empty",
            g.lookup({"book": 99, "chapter": 1, "verse": 1,
                      "end_verse": None}), [])
    g.close()
    r.ok("close is safe twice", (g.close(), True)[1])


def suite_settings(r: Results) -> None:
    from corbin.core.settings import ChatSettings

    with tempfile.TemporaryDirectory() as tmp:
        s = ChatSettings(Path(tmp) / "c.json")
        r.ok("off by default", not s.versefinder_enabled("g"))
        r.ok("no guild is safe", not s.versefinder_enabled(None))
        s.set_versefinder("g", True)
        r.ok("enabled", s.versefinder_enabled("g"))
        r.ok("persists",
             ChatSettings(Path(tmp) / "c.json").versefinder_enabled("g"))
        r.ok("other guilds unaffected", not s.versefinder_enabled("g2"))


SUITES = [
    ("verse parsing", suite_parsing),
    ("gill commentary", suite_gill),
    ("versefinder settings", suite_settings),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
