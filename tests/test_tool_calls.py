"""Offline suite for the model layer and tool calls.

The model gets one tool (lookup_commands) on the plain-chat path. These
cover each hop: parsing tool calls, the LLM layer routing and retrying,
vision translation, and the conversation layer returning a ToolRequest
without storing a turn.
"""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai import conversation as conv_mod
from corbin.ai.conversation import Conversation, ToolRequest, REPLY_TOOLS
from corbin.ai.llm import LLM
from corbin.ai.ollama import OllamaProvider, OllamaResult, parse_tool_calls


def suite_parse(r: Results) -> None:
    r.check("OpenAI-shaped tool calls are parsed",
            parse_tool_calls([{"function": {
                "name": "web_search", "arguments": '{"query": "nba finals"}'}}]),
            [{"name": "web_search", "arguments": {"query": "nba finals"}}])
    r.check("dict arguments pass through",
            parse_tool_calls([{"function": {"name": "think_carefully",
                                            "arguments": {}}}]),
            [{"name": "think_carefully", "arguments": {}}])
    r.check("bad JSON keeps the call, with no arguments",
            parse_tool_calls([{"function": {"name": "web_search",
                                            "arguments": "{oops"}}]),
            [{"name": "web_search", "arguments": {}}])
    r.check("nameless calls are dropped",
            parse_tool_calls([{"function": {"arguments": "{}"}}]), [])
    r.check("no tool calls, empty list", parse_tool_calls(None), [])
    r.ok("a tool-call-only result is truthy",
         OllamaResult(text="", tool_calls=[{"name": "x", "arguments": {}}]))
    r.ok("an empty result is still falsy", not OllamaResult(text=""))


def suite_context_window(r: Results) -> None:
    """The window must fit the prompt AND the thinking AND the answer.

    Unset, Ollama uses 4096; with 2.5-4K-token prompts the model ran out
    mid-thought, twice, at exactly in+out = 4096, and replied with nothing.
    """
    from pathlib import Path

    from corbin.ai.ollama import MIN_CONTEXT, parse_num_ctx

    r.check("num_ctx is read from /api/show parameters",
            parse_num_ctx({"parameters": "top_k 20\nnum_ctx 16384\n"}),
            16384)
    r.check("unset means None (Ollama's default applies)",
            parse_num_ctx({"parameters": "top_k 20"}), None)
    r.check("junk is None, not a crash", parse_num_ctx(None), None)

    modelfile = Path(__file__).resolve().parents[1] / "ollama" / "Modelfile"
    if modelfile.exists():
        ctx = parse_num_ctx({"parameters": "\n".join(
            " ".join(line.split()[1:]) for line in
            modelfile.read_text(encoding="utf-8").splitlines()
            if line.startswith("PARAMETER"))})
        r.ok(f"the Modelfile sets a context of at least {MIN_CONTEXT}",
             ctx is not None and ctx >= MIN_CONTEXT)


class FakeProvider:
    persona_baked_in = False
    vision = False

    def __init__(self, model, results):
        self.model, self.results, self.seen = model, list(results), []
        self.down = False

    async def available(self, force=False):
        return True

    async def call(self, **kw):
        self.seen.append(kw)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def mark_down(self):
        self.down = True


def _ok(text="hi", **kw):
    return OllamaResult(text=text, model="m", **kw)


