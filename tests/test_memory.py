"""Offline suite for memory storage and extraction parsing."""
from __future__ import annotations
import json, logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.extraction import build_prompt, parse
from corbin.core.memory import (MAX_USER_FACTS, MemoryStore, ServerMemory,
                                UserMemory, clean_fact)


def suite_user_memory(r: Results) -> None:
    m = UserMemory("u1")
    r.check("plain fact added", m.apply(["has a dog"]), 1)
    r.check("exact duplicate ignored", m.apply(["has a dog"]), 0)
    r.check("case-insensitive duplicate ignored", m.apply(["HAS A DOG"]), 0)
    r.check("blank ignored", m.apply(["", "   "]), 0)

    r.check("IANA timezone accepted",
            m.apply(["TIMEZONE: America/Denver"]), 1)
    r.check("timezone stored as a preference", m.timezone, "America/Denver")
    r.ok("timezone is not also a fact",
         not any("TIMEZONE" in f for f in m.facts))
    r.check("abbreviation rejected", m.apply(["TIMEZONE: EST"]), 0)
    r.check("offset rejected", m.apply(["TIMEZONE: UTC+3"]), 0)
    r.check("timezone unchanged by junk", m.timezone, "America/Denver")
    r.check("same timezone again is no change",
            m.apply(["TIMEZONE: America/Denver"]), 0)

    r.check("UPDATE replaces in place", m.apply(["UPDATE:0: has two dogs"]), 1)
    r.check("fact really replaced", m.facts[0], "has two dogs")
    r.check("UPDATE out of range appends instead",
            m.apply(["UPDATE:99: brand new fact"]), 1)
    r.ok("appended, not crashed", "brand new fact" in m.facts)

    r.check("long fact truncated", len(clean_fact("x" * 900)), 300)
    r.check("whitespace collapsed", clean_fact("a   b\n c"), "a b c")

    big = UserMemory("u2")
    big.apply([f"fact number {i}" for i in range(MAX_USER_FACTS + 15)])
    r.check("fact list is capped", len(big.facts), MAX_USER_FACTS)
    r.ok("newest kept, oldest dropped",
         big.facts[-1].endswith(str(MAX_USER_FACTS + 14)))

    m.touch("Sam")
    r.check("display name recorded", m.display_name, "Sam")
    r.check("interaction counted", m.interaction_count, 1)
    r.ok("last seen stamped", bool(m.last_seen))


def suite_server_memory(r: Results) -> None:
    s = ServerMemory("g1")
    r.check("SERVER prefix stripped", s.apply(["SERVER:game night is Fridays"]), 1)
    r.check("stored without prefix", s.facts[0], "game night is Fridays")
    r.check("unprefixed still accepted", s.apply(["has 3 mods"]), 1)
    r.check("duplicate ignored", s.apply(["SERVER:has 3 mods"]), 0)
    r.ok("last_updated stamped", bool(s.last_updated))


