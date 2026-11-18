"""Documents the bot sends: new files from a reply, and edits to theirs.

Owner, 2026-11-18. The model writes a document as a fenced block:

* `````file:NAME.EXT`` ... `````: a new file, written in Markdown (``#``
  headings, ``**bold**``, ``*italic*``, ``-`` bullets, ``1.`` numbered
  lists, ``| tables |``). EXT is txt, md, docx or pdf; .txt and .md go out
  as written, .docx is built with the standard library, .pdf with
  ``fpdf2`` (``pip install fpdf2``).
* `````edit:NAME`` ... `````: ``FIND:`` / ``REPLACE:`` pairs applied to a
  file the user attached. A .docx is edited **inside the original**
  (:mod:`corbin.core.docx_edit`), so its fonts, layout and images stay;
  a text file by plain replacement. A PDF can't be edited in place.

:func:`extract` swaps each block for a short note and returns the files.
Nothing fails silently: a format that can't be made, an edit with no
original, or a FIND that matches nothing is said in the reply itself.
"""

from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Mapping, Optional
from xml.sax.saxutils import escape

logger = logging.getLogger("corbin.doc_output")

#: A file or edit block. Closed by ``` alone on its line, so a file may
#: hold inline code; it can't hold a fenced code block of its own.
BLOCK = re.compile(r"```(file|edit):[ \t]*([^\n`]+?)[ \t]*\n(.*?)\n```"
                   r"[ \t]*(?=\n|$)", re.DOTALL | re.IGNORECASE)
#: Formats made from Markdown. Anything else plain-text-like goes out
#: as written; an unknown extension becomes .txt.
BUILT = (".docx", ".pdf")
AS_WRITTEN = (".txt", ".md", ".csv", ".json", ".html", ".xml", ".yaml",
              ".yml", ".ini", ".log", ".srt", ".rtf", ".tex")
#: Files per reply. A reply with more is almost certainly a loop.
MAX_FILES = 5
#: Fenced blocks of a reply that is a document request get this long.
MAX_DOC_CHARS = 200_000


class DocumentError(Exception):
    """A document that couldn't be made, worded for the person."""


# -- Markdown, the small part of it the model is told to use ----------------

@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


@dataclass
class Block:
    kind: str                      # heading, para, bullet, number, table, rule
    runs: list = field(default_factory=list)
    level: int = 0                 # heading level, or list nesting
    number: int = 0
    rows: list = field(default_factory=list)   # table: [[ [Run] ]]


