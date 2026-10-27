"""Offline suite for link previews: read, stored, and shown only to replies."""
from __future__ import annotations
import asyncio, datetime as dt, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.conversation import Conversation
from corbin.ai.presence import PresenceEngine, numbered_transcript
from corbin.core.link_previews import (MAX_PREVIEW_CHARS, embeds_of,
                                       has_link, preview_text, preview_texts,
                                       prompt_block)

#: The shape FxTwitter unfurled in the screenshot that started this.
TWEET = {
    "type": "rich", "url": "https://fixupx.com/vincsmith/status/1",
    "author": {"name": "V.Smith (@vincsmith)"},
    "description": "Bitches can't even drive in the rain and yall showing "
                   "me flying cars?",
    "footer": {"text": "FxTwitter"},
}


def suite_reading(r: Results) -> None:
    line = preview_text(TWEET)
    r.ok("the tweet text is read", "flying cars" in line)
    r.ok("the author is kept", "V.Smith (@vincsmith)" in line)
    r.ok("the source is named", line.startswith("FxTwitter: "))
    r.check("an image-only embed has nothing to read",
            preview_text({"type": "image",
                          "thumbnail": {"url": "https://x/y.png"}}), None)
    r.check("a provider name wins over the footer",
            preview_text({"provider": {"name": "YouTube"},
                          "title": "Raven calls", "footer": {"text": "f"}}),
            "YouTube: Raven calls")
    r.ok("long text is clipped",
         len(preview_text({"description": "x" * 2000})) <= MAX_PREVIEW_CHARS)
    r.check("duplicates collapse", len(preview_texts([TWEET, TWEET])), 1)
    r.check("embeds read off a dict", embeds_of({"embeds": [TWEET]}),
            [TWEET])
    r.check("no message, no embeds", embeds_of(None), [])
    r.ok("a URL is a link", has_link("so this https://fixupx.com/a/status/1"))
    r.ok("plain text is not", not has_link("no links here"))
    r.check("no previews, no block", prompt_block([]), "")


class _FakeLLM:
    class constants:
        MODEL_MAIN = MODEL_REPLY = MODEL_THINK = MODEL_FAST = "m"
        EFFORT_MAIN = EFFORT_SEARCH = EFFORT_THINK = "low"
        EFFORT_FAST = "none"; EFFORT_PRESENCE = "low"

    def __init__(self):
        self.seen = []

    async def call(self, **kw):
        self.seen.append(kw)
        return type("R", (), {"text": "ha, fair", "truncated": False,
                              "tool_calls": None})()


def suite_conversation(r: Results) -> None:
    llm = _FakeLLM()
    conv = Conversation(llm, soul_path="/nonexistent/SOUL.md")
    conv.record("c", "Quill", "https://fixupx.com/vincsmith/status/1 LMAO",
                message_id="m1")
    r.ok("no preview yet", "link preview" not in conv.transcript("c"))
    r.ok("a late preview attaches to its message",
         conv.set_previews("c", "m1", preview_texts([TWEET])))
    r.ok("an unknown message is ignored",
         not conv.set_previews("c", "nope", ["x"]))
    r.ok("an empty update keeps what was there",
         not conv.set_previews("c", "m1", []))
    r.ok("the reply transcript shows it",
         "[link preview: FxTwitter: " in conv.transcript("c"))

    content = "corbin look https://fixupx.com/vincsmith/status/1"
    asyncio.run(conv.reply(user_id="u", speaker="Quill",
                           user_content=content, channel_id="c",
                           link_previews=preview_texts([TWEET])))
    turn = llm.seen[-1]["messages"][-1]["content"]
    r.ok("the reply's own turn carries the preview",
         "[Tool:LinkPreview]" in turn and "flying cars" in turn)
    r.check("history stores only what was typed",
            conv.user_history("u")[-2]["content"], content)


def _e(author, content, previews=(), is_bot=False):
    return {"author": author, "content": content, "is_bot": is_bot,
            "media": [], "previews": list(previews),
            "timestamp": dt.datetime.now(dt.timezone.utc)}


def suite_presence(r: Results) -> None:
    window = [_e("Quill", "https://fixupx.com/vincsmith/status/1 LMAO",
                 preview_texts([TWEET])),
              _e("Kit", "lmaooo")]
    gate_text, _ = numbered_transcript(window)
    r.ok("the gate never sees a preview", "flying cars" not in gate_text)

    class _Conv:
        soul = ""
        def entries(self, cid): return list(window)

    llm = _FakeLLM()

    async def decide(**kw):
        llm.seen.append(kw)
        text = ("RESPOND #1 | funny" if kw.get("structured")
                else "rain is the real test")
        return type("R", (), {"text": text, "truncated": False})()

    llm.call = decide
    sent = []

    async def send(cid, text, target):
        sent.append(text)

    eng = PresenceEngine(llm, _Conv(), send=send, is_enabled=lambda g, c: True)
    r.ok("it chimes in", asyncio.run(eng.consider("g", "c")))
    gate, chime = llm.seen[0], llm.seen[1]
    r.ok("the gate's prompt has no preview",
         "flying cars" not in gate["messages"][0]["content"])
    r.ok("the chime-in's prompt does",
         "flying cars" in chime["messages"][0]["content"])


SUITES = [
    ("link previews: reading", suite_reading),
    ("link previews: conversation", suite_conversation),
    ("link previews: presence", suite_presence),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
