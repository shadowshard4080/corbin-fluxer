"""Offline suite for describing media Corbin cannot see.

An image-only message used to be dropped from the transcript entirely,
so the history had a hole where the picture was. Asked about it, he
filled the hole with a guess -- in the wild, inventing that a posted
image related to someone's dog.
"""
from __future__ import annotations
import logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.conversation import Conversation
from corbin.core.media import (emoji_names, media_labels, strip_emoji_ids)


class Attachment:
    def __init__(self, filename): self.filename = filename


class Message:
    def __init__(self, content="", attachments=(), stickers=()):
        self.content = content
        self.attachments = [Attachment(a) for a in attachments]
        self.stickers = stickers


class Sticker:
    def __init__(self, name): self.name = name


def convo():
    llm = type("L", (), {"constants": type("K", (), {
        "MODEL_MAIN": "m", "EFFORT_MAIN": "low"})})
    return Conversation(llm, soul_path="/nonexistent")


def suite_labels(r: Results) -> None:
    r.check("an image is named by kind and filename",
            media_labels(Message(attachments=["pic.png"])),
            ["an image called pic.png"])
    r.check("a GIF is called out as animated",
            media_labels(Message(attachments=["wave.gif"])),
            ["an animated GIF called wave.gif"])
    r.check("video", media_labels(Message(attachments=["clip.mp4"])),
            ["a video called clip.mp4"])
    r.check("audio", media_labels(Message(attachments=["song.mp3"])),
            ["an audio file called song.mp3"])
    r.check("anything else is just a file",
            media_labels(Message(attachments=["notes.pdf"])),
            ["a file called notes.pdf"])
    r.check("a sticker", media_labels(Message(stickers=[Sticker("howl")])),
            ["a sticker called howl"])
    r.check("plain text has no media", media_labels(Message("hello")), [])


def suite_emoji(r: Results) -> None:
    r.check("custom emoji are listed by name",
            media_labels(Message("hey <:ziggy:12345>")),
            ["custom emoji (:ziggy:)"])
    r.check("animated ones too",
            emoji_names("<a:dance:9> <:hug:8>"), ["dance", "hug"])
    r.check("duplicates collapse",
            emoji_names("<:hug:8> <:hug:8> <:hug:8>"), ["hug"])
    r.check("unicode emoji are not custom emoji", emoji_names("hey 🐦"), [])

    # The id is noise he cannot use; the name carries the meaning.
    r.check("ids are stripped, names kept",
            strip_emoji_ids("hey <:ziggy:12345> and <a:dance:9>!"),
            "hey :ziggy: and :dance:!")
    r.check("text with no emoji is untouched",
            strip_emoji_ids("just talking"), "just talking")
    r.check("a mention is not mistaken for an emoji",
            strip_emoji_ids("<@1234> hi"), "<@1234> hi")


def suite_transcript(r: Results) -> None:
    c = convo()
    # The exact case that broke: an image with no caption.
    entry = c.record("ch", "Riley", "",
                     media=["an image called landscape.png"])
    r.ok("an image-only message is recorded, not dropped", bool(entry))

    c.record("ch", "Robin", "i dont even know")
    text = c.transcript("ch")
    r.ok("the picture appears in the transcript", "landscape.png" in text)
    # Without this he describes what he imagines is in it.
    r.ok("...marked as something he cannot see",
         "cannot see" in text)
    r.ok("the later message is still there", "i dont even know" in text)

    c2 = convo()
    r.check("a message with neither text nor media is still dropped",
            c2.record("ch", "X", "  "), {})
    c2.record("ch", "X", "hello")
    r.ok("a plain message gains no media marker",
         "cannot see" not in c2.transcript("ch"))

    c3 = convo()
    c3.record("ch", "X", "look at this", media=["an image called a.png"])
    line = c3.transcript("ch")
    r.ok("text and media appear together", "look at this" in line
         and "a.png" in line)


def suite_rules(r: Results) -> None:
    from corbin.ai.conversation import COMPACT_RULES

    low = COMPACT_RULES.lower()
    # He CAN see an attachment on the message he is answering; he cannot
    # see one the transcript merely mentions. The rule has to draw that
    # line, or he either refuses to look at what is in front of him or
    # invents what was posted ten minutes ago.
    r.ok("the rules allow seeing an attached image",
         "attached to the message you are answering" in low)
    r.ok("...but not one only mentioned in the transcript",
         "you cannot see" in low)
    r.ok("...and forbid guessing what one shows",
         "never guess what a picture shows" in low)
    # The tells he actually produced, each pinned to its rule.
    r.ok("no 'not X, but Y' antithesis", "not x, but y" in low)
    r.ok("no anaphoric repetition down a sentence",
         "no repeating a phrase down a sentence" in low)
    r.ok("no solemn abstraction", "solemn abstraction" in low)
    # This block is sent to a 13.8B model. Past a certain size it stops
    # acting on any of it, which is why the long SOUL sections were
    # pulled out in the first place.
    r.ok("the rule block stays short enough to be followed",
         len(COMPACT_RULES) < 1500)


