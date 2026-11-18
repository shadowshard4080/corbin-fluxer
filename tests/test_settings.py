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


def suite_silence(r: Results) -> None:
    from corbin.commands.config_cmds import (QUIET_ESCAPE, match_silence_answer,
                                             works_while_quiet)
    from corbin.core.registry import registry
    with tempfile.TemporaryDirectory() as tmp:
        cs = ChatSettings(Path(tmp) / "chat.json")
        silence = registry.get("silence")
        unsilence = registry.get("unsilence")
        r.ok("unsilence exists", unsilence is not None)
        # The classifier sees only examples[:3]; "turn off presence in #x"
        # was refused as an unknown command "presence" when it wasn't there.
        r.ok("silence's visible examples mention presence",
             any("presence" in e for e in silence.examples[:3]))
        r.ok("...and so do unsilence's",
             any("presence" in e for e in unsilence.examples[:3]))
        # Checked against the REAL spec names: the allowlist once held
        # snake_case while specs are kebab-case, and dropped everything.
        r.ok("unsilence works while quiet", works_while_quiet(unsilence.name))
        r.ok("...and so does the mode switch",
             works_while_quiet(registry.get("chat_mode").name))
        r.ok("...and silence (answers 'already silenced')",
             works_while_quiet(silence.name))
        r.ok("other commands don't",
             not works_while_quiet(registry.get("reply_channel").name))

        # A named channel is silenced, not the one asked from.
        ctx = _ctx(cs)
        ctx.extras["channel_index"] = {"art": "c3"}
        out = asyncio.run(silence.handler(ctx, channel="#art"))
        r.ok("named channel silenced", cs.is_silenced("g1", "c3"))
        r.ok("current channel untouched", not cs.is_silenced("g1", "c1"))
        r.ok("reply names it", "<#c3>" in out)
        ctx_x = _ctx(cs)
        ctx_x.extras["channel_index"] = {}
        out = asyncio.run(silence.handler(ctx_x, channel="#nowhere"))
        r.ok("unknown channel refuses, doesn't fall back",
             "couldn't find" in out and not cs.is_silenced("g1", "c1"))
        out = asyncio.run(silence.handler(ctx, channel="art"))
        r.ok("silencing twice is not a toggle",
             cs.is_silenced("g1", "c3") and "already" in out)

        # Unsilence: from here, by name, and from inside the channel.
        out = asyncio.run(unsilence.handler(ctx, channel="#art"))
        r.ok("unsilence by name", not cs.is_silenced("g1", "c3"))
        cs.set_silenced("g1", "c1", True)
        asyncio.run(unsilence.handler(_ctx(cs)))
        r.ok("unsilence in the silenced channel itself",
             not cs.is_silenced("g1", "c1"))
        out = asyncio.run(unsilence.handler(_ctx(cs)))
        r.ok("unsilence when not silenced says so", "wasn't" in out)
        cs.set_silenced("g1", "c4", True)
        cs.set_presence_off("g1", "c5", True)
        out = asyncio.run(unsilence.handler(_ctx(cs), channel="everywhere"))
        r.ok("unsilence everywhere clears both kinds",
             not cs.silenced("g1") and not cs.presence_off("g1"))
        cs.set_mode("g1", "off")
        out = asyncio.run(unsilence.handler(_ctx(cs)))
        r.ok("mode off is pointed out", "server-wide" in out)

        # Global mode asks presence-or-silence, and remembers asking.
        cs.set_mode("g1", "global")
        asked = {}
        ctx_q = _ctx(cs)
        ctx_q.extras["silence_questions"] = asked
        out = asyncio.run(silence.handler(ctx_q))
        r.ok("global mode asks", "Presence or silence?" in out)
        r.ok("...nothing changed yet", not cs.is_silenced("g1", "c1"))
        r.check("...question waits for this person here",
                asked.get(("c1", "u1"), (None, None))[:2], ("g1", "c1"))
        out = asyncio.run(silence.handler(ctx_q, scope="presence"))
        r.ok("explicit presence scope", cs.is_presence_off("g1", "c1")
             and not cs.is_silenced("g1", "c1"))
        asked.clear()
        out = asyncio.run(silence.handler(ctx_q))
        r.ok("presence already off: silences without asking",
             cs.is_silenced("g1", "c1") and not asked)
        cs.set_mode("g1", "mention")
        cs.set_silenced("g1", "c1", False)
        cs.set_presence_off("g1", "c1", False)
        asyncio.run(silence.handler(ctx_q))
        r.ok("mention mode: no question, just silence",
             cs.is_silenced("g1", "c1") and not asked)

        r.check("'presence' answers", match_silence_answer("presence"), "presence")
        r.check("'just stop chiming in'", match_silence_answer(
            "just stop chiming in"), "presence")
        r.check("'completely'", match_silence_answer("completely"), "all")
        r.check("'silence'", match_silence_answer("silence please"), "all")
        r.check("'go fully silent'", match_silence_answer("go fully silent"), "all")
        r.check("unrelated is not an answer",
                match_silence_answer("lol nice art"), None)
        r.check("ambiguous is not an answer",
                match_silence_answer("stop chiming in at all"), None)

        r.ok("escape filter passes unsilence", bool(QUIET_ESCAPE.search(
            "@Corbin unsilence")))
        r.ok("escape filter passes 'you can talk again'",
             bool(QUIET_ESCAPE.search("you can talk here again")))
        r.ok("escape filter drops chatter",
             not QUIET_ESCAPE.search("anyone seen my keys"))

        # Unsilence is matched without the classifier, typos included.
        from corbin.commands.config_cmds import match_unsilence
        for said in ("@Corbin unsilence", "corbin unsilnce", "Corbin un-silence",
                     "corbin unslience pls", "Corbin unsilense",
                     "corbin you can talk here again", "Corbin unmute",
                     "corbin stop being silent", "corbin lift the silence"):
            r.check(f"unsilence matched: {said!r}", match_unsilence(said), {})
        for said in ("corbin that was unsanctioned", "corbin be quiet",
                     "corbin silence", "corbin I'm unsure"):
            r.check(f"not unsilence: {said!r}", match_unsilence(said), None)
        r.check("unsilence everywhere", match_unsilence(
            "corbin unsilence everywhere"), {"channel": "all"})
        r.check("unsilence a linked channel", match_unsilence(
            "corbin unsilence #art", ["c3"]), {"channel": "c3"})

        # ...and run with the hard gates still on.
        from corbin.core.dispatch import Dispatcher
        from corbin.core.risk import Decision
        d = Dispatcher(registry, classifier=None)
        mod = member("u1", perms=Perm.MANAGE_GUILD)
        res = d.direct("unsilence", {}, invoker=mod, guild_id="g1")
        r.ok("a mod's direct unsilence executes",
             res.decision is Decision.EXECUTE and works_while_quiet(res.spec.name))
        pleb = member("u2")
        res = d.direct("unsilence", {}, invoker=pleb, guild_id="g1")
        r.ok("a non-mod's is denied", res.decision is Decision.DENIED
             and "permission" in res.message)


