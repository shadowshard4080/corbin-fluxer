"""Offline suite for the conversational layer."""
from __future__ import annotations
from pathlib import Path
import logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.conversation import (Conversation, has_content,
                                    resolve_mentions, split_message,
                                    trim_incomplete_tail, MAX_MESSAGE_CHARS)


class FakeLLM:
    class constants:
        MODEL_MAIN = "m"; EFFORT_MAIN = "low"


def suite_text(r: Results) -> None:
    r.ok("blank is empty", not has_content(""))
    r.ok("whitespace is empty", not has_content("   \n "))
    r.ok("text is content", has_content("hi"))
    r.ok("nested list", has_content([{"text": "x"}]))
    r.ok("empty list", not has_content([]))

    r.check("known user", resolve_mentions("hi <@5>", users={"5": "Sam"}), "hi @Sam")
    r.check("nickname form", resolve_mentions("hi <@!5>", users={"5": "Sam"}), "hi @Sam")
    r.check("unknown user degrades", resolve_mentions("hi <@9>"), "hi @someone")
    r.check("role", resolve_mentions("<@&2>", roles={"2": "Mods"}), "@Mods")
    r.check("channel", resolve_mentions("<#3>", channels={"3": "general"}), "#general")
    # An unresolvable channel must keep its id. The old placeholder
    # "#a channel" was extracted by the classifier as a real channel name
    # and written into settings as a broken target.
    r.check("unknown channel keeps the id",
            resolve_mentions("go to <#123>"), "go to #123")
    r.ok("no placeholder text leaks",
         "a channel" not in resolve_mentions("<#9> and <#8>"))
    r.ok("no raw id survives",
         "<@" not in resolve_mentions("a <@1> b <@&2> c <#3>"))
    r.check("plain text untouched", resolve_mentions("hello there"), "hello there")

    r.check("complete kept", trim_incomplete_tail("Done."), "Done.")
    r.check("question kept", trim_incomplete_tail("Really?"), "Really?")
    r.check("quote-ending kept", trim_incomplete_tail('He said "no."'), 'He said "no."')
    r.check("trailing comma dropped", trim_incomplete_tail("Well,"), "")
    r.check("rolls back to last sentence",
            trim_incomplete_tail("One. Two. Three and"), "One. Two.")
    r.check("single fragment dropped", trim_incomplete_tail("nope"), "")
    r.check("empty stays empty", trim_incomplete_tail(""), "")

    r.check("short not split", len(split_message("hi")), 1)
    r.check("empty splits to nothing", split_message(""), [])
    chunks = split_message("x" * 5000)
    r.ok("all chunks within limit",
         all(len(c) <= MAX_MESSAGE_CHARS for c in chunks))
    r.check("5000 chars -> 3 chunks", len(chunks), 3)
    r.ok("prefers newline boundary",
         split_message("a" * 1500 + "\n\n" + "b" * 1000)[0].endswith("a"))
    fenced = split_message("a" * 1990 + "\n```py\nl1\nl2\nl3\n```")
    r.ok("every chunk has balanced fences",
         all(c.count("```") % 2 == 0 for c in fenced))
    r.ok("continuation reopens the fence", fenced[1].startswith("```"))


