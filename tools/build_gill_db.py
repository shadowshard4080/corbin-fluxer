"""One-time builder for the bundled John Gill's Exposition dataset.

Scrapes Gill's per-verse commentary from biblehub (public-domain text) and writes
a verse-keyed SQLite database the bot reads locally, so the verse finder
commentary tab works fully offline with no runtime scraping.

Usage:
    python tools/build_gill_db.py            # full build -> data/gill_commentary.sqlite
    python tools/build_gill_db.py --test     # tiny subset -> data/gill_test.sqlite

Resumable: chapters already recorded (see the `progress` table) are skipped, so
you can re-run after an interruption. Books use biblehub's slugs; verses are keyed
by the Protestant 66-book number (matching corbin/core/verse_refs.py).
"""

import os
import re
import sqlite3
import sys
import time
import urllib.request

from bs4 import BeautifulSoup, Tag

BASE = "https://biblehub.com/commentaries/gill/{slug}/{chapter}.htm"
HEADERS = {"User-Agent": "Mozilla/5.0 (one-time Gill dataset build)"}
DELAY = 0.25          # politeness between requests (seconds)
TIMEOUT = 25
RETRIES = 4

# (book_number, biblehub_slug, chapter_count) — Protestant 66-book canon.
BOOKS = [
    (1, "genesis", 50), (2, "exodus", 40), (3, "leviticus", 27), (4, "numbers", 36),
    (5, "deuteronomy", 34), (6, "joshua", 24), (7, "judges", 21), (8, "ruth", 4),
    (9, "1_samuel", 31), (10, "2_samuel", 24), (11, "1_kings", 22), (12, "2_kings", 25),
    (13, "1_chronicles", 29), (14, "2_chronicles", 36), (15, "ezra", 10), (16, "nehemiah", 13),
    (17, "esther", 10), (18, "job", 42), (19, "psalms", 150), (20, "proverbs", 31),
    (21, "ecclesiastes", 12), (22, "songs", 8), (23, "isaiah", 66), (24, "jeremiah", 52),
    (25, "lamentations", 5), (26, "ezekiel", 48), (27, "daniel", 12), (28, "hosea", 14),
    (29, "joel", 3), (30, "amos", 9), (31, "obadiah", 1), (32, "jonah", 4),
    (33, "micah", 7), (34, "nahum", 3), (35, "habakkuk", 3), (36, "zephaniah", 3),
    (37, "haggai", 2), (38, "zechariah", 14), (39, "malachi", 4), (40, "matthew", 28),
    (41, "mark", 16), (42, "luke", 24), (43, "john", 21), (44, "acts", 28),
    (45, "romans", 16), (46, "1_corinthians", 16), (47, "2_corinthians", 13), (48, "galatians", 6),
    (49, "ephesians", 6), (50, "philippians", 4), (51, "colossians", 4), (52, "1_thessalonians", 5),
    (53, "2_thessalonians", 3), (54, "1_timothy", 6), (55, "2_timothy", 4), (56, "titus", 3),
    (57, "philemon", 1), (58, "hebrews", 13), (59, "james", 5), (60, "1_peter", 5),
    (61, "2_peter", 3), (62, "1_john", 5), (63, "2_john", 1), (64, "3_john", 1),
    (65, "jude", 1), (66, "revelation", 22),
]

TEST_BOOKS = [(31, "obadiah", 1), (65, "jude", 1), (43, "john", 3)]  # small, incl. multi-verse


def _ancestor_has_class(node, cls):
    p = node.parent
    while isinstance(p, Tag):
        if cls in (p.get("class") or []):
            return True
        p = p.parent
    return False


def parse_chapter(html):
    """Return {verse_number: commentary_text} for a biblehub Gill chapter page.

    The page nests everything in one `div.chap`; versenum/verse/commentary are a
    flat interleaved stream, so we walk the text in document order and segment at
    each `versenum` marker, skipping the `.verse` (KJV text) spans.
    """
    soup = BeautifulSoup(html, "html.parser")
    chap = soup.find("div", class_="chap")
    if not chap:
        return {}
    verses, cur, buf = {}, None, []

    def flush():
        if cur is not None and buf:
            t = re.sub(r"\s+", " ", "".join(buf)).strip()
            t = re.sub(r"\s+([,.;:!?])", r"\1", t)
            if t:
                verses[cur] = t

    for s in chap.strings:
        if _ancestor_has_class(s, "versenum"):
            m = re.search(r":(\d+)", str(s))
            if m:
                flush()
                buf = []
                cur = int(m.group(1))
            continue
        if _ancestor_has_class(s, "verse"):
            continue
        if cur is not None:
            buf.append(str(s))
    flush()
    return verses


def fetch(url):
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(min(2 ** attempt, 20))
    print(f"  !! giving up on {url}: {last}")
    return None


def main():
    test = "--test" in sys.argv
    books = TEST_BOOKS if test else BOOKS
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "..", "data")
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "gill_test.sqlite" if test else "gill_commentary.sqlite")

    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE IF NOT EXISTS commentary "
               "(book INT, chapter INT, verse INT, text TEXT, PRIMARY KEY(book, chapter, verse))")
    db.execute("CREATE TABLE IF NOT EXISTS progress (book INT, chapter INT, verses INT, PRIMARY KEY(book, chapter))")
    db.commit()

    done = {(b, c) for b, c in db.execute("SELECT book, chapter FROM progress")}
    total_ch = sum(n for _, _, n in books)
    done_ch = 0
    total_verses = 0
    for book, slug, nchap in books:
        for chapter in range(1, nchap + 1):
            done_ch += 1
            if (book, chapter) in done:
                continue
            url = BASE.format(slug=slug, chapter=chapter)
            html = fetch(url)
            if html is None:
                continue
            verses = parse_chapter(html)
            if not verses:
                print(f"  ?? no verses parsed: {slug} {chapter} ({url})")
            rows = [(book, chapter, v, t) for v, t in sorted(verses.items())]
            db.executemany("INSERT OR REPLACE INTO commentary VALUES (?,?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?)", (book, chapter, len(rows)))
            db.commit()
            total_verses += len(rows)
            if done_ch % 25 == 0 or test:
                print(f"[{done_ch}/{total_ch}] {slug} {chapter}: {len(rows)} verses "
                      f"(running total {total_verses})", flush=True)
            time.sleep(DELAY)

    # summary
    n_ch = db.execute("SELECT COUNT(*) FROM progress").fetchone()[0]
    n_v = db.execute("SELECT COUNT(*) FROM commentary").fetchone()[0]
    print(f"\nDONE. chapters={n_ch}, verses={n_v}, db={db_path} "
          f"({os.path.getsize(db_path)//1024} KB)")
    db.close()


if __name__ == "__main__":
    main()
