"""Offline suite for deciding whether a message is aimed at Corbin.

Two failures this pins, both shipped and both caught in use:

* Treating *any* reply as an invocation, so two people replying to each
  other pulled Corbin into the middle of it.
* Then checking replies against a cache that only ever held *incoming*
  messages, so replies to Corbin matched nothing and he ignored them.
"""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.audit import CachedMessage, MessageCache

ME = "bot1"


class Adapter:
    """Knows the author of messages the cache has forgotten."""

    def __init__(self, authors=None):
        self.authors = authors or {}
        self.calls = []

    async def message_author(self, channel_id, message_id):
        self.calls.append(str(message_id))
        return self.authors.get(str(message_id))


def build():
    cache = MessageCache()
    cache.put(CachedMessage(id="bot_msg", channel_id="c", author_id=ME,
                            author_name="Corbin", content="hi",
                            attachments=()))
    cache.put(CachedMessage(id="kit_msg", channel_id="c", author_id="kit",
                            author_name="Kit", content="yo",
                            attachments=()))
    return cache


async def reply_is_to_bot(cache, adapter, ref, me_id=ME):
    """Mirrors bot.py's _reply_is_to_bot."""
    if not ref or not me_id:
        return False
    cached = cache.get("c", str(ref))
    if cached is not None:
        return str(cached.author_id) == str(me_id)
    author = await adapter.message_author("c", str(ref))
    return author is not None and str(author) == str(me_id)


def suite_reply_target(r: Results) -> None:
    cache, adapter = build(), Adapter({"old_bot": ME, "old_kit": "kit"})

    def check(ref):
        return asyncio.run(reply_is_to_bot(cache, adapter, ref))

    r.ok("a reply to Corbin counts", check("bot_msg"))
    r.ok("a reply to someone else does not", not check("kit_msg"))
    r.ok("no reference at all does not", not check(None))
    r.check("a cached answer costs no API call", adapter.calls, [])

    # The cache is small; a reply to anything older must still work.
    r.ok("an uncached reply to Corbin is looked up", check("old_bot"))
    r.ok("...and one to someone else is not his", not check("old_kit"))
    r.check("those did hit the API", adapter.calls, ["old_bot", "old_kit"])

    # Unknown even after asking: answering a stranger's reply to a
    # stranger is the worse mistake.
    r.ok("an unknowable reference is not treated as his",
         not check("vanished"))


def suite_ai_channel(r: Results) -> None:
    """An AI channel answers everything except people talking to
    each other in it."""

    class Channel:
        is_dm = False

    class Msg:
        def __init__(self, content="", ref=None):
            self.content, self.reference_id = content, ref
            self.channel = Channel()

    class Raw:
        def __init__(self, mentions=()):
            self.mentions = [type("U", (), {"id": m})() for m in mentions]

    # Mirrors bot.py: uncertainty ANSWERS. An unverifiable reference
    # used to mean silence, and silence in the ask-Corbin channel is
    # the worse failure.
    authors = {"bot_msg": ME, "kit_msg": "kit"}

    def aimed_elsewhere(message, raw):
        ref = getattr(message, "reference_id", None)
        if ref:
            author = authors.get(str(ref))
            if author is None:
                return False
            return author != ME
        return any(str(u.id) != ME for u in (raw.mentions or []))

    def decide(msg, raw, ai_channel, to_corbin=False):
        addressed = ME in msg.content
        invoked = (addressed or to_corbin or msg.channel.is_dm
                   or ai_channel)
        if not invoked:
            return False
        if (ai_channel and not addressed and not to_corbin
                and aimed_elsewhere(msg, raw)):
            return False
        return True

    r.ok("a plain question in the AI channel is answered",
         decide(Msg("what's the weather"), Raw(), True))
    r.ok("a mention is answered",
         decide(Msg(f"hey {ME}"), Raw([ME]), True))
    r.ok("a reply to Corbin is answered",
         decide(Msg("thanks", ref="bot_msg"), Raw(), True, to_corbin=True))

    r.ok("a reply to another member is ignored",
         not decide(Msg("lol yeah", ref="kit_msg"), Raw(), True))
    r.ok("a message mentioning another member is ignored",
         not decide(Msg("@Kit you there"), Raw(["kit"]), True))
    # Naming both means he was included on purpose.
    r.ok("mentioning Corbin and someone else is answered",
         decide(Msg(f"{ME} ask @Kit"), Raw([ME, "kit"]), True))

    # Outside an AI channel nothing changes.
    r.ok("an ordinary channel ignores chatter",
         not decide(Msg("just talking"), Raw(), False))
    r.ok("...but still answers a reply to Corbin",
         decide(Msg("ta", ref="bot_msg"), Raw(), False, to_corbin=True))

    # The silence path: a reply whose target cannot be identified.
    r.ok("an unverifiable reply is answered, not ignored",
         decide(Msg("what about this", ref="vanished"), Raw(), True))
    r.ok("...while a verified reply to someone else is still ignored",
         not decide(Msg("lol", ref="kit_msg"), Raw(), True))


SUITES = [
    ("reply targeting", suite_reply_target),
    ("ai channel filtering", suite_ai_channel),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