def suite_status_card(r: Results) -> None:
    """corbin-status shows every server setting, set or not (owner,
    2026-11-17), and a fully configured server still fits the embed:
    the adapter drops fields past EMBED_MAX_FIELDS with no warning."""
    import time as _t
    import corbin.commands  # noqa: F401  (registers the commands)
    from corbin.core.context import EMBED_FIELD_VALUE_LIMIT, EMBED_MAX_FIELDS
    from corbin.core.registry import registry
    status = registry.get("chat_status")
    expected = {"Mode", "General chat", "This channel", "Verified", "Voice & streaming", "Always-on channels",
                "Silenced", "No chiming in", "Ranking", "Welcome & farewell",
                "Audit log", "Spam honeypot", "Verse finder", "Daily quote",
                "Hourly animals", "Full moon", "Weather alerts",
                "Banner rotation", "Events calendar",
                "Reaction roles", "Live TV & videos"}
    with tempfile.TemporaryDirectory() as tmp:
        cs = ChatSettings(Path(tmp) / "chat.json")
        card = asyncio.run(status.handler(_ctx(cs)))
        values = {f.name: f.value for f in card.fields}
        r.check("a blank server shows every setting",
                sorted(expected - set(values)), [])
        r.check("ranking reads off", values["Ranking"].split("\n")[0], "off")
        r.check("quote reads off", values["Daily quote"], "off")
        r.check("banner reads not set up", values["Banner rotation"],
                "not set up")
        r.check("events read not set up", values["Events calendar"],
                "not set up")
        r.check("not verified", values["Verified"], "no")

        # Everything on, at its longest.
        g = "g1"
        cs.set_ranking(g, True); cs.set_ranking_channel(g, "c5")
        cs.set_welcome(g, enabled=True, channel_id="c6", card=True,
                       background=True, message="hi")
        cs.set_farewell(g, enabled=True, channel_id="c6")
        cs.set_greeting(g, enabled=True, custom_info="read #rules")
        cs.set_audit(g, enabled=True, channel_id="c7")
        cs.set_daily_quote(g, enabled=True, channel_id="c8", hour=9)
        cs.set_hourly_animals(g, enabled=True, channel_id="c9",
                              animal="fox")
        cs.set_versefinder(g, True); cs.set_honeypot(g, "c10")
        cs.set_fullmoon(g, "c11")
        cs.set_weather_alerts(g, [
            {"location": f"Somewhere Long Named {i}", "hour": 7,
             "minute": 0, "tz": "America/Los_Angeles",
             "channel_id": "123456789012345678"} for i in range(10)])
        cs.set_general_channel(g, "c2")
        for i in range(40):
            cs.add_ai_channel(g, f"1234567890123456{i:02d}")
            cs.set_silenced(g, f"2234567890123456{i:02d}", True)
            cs.set_presence_off(g, f"3234567890123456{i:02d}", True)
        cs.store.set(g, "banner_rotation", {"enabled": True,
                     "channel_id": "c12", "interval": 43200,
                     "last_error": "x" * 900})
        cs.store.set(g, "live_tv", {"block": ["sports", "news", "kids"],
                                    "max_minutes": 120,
                                    "favourites": ["a", "b"]})
        cs.store.set(g, "events", {"channel_id": "c14", "tz": "UTC",
                                   "subscribers": {"u": {}}})
        cs.set_verified(g, True)
        card = asyncio.run(status.handler(_ctx(cs)))
        values = {f.name: f.value for f in card.fields}
        r.ok("fits the field cap", len(card.fields) <= EMBED_MAX_FIELDS)
        r.ok("no field over the value limit",
             all(len(f.value) <= EMBED_FIELD_VALUE_LIMIT
                 for f in card.fields))
        total = sum(len(f.name) + len(f.value) for f in card.fields) + \
            len(card.title or "") + len(card.description or "")
        r.ok(f"whole card under 6,000 chars ({total})", total <= 6000)
        r.ok("ranking shows its channel", "<#c5>" in values["Ranking"])
        r.ok("quote shows channel and time",
             "<#c8>" in values["Daily quote"]
             and "09:00" in values["Daily quote"])
        r.ok("banner shows its interval",
             "every 12 hours" in values["Banner rotation"])
        r.ok("events show their channel and subscribers",
             "<#c14>" in values["Events calendar"]
             and "1 subscribed" in values["Events calendar"])
        r.ok("tv shows its rules", "no sports" in values["Live TV & videos"]
             and "120 min" in values["Live TV & videos"])
        r.ok("long lists say how many more",
             "…and 30 more" in values["Silenced"])
        r.check("verified", values["Verified"], "yes")


SUITES = [
    ("settings store", suite_store),
    ("chat settings", suite_chat_settings),
    ("config commands", suite_config_commands),
    ("silence", suite_silence),
    ("status card", suite_status_card),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
