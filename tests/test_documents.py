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


# -- documents he sends (owner, 2026-11-18) -----------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def formatted_docx() -> bytes:
    """A Word file the way Word writes one: a run split by formatting,
    a curly apostrophe, mc:Ignorable naming prefixes only in an
    attribute (what ElementTree would break), a header, a tab."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        'mc:Ignorable="w14"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>'
        '<w:r><w:rPr><w:sz w:val="40"/></w:rPr><w:t>The quick </w:t></w:r>'
        '<w:r><w:rPr><w:b/></w:rPr><w:t>brown</w:t></w:r>'
        '<w:r><w:t xml:space="preserve"> fox doesn’t jump &amp; run.</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Their going.</w:t></w:r><w:r><w:tab/></w:r>'
        '<w:r><w:t>Tab here</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Their going again.</w:t></w:r></w:p>'
        '</w:body></w:document>')
    header = (f'<?xml version="1.0"?><w:hdr xmlns:w="{W}"><w:p><w:r>'
              '<w:t>Draft teh header</w:t></w:r></w:p></w:hdr>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", xml)
        zf.writestr("word/header1.xml", header)
        zf.writestr("word/media/image1.png", b"\x89PNG not really")
    return buf.getvalue()


def suite_made(r: Results) -> None:
    from corbin.core import doc_output as D

    md = ("# Title\nIntro with **bold** and *italic*.\nLine two.\n\n"
          "- one\n- two\n1. first\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
    reply = (f"Here.\n```file:notes.docx\n{md}```\n"
             "```file:../../etc/passwd\nplain\n```")
    text, files = D.extract(reply)
    r.check("blocks become notes", text,
            "Here.\n*Attached: `notes.docx`*\n*Attached: `passwd.txt`*")
    r.check("a path in the name is dropped", [n for n, _ in files],
            ["notes.docx", "passwd.txt"])
    data = files[0][1]
    body = documents.extract_docx(data)
    r.ok("the docx reads back", body and "Title" in body
         and "Intro with bold and italic." in body and "•\tone" in body
         and "1.\tfirst" in body)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        xml = zf.read("word/document.xml").decode()
    r.ok("a complete Word package",
         {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
          "word/styles.xml", "word/_rels/document.xml.rels"} <= names)
    r.ok("headings use Word's styles", 'w:val="Heading1"' in xml)
    r.ok("bold and italic are runs", "<w:b/>" in xml and "<w:i/>" in xml)
    r.ok("a line break inside a paragraph is kept", "<w:br/>" in xml)
    r.ok("the table is a table", "<w:tbl>" in xml)
    bad = D.docx("a\x01b & <c>")
    r.ok("control characters and markup can't break the XML",
         "ab & <c>" in documents.extract_docx(bad))

    t, f = D.extract("x\n```file:report.pdf\n# Hi\n```")
    why = D.pdf_available()
    if why:
        r.ok("no fpdf2: said in the reply, nothing attached",
             not f and "Couldn't send report.pdf" in t and "fpdf2" in t)
    else:
        r.ok("a PDF", f and f[0][1].startswith(b"%PDF"))

    many = "".join(f"```file:f{i}.txt\nx\n```\n" for i in range(7))
    t, f = D.extract(many)
    r.check("at most five files a message", len(f), D.MAX_FILES)
    r.ok("...and the rest are said to be left out", "Left out" in t)
    r.check("an unclosed block is left alone",
            D.extract("```file:a.txt\nno end")[1], [])
    r.ok("a request for a file wants the note",
         D.wants_document("can you make that a PDF?")
         and D.wants_document("put it in a word doc"))
    r.ok("chat doesn't", not D.wants_document("how was your day?"))


def suite_edits(r: Results) -> None:
    from corbin.core import doc_output as D

    orig = {"Story.docx": D.Original("Story.docx", formatted_docx())}
    reply = ("Fixed.\n```edit:story.docx\n"
             "FIND: quick brown fox doesn't jump & run\n"
             "REPLACE: quick red fox does not jump & run\n"
             "FIND: Their going\nREPLACE: They're going\n"
             "FIND: going.\tTab\nREPLACE: x\n"
             "FIND: teh\nREPLACE: the\n"
             "FIND: not in the file\nREPLACE: y\n```")
    text, files = D.extract(reply, orig)
    r.check("the note says what happened", text,
            "Fixed.\n*Edited copy attached: `Story.docx` (3 changes, 2 "
            "not found: “going.\tTab”; “not in the file”)*")
    r.check("the original's name is kept", files[0][0], "Story.docx")
    data = files[0][1]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode()
        header = zf.read("word/header1.xml").decode()
        order = zf.namelist()
        image = zf.read("word/media/image1.png")
    r.ok("across runs, curly quote matched by a straight one",
         "The quick red fox does not jump & run." in
         documents.extract_docx(data).replace("\n", ""))
    r.ok("the new words take the first run's formatting",
         '<w:sz w:val="40"/></w:rPr><w:t xml:space="preserve">The quick red'
         ' fox does not jump &amp; run</w:t>' in xml)
    r.ok("...and text after the match keeps its own run",
         '<w:t xml:space="preserve">.</w:t>' in xml)
    r.ok("every occurrence is replaced",
         xml.count("They&apos;re going") + xml.count("They're going") == 2)
    r.ok("the rest of the file is untouched", 'mc:Ignorable="w14"' in xml
         and 'xmlns:w14=' in xml and '<w:pStyle w:val="Title"/>' in xml
         and "<w:tab/>" in xml)
    r.ok("headers are edited too", "Draft the header" in header)
    r.check("entries keep their order", order[0], "[Content_Types].xml")
    r.check("other parts byte for byte", image, b"\x89PNG not really")

    t, f = D.extract("```edit:story.docx\nFIND: nope\nREPLACE: x\n```", orig)
    r.ok("nothing matched: no copy, and it says so",
         not f and "none of my changes matched" in t)
    t, f = D.extract("```edit:x.docx\nFIND: a\nREPLACE: b\n```", {})
    r.ok("no original: asks for it again", not f and "send it again" in t)
    t, f = D.extract("```edit:a.pdf\nFIND: a\nREPLACE: b\n```",
                     {"a.pdf": D.Original("a.pdf", b"%PDF-1.4")})
    r.ok("a PDF can't be edited in place", not f and "PDF in place" in t)
    txt = {"list.txt": D.Original("list.txt", "milk\neggs\n".encode())}
    t, f = D.extract("```edit:list.txt\nFIND: eggs\nREPLACE: bread\n```", txt)
    r.check("a text file is edited by replacement", f[0][1],
            b"milk\nbread\n")
    r.check("FIND/REPLACE values may span lines",
            D.parse_edits("FIND: a\nb\nREPLACE: c\nd\nFIND: e"),
            [("a\nb", "c\nd"), ("e", "")])


def suite_wiring(r: Results) -> None:
    from pathlib import Path

    from corbin.ai.conversation import drop_repeats
    from corbin.core.doc_output import NOTE

    para = ("A paragraph of the document long enough to count as a "
            "repeat if it weren't inside a file block.")
    r.ok("a re-sent document keeps its paragraphs",
         para in drop_repeats(f"Again.\n\n```file:a.docx\n{para}\n```",
                              [para]))

    llm = RecordingLLM()
    conv = Conversation(llm, soul_path="/nonexistent/SOUL.md")
    asyncio.run(conv.reply(user_id="u", speaker="U", user_content="pdf it",
                           channel_id="c", documents_note=True))
    r.ok("the note reaches the model", "```file:NAME.EXT" in str(llm.seen))
    asyncio.run(conv.reply(user_id="u", speaker="U", user_content="hi",
                           channel_id="c"))
    r.ok("...only when asked for", "[Tool:Documents]" not in str(llm.seen))
    r.ok("...and never into history", not any(
        "[Tool:Documents]" in str(t) for t in conv.user_history("u")))
    r.ok("the note quotes the exact block shapes",
         "```edit:" in NOTE and "FIND:" in NOTE and "REPLACE:" in NOTE)

    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    post = src[src.index("    async def _post_reply("):]
    post = post[:post.index("\n    async def ", 10)]
    r.ok("documents are built before code files",
         post.index("doc_output.extract") < post.index("extract_code_files"))
    r.ok("...off the event loop", "asyncio.to_thread(\n                    "
         "doc_output.extract" in post)
    conv_src = src[src.index("    async def _converse("):]
    r.ok("attachments are kept for edits",
         "documents.collect(message, vision.fetch,\n" in conv_src
         and "originals=originals" in conv_src)
    r.ok("spoken replies never get the note",
         "not spoken and adapter.capabilities().file_upload" in conv_src)


SUITES = [
    ("document classification", suite_classify),
    ("document extraction", suite_extract),
    ("document collection", suite_collect),
    ("files in replies", suite_reply),
    ("documents he makes", suite_made),
    ("documents he edits", suite_edits),
    ("documents: wiring", suite_wiring),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
