"""Offline suite for the on-demand command reference.

The catalogue is too big to live in every prompt, so Corbin looks it up
when asked. These cover the lookup itself, that it never offers a command
the risk gates would refuse, the free regex gate that works on the cloud
model too, and the plumbing into a reply.
"""
from __future__ import annotations
import asyncio, importlib, logging, pkgutil
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.command_help import (CATEGORIES, LOOKUP_COMMANDS_TOOL,
                                    MAX_REFERENCE_CHARS, CommandHelp,
                                    category_of, is_command_question)
from corbin.ai.conversation import REPLY_TOOLS, ToolRequest
from corbin.core.permissions import Perm
from corbin.core.registry import CommandSpec, Param, Registry


async def _noop(ctx):
    return None


def spec(name, description, **kw):
    return CommandSpec(name=name, description=description, handler=_noop,
                       **kw)


def tiny_registry() -> Registry:
    reg = Registry()
    reg.register(spec("remind", "Set a reminder",
                      params=(Param("when", "when"), Param("text", "what")),
                      examples=("remind me in 20 minutes to stretch",)))
    reg.register(spec("reminders", "List your pending reminders"))
    reg.register(spec("ban", "Ban a member from the server",
                      permission=Perm.BAN_MEMBERS,
                      examples=("ban @troll",)))
    reg.register(spec("shutdown", "Restart the bot", owner_only=True))
    reg.register(spec("hidden", "Prefix only", ai_invocable=False))
    reg.register(spec("ping", "Check the bot responds", guild_only=False))
    return reg


class Who:
    def __init__(self, uid="1", perms=Perm.NONE):
        self.id, self.permissions = uid, perms


def suite_lookup(r: Results) -> None:
    h = CommandHelp(tiny_registry(), bot_owner_id="999")
    names = [s.name for s in h.search("reminders")]
    r.ok("a topic finds its commands", {"remind", "reminders"} <= set(names))
    r.ok("...and not unrelated ones", "ban" not in names)

    text = h.lookup("set a reminder", Who(), in_guild=True)
    r.ok("details carry the params", "takes: when" in text)
    r.ok("...and how people phrase it", "remind me in 20 minutes" in text)

    ov = h.lookup("what commands do you have", Who(), in_guild=True)
    r.ok("a generic question gets the overview", "Everything" in ov)
    r.ok("...listing what they can run", "remind" in ov and "ping" in ov)
    r.ok("...but not staff commands by name", "ban" not in ov.split(":", 1)[1]
         .split("(Plus")[0])
    r.ok("...while saying staff ones exist", "Plus 1 moderator" in ov)
    r.ok("...with descriptions, so nothing is guessed from a name",
         "remind: Set a reminder" in ov)
    r.ok("...and the features that need no command",
         "no command needed" in ov)

    nothing = h.lookup("teleport", Who(), in_guild=True)
    r.ok("an unmatched topic says so and falls back to the overview",
         nothing.startswith("Nothing matched") and "remind" in nothing)


def suite_gates(r: Results) -> None:
    h = CommandHelp(tiny_registry(), bot_owner_id="999",
                    grants={"7": frozenset({"ban"})})
    everyone = h.lookup("ban", Who(), in_guild=True)
    r.ok("a staff command is still findable, marked as such",
         "ban" in everyone and "needs Ban Members" in everyone)
    mod = h.lookup("ban", Who(perms=Perm.BAN_MEMBERS), in_guild=True)
    r.ok("...and unmarked for someone who holds the permission",
         "can't run" not in mod)
    r.ok("the bot owner bypasses guild permissions, like assess()",
         "can't run" not in h.lookup("ban", Who("999"), in_guild=True))
    r.ok("a per-person grant counts, like assess()",
         "can't run" not in h.lookup("ban", Who("7"), in_guild=True))

    all_text = h.lookup("", Who("999"), in_guild=True)
    r.ok("the bot owner sees owner-only commands", "shutdown" in all_text
         and "BOT OWNER ONLY" in all_text)
    admin_text = h.lookup("", Who("5", perms=Perm(sum(int(p) for p in Perm))),
                          in_guild=True)
    r.ok("...and nobody else does, admins included",
         "shutdown" not in admin_text and "BOT OWNER" not in admin_text)
    r.ok("...not even on a direct lookup",
         "shutdown" not in h.lookup("restart the bot", Who(), in_guild=True))
    r.ok("non-AI commands are never advertised", "hidden" not in all_text)

    dm = h.lookup("reminders", Who(), in_guild=False)
    r.ok("server-only commands are flagged in DMs",
         "only works in a server" in dm)
    r.ok("...and DM-safe ones are not",
         "only works in a server" not in h.lookup("ping", Who(),
                                                   in_guild=False))