def suite_llm(r: Results) -> None:
    LLM.RETRY_DELAY = 0.0
    tool_result = _ok("", tool_calls=[{"name": "lookup_commands",
                                       "arguments": {"topic": "music"}}])
    main = FakeProvider("big", [tool_result])
    llm = LLM(main)
    got = asyncio.run(llm.call(model="big", messages=[], tools=REPLY_TOOLS))
    r.ok("a tool call is returned, not treated as an empty reply",
         got is tool_result)
    r.check("...and the tools reached the model", main.seen[0].get("tools"),
            REPLY_TOOLS)

    main = FakeProvider("big", [_ok()])
    asyncio.run(LLM(main).call(model="big", messages=[]))
    r.ok("no tools, no tools key", "tools" not in main.seen[0])

    main, fast = FakeProvider("big", [_ok()]), FakeProvider("small", [_ok()])
    llm = LLM(main, fast)
    r.check("constants name both models",
            (llm.constants.MODEL_MAIN, llm.constants.MODEL_FAST),
            ("big", "small"))
    asyncio.run(llm.call(model=llm.constants.MODEL_FAST, messages=[]))
    r.ok("MODEL_FAST goes to the fast model", fast.seen and not main.seen)
    r.check("one model serves both when no fast one is set",
            LLM(FakeProvider("big", [])).constants.MODEL_FAST, "big")

    main = FakeProvider("big", [RuntimeError("blip"), _ok("second")])
    r.check("a failure is retried once",
            asyncio.run(LLM(main).call(model="big", messages=[])).text,
            "second")
    main = FakeProvider("big", [RuntimeError("a"), RuntimeError("b")])
    try:
        asyncio.run(LLM(main).call(model="big", messages=[]))
        r.ok("two failures raise", False)
    except RuntimeError:
        r.ok("two failures raise", True)
    r.ok("...and mark the model down for health", main.down)

    call = LLM(FakeProvider("big", [_ok("{}")])).classifier_call()
    r.check("the classifier call returns text",
            asyncio.run(call(system="s", user="u", max_tokens=10)), "{}")


def suite_vision(r: Results) -> None:
    image = {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": "AAAA"}}
    turn = [{"role": "user", "content": [image,
                                         {"type": "text", "text": "what?"}]}]
    flat = OllamaProvider._to_openai(turn, "sys")
    r.check("without vision, images are dropped to text", flat[1],
            {"role": "user", "content": "what?"})
    seen = OllamaProvider._to_openai(turn, None, vision=True)
    r.check("with vision, an image_url part is sent",
            seen[0]["content"][0],
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AAAA"}})
    r.check("...followed by the text", seen[0]["content"][1],
            {"type": "text", "text": "what?"})


class ToolLLM:
    persona_baked_in = True

    class constants:
        MODEL_MAIN = "m"

    def __init__(self, text="", calls=()):
        self.text, self.calls, self.seen = text, list(calls), None

    async def call(self, **kw):
        self.seen = kw
        text, calls = self.text, self.calls

        class Result:
            pass
        res = Result()
        res.text, res.tool_calls, res.truncated = text, calls, False
        return res


def reply(llm, **kw):
    convo = Conversation(llm, soul_path="/nonexistent/SOUL.md")
    out = asyncio.run(convo.reply(user_id="u", speaker="Robin",
                                  user_content="who won the finals?",
                                  channel_id="c", **kw))
    return out, convo


def suite_conversation(r: Results) -> None:
    llm = ToolLLM(calls=[{"name": "lookup_commands",
                          "arguments": {"topic": "music"}}])
    out, convo = reply(llm, tools=REPLY_TOOLS)
    r.ok("a lookup request comes back as a ToolRequest",
         isinstance(out, ToolRequest) and out.name == "lookup_commands")
    r.check("...carrying the model's topic", out.arguments,
            {"topic": "music"})
    r.ok("...which is never mistaken for a sendable reply", not out)
    r.check("...and stores no turn", convo.user_history("u"), [])
    r.ok("tools go out with the call", llm.seen.get("tools") == REPLY_TOOLS)

    out, convo = reply(ToolLLM(text="Denver did."), tools=REPLY_TOOLS)
    r.check("a direct answer is just text", out, "Denver did.")
    r.check("...and is stored as a turn", len(convo.user_history("u")), 2)

    out, _ = reply(ToolLLM(text="", calls=[{"name": "rm_rf", "arguments": {}}]),
                   tools=REPLY_TOOLS)
    r.check("an unknown tool with no text is silence, not a crash", out, None)

    out, _ = reply(ToolLLM(text="ok", calls=[{"name": "lookup_commands",
                                               "arguments": {}}]))
    r.check("tool calls are ignored when no tools were offered", out, "ok")

    llm = ToolLLM(text="ok")
    reply(llm)
    r.ok("no tools offered, no tools key", "tools" not in llm.seen)

    names = [t["function"]["name"] for t in conv_mod.REPLY_TOOLS]
    r.check("the one tool on offer", names, ["lookup_commands"])


SUITES = [
    ("local context window", suite_context_window),
    ("tool call parsing", suite_parse),
    ("model layer", suite_llm),
    ("vision translation", suite_vision),
    ("tool calls in replies", suite_conversation),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