def suite_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(tmp)
        r.check("unknown user is empty", store.get_user("u").facts, [])

        entry = store.get_user("u")
        entry.apply(["likes ravens"]); entry.touch("Sam")
        store.save_user(entry)
        r.check("persists", store.get_user("u").facts, ["likes ravens"])
        r.check("display name persists", store.get_user("u").display_name, "Sam")
        r.check("no temp files", list(store.users.glob("*.tmp")), [])

        srv = store.get_server("g"); srv.apply(["SERVER:weekly movie night"])
        store.save_server(srv)
        r.check("server persists", store.get_server("g").facts,
                ["weekly movie night"])

        block = store.context_block("u", "g", "Sam")
        r.ok("names the person", "Sam" in block)
        r.ok("includes user facts", "likes ravens" in block)
        r.ok("includes server facts", "weekly movie night" in block)
        r.ok("tells the model not to recite", "never as a list" in block)
        r.check("empty when nothing known",
                store.context_block("nobody", None), "")

        # A corrupt file must be quarantined, not silently replaced: these
        # facts cannot be regenerated.
        path = store.user_path("broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        r.check("corrupt reads as empty", store.get_user("broken").facts, [])
        r.ok("original quarantined",
             any(p.name.startswith("broken.corrupt")
                 for p in store.users.glob("*.json")))

        r.ok("forget removes", store.forget_user("u"))
        r.check("gone after forget", store.get_user("u").facts, [])
        r.ok("forget twice is False", not store.forget_user("u"))

        r.check("path traversal sanitised",
                store.user_path("../../etc/passwd").name, "etcpasswd.json")


def suite_extraction(r: Results) -> None:
    p = build_prompt(display_name="Sam", user_content="my dog hates rain",
                     bot_response="ha", known_facts=["likes post-rock"],
                     include_server=True)
    r.ok("names the speaker", "Sam" in p)
    r.ok("indexes known facts", "[0] likes post-rock" in p)
    r.ok("explains UPDATE", "UPDATE:<index>:" in p)
    r.ok("demands IANA", "IANA" in p)
    r.ok("asks for server facts", "server_facts" in p)
    r.ok("forbids third-party facts", "about other people" in p)
    # A fact can span turns: "Her name is Ziggy" means nothing on its own,
    # but after "what's my dog's name?" it clearly names the user's dog.
    with_ctx = build_prompt(
        display_name="Sam", user_content="Her name is Ziggy",
        bot_response="Got it.", known_facts=[],
        recent=[{"role": "user", "content": "what's my dog's name?"},
                {"role": "assistant", "content": "I don't know it."}])
    r.ok("prior turns included", "EARLIER IN THE CONVERSATION" in with_ctx)
    r.ok("prior user turn shown", "what's my dog's name?" in with_ctx)
    r.ok("bot turns labelled Corbin", "Corbin: I don't know it." in with_ctx)
    r.ok("no context section when none given",
         "EARLIER IN" not in build_prompt(
             display_name="L", user_content="x", bot_response="y",
             known_facts=[]))

    nos = build_prompt(display_name="L", user_content="x", bot_response="y",
                       known_facts=[], include_server=False)
    r.ok("server section omitted", "server_facts" not in nos)
    r.ok("empty facts noted", "(none yet)" in nos)

    r.check("normal payload",
            parse('{"user_facts":["has a dog"],"server_facts":["SERVER:x"]}'),
            (["has a dog"], ["SERVER:x"]))
    r.check("null list tolerated",
            parse('{"user_facts":["a"],"server_facts":null}'), (["a"], []))
    r.check("blank entries dropped",
            parse('{"user_facts":["a","","  "]}'), (["a"], []))
    r.check("truncated payload salvaged",
            parse('{"user_facts":["has a dog'), (["has a dog"], []))
    r.check("prose returns nothing", parse("sorry!"), ([], []))
    r.check("empty returns nothing", parse(""), ([], []))
    r.check("non-list ignored", parse('{"user_facts":"a string"}'), ([], []))


def suite_memory_framing(r: Results) -> None:
    """Remembered facts must not read as a to-do list.

    A stored fact saying "actively seeking casserole recipes using 2 lbs
    of ground beef" was answered, unprompted and at length, in reply to
    "alright, couple little hiccups". The local model treated the memory
    block as pending requests.
    """
    import tempfile
    from corbin.core.memory import MemoryStore, UserMemory
    from corbin.ai.extraction import build_prompt

    entry = UserMemory("u", display_name="Riley")
    entry.facts = ["likes casseroles", "has a dog called Ziggy"]
    store = MemoryStore(tempfile.mkdtemp())
    store.save_user(entry)
    block = store.context_block("u", None, "Riley")

    # Before the facts, where a small model is still reading.
    head = block[:block.index("- likes casseroles")]
    r.ok("the block is marked background before any fact is listed",
         "NOT questions" in head and "NOT things to do" in head)
    r.ok("...and again afterwards", "None of the above is a request" in block)
    r.ok("answering only the transcript is stated",
         "only what was actually said" in block)
    r.ok("reciting them is still forbidden", "never as a list" in block)

    empty = MemoryStore(tempfile.mkdtemp())
    r.check("no facts means no block at all",
            empty.context_block("nobody", None, "X"), "")

    # The extractor is what let an errand become a fact.
    prompt = build_prompt(display_name="Riley", user_content="hi",
                          bot_response="hello", known_facts=[])
    r.ok("the extractor refuses requests and tasks",
         "never store a request" in prompt.lower())
    r.ok("...with the casserole case named", "casserole" in prompt.lower())
    r.ok("...and asks for attributes, not actions in progress",
         "stable attribute" in prompt.lower())


SUITES = [
    ("user memory", suite_user_memory),
    ("server memory", suite_server_memory),
    ("memory store", suite_store),
    ("memory framing", suite_memory_framing),
    ("memory extraction", suite_extraction),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