def suite_question_gate(r: Results) -> None:
    for q in ("what commands do you have?", "what can you do?",
              "Corbin, what are you able to do", "is there a command for "
              "reminders", "list your commands", "how do I use you"):
        r.ok(f"asks about commands: {q!r}", is_command_question(q))
    for q in ("what can I do about my car", "the commands are weird",
              "can you believe this", "what features does the new iphone "
              "have", "hey corbin"):
        r.ok(f"ordinary chat: {q!r}", not is_command_question(q))


def suite_real_registry(r: Results) -> None:
    import corbin.commands as pkg
    from corbin.core.registry import registry

    for m in pkgutil.iter_modules(pkg.__path__):
        if not m.name.startswith("_"):
            importlib.import_module(f"corbin.commands.{m.name}")
    stems = {getattr(s.handler, "__module__", "").rsplit(".", 1)[-1]
             for s in registry.all()}
    r.check("every command module has a readable category",
            sorted(stems - set(CATEGORIES)), [])

    h = CommandHelp(registry, bot_owner_id="999")
    admin = Who(perms=Perm(sum(int(p) for p in Perm)))
    ov = h.lookup("", admin, in_guild=True)
    r.ok("the full overview fits the reference cap",
         len(ov) <= MAX_REFERENCE_CHARS and not ov.endswith("…"))
    offered = [s for s in registry.ai_invocable() if not s.owner_only]
    missing = [s.name for s in offered if f"- {s.name}:" not in ov]
    r.check("...and names every command an admin can run", missing, [])
    mine = h.lookup("", Who("999", perms=admin.permissions), in_guild=True)
    r.ok("the owner's view, the largest, fits too",
         len(mine) <= MAX_REFERENCE_CHARS and not mine.endswith("…"))
    r.check("...and names every single command",
            [s.name for s in registry.all() if f"- {s.name}:" not in mine],
            [])
    den = h.lookup("", admin, in_guild=True, guild_id="den")
    r.ok("staff see their tools first",
         0 < den.index("MODERATOR AND ADMIN TOOLS")
         < den.index("FOR EVERYONE"))
    r.ok("music is for everyone, not a staff tool",
         den.index("- play:") > den.index("FOR EVERYONE"))
    r.ok("ban is a staff tool",
         den.index("- ban:") < den.index("FOR EVERYONE"))
    staff_part = den.split("FOR EVERYONE")[0]
    r.ok("staff tools come with arguments and an example",
         "takes: user = the member to ban" in staff_part
         and "people say:" in staff_part)
    r.ok("...and the notes a one-liner can't carry",
         "at or above yours" in staff_part)
    r.ok("the everyday half stays brief for staff",
         "people say:" not in den.split("FOR EVERYONE")[1])

    member = Who(perms=Perm.SEND_MESSAGES | Perm.CONNECT)
    mine = h.lookup("", member, in_guild=True, guild_id="den")
    r.ok("members get no staff section",
         "MODERATOR AND ADMIN TOOLS" not in mine)
    r.ok("...just a count of it", "moderator, admin or server-only" in mine)
    r.ok("...and none of the staff notes", "at or above yours" not in mine)

    mod = Who(perms=Perm.BAN_MEMBERS | Perm.MODERATE_MEMBERS)
    only = h.lookup("", mod, in_guild=True, guild_id="den").split(
        "FOR EVERYONE")[0]
    r.ok("a moderator sees the tools they hold",
         "- ban:" in only and "- warn:" in only)
    r.ok("...but not admin tools they lack", "- audit-logs:" not in only)
    r.ok("categories come from the handler module",
         category_of(registry.get("remind")) == "Reminders")


def suite_plumbing(r: Results) -> None:
    from test_tool_calls import ToolLLM, reply

    r.ok("the local model is offered the lookup tool",
         LOOKUP_COMMANDS_TOOL in REPLY_TOOLS)
    llm = ToolLLM(calls=[{"name": "lookup_commands",
                          "arguments": {"topic": "reminders"}}])
    out, _ = reply(llm, tools=REPLY_TOOLS)
    r.ok("a lookup request comes back as a ToolRequest",
         isinstance(out, ToolRequest) and out.name == "lookup_commands")

    llm = ToolLLM(text="Sure, just ask me.")
    out, convo = reply(llm, reference="Commands matching 'x':\n- remind")
    user_turn = llm.seen["messages"][-1]["content"]
    r.ok("the reference reaches the model", "[Tool:CommandReference]"
         in user_turn and "- remind" in user_turn)
    stored = [t["content"] for t in convo.user_history("u")]
    r.ok("...and is not stored in history",
         not any("CommandReference" in str(t) for t in stored))


SUITES = [
    ("command lookup", suite_lookup),
    ("command permission gates", suite_gates),
    ("command question gate", suite_question_gate),
    ("command catalogue coverage", suite_real_registry),
    ("command lookup plumbing", suite_plumbing),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
