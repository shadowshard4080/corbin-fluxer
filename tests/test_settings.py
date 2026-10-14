"""Offline suite for persisted settings and the config commands."""
from __future__ import annotations
import asyncio, json, logging, os, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, member, run

from corbin.core.context import (Channel, CommandContext, Guild, Member,
                                 Platform, Surface, User)
from corbin.core.permissions import Perm
from corbin.core.registry import Registry
from corbin.core.settings import ChatSettings, SettingsStore


class _R:
    async def send(self, r): return "m"
    async def fetch_member(self, g, u): return None


def _ctx(settings, *, guild_id="g1", channel_id="c1"):
    return CommandContext(
        platform=Platform.FLUXER, surface=Surface.AI,
        invoker=Member(user=User(id="u1", name="Mod"), guild_id=guild_id),
        channel=Channel(id=channel_id, name="general", guild_id=guild_id),
        guild=Guild(id=guild_id, name="Test"), responder=_R(),
        extras={"settings": settings},
    )


def suite_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.json"
        s = SettingsStore(path)
        r.check("empty to start", len(s), 0)
        r.check("missing key default", s.get("g", "nope", "fallback"), "fallback")

        s.set("g", "a", 1)
        r.ok("file created on write", path.exists())
        r.check("reads back", s.get("g", "a"), 1)
        r.check("reload from disk", SettingsStore(path).get("g", "a"), 1)

        s.update("g", b=2, c=3)
        r.check("update merges", s.guild("g"), {"a": 1, "b": 2, "c": 3})
        s.unset("g", "b")
        r.ok("unset removes", "b" not in s.guild("g"))
        s.unset("g", "absent")  # must not raise
        r.check("guilds listed", list(s.guilds()), ["g"])
        s.clear_guild("g")
        r.check("guild cleared", s.guild("g"), {})

        # atomicity: no stray temp files left behind
        s.set("g2", "x", 1)
        leftovers = [f for f in os.listdir(tmp) if f.startswith(".")]
        r.check("no temp files left", leftovers, [])

        corrupt = Path(tmp) / "bad.json"
        corrupt.write_text("{not json")
        r.check("corrupt file starts empty", len(SettingsStore(corrupt)), 0)


def suite_chat_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chat.json"
        cs = ChatSettings(path)

        r.check("default mode", cs.mode("g1"), "mention")
        r.ok("no guild -> not an AI channel", not cs.is_ai_channel(None, "c1"))
        r.check("no guild -> default mode", cs.mode(None), "mention")

        r.ok("toggle on returns True", cs.toggle_ai_channel("g1", "c1"))
        r.ok("now an AI channel", cs.is_ai_channel("g1", "c1"))
        r.ok("toggle off returns False", not cs.toggle_ai_channel("g1", "c1"))
        r.ok("no longer an AI channel", not cs.is_ai_channel("g1", "c1"))

        r.ok("add returns True", cs.add_ai_channel("g1", "c2"))
        r.ok("re-add returns False", not cs.add_ai_channel("g1", "c2"))
        r.ok("remove returns True", cs.remove_ai_channel("g1", "c2"))
        r.ok("remove absent returns False", not cs.remove_ai_channel("g1", "c2"))

        cs.set_mode("g1", "global")
        r.check("mode persisted", ChatSettings(path).mode("g1"), "global")
        r.raises("bad mode rejected", ValueError, cs.set_mode, "g1", "banana")
        r.check("bad stored mode falls back", ChatSettings(path).mode("nope"),
                "mention")

        r.ok("silence on", cs.toggle_silence("g1", "c5"))
        r.ok("is silenced", cs.is_silenced("g1", "c5"))
        r.ok("silence off", not cs.toggle_silence("g1", "c5"))
        r.ok("guild isolation", not cs.is_ai_channel("g2", "c1"))