def suite_history(r: Results) -> None:
    conv = Conversation(FakeLLM(), soul_path="/nonexistent/SOUL.md")
    r.check("missing soul degrades to empty", conv.soul, "")

    conv.record("c", "Sam", "hello")
    conv.record("c", "Corbin", "hey")
    conv.record("c", "Sam", "   ")
    conv.record("c", "Sam", "")
    r.check("blank lines dropped", len(conv._channels["c"]), 2)
    e = conv.entries("c")[0]
    r.check("entry records the author", e["author"], "Sam")
    r.ok("entry is timestamped", e["timestamp"] is not None)
    r.ok("human entries not flagged as bot", not e["is_bot"])
    r.check("speaker labels", conv.transcript("c"),
            "[Sam]: hello\n[Corbin]: hey")
    r.check("unknown channel is empty", conv.transcript("nope"), "")
    r.ok("highlight marks the target",
         "<<< YOU ARE REPLYING" in conv.transcript("c", highlight="hello"))
    r.ok("bot entries are flagged",
         conv.record("c", "Corbin", "hi", is_bot=True)["is_bot"])

    # The search pipeline needs Corbin's last line to spot "yes" replies to
    # an offer. A caller unpacking entries itself broke when the entry
    # shape changed, which silently killed every conversational reply.
    r.check("last bot message found", conv.last_bot_message("c"), "hi")
    conv.record("c", "Sam", "and another thing")
    r.check("human messages do not shadow it",
            conv.last_bot_message("c"), "hi")
    conv.record("c", "Corbin", "newer", is_bot=True)
    r.check("most recent bot line wins",
            conv.last_bot_message("c"), "newer")
    r.check("empty channel is safe", conv.last_bot_message("nope"), "")
    r.ok("entries are dicts, not tuples",
         all(isinstance(e, dict) for e in conv.entries("c")))
    r.ok("every entry has the presence fields",
         all({"author", "content", "is_bot", "timestamp"} <= set(e)
             for e in conv.entries("c")))

    for i in range(60):
        conv.record("big", "A", f"msg {i}")
    r.ok("channel history is bounded", len(conv._channels["big"]) <= 25)

    conv.remember_turn("u", "user", "hi")
    conv.remember_turn("u", "assistant", "hey")
    conv.remember_turn("u", "user", "  ")
    r.check("blank turn refused", len(conv.user_history("u")), 2)
    conv.clear_user("u")
    r.check("clear works", conv.user_history("u"), [])

    msgs = conv.build_messages(user_id="u", user_content="what's up",
                               channel_id="c")
    r.ok("no empty turns ever",
         all(has_content(m["content"]) for m in msgs))
    r.check("last turn is exactly the message being answered",
            (msgs[-1]["role"], msgs[-1]["content"]), ("user", "what's up"))
    r.ok("the transcript is NOT in any turn (in the question's turn, the "
         "model answered it: asked about 'SO', it explained other "
         "people's jokes)",
         not any("channel messages" in str(m["content"]).lower()
                 for m in msgs))
    sp = conv.system_prompt(guild_name="g", channel_name="c", speaker="Sam",
                            channel_context=conv.transcript("c"))
    r.ok("it goes in the system prompt, as background",
         "background only" in sp and "[Sam]: hello" in sp)
    r.ok("...told plainly to answer only the latest message",
         "Answer ONLY the latest message" in sp)
    r.ok("no context, no section",
         "background only" not in conv.system_prompt(
             guild_name="g", channel_name="c", speaker="Sam"))
    conv.record("c", "Sam", "what does SO mean")
    r.ok("the message being answered isn't duplicated into the context",
         "what does SO mean" not in conv.transcript(
             "c", omit_latest="what does SO mean"))
    r.ok("...but stays when it isn't the latest line",
         "[Sam]: hello" in conv.transcript("c", omit_latest="hello"))
    r.ok("no invented assistant turn (it got repeated back: "
         "'Understood, I'm caught up.')",
         not any("caught up" in str(m["content"]).lower() for m in msgs
                 if m["role"] == "assistant"))
    r.ok("roles still alternate", all(
        a["role"] != b["role"] for a, b in zip(msgs, msgs[1:])))

    bare = Conversation(FakeLLM(), soul_path="/nonexistent")
    solo = bare.build_messages(user_id="x", user_content="yo", channel_id="empty")
    r.check("no transcript -> single turn", len(solo), 1)
    r.check("...holding just the message", solo[0]["content"], "yo")

    from corbin.ai.conversation import COMPACT_RULES
    low = COMPACT_RULES.lower()
    r.ok("the rules invite jokes, not just curb explaining them",
         "jokes are welcome" in low and "never" in low
         and "explain it" in low)

    from corbin.ai.conversation import drop_repeats
    long_para = ("She's a corvid. A snarky little one. And I genuinely "
                 "delight in corvids, the gothic-meets-neon aesthetic.")
    reply = long_para + "\n\nIt's a compliment, honestly."
    r.check("a paragraph he already sent is dropped",
            drop_repeats(reply, [long_para + "\n\nsomething else"]),
            "It's a compliment, honestly.")
    r.check("...matching despite punctuation and case",
            drop_repeats(long_para.upper().replace(".", "!"), [long_para]),
            "")
    r.check("short lines may recur", drop_repeats("lol", ["lol"]), "lol")
    r.check("nothing earlier, nothing dropped",
            drop_repeats(reply, []), reply)

    # Asked for "the full css" again, he must re-send code he already
    # posted. Blank-line-separated rules used to be dropped one by one,
    # leaving the prose around an empty gap.
    rule = ('[data-flx*="popout"] {\n  background: #111 !important;\n'
            '  border: 1px solid #222 !important;\n}')
    css = f"```css\n{rule}\n\n{rule.replace('popout', 'menu')}\n```"
    again = f"Here's the whole replacement.\n\n{css}\n\nReload after."
    r.check("code he already sent is re-sent intact",
            drop_repeats(again, [css]), again)
    r.check("...while repeated prose around it still goes",
            drop_repeats(f"{long_para}\n\n{css}", [long_para]), css)

    from corbin.ai.conversation import extract_code_files
    r.check("a short block stays inline",
            extract_code_files("see ```py\nx = 1\n```"),
            ("see ```py\nx = 1\n```", []))
    long_code = "\n".join(f"line {i}" for i in range(12))
    body, files = extract_code_files(
        f"Here you go.\n\n```css\n{long_code}\n```\n\nReload after.")
    r.check("a long block becomes a file named for its language",
            [n for n, _ in files], ["code_1.css"])
    r.check("...holding the code itself", files[0][1].decode(),
            long_code + "\n")
    r.ok("...and the text points at it instead",
         "code_1.css" in body and "```" not in body
         and body.startswith("Here you go.")
         and body.endswith("Reload after."))
    r.check("an unknown language falls back to .txt",
            extract_code_files(f"```\n{long_code}\n```")[1][0][0],
            "code_1.txt")

    import asyncio

    class SeqLLM:
        persona_baked_in = True

        class constants:
            MODEL_MAIN = "m"
            EFFORT_MAIN = EFFORT_SEARCH = "low"
            EFFORT_THINK = "high"

        def __init__(self, *texts):
            self.texts, self.calls = list(texts), 0

        async def call(self, **kw):
            self.calls += 1
            text = self.texts.pop(0)
            return type("R", (), {"text": text, "tool_calls": (),
                                  "truncated": False})()

    llm = SeqLLM(long_para, "Mostly that she's a menace. A lovable one.")
    conv2 = Conversation(llm, soul_path="/nonexistent")
    conv2.record("ch", "Corbin", long_para, is_bot=True)
    out = asyncio.run(conv2.reply(user_id="w", speaker="wolf",
                                  user_content="what is bro yapping about",
                                  channel_id="ch"))
    r.check("a reply that is wholly a repeat is retried once",
            (llm.calls, out),
            (2, "Mostly that she's a menace. A lovable one."))


