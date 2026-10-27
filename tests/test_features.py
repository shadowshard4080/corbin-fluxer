"""Offline suite for the owner's feature allowlist (VOICE_ROADMAP.md,
Phase G) and the voice-input gates in the risk engine.

Covers what the roadmap asks for: default deny, voice and streaming
granted independently, revoking, a DM refused, a non-owner refused, an
unknown server refused, and a source check that every voice command
declares its feature. Plus the spoken-input rules: nothing staff,
owner-only or destructive runs from voice, not even on a spoken "yes".
"""
from __future__ import annotations
import ast, asyncio, logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, member, run

from corbin.core.context import Surface
from corbin.core.features import FeatureAllowlist, parse_features
from corbin.core.permissions import Perm
from corbin.core.registry import CommandSpec, Risk
from corbin.core.risk import Decision, assess
from corbin.core.settings import ChatSettings

ROOT = Path(__file__).resolve().parent.parent
DEN, OTHER = "111111111111111111", "222222222222222222"


async def _noop(ctx, **_kw):
    return "ok"


def spec(name="cmd", **kw) -> CommandSpec:
    return CommandSpec(name=name, description="d", handler=_noop, **kw)


def _fresh() -> tuple[FeatureAllowlist, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    return FeatureAllowlist(ChatSettings(Path(tmp.name) / "s.json")), tmp


def test_allowlist(r: Results) -> None:
    allow, tmp = _fresh()
    with tmp:
        r.ok("default deny", not allow.allowed(DEN, "voice"))
        r.check("nothing on anywhere", allow.enabled(DEN), frozenset())
        r.ok("DMs never", not allow.allowed(None, "voice"))
        r.ok("unknown feature never", not allow.allowed(DEN, "telepathy"))

        allow.set(DEN, ["voice"], True)
        r.ok("voice on", allow.allowed(DEN, "voice"))
        r.ok("streaming is separate", not allow.allowed(DEN, "streaming"))
        r.ok("only that server", not allow.allowed(OTHER, "voice"))

        allow.set(DEN, ["streaming"], True)
        r.check("both", allow.enabled(DEN),
                frozenset({"voice", "streaming"}))
        allow.set(DEN, ["voice"], False)
        r.check("revoking one keeps the other", allow.enabled(DEN),
                frozenset({"streaming"}))

        again = FeatureAllowlist(ChatSettings(Path(tmp.name) / "s.json"))
        r.check("survives a restart", again.enabled(DEN),
                frozenset({"streaming"}))
        r.check("listing", again.entries(), {DEN: frozenset({"streaming"})})
        r.raises("refuses unknown features", ValueError,
                 allow.set, DEN, ["telepathy"], True)

    class Broken:
        class store:
            @staticmethod
            def get(*_a, **_k):
                raise OSError("unreadable")

            @staticmethod
            def guild(*_a, **_k):
                raise OSError("unreadable")

    shaky = FeatureAllowlist(Broken())
    r.ok("unreadable settings allow nothing", not shaky.allowed(DEN, "voice"))
    r.check("...and list nothing", shaky.entries(), {})


def test_seed(r: Results) -> None:
    allow, tmp = _fresh()
    with tmp:
        r.check("first boot seeds from .env",
                allow.seed([DEN, OTHER, " "], "voice"), 2)
        r.ok("seeded servers are on", allow.allowed(OTHER, "voice"))
        allow.set(OTHER, ["voice"], False)
        r.check("never again: the owner's removal sticks",
                allow.seed([DEN, OTHER], "voice"), 0)
        r.ok("removed stays removed", not allow.allowed(OTHER, "voice"))


def test_parse(r: Results) -> None:
    r.check("voice", parse_features("voice"), ("voice",))
    r.check("stream", parse_features("live streaming"), ("streaming",))
    r.check("both", parse_features("both"), ("voice", "streaming"))
    r.check("voice and streaming",
            parse_features("voice and streaming"), ("voice", "streaming"))
    r.check("nonsense", parse_features("dancing"), ())


#: What an ordinary member holds.
EVERYONE = (Perm.VIEW_CHANNEL | Perm.SEND_MESSAGES | Perm.CONNECT | Perm.SPEAK
            | Perm.READ_MESSAGE_HISTORY)


def test_risk_gates(r: Results) -> None:
    alice = member("alice", perms=EVERYONE)
    mod = member("mod", perms=Perm.BAN_MEMBERS | Perm.KICK_MEMBERS)
    voice_cmd = spec("voice-chat", feature="voice", permission=Perm.CONNECT,
                     risk=Risk.WRITE)

    v = assess(voice_cmd, surface=Surface.AI, invoker=alice, args={})
    r.check("feature off: refused", v.decision, Decision.REFUSED)
    r.ok("...saying so, without naming who can turn it on",
         "isn't turned on" in v.reason and "owner" not in v.reason)
    v = assess(voice_cmd, surface=Surface.AI, invoker=member("o"), args={},
               is_bot_owner=True)
    r.check("the owner isn't exempt either", v.decision, Decision.REFUSED)
    v = assess(voice_cmd, surface=Surface.AI, invoker=alice, args={},
               features=frozenset({"voice"}))
    r.check("feature on: runs", v.decision, Decision.EXECUTE)
    v = assess(voice_cmd, surface=Surface.AI, invoker=alice, args={},
               features=frozenset({"streaming"}))
    r.check("the other feature doesn't count", v.decision, Decision.REFUSED)

    ban = spec("ban", permission=Perm.BAN_MEMBERS, risk=Risk.DESTRUCTIVE)
    v = assess(ban, surface=Surface.AI, invoker=mod, args={}, spoken=True)
    r.check("ban by voice: refused", v.decision, Decision.REFUSED)
    r.ok("...pointing at chat", "chat" in v.reason)
    v = assess(ban, surface=Surface.PREFIX, invoker=mod, args={},
               spoken=True, is_bot_owner=True)
    r.check("not even the owner's spoken yes", v.decision, Decision.REFUSED)

    role = spec("assignrole", permission=Perm.MANAGE_ROLES, risk=Risk.WRITE)
    v = assess(role, surface=Surface.AI, invoker=mod, args={}, spoken=True,
               is_bot_owner=True)
    r.check("staff tools by voice: refused", v.decision, Decision.REFUSED)
    owner_cmd = spec("servers", owner_only=True)
    v = assess(owner_cmd, surface=Surface.AI, invoker=mod, args={},
               spoken=True, is_bot_owner=True)
    r.check("owner-only by voice: refused", v.decision, Decision.REFUSED)
    play = spec("play", permission=Perm.CONNECT, risk=Risk.WRITE)
    v = assess(play, surface=Surface.AI, invoker=alice, args={}, spoken=True)
    r.check("music by voice: fine", v.decision, Decision.EXECUTE)
    v = assess(ban, surface=Surface.AI, invoker=mod, args={"user": "x",
                                                           "reason": "spam"})
    r.ok("typed ban is untouched by the voice gate",
         v.decision is not Decision.REFUSED or "voice" not in v.reason)

    sure = spec("feature-allowlist", owner_only=True, always_confirm=True)
    v = assess(sure, surface=Surface.AI, invoker=mod, args={},
               is_bot_owner=True, confidence=1.0)
    r.check("always_confirm asks even at full confidence", v.decision,
            Decision.CONFIRM)
    v = assess(sure, surface=Surface.PREFIX, invoker=mod, args={},
               is_bot_owner=True)
    r.check("...and the typed yes then runs it", v.decision,
            Decision.EXECUTE)


def test_dm_gate(r: Results) -> None:
    """guild_only is enforced, not just hidden from the help: music and
    radio find a voice channel from voice state in any server, so a DM
    "put the radio on" must never reach them."""
    alice = member("alice", perms=EVERYONE)
    radio = spec("radio", risk=Risk.SAFE)            # guild_only default
    v = assess(radio, surface=Surface.AI, invoker=alice, args={},
               in_guild=False)
    r.check("server-only command in a DM: refused", v.decision,
            Decision.REFUSED)
    r.ok("...saying why", "server" in v.reason and "DM" in v.reason)
    v = assess(radio, surface=Surface.AI, invoker=member("o"), args={},
               is_bot_owner=True, in_guild=False)
    r.check("the owner isn't exempt", v.decision, Decision.REFUSED)
    voice_cmd = spec("voice-chat", feature="voice", risk=Risk.WRITE)
    v = assess(voice_cmd, surface=Surface.AI, invoker=alice, args={},
               in_guild=False, features=frozenset({"voice"}))
    r.check("voice chat in a DM: refused even if features leaked in",
            v.decision, Decision.REFUSED)
    dm_ok = spec("remind", risk=Risk.SAFE, guild_only=False)
    v = assess(dm_ok, surface=Surface.AI, invoker=alice, args={},
               in_guild=False)
    r.check("a DM-ok command still runs in a DM", v.decision,
            Decision.EXECUTE)
    r.check("and server commands run in a server",
            assess(radio, surface=Surface.AI, invoker=alice,
                   args={}).decision, Decision.EXECUTE)

    src = (ROOT / "corbin" / "core" / "dispatch.py").read_text("utf-8")
    r.check("every dispatcher assess() passes in_guild",
            src.count("in_guild=bool(guild_id)"), src.count(" assess("))

    from corbin.core.registry import registry
    import corbin.commands.music_cmds, corbin.commands.radio_cmds  # noqa
    import corbin.commands.voicechat_cmds, corbin.commands.stream_cmds  # noqa
    joiners = ("play", "radio", "voice-chat", "stream-video", "tv-watch",
               "video-play")
    specs = {s.name: s for s in registry.ai_invocable()}
    r.ok("nothing that joins voice is DM-ok",
         all(specs[n].guild_only for n in joiners if n in specs))

    # Owner-only commands are bot-level; nobody else sees them.
    import corbin.commands.util_cmds  # noqa: F401
    from corbin.ai.command_help import CommandHelp
    help_ = CommandHelp(registry, bot_owner_id="owner")
    for where in (None, "g1"):
        shown = {s.name for s in help_._offered(where, owner=False)}
        r.ok(f"owner-only commands hidden from members ({where or 'DM'})",
             not {"servers", "leave"} & shown)
    r.ok("...and offered to the owner",
         {"servers", "leave"} <= {s.name for s in
                                  help_._offered(None, owner=True)})
    r.ok("echo is a server staff command, not an owner pass",
         not specs["echo"].owner_only and specs["echo"].guild_only)
    r.ok("verify is gone: nothing reads it without donor limits",
         "verify" not in specs)


def test_spoken_yes(r: Results) -> None:
    """A confirmation typed in chat can't be completed by a spoken yes
    when the command is one voice may not run."""
    from corbin.core.dispatch import Dispatcher
    from corbin.core.registry import Registry

    reg = Registry()
    reg.register(spec("ban", permission=Perm.BAN_MEMBERS,
                      risk=Risk.DESTRUCTIVE))
    reg.register(spec("play", permission=Perm.CONNECT, risk=Risk.WRITE))
    d = Dispatcher(reg, classifier=None, bot_owner_id="owner")
    mod = member("mod", perms=Perm.BAN_MEMBERS | EVERYONE)

    def ask(cmd: str, spoken: bool):
        d.store.put(guild_id="g1", channel_id="c1", user_id="mod",
                    command=cmd, args={}, summary=cmd)
        return asyncio.run(d.handle("yes.", invoker=mod, channel_id="c1",
                                    guild_id="g1", directly_invoked=True,
                                    spoken=spoken))

    res = ask("ban", spoken=True)
    r.check("spoken yes to a ban: refused", res.decision, Decision.REFUSED)
    res = ask("ban", spoken=False)
    r.check("typed yes to a ban: runs", res.decision, Decision.EXECUTE)
    res = ask("play", spoken=True)
    r.check("spoken yes to music: runs", res.decision, Decision.EXECUTE)

    d2 = Dispatcher(reg, classifier=None,
                    features=lambda g: (_ for _ in ()).throw(OSError()))
    r.check("an allowlist that throws allows nothing",
            d2._features("g1"), frozenset())


def _decorators(path: Path) -> dict[str, dict[str, ast.AST]]:
    """command name -> its @command keyword arguments."""
    out = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") \
                    == "command":
                out[node.name] = {k.arg: k.value for k in dec.keywords}
    return out


def test_source(r: Results) -> None:
    cmds = _decorators(ROOT / "corbin/commands/voicechat_cmds.py")
    # Stopping and opting out always work: leaving and privacy must
    # never depend on a setting.
    ungated = {"stop_voice_chat", "voice_optout"}
    for name, kw in cmds.items():
        if name in ungated:
            r.ok(f"{name} works everywhere", "feature" not in kw)
        else:
            r.ok(f"{name} declares feature=\"voice\"",
                 isinstance(kw.get("feature"), ast.Constant)
                 and kw["feature"].value == "voice")
    r.ok("the start command is gated", "voice_chat" in cmds)

    allow = _decorators(ROOT / "corbin/commands/allowlist_cmds.py")
    for name, kw in allow.items():
        r.ok(f"{name} is owner-only",
             isinstance(kw.get("owner_only"), ast.Constant)
             and kw["owner_only"].value is True)
        r.ok(f"{name} works in DMs",
             isinstance(kw.get("guild_only"), ast.Constant)
             and kw["guild_only"].value is False)
    r.ok("changing the allowlist always confirms",
         isinstance(allow["feature_allowlist"].get("always_confirm"),
                    ast.Constant))


def test_help(r: Results) -> None:
    import importlib
    from corbin.ai.command_help import CommandHelp
    from corbin.core.registry import registry

    importlib.import_module("corbin.commands.voicechat_cmds")
    on = {DEN}
    h = CommandHelp(registry, bot_owner_id="owner",
                    features=lambda g: frozenset({"voice"}) if g in on
                    else frozenset())
    who = member("alice", perms=Perm.CONNECT | Perm.SEND_MESSAGES)
    here = h.lookup("", who, in_guild=True, guild_id=DEN)
    there = h.lookup("", who, in_guild=True, guild_id=OTHER)
    r.ok("offered where voice is on", "- voice-chat:" in here)
    r.ok("hidden where it's off", "- voice-chat:" not in there)
    r.ok("opting out is offered everywhere", "- voice-optout:" in there)


def test_platform(r: Results) -> None:
    """Every prompt that carries his persona also says he's on Fluxer.
    Presence and musings once sent the persona with no Current Context
    block, and he sometimes talked as if he were on Discord."""
    from types import SimpleNamespace

    from corbin.ai.conversation import (COMPACT_RULES, Conversation,
                                        PLATFORM_BLOCK, PLATFORM_LINE)

    c = Conversation.__new__(Conversation)
    c.soul = "He runs on both Discord and Fluxer."
    c.soul_additions = ""
    c.compact_rules = COMPACT_RULES
    c.llm = SimpleNamespace(persona_baked_in=False)
    got = c.standalone_system()
    r.ok("persona kept", c.soul in got)
    r.ok("says Fluxer, not Discord", PLATFORM_LINE in got)
    c.llm = SimpleNamespace(persona_baked_in=True)
    got = c.standalone_system()
    r.ok("a baked persona gets the compact rules, not the persona file",
         c.soul not in got and PLATFORM_LINE in got)
    r.ok("the block names Fluxer and negates Discord",
         "Fluxer" in PLATFORM_BLOCK and "NOT Discord" in PLATFORM_BLOCK)

    bot = (ROOT / "bot.py").read_text()
    r.ok("greetings get the platform block",
         "greeter.soul = conversation.standalone_system()" in bot)
    for path in ("corbin/ai/presence.py", "corbin/ai/musings.py"):
        src = (ROOT / path).read_text()
        r.ok(f"{path} uses standalone_system", "standalone_system" in src)


def test_nickname(r: Results) -> None:
    """He called the owner "Nightjar" (account) not "Quill" (server)."""
    from types import SimpleNamespace as NS
    from corbin.platforms.fluxer.adapter import to_member

    account = NS(id="1", username="nightjar", global_name="Nightjar",
                 bot=False)
    m = to_member(NS(user=account, nick="Quill", roles=[]), guild_id="g")
    r.check("the server nickname wins", m.display_name, "Quill")
    m = to_member(NS(user=account, nick=None, roles=[]), guild_id="g")
    r.check("no nickname: the account's display name", m.display_name,
            "Nightjar")
    m = to_member(account, guild_id="g")
    r.check("a bare account (no member data) has no nickname",
            m.display_name, "Nightjar")


def test_gaming_mode(r: Results) -> None:
    """The owner's GPU runs the local model; gaming mode keeps him off it."""
    from corbin.commands.util_cmds import (GAMING_BUCKET, GAMING_KEY,
                                           gaming_mode)
    from corbin.core.registry import registry
    from test_tool_calls import FakeLocal, bridge

    unloaded = []

    class Local(FakeLocal):
        async def unload(self):
            unloaded.append(True)
            return True

    allow, tmp = _fresh()
    with tmp:
        llm = bridge(Local(None))
        ctx = type("C", (), {"extras": {"llm": llm,
                                        "settings": allow.settings}})()
        out = asyncio.run(gaming_mode(ctx, True))
        r.ok("on: local set aside", llm.local_held)
        r.check("on: the GPU is freed now", unloaded, [True])
        r.ok("on: saved for restarts",
             allow.settings.store.get(GAMING_BUCKET, GAMING_KEY) is True)
        r.ok("on: says so, without naming vendors",
             "Gaming mode on" in out and "Ollama" not in out)

        # Voice chat comes and goes while gaming: local stays aside.
        llm.hold_local("voice:g1")
        llm.release_local("voice:g1")
        r.ok("a voice chat ending doesn't bring local back", llm.local_held)

        llm.hold_local("voice:g1")
        out = asyncio.run(gaming_mode(ctx, False))
        r.ok("off during voice chat: still aside until it ends",
             llm.local_held and "voice chat ends" in out)
        llm.release_local("voice:g1")
        r.ok("...and back once it does", not llm.local_held)
        r.ok("off: saved", allow.settings.store.get(GAMING_BUCKET,
                                                    GAMING_KEY) is False)

    spec = registry.get("gaming-mode")
    r.ok("owner-only", spec is not None and spec.owner_only)
    v = assess(spec, surface=Surface.AI, invoker=member("mod"), args={},
               is_bot_owner=False)
    r.check("refused for everyone else", v.decision, Decision.DENIED)
    v = assess(spec, surface=Surface.AI, invoker=member("o"), args={},
               is_bot_owner=True, spoken=True)
    r.check("and typed only, even for the owner", v.decision,
            Decision.REFUSED)


SUITES = [
    ("server nicknames", test_nickname),
    ("platform in every persona prompt", test_platform),
    ("feature allowlist", test_allowlist),
    ("feature allowlist seed", test_seed),
    ("feature names", test_parse),
    ("feature + voice risk gates", test_risk_gates),
    ("server-only commands in DMs", test_dm_gate),
    ("spoken confirmations", test_spoken_yes),
    ("voice command declarations", test_source),
    ("voice commands in help", test_help),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