def suite_config_commands(r: Results) -> None:
    import corbin.commands.config_cmds  # noqa: F401
    from corbin.core.registry import registry

    with tempfile.TemporaryDirectory() as tmp:
        cs = ChatSettings(Path(tmp) / "chat.json")
        ctx = _ctx(cs)

        spec = registry.get("reply_channel")
        r.ok("snake_case resolves", spec is not None)
        r.check("kebab-case resolves too", registry.get("reply-channel").name,
                spec.name)
        r.check("needs MANAGE_GUILD", spec.permission, Perm.MANAGE_GUILD)
        r.check("is a WRITE, not destructive", spec.risk.value, "write")

        out = asyncio.run(spec.handler(ctx))
        r.ok("turning on is confirmed in words", "answer everything" in out)
        r.ok("setting actually persisted", cs.is_ai_channel("g1", "c1"))
        out = asyncio.run(spec.handler(ctx))
        r.ok("toggling off is confirmed", "mention-only" in out)
        r.ok("setting removed", not cs.is_ai_channel("g1", "c1"))

        respond = registry.get("chat_mode")
        r.ok("global mode acknowledged",
             "Global" in asyncio.run(respond.handler(ctx, mode="global")))
        r.check("mode stored", cs.mode("g1"), "global")

        # Global with no general chat asks for one, and remembers asking.
        from corbin.commands.config_cmds import match_general_answer
        cs.set_mode("g1", "mention")
        asked = {}
        ctx_q = _ctx(cs)
        ctx_q.extras["general_questions"] = asked
        out = asyncio.run(respond.handler(ctx_q, mode="global"))
        r.ok("global asks which channel is general", "general chat?" in out)
        r.ok("...and waits for this person here", ("c1", "u1") in asked)

        ctx_n = _ctx(cs)
        ctx_n.extras["channel_index"] = {"lounge": "c9"}
        out = asyncio.run(respond.handler(ctx_n, mode="global",
                                          channel="#lounge"))
        r.check("a named channel is stored", cs.general_channel("g1"), "c9")
        r.ok("...and not asked about", "<#c9>" in out and "?" not in out)
        asked.clear()
        ctx_q2 = _ctx(cs)
        ctx_q2.extras["general_questions"] = asked
        asyncio.run(respond.handler(ctx_q2, mode="global"))
        r.ok("already set: no question", not asked)

        gen = registry.get("general_channel")
        ctx_g = _ctx(cs)
        ctx_g.extras["channel_index"] = {"hangout": "c7"}
        asyncio.run(gen.handler(ctx_g, channel="hangout"))
        r.check("general channel command sets it", cs.general_channel("g1"),
                "c7")

        idx = {"lounge": "c9", "general": "c2", "art": "c3"}
        r.check("a linked channel answers", match_general_answer(
            "#lounge", {"c9": "lounge"}, idx, "c1"), "c9")
        r.check("a bare name answers", match_general_answer(
            "lounge", {}, idx, "c1"), "c9")
        r.check("'it's the lounge' answers", match_general_answer(
            "it's the lounge", {}, idx, "c1"), "c9")
        r.check("'#general' answers", match_general_answer(
            "#general", {}, idx, "c1"), "c2")
        r.check("'this one' means here", match_general_answer(
            "this one", {}, idx, "c1"), "c1")
        r.check("an unrelated message is not an answer", match_general_answer(
            "lol did you see the art thread", {}, idx, "c1"), None)
        r.check("two channels is not an answer", match_general_answer(
            "lounge or art", {}, idx, "c1"), None)
        r.ok("off mode acknowledged",
             "quiet" in asyncio.run(respond.handler(ctx, mode="off")))

        asyncio.run(registry.get("reply_channel").handler(ctx))
        status = asyncio.run(registry.get("chat_status").handler(ctx))
        names = {f.name for f in status.fields}
        r.ok("status reports mode", "Mode" in names)
        r.ok("status reports this channel", "This channel" in names)
        r.ok("status lists always-on channels", "Always-on channels" in names)
        values = {f.name: f.value for f in status.fields}
        r.check("this channel reads always-on", values["This channel"],
                "always-on")

        silence = registry.get("silence")
        asyncio.run(silence.handler(ctx))
        status2 = asyncio.run(registry.get("chat_status").handler(ctx))
        r.check("silenced wins over always-on",
                {f.name: f.value for f in status2.fields}["This channel"],
                "silenced")


SUITES = [
    ("settings store", suite_store),
    ("chat settings", suite_chat_settings),
    ("config commands", suite_config_commands),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
