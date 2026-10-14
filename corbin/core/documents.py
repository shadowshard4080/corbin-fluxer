"""Reading attached text files and documents so the model can see them.

A port of the Discord build's attachment reader (``chatbot_cog.py``,
``_extract_document_text`` and the text heuristic around it), with two
deliberate differences:

* **DOCX is parsed with the standard library.** A .docx is a zip holding
  ``word/document.xml``; reading paragraphs out of that needs no
  python-docx, so this works on a server that never installed it.
  PDF still needs a library (``pypdf`` or ``PyPDF2``) and is skipped,
  with a log line, when neither is present.
* **Binary files are refused, not decoded as latin-1.** The Discord build
  accepts any small unknown file as text, and latin-1 decodes *every*
  byte string, so a zip or an .exe reached the model as a wall of
  mojibake. A NUL byte in the head is the tell.

Everything here except ``collect`` is pure and synchronous. Extraction is
CPU work, so ``collect`` runs it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from typing import Optional, Sequence
from xml.etree import ElementTree

logger = logging.getLogger("corbin.documents")

#: Extensions read as plain text.
TEXT_EXTENSIONS = (
    ".txt", ".log", ".md", ".json", ".csv", ".tsv", ".py", ".js", ".ts",
    ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".lua",
    ".sh", ".xml", ".html", ".css", ".yml", ".yaml", ".toml", ".cfg",
    ".ini", ".conf", ".srt", ".rtf",
)
#: Extensions that need a parser rather than a decode.
DOCUMENT_EXTENSIONS = (".docx", ".pdf")
DOCUMENT_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml"
    ".document": ".docx",
}
#: An unknown file this small is tried as text (and refused if binary).
MAX_UNKNOWN_TEXT_BYTES = 200 * 1024
#: Per-file character budget. Far below the Discord build's 200K: this
#: lands in every prompt of the reply, and a pasted novel should not
#: cost a dollar to say "nice".
MAX_FILE_CHARS = 40_000
#: Files read per message.
MAX_FILES = 3

#: Never read as text; images go through vision instead.
_MEDIA_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".mp3",
    ".wav", ".ogg", ".flac", ".m4a", ".mp4", ".mov", ".webm", ".mkv",
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def classify(name: str, content_type: str = "",
             size: Optional[int] = None) -> Optional[str]:
    """``"document"``, ``"text"``, or None for anything not worth reading."""
    lower = (name or "").lower()
    ctype = (content_type or "").lower().split(";")[0].strip()
    if ctype.startswith(("image/", "audio/", "video/")) \
            or lower.endswith(_MEDIA_EXTENSIONS):
        return None
    if lower.endswith(DOCUMENT_EXTENSIONS) or ctype in DOCUMENT_TYPES:
        return "document"
    if (ctype.startswith("text/")
            or ctype in ("application/json", "application/xml")
            or lower.endswith(TEXT_EXTENSIONS)):
        return "text"
    if size is not None and 0 < size <= MAX_UNKNOWN_TEXT_BYTES:
        return "text"
    return None


def extract_docx(raw: bytes) -> Optional[str]:
    """Paragraph text of a .docx, or None if it is not one."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml")
        root = ElementTree.fromstring(xml)
    except Exception:
        return None
    paragraphs = []
    for para in root.iter(f"{_W}p"):
        pieces = []
        for node in para.iter():
            if node.tag == f"{_W}t" and node.text:
                pieces.append(node.text)
            elif node.tag == f"{_W}tab":
                pieces.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                pieces.append("\n")
        text = "".join(pieces)
        if text.strip():
            paragraphs.append(text)
    return "\n\n".join(paragraphs) or None


def extract_pdf(raw: bytes) -> Optional[str]:
    """Text of a PDF, or None if unreadable or no PDF library is installed."""
    reader_cls = None
    try:
        from pypdf import PdfReader as reader_cls
    except Exception:
        try:
            from PyPDF2 import PdfReader as reader_cls
        except Exception:
            logger.info("PDF attached but neither pypdf nor PyPDF2 is "
                        "installed; skipping it")
            return None
    try:
        reader = reader_cls(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return None
    text = "\n\n".join(p for p in pages if p.strip())
    return text or None


def decode_text(raw: bytes) -> Optional[str]:
    """Decode a text file, refusing anything that is really binary."""
    if b"\x00" in raw[:4096]:
        return None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def truncate(text: str, limit: int = MAX_FILE_CHARS) -> str:
    """Keep the head and tail: the end of a log is usually the point."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n...[truncated]...\n\n{text[-half:]}"


def extract(raw: bytes, name: str, kind: str) -> Optional[str]:
    """Readable text of one attachment, or None. Blocking."""
    text = None
    lower = (name or "").lower()
    if kind == "document":
        text = extract_pdf(raw) if lower.endswith(".pdf") else \
            extract_docx(raw) or extract_pdf(raw)
    else:
        text = decode_text(raw)
    if text is None or not text.strip():
        return None
    return truncate(text.strip())


def prompt_block(files: Sequence[tuple]) -> str:
    """The files, formatted for the final user turn."""
    parts = [f"[Tool:FileContents]\nFilename: {name}\n\n{text}"
             for name, text in files]
    parts.append(
        "[Tool:UseFileData] The user attached the file(s) above. Use their "
        "contents to answer. Do not mention internal tool tags.")
    return "\n\n".join(parts)


async def collect(message, fetch) -> list:
    """``(filename, text)`` for each readable non-image attachment.

    ``fetch`` is an async ``url -> bytes | None``; the vision module's
    downloader already enforces a size cap, so it is reused.
    """
    files: list = []
    for item in (getattr(message, "attachments", None) or ()):
        if len(files) >= MAX_FILES:
            break
        name = str(getattr(item, "filename", "") or "file")
        kind = classify(name,
                        str(getattr(item, "content_type", "") or ""),
                        getattr(item, "size", None))
        url = str(getattr(item, "url", "") or "")
        if kind is None or not url:
            continue
        raw = await fetch(url)
        if raw is None:
            continue
        text = await asyncio.to_thread(extract, raw, name, kind)
        if text is None:
            logger.info("could not read attachment %s as %s", name, kind)
            continue
        files.append((name, text))
    if files:
        logger.info("files: reading %d attachment(s) with this reply: %s",
                    len(files), ", ".join(n for n, _ in files))
    return files