def suite_prompt(r: Results) -> None:
    conv = Conversation(FakeLLM(), soul_path="/nonexistent")
    p = conv.system_prompt(guild_name="Riley", channel_name="general",
                           speaker="Sam", known_names=["Sam", "Alex"])
    r.ok("names the server", "Riley" in p)
    r.ok("names the channel", "general" in p)
    r.ok("names the speaker", "Sam" in p)
    r.ok("lists participants", "Alex" in p)
    r.ok("states the label rule", "[Name]:" in p)
    r.ok("forbids inventing names", "Never invent" in p)
    r.ok("forbids self-labelling", "do not prefix" in p.lower())
    r.ok("mentions Fluxer", "Fluxer" in p)

    real = Conversation(
        FakeLLM(),
        soul_path=Path(__file__).resolve().parent.parent / "persona"
        / "SOUL.md")
    if real.soul:
        r.ok("real SOUL.md loads", len(real.soul) > 1000)
        r.ok("persona precedes context",
             real.system_prompt(guild_name=None, channel_name=None,
                                speaker="Sam").index("Current Context") > 100)


class RecordingLLM:
    """Captures the kwargs of the one call a reply makes."""

    class constants:
        MODEL_MAIN = "m"
        EFFORT_MAIN = "low"
        EFFORT_SEARCH = "low"
        EFFORT_THINK = "high"

    persona_baked_in = False

    def __init__(self):
        self.seen = None

    async def call(self, **kw):
        self.seen = kw

        class Result:
            text = "an answer"
            truncated = False
        return Result()


def suite_provider_routing(r: Results) -> None:
    """The persona goes out once: from the file, or baked into the model."""
    import asyncio
    import tempfile
    from pathlib import Path
    from corbin.ai.conversation import COMPACT_RULES, MAX_TOKENS_REPLY

    with tempfile.TemporaryDirectory() as tmp:
        soul = Path(tmp) / "SOUL.md"
        soul.write_text("# Persona\nYou are a test raven.", encoding="utf-8")

        def reply(baked):
            llm = RecordingLLM()
            llm.persona_baked_in = baked
            convo = Conversation(llm, soul_path=soul)
            asyncio.run(convo.reply(user_id="u", speaker="Sam",
                                    user_content="why is the sky blue",
                                    channel_id="c"))
            return llm.seen

        sent = reply(False)
        r.ok("the persona file is the system prompt",
             "test raven" in sent["system"])
        r.check("...with the full reply budget",
                sent["max_tokens"], MAX_TOKENS_REPLY)
        baked = reply(True)
        r.ok("a baked persona is not sent again",
             "test raven" not in baked["system"])
        r.ok("...the compact rules go instead",
             COMPACT_RULES.splitlines()[0] in baked["system"])
        r.ok("no provider-specific arguments reach the model",
             not {"effort", "prefer_local", "system_local"} & set(sent))


def suite_platform(r: Results) -> None:
    """He must know which chat app he is in.

    Both personas say Discord -- SOUL.md mentions it and the local
    model has it baked into its Modelfile -- so the context block has
    to contradict it outright, not merely mention Fluxer.
    """
    conv = Conversation(FakeLLM(), soul_path="/nonexistent")
    for include_soul in (True, False):
        prompt = conv.system_prompt(speaker="Sam", guild_name="The Den",
                                    channel_name="general",
                                    include_soul=include_soul)
        label = "with soul" if include_soul else "local model"
        r.ok(f"{label}: says Fluxer", "Fluxer" in prompt)
        r.ok(f"{label}: says it is not Discord",
             "NOT Discord" in prompt)
        r.ok(f"{label}: forbids referring to Discord features",
             "Never say Discord" in prompt)


SUITES = [
    ("conversation text helpers", suite_text),
    ("provider routing", suite_provider_routing),
    ("conversation history", suite_history),
    ("system prompt", suite_prompt),
    ("platform identity", suite_platform),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
