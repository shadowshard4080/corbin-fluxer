"""Edit the text of a .docx inside the original file (owner, 2026-11-18:
"keep original formatting").

Only the text inside ``<w:t>`` elements changes; every other byte of
every part is copied as it was. That is deliberate. ElementTree can't
round-trip a Word part: it renames namespace prefixes and drops
declarations that look unused, but ``mc:Ignorable="w14 wp14"`` names
prefixes in an attribute value, and Word then calls the file corrupt.

A FIND is matched within one paragraph, across however many runs Word
split it into (a spell-check mark or one bold letter splits a run). The
replacement goes into the run where the match starts, so it takes that
run's formatting; the rest of the matched text is removed from the
runs after it, which keep their own formatting for whatever follows.
Curly and straight quotes, and a non-breaking space and a space, match
each other, since the model can't tell them apart in the text it read.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape, unescape

logger = logging.getLogger("corbin.docx_edit")

#: Parts with body text a person would want edited.
_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|"
                    r"endnotes)\.xml$")
_TOKEN = re.compile(
    r"<w:p(?:\s[^>]*)?/>"                         # empty paragraph
    r"|<w:p(?:\s[^>]*)?>"                         # paragraph opens
    r"|</w:p>"
    r"|<w:t(?:\s[^>]*)?>(.*?)</w:t>"              # text
    r"|<w:t(?:\s[^>]*)?/>"
    r"|<w:tab/>|<w:br(?:\s[^>]*)?/>|<w:cr/>",     # tab / break chars
    re.DOTALL)
_ENTITIES = {"&quot;": '"', "&apos;": "'"}
#: Same length on both sides, so offsets in the folded text are offsets
#: in the real one.
_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"',
                       "”": '"', " ": " ", "‑": "-"})
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass
class _Piece:
    """One character source in a paragraph: a ``<w:t>``'s text, or a
    tab/break element (``node`` None), which can't be edited."""
    text: str
    node: int | None          # index into the part's text nodes


@dataclass
class _Node:
    start: int                # where the element starts in the XML
    end: int
    open_tag: str             # "<w:t ...>" as written
    text: str                 # unescaped


def _scan(xml: str) -> tuple[list[_Node], list[list[_Piece]]]:
    """Text nodes, and each paragraph's pieces (innermost paragraph:
    a text box's paragraphs are their own)."""
    nodes: list[_Node] = []
    paragraphs: list[list[_Piece]] = []
    stack: list[list[_Piece]] = []
    for m in _TOKEN.finditer(xml):
        tok = m.group(0)
        if tok.startswith("<w:p") and tok.endswith("/>"):
            continue
        if tok.startswith("<w:p"):
            stack.append([])
        elif tok == "</w:p>":
            if stack:
                paragraphs.append(stack.pop())
        elif tok.startswith("<w:t"):
            if not stack:
                continue
            if tok.endswith("/>"):
                continue
            raw = m.group(1) or ""
            text = unescape(raw, _ENTITIES)
            open_tag = tok[:tok.index(">") + 1]
            nodes.append(_Node(m.start(), m.end(), open_tag, text))
            stack[-1].append(_Piece(text, len(nodes) - 1))
        elif stack:
            stack[-1].append(_Piece("\t" if tok == "<w:tab/>" else "\n",
                                    None))
    return nodes, paragraphs


def _replace_in(paragraph: list[_Piece], nodes: list[_Node], find: str,
                repl: str) -> tuple[int, bool]:
    """Every match of ``find`` in one paragraph, applied to ``nodes``'
    text. ``(matches, a_match_crossed_a_tab_or_break)``."""
    count, crossed, start = 0, False, 0
    target = find.translate(_FOLD)
    while True:
        # Rebuilt each time: an earlier replacement changed the text.
        spans, pos = [], 0
        for piece in paragraph:
            text = nodes[piece.node].text if piece.node is not None \
                else piece.text
            spans.append((pos, pos + len(text), piece))
            pos += len(text)
        folded = "".join(nodes[p.node].text if p.node is not None
                         else p.text for _s, _e, p in spans).translate(_FOLD)
        at = folded.find(target, start)
        if at < 0:
            return count, crossed
        end = at + len(target)
        touched = [(s, e, p) for s, e, p in spans if s < end and e > at]
        if any(p.node is None for _s, _e, p in touched):
            # A tab or line break inside the match isn't text to
            # rewrite; leave this one and look further on.
            crossed = True
            start = at + 1
            continue
        first = True
        for s, e, p in touched:
            node = nodes[p.node]
            lo, hi = max(at, s) - s, min(end, e) - s
            if first:
                node.text = node.text[:lo] + repl + node.text[hi:]
                first = False
            else:
                node.text = node.text[:lo] + node.text[hi:]
        count += 1
        start = at + len(repl)


def _rewrite(xml: str, nodes: list[_Node], before: list[str]) -> str:
    out, pos = [], 0
    for node, old in zip(nodes, before):
        if node.text == old:
            continue
        tag = node.open_tag
        if "xml:space" not in tag:
            tag = tag[:-1].rstrip("/") + ' xml:space="preserve">'
        out.append(xml[pos:node.start])
        out.append(f"{tag}{escape(_BAD_XML.sub('', node.text))}</w:t>")
        pos = node.end
    out.append(xml[pos:])
    return "".join(out)


def apply(raw: bytes, edits: list[tuple[str, str]]
          ) -> tuple[bytes, int, list[str]]:
    """``(new_docx, edits_applied, finds_not_found)``. Raises ValueError
    for a file that isn't a .docx."""
    try:
        zin = zipfile.ZipFile(io.BytesIO(raw))
        names = zin.namelist()
    except zipfile.BadZipFile as exc:
        raise ValueError("that isn't a Word document") from exc
    if "word/document.xml" not in names:
        raise ValueError("that isn't a Word document")

    parts = {}
    for name in names:
        if _PARTS.match(name):
            xml = zin.read(name).decode("utf-8")
            nodes, paragraphs = _scan(xml)
            parts[name] = [xml, nodes, paragraphs,
                           [n.text for n in nodes]]

    applied, missed = 0, []
    for find, repl in edits:
        hits = crossed = 0
        for xml, nodes, paragraphs, _before in parts.values():
            for para in paragraphs:
                n, c = _replace_in(para, nodes, find, repl)
                hits += n
                crossed += c
        if hits:
            applied += 1
        else:
            missed.append(find)
            if crossed:
                logger.info("docx edit: a match crosses a tab or line "
                            "break, not changed")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename in parts:
                xml, nodes, _p, before = parts[info.filename]
                if any(n.text != b for n, b in zip(nodes, before)):
                    data = _rewrite(xml, nodes, before).encode("utf-8")
            # Same entry, same order, same compression: [Content_Types]
            # stays first, which some readers need.
            zout.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue(), applied, missed
