"""Offline suite for reading attached text files and documents.

The Discord build could read .txt and .docx attachments; this port
could not, so a posted file reached Corbin as nothing but a filename.
"""
from __future__ import annotations
import asyncio, io, logging, zipfile
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.conversation import Conversation
from corbin.core import documents
from test_conversation import RecordingLLM


def make_docx(*paragraphs: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>'
           f"{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def suite_classify(r: Results) -> None:
    c = documents.classify
    r.check(".txt is text", c("notes.txt"), "text")
    r.check("text/* content type is text", c("x", "text/plain; charset=utf-8"), "text")
    r.check(".docx is a document", c("Essay.DOCX"), "document")
    r.check("the docx MIME type is a document", c("upload",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "document")
    r.check(".pdf is a document", c("paper.pdf"), "document")
    r.check("images are left to vision", c("pic.png", size=100), None)
    r.check("image MIME is left to vision", c("x", "image/webp", 100), None)
    r.check("audio is not text", c("song.mp3", size=100), None)
    r.check("a small unknown file is tried as text", c("README", size=500), "text")
    r.check("a large unknown file is skipped", c("blob.bin", size=10_000_000), None)
    r.check("unknown size and type is skipped", c("blob"), None)


def suite_extract(r: Results) -> None:
    r.check("docx paragraphs are read in order",
            documents.extract(make_docx("First line", "Second line"),
                              "a.docx", "document"),
            "First line\n\nSecond line")
    r.check("a file named .docx that isn't one yields nothing",
            documents.extract(b"not a zip", "a.docx", "document"), None)
    r.check("utf-8 text decodes",
            documents.extract("café — ok".encode(), "a.txt", "text"),
            "café — ok")
    r.check("a BOM is stripped",
            documents.extract(b"\xef\xbb\xbfhello", "a.txt", "text"), "hello")
    r.check("cp1252 text still decodes",
            documents.extract(b"smart \x93quotes\x94", "a.txt", "text"),
            "smart “quotes”")
    r.check("binary is refused, not decoded as mojibake",
            documents.extract(b"PK\x03\x04\x00\x00junk", "README", "text"), None)
    r.check("whitespace-only yields nothing",
            documents.extract(b"  \n ", "a.txt", "text"), None)
    long = "a" * 50 + "b" * (documents.MAX_FILE_CHARS * 2) + "z" * 50
    cut = documents.truncate(long)
    r.ok("long text is truncated", len(cut) < len(long))
    r.ok("...keeping the head", cut.startswith("a" * 50))
    r.ok("...and the tail", cut.endswith("z" * 50))
    r.ok("...with a marker", "[truncated]" in cut)


class Att:
    def __init__(self, filename, url="u", content_type="", size=None):
        self.filename, self.url = filename, url
        self.content_type, self.size = content_type, size


class Msg:
    def __init__(self, *atts): self.attachments = list(atts)


def suite_collect(r: Results) -> None:
    blobs = {"t": b"hello there", "d": make_docx("Doc body"),
             "p": b"\x89PNG\r\n", "bad": None}

    async def fetch(url):
        return blobs[url]

    got = asyncio.run(documents.collect(Msg(
        Att("pic.png", "p", "image/png"), Att("a.txt", "t"),
        Att("b.docx", "d"), Att("gone.txt", "bad")), fetch))
    r.check("text and docx are read; images and failed fetches skipped",
            got, [("a.txt", "hello there"), ("b.docx", "Doc body")])

    many = asyncio.run(documents.collect(
        Msg(*[Att(f"{i}.txt", "t") for i in range(6)]), fetch))
    r.check("files per message are capped", len(many), documents.MAX_FILES)
    r.check("no attachments, no files", asyncio.run(documents.collect(Msg(), fetch)), [])


def suite_reply(r: Results) -> None:
    def reply(content, files):
        llm = RecordingLLM()
        convo = Conversation(llm, soul_path="/nonexistent/SOUL.md")
        out = asyncio.run(convo.reply(user_id="u", speaker="Sam",
                                      user_content=content, channel_id="c",
                                      files=files))
        return llm.seen, out, convo

    seen, out, convo = reply("summarise this", [("notes.txt", "The secret is 42.")])
    final = seen["messages"][-1]["content"]
    r.ok("file text reaches the model", "The secret is 42." in final)
    r.ok("...with its filename", "notes.txt" in final)
    r.ok("...alongside the user's words", "summarise this" in final)
    stored = convo.user_history("u")[0]["content"]
    r.ok("history keeps the user's words", "summarise this" in stored)
    r.ok("...notes the upload", "notes.txt" in stored)
    r.ok("...but not the file body", "The secret is 42." not in stored)

    seen, out, _ = reply("", [("notes.txt", "body text")])
    r.check("a file with no caption still gets a reply", out, "an answer")
    r.ok("...and the model sees the file", "body text" in seen["messages"][-1]["content"])

    seen, out, _ = reply("", [])
    r.check("no text and no file is still silence", out, None)


SUITES = [
    ("document classification", suite_classify),
    ("document extraction", suite_extract),
    ("document collection", suite_collect),
    ("files in replies", suite_reply),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