_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|`[^`]+`|\*[^*\s][^*]*?\*"
                     r"|(?<![\w])_[^_\s][^_]*?_(?![\w]))")


def runs(text: str) -> list[Run]:
    """Inline Markdown to runs: **bold**, *italic* / _italic_, `code`."""
    out: list[Run] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(Run(text[pos:m.start()]))
        tok = m.group(0)
        if tok.startswith(("**", "__")):
            for r in runs(tok[2:-2]):
                r.bold = True
                out.append(r)
        elif tok.startswith("`"):
            out.append(Run(tok[1:-1], code=True))
        else:
            for r in runs(tok[1:-1]):
                r.italic = True
                out.append(r)
        pos = m.end()
    if pos < len(text):
        out.append(Run(text[pos:]))
    return [r for r in out if r.text]


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d{1,4})[.)]\s+(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def parse(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            # Line breaks inside a paragraph are kept (letters, poems,
            # addresses); "\n" in a run becomes a break in every format.
            blocks.append(Block("para", runs("\n".join(para))))
            para.clear()

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
        elif _HEADING.match(line):
            flush()
            m = _HEADING.match(line)
            blocks.append(Block("heading", runs(m.group(2)),
                                level=min(len(m.group(1)), 3)))
        elif _RULE.match(line):
            flush()
            blocks.append(Block("rule"))
        elif line.lstrip().startswith("|") and i + 1 < len(lines) \
                and _TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = [[runs(c) for c in _cells(line)]]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([runs(c) for c in _cells(lines[i])])
                i += 1
            blocks.append(Block("table", rows=rows))
            continue
        elif _BULLET.match(line):
            flush()
            m = _BULLET.match(line)
            blocks.append(Block("bullet", runs(m.group(2)),
                                level=min(len(m.group(1)) // 2, 3)))
        elif _NUMBER.match(line):
            flush()
            m = _NUMBER.match(line)
            blocks.append(Block("number", runs(m.group(3)),
                                level=min(len(m.group(1)) // 2, 3),
                                number=int(m.group(2))))
        else:
            para.append(line.rstrip())
        i += 1
    flush()
    return blocks


def plain(blocks: list[Block]) -> str:
    """For checks and logs: the text with no formatting."""
    out = []
    for b in blocks:
        if b.kind == "table":
            out.extend(" | ".join("".join(r.text for r in c) for c in row)
                       for row in b.rows)
        else:
            out.append("".join(r.text for r in b.runs))
    return "\n".join(out)


# -- .docx --------------------------------------------------------------------

#: XML 1.0 can't hold these; a stray control character makes Word refuse
#: the whole file.
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '</Types>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>')
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>')
_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _style(sid: str, name: str, size: int, *, bold: bool = False,
           before: int = 0, after: int = 120) -> str:
    return (f'<w:style w:type="paragraph" w:styleId="{sid}">'
            f'<w:name w:val="{name}"/><w:basedOn w:val="Normal"/>'
            f'<w:next w:val="Normal"/><w:qFormat/>'
            f'<w:pPr><w:keepNext/><w:spacing w:before="{before}" '
            f'w:after="{after}"/></w:pPr>'
            f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr>'
            f'</w:style>')


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles {_W_NS}>'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="264" w:lineRule="auto"/>'
    '</w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    + _style("Heading1", "heading 1", 36, bold=True, before=240)
    + _style("Heading2", "heading 2", 30, bold=True, before=200)
    + _style("Heading3", "heading 3", 26, bold=True, before=160)
    + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
    '<w:tblPr><w:tblBorders>'
    + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
              for s in ("top", "left", "bottom", "right", "insideH",
                        "insideV"))
    + '</w:tblBorders><w:tblCellMar><w:left w:w="100" w:type="dxa"/>'
    '<w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    '</w:styles>')


def _xml_text(text: str) -> str:
    return escape(_BAD_XML.sub("", text))


def _docx_runs(rs: list[Run], *, bold: bool = False) -> str:
    out = []
    for r in rs:
        props = ""
        if r.bold or bold:
            props += "<w:b/>"
        if r.italic:
            props += "<w:i/>"
        if r.code:
            props += ('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
                      'w:cs="Consolas"/>')
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        pieces = r.text.split("\n")
        body = '<w:br/>'.join(
            f'<w:t xml:space="preserve">{_xml_text(p)}</w:t>' for p in pieces)
        out.append(f"<w:r>{rpr}{body}</w:r>")
    return "".join(out)


def docx(markdown: str) -> bytes:
    """A Word document from Markdown, with no library beyond the stdlib.
    Lists are written with their bullet or number as text and a hanging
    indent: real Word lists need a numbering part, and this reads the
    same in Word, LibreOffice and Google Docs."""
    body = []
    for b in parse(markdown):
        if b.kind == "heading":
            body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{b.level}"/>'
                        f'</w:pPr>{_docx_runs(b.runs)}</w:p>')
        elif b.kind in ("bullet", "number"):
            mark = "•" if b.kind == "bullet" else f"{b.number}."
            left = 360 * (b.level + 1)
            body.append(
                f'<w:p><w:pPr><w:spacing w:after="60"/>'
                f'<w:ind w:left="{left}" w:hanging="360"/></w:pPr>'
                f'<w:r><w:t xml:space="preserve">{mark}</w:t></w:r>'
                f'<w:r><w:tab/></w:r>{_docx_runs(b.runs)}</w:p>')
        elif b.kind == "rule":
            body.append('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" '
                        'w:sz="6" w:space="1" w:color="999999"/></w:pBdr>'
                        '</w:pPr></w:p>')
        elif b.kind == "table":
            cols = max(len(row) for row in b.rows)
            rows = []
            for n, row in enumerate(b.rows):
                cells = "".join(
                    f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                    f'<w:p>{_docx_runs(c, bold=n == 0)}</w:p></w:tc>'
                    for c in row + [[]] * (cols - len(row)))
                rows.append(f"<w:tr>{cells}</w:tr>")
            grid = "".join('<w:gridCol w:w="2000"/>' for _ in range(cols))
            body.append(f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
                        f'<w:tblW w:w="5000" w:type="pct"/></w:tblPr>'
                        f'<w:tblGrid>{grid}</w:tblGrid>{"".join(rows)}'
                        f'</w:tbl><w:p/>')
        else:
            body.append(f"<w:p>{_docx_runs(b.runs)}</w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document {_W_NS}><w:body>{"".join(body) or "<w:p/>"}'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _STYLES)
    return buf.getvalue()


# -- .pdf ---------------------------------------------------------------------

#: DejaVu: matplotlib ships it (a dependency), most Linux hosts have it.
#: fpdf2's built-in fonts are Latin-1 only, so curly quotes, dashes and
#: every non-Western script would fail without a real font.
_PDF_FONTS = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf",
              "I": "DejaVuSans-Oblique.ttf", "BI": "DejaVuSans-BoldOblique.ttf"}
_MONO = "DejaVuSansMono.ttf"
_FONT_DIRS = ("/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/dejavu")


def _font_dir() -> Optional[str]:
    import importlib.util

    dirs = []
    try:
        spec = importlib.util.find_spec("matplotlib")
    except Exception:
        spec = None
    for base in (spec.submodule_search_locations or []) if spec else []:
        dirs.append(os.path.join(base, "mpl-data", "fonts", "ttf"))
    dirs.extend(_FONT_DIRS)
    for d in dirs:
        if all(os.path.isfile(os.path.join(d, f))
               for f in _PDF_FONTS.values()):
            return d
    return None


def pdf_available() -> Optional[str]:
    """None when PDFs can be made here, else why not (for the reply)."""
    try:
        import fpdf  # noqa: F401
    except Exception:
        return "PDF support isn't installed on my server (fpdf2)"
    if _font_dir() is None:
        return "there's no Unicode font (DejaVu) on my server for PDFs"
    return None


def pdf(markdown: str, title: str = "") -> bytes:
    from fpdf import FPDF

    fonts = _font_dir()
    if fonts is None:
        raise DocumentError(pdf_available() or "no font for PDFs")
    doc = FPDF(format="Letter")
    doc.set_margins(20, 20, 20)
    doc.set_auto_page_break(True, margin=20)
    for style, name in _PDF_FONTS.items():
        doc.add_font("Body", style, os.path.join(fonts, name))
    mono = os.path.join(fonts, _MONO)
    has_mono = os.path.isfile(mono)
    if has_mono:
        doc.add_font("Mono", "", mono)
    if title:
        doc.set_title(title)
    doc.add_page()

    def write(rs: list[Run], size: float, *, bold: bool = False,
              height: float = 0) -> None:
        line = height or size * 0.5
        for r in rs:
            if r.code and has_mono:
                doc.set_font("Mono", "", size)
            else:
                style = ("B" if r.bold or bold else "") + \
                    ("I" if r.italic else "")
                doc.set_font("Body", style, size)
            doc.write(line, r.text)

    for b in parse(markdown):
        if b.kind == "heading":
            size = {1: 18, 2: 15, 3: 13}[b.level]
            doc.ln(3)
            write(b.runs, size, bold=True)
            doc.ln(size * 0.5 + 2)
        elif b.kind in ("bullet", "number"):
            indent = 6 * (b.level + 1)
            mark = "•" if b.kind == "bullet" else f"{b.number}."
            doc.set_x(doc.l_margin + indent - 5)
            doc.set_font("Body", "", 11)
            doc.write(5.5, mark + " ")
            left = doc.l_margin
            doc.set_left_margin(left + indent)
            write(b.runs, 11, height=5.5)
            doc.set_left_margin(left)
            doc.ln(7)
        elif b.kind == "rule":
            y = doc.get_y() + 2
            doc.set_draw_color(150, 150, 150)
            doc.line(doc.l_margin, y, doc.w - doc.r_margin, y)
            doc.ln(6)
        elif b.kind == "table":
            doc.set_font("Body", "", 10)
            try:
                with doc.table(first_row_as_headings=True,
                               line_height=6) as table:
                    for row in b.rows:
                        cells = table.row()
                        for c in row:
                            cells.cell("".join(r.text for r in c))
            except Exception:
                # Older fpdf2 without tables, or a ragged table: still
                # every cell, one row to a line.
                logger.info("pdf: table drawn as text", exc_info=True)
                for row in b.rows:
                    doc.multi_cell(0, 6, "  |  ".join(
                        "".join(r.text for r in c) for c in row))
            doc.ln(3)
        else:
            write(b.runs, 11, height=5.5)
            doc.ln(8)
    return bytes(doc.output())


# -- building one file --------------------------------------------------------

_UNSAFE_NAME = re.compile(r"[^\w .()+,-]")


def safe_name(name: str) -> str:
    """A filename with no path and no odd characters; .txt when it has
    no extension the formats know."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _UNSAFE_NAME.sub("_", base).strip(" .") or "document.txt"
    stem, ext = os.path.splitext(base)
    if ext.lower() not in BUILT + AS_WRITTEN:
        base = (base if not ext else stem) + ".txt"
    return base[:100]


def build(name: str, content: str) -> tuple[str, bytes]:
    """``(filename, bytes)`` for a ``file:`` block. Raises DocumentError."""
    name = safe_name(name)
    ext = os.path.splitext(name)[1].lower()
    if ext == ".docx":
        return name, docx(content)
    if ext == ".pdf":
        why = pdf_available()
        if why:
            raise DocumentError(why)
        try:
            return name, pdf(content, title=os.path.splitext(name)[0])
        except DocumentError:
            raise
        except Exception as exc:
            logger.warning("pdf %s failed", name, exc_info=True)
            raise DocumentError(f"the PDF writer failed ({exc})") from exc
    return name, content.encode("utf-8")


# -- edits ----------------------------------------------------------------------

_EDIT_LINE = re.compile(r"^\s*(FIND|REPLACE)\s*:[ \t]?(.*)$", re.IGNORECASE)


def parse_edits(body: str) -> list[tuple[str, str]]:
    """FIND/REPLACE pairs. A value runs on to the next marker, so either
    may span lines; a FIND with no REPLACE deletes the text."""
    pairs: list[list[str]] = []
    current: Optional[list] = None
    which = None
    for line in (body or "").split("\n"):
        m = _EDIT_LINE.match(line)
        if m:
            which = m.group(1).upper()
            if which == "FIND":
                current = [m.group(2), None]
                pairs.append(current)
            elif current is not None and current[1] is None:
                current[1] = m.group(2)
            continue
        if current is None:
            continue
        idx = 0 if which == "FIND" else 1
        if current[idx] is not None:
            current[idx] += "\n" + line
    return [(f.strip("\n"), (r or "").strip("\n")) for f, r in pairs
            if f and f.strip()]


def apply_text_edits(text: str, edits: list[tuple[str, str]]
                     ) -> tuple[str, int, list[str]]:
    """``(new_text, edits_applied, finds_not_found)`` for a plain file."""
    applied, missed = 0, []
    for find, repl in edits:
        if find in text:
            text = text.replace(find, repl)
            applied += 1
        else:
            missed.append(find)
    return text, applied, missed


@dataclass
class Original:
    """A file the user attached, kept so an edit can be made to it."""
    name: str
    raw: bytes


def _lookup(originals: Mapping[str, Original], name: str
            ) -> Optional[Original]:
    want = os.path.basename(name.strip()).lower()
    for key, value in originals.items():
        if key.lower() == want:
            return value
    # One attachment and a slightly different name: it's that one.
    return next(iter(originals.values())) if len(originals) == 1 else None


def edit(name: str, body: str, originals: Mapping[str, Original]
         ) -> tuple[tuple[str, bytes], str]:
    """``((filename, bytes), note)`` for an ``edit:`` block. Raises
    DocumentError when it can't be done at all."""
    from . import docx_edit

    original = _lookup(originals, name)
    if original is None:
        raise DocumentError(f"I don't have {name} to edit; send it again "
                            "with what to change")
    edits = parse_edits(body)
    if not edits:
        raise DocumentError("I didn't write any changes for it")
    ext = os.path.splitext(original.name)[1].lower()
    if ext == ".docx":
        data, applied, missed = docx_edit.apply(original.raw, edits)
    elif ext == ".pdf":
        raise DocumentError("I can't edit a PDF in place, only make a new "
                            "one")
    else:
        try:
            text = original.raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = original.raw.decode("latin-1")
        text, applied, missed = apply_text_edits(text, edits)
        data = text.encode("utf-8")
    if not applied:
        raise DocumentError(
            "none of my changes matched its text, so I didn't send a copy")
    note = f"{applied} change{'s' if applied != 1 else ''}"
    if missed:
        shown = "; ".join(f"“{m[:60]}{'…' if len(m) > 60 else ''}”"
                          for m in missed[:3])
        note += (f", {len(missed)} not found: {shown}"
                 + (f" and {len(missed) - 3} more" if len(missed) > 3 else ""))
    return (original.name, data), note


# -- the reply ----------------------------------------------------------------

def has_blocks(text: str) -> bool:
    return bool(BLOCK.search(text or ""))


def extract(text: str, originals: Optional[Mapping[str, Original]] = None
            ) -> tuple[str, list[tuple[str, bytes]]]:
    """Swap each file/edit block in a reply for a note, and build the
    files. Blocking (PDF and docx work): run it in a thread."""
    files: list[tuple[str, bytes]] = []
    originals = originals or {}

    def _sub(m: re.Match) -> str:
        kind, name, body = m.group(1).lower(), m.group(2), m.group(3)
        if len(files) >= MAX_FILES:
            logger.warning("documents: more than %d in one reply; dropped "
                           "%s", MAX_FILES, name)
            return f"*(Left out {safe_name(name)}: {MAX_FILES} files per message.)*"
        try:
            if kind == "file":
                made = build(name, body[:MAX_DOC_CHARS])
                note = ""
            else:
                made, note = edit(name, body, originals)
        except DocumentError as exc:
            logger.warning("documents: %s %s not sent: %s", kind, name, exc)
            return f"*(Couldn't send {safe_name(name)}: {exc}.)*"
        except Exception as exc:
            logger.exception("documents: %s %s failed", kind, name)
            return f"*(Couldn't send {safe_name(name)}: {exc}.)*"
        files.append(made)
        logger.info("documents: %s %s (%d bytes)%s", "made" if kind ==
                    "file" else "edited", made[0], len(made[1]),
                    f": {note}" if note else "")
        return (f"*Attached: `{made[0]}`*" if kind == "file"
                else f"*Edited copy attached: `{made[0]}` ({note})*")

    return BLOCK.sub(_sub, text or ""), files


#: A message that might want a file made or changed. Only these (and
#: messages with a document attached) get the note telling the model how,
#: so ordinary chat doesn't pay for it.
WANTS_DOCUMENT = re.compile(
    r"\b(pdf|docx?|word (?:doc|document|file)|txt|text file|\.md\b|markdown"
    r" file|document|file|attachment|download|export|resume|cv|"
    r"cover letter|essay|report|worksheet|spreadsheet|handout)\b",
    re.IGNORECASE)


def wants_document(text: str) -> bool:
    return bool(WANTS_DOCUMENT.search(text or ""))


#: Told to the model on a reply that may need it (``Conversation.reply``,
#: ``documents_note``). Quotes the block shapes exactly: the parser takes
#: nothing else.
NOTE = (
    "[Tool:Documents] You can send files, attached to your message. Only "
    "when they ask for a file, or for changes to one they sent:\n"
    "- A new file: put its whole content in a block that opens with "
    "```file:NAME.EXT on its own line and closes with ``` on its own line. "
    "EXT is txt, md, docx or pdf. Write the content in Markdown: # "
    "headings, **bold**, *italic*, - bullets, 1. numbered lists, | tables |. "
    "No fenced code inside it.\n"
    "- Changes to a Word document or text file they attached: don't "
    "rewrite it. Send only the changes, in a block that opens with "
    "```edit:THEIR_EXACT_FILENAME and closes with ```, as pairs of lines:\n"
    "FIND: exact text copied from one paragraph of their file\n"
    "REPLACE: what it becomes\n"
    "Every place the FIND text appears is replaced and their formatting "
    "is kept, so quote enough to be unique, and keep each FIND inside one "
    "paragraph. Edits change text only: to add or remove sections, or to "
    "change a PDF, send a new file instead.\n"
    "The block is turned into the file and not shown, so say in a "
    "sentence what you made or changed, and never also paste the content "
    "outside it.")