def suite_vision(r: Results) -> None:
    """Preparing an image for the vision model."""
    import base64, io
    from PIL import Image
    from corbin.core import vision

    def encode(fmt, size=(64, 64), mode="RGB", frames=1):
        if frames > 1:
            imgs = [Image.new(mode, size, (i * 40 % 255, 60, 90))
                    for i in range(frames)]
            buf = io.BytesIO()
            imgs[0].save(buf, fmt, save_all=True, append_images=imgs[1:],
                         duration=80)
            return buf.getvalue()
        buf = io.BytesIO()
        Image.new(mode, size, (200, 40, 40)).save(buf, fmt)
        return buf.getvalue()

    raw = encode("PNG")
    got = vision.prepare(raw)
    r.check("a small PNG passes through untouched",
            base64.b64decode(got[0]), raw)
    r.check("...declared correctly", got[1], "image/png")

    # Grok accepts PNG and JPEG only, and Fluxer serves plenty of WebP.
    got = vision.prepare(encode("WEBP"))
    r.ok("WebP is re-encoded to something acceptable",
         got[1] in ("image/png", "image/jpeg"))

    got = vision.prepare(encode("GIF", frames=6))
    sheet = Image.open(io.BytesIO(base64.b64decode(got[0])))
    r.check("an animation becomes a square contact sheet",
            sheet.width, sheet.height)
    r.ok("...sized for the model", sheet.width <= vision.ANIM_MAX_PX)
    r.ok("...and flagged so the prompt explains the grid", got[2])

    # Too few frames to tile: it is a still, so the grid hint would lie.
    got = vision.prepare(encode("GIF", frames=2))
    r.ok("a two-frame animation falls back to a still", not got[2])

    r.check("undecodable bytes are dropped, never forwarded",
            vision.prepare(b"definitely not an image"), None)
    r.check("empty input is dropped", vision.prepare(b""), None)

    big = vision.prepare(encode("PNG", size=(6000, 6000)))
    r.ok("an oversized image is compressed under the cap",
         len(base64.b64decode(big[0])) <= vision.MAX_IMAGE_BYTES)

    block = vision.image_block("QUFB", "image/png")
    r.check("the block is Anthropic-shaped", block["type"], "image")
    r.check("...base64 source", block["source"]["type"], "base64")
    r.check("...with its media type", block["source"]["media_type"],
            "image/png")

    r.check("magic bytes identify PNG",
            vision.detect_media_type(encode("PNG")), "image/png")
    r.check("...and JPEG", vision.detect_media_type(encode("JPEG")),
            "image/jpeg")


def suite_corbin_emoji(r: Results) -> None:
    """:corbin: has to become a token on the way out, and only there."""
    import asyncio
    from corbin.core.emoji import render, token
    from corbin.platforms.fluxer.adapter import FluxerAdapter
    from corbin.core.context import Response

    eid = "111111111111111111"
    r.check("the shorthand expands", render("hi :corbin:", eid),
            f"hi <:corbin:{eid}>")
    # The wrong prefix renders as literal text, so this is not cosmetic.
    r.check("an animated emoji uses the a: prefix",
            render("hi :corbin:", eid, animated=True),
            f"hi <a:corbin:{eid}>")
    r.check("every occurrence is expanded",
            render(":corbin: x :corbin:", eid).count(f"<:corbin:{eid}>"), 2)
    r.check("text without it is untouched", render("nothing", eid),
            "nothing")
    # Better a literal ":corbin:" than a token pointing at nothing.
    r.check("no configured emoji leaves the shorthand alone",
            render("hi :corbin:", None), "hi :corbin:")
    r.check("rendering twice does not nest tokens",
            render(render("hi :corbin:", eid), eid),
            render("hi :corbin:", eid))
    r.ok("an already-rendered token survives a second pass",
         render("<:corbin:999> and :corbin:", eid).count("<:corbin:999>") == 1)

    # It must happen at the send boundary, so every path gets it.
    class HTTP:
        async def get_guild_emojis(self, gid):
            return [{"id": eid, "name": "corbin", "animated": True}]

    adapter = FluxerAdapter(type("C", (), {"_http": HTTP()})())
    r.ok("the emoji resolves from the guild",
         asyncio.run(adapter.set_emoji(eid, "g1")))
    r.ok("...picking up that it is animated", adapter.emoji_animated)
    payload = adapter._payload(Response(content="nice :corbin:"))
    r.check("the outgoing payload carries the token",
            payload["content"], f"nice <a:corbin:{eid}>")

    # An id that is not in the guild must not produce a broken token.
    class Empty:
        async def get_guild_emojis(self, gid):
            return []
    other = FluxerAdapter(type("C", (), {"_http": Empty()})())
    r.ok("an emoji missing from the guild is reported, not assumed",
         not asyncio.run(other.set_emoji(eid, "g1")))


SUITES = [
    ("media labels", suite_labels),
    ("custom emoji", suite_emoji),
    ("media in the transcript", suite_transcript),
    ("media rules", suite_rules),
    ("vision preparation", suite_vision),
    ("corbin emoji rendering", suite_corbin_emoji),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
