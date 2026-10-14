"""What Corbin can do, looked up on demand rather than carried in every prompt.

The registry is the single source of truth for commands, but the full
catalogue is ~80 commands. Putting it in the system prompt would cost
thousands of tokens on every casual reply, and a small local model stops
acting on long prompts anyway. So it is fetched only when someone asks
what he can do:

* the model calls the ``lookup_commands`` tool itself, and
* a free regex gate (``is_command_question``) catches the obvious
  phrasings, so the answer is right even when the model doesn't support
  tool calls.

Only commands the asker could actually run are offered as runnable.
Staff commands are still listed on a specific lookup, marked with what
they need, so "can you ban people?" gets "yes, for mods" rather than a
confident no.
"""

from __future__ import annotations

import re
from typing import AbstractSet, Any, Iterable, Optional, Sequence

from corbin.core.permissions import Perm, has
from corbin.core.registry import CommandSpec, Registry

#: Hard ceiling on the injected block. The overview carries a one-line
#: description per command, because a names-only list got the model
#: inventing what each did ("copy-emojis" became "copy emoji packs from
#: other channels"). A member's overview is ~3K chars; an admin's, with
#: the staff tools in detail, ~16K; the bot owner's ~16.2K (~4.5K tokens).
#: It is only ever sent when someone asked, so that is affordable.
MAX_REFERENCE_CHARS = 17000
#: Detailed entries returned for a topic lookup.
MAX_MATCHES = 6

#: Module stem -> the heading people would recognise.
CATEGORIES = {
    "audit_cmds": "Audit logs",
    "config_cmds": "Chat settings",
    "digest_cmds": "Daily digest",
    "expression_cmds": "Emoji and stickers",
    "general": "General",
    "info": "Weather, time and info",
    "memory_cmds": "Memory",
    "moderation": "Moderation",
    "moon_cmds": "Moon",
    "music_cmds": "Music",
    "radio_cmds": "Radio",
    "ranking_cmds": "Ranking and XP",
    "reminder_cmds": "Reminders",
    "scheduled_cmds": "Scheduled posts",
    "spamblock_cmds": "Spam blocker",
    "util_cmds": "Utilities",
    "verse_cmds": "Bible verses",
    "warn_cmds": "Warnings",
    "welcome_cmds": "Welcome and farewell",
}

#: What a bare command list cannot explain. Shown under the category
#: heading in the overview and above a detailed lookup.
CATEGORY_NOTES: dict[str, str] = {}

#: What a moderator needs to know that a one-line description can't say.
#: Only shown to someone who can run the tools in that category.
STAFF_NOTES = {
    "moderation": (
        "assignrole can only give a role, not take one away. "
        "A ban or kick happens straight away only when the member has no "
        "mod permissions AND you give a reason; otherwise Corbin asks "
        "first, and he always asks before acting on a moderator. Corbin "
        "refuses to act on the server owner, a bot, yourself, "
        "or anyone whose top role is at or above yours. Bans can be timed "
        "(\"for 2h\", \"for 7 days\") and lift on their own, or permanent "
        "if no time is given; listbans shows what's left on each."),
    "warn_cmds": (
        "Each warning gets an ID. warn can also time out, kick or ban in "
        "the same step. The member can appeal a warning, and unwarn "
        "deletes one by its ID. A warning's reason can't be edited; delete "
        "it and warn again."),
    "config_cmds": (
        "chat-mode sets the server-wide mode: mention (only when "
        "mentioned or replied to), global (he may also join conversations "
        "on his own), or off. reply-channel makes one channel answer "
        "every message; silence mutes him in one channel."),
    "audit_cmds": (
        "audit-logs turns logging on and picks the channel; audit-category "
        "switches single kinds on or off: message edits and deletes, joins "
        "and leaves, channel changes, voice. Fluxer doesn't report who "
        "performed an action, so entries say what happened, not who did "
        "it."),
    "welcome_cmds": (
        "Welcome and leave messages fill in {member}, {server} and {count} "
        "(the member count). Both can carry an image card: the member's "
        "avatar in the middle, a line of text on top (card_text) and their "
        "name below. card_background sets the card's picture from an "
        "attached image or any link, image or page (\"set this image as the "
        "welcomer background\"); \"none\" clears it. Settings can be "
        "changed one at a time without redoing the setup. greeting has "
        "Corbin write each welcome himself instead, and can mention "
        "something like reading the rules."),
    "scheduled_cmds": "Daily quote times are in UTC (hour 0-23).",
    "info": ("Weather alerts take any time and timezone (\"7am PST\"), "
             "default to the asker's own timezone, and can post to another "
             "channel. Each has an id; remove one by id or place, or say "
             "\"all\"."),
    "spamblock_cmds": (
        "spamblock creates a #do-not-type channel at the top of the "
        "server. Scam bots post in every channel they can, so anyone "
        "posting there is banned on the spot; staff are exempt and just "
        "get the message deleted."),
    "expression_cmds": (
        "Copies from a Discord server the bot owner has configured "
        "(DISCORD_TOKEN and DISCORD_GUILD_ID). Needs Manage Expressions."),
    "memory_cmds": "forget-server can't be undone.",
    "util_cmds": "messager DMs every member of a role; it confirms first.",
}

#: Notes for everyone.
CATEGORY_NOTES.update({
    "music_cmds": (
        "A YouTube or Spotify playlist or album link queues up to 50 songs: "
        "the first starts right away and the rest load in the background."),
    "ranking_cmds": (
        "XP comes from chatting. Time in voice isn't counted on Fluxer yet."),
})

#: Things he does without anyone running a command. Asked "what can you
#: do?", a list of commands alone undersells him.
AUTOMATIC = (
    "Also, no command needed:\n"
    "- Chats when mentioned, replied to, in DMs, or in a channel set up for "
    "him, and now and then joins a conversation on his own.\n"
    "- Remembers things people tell him (see Memory to view or erase it).\n"
    "- Reads attached text or Word files when you talk to him, and sees "
    "images if his model has vision.\n"
    "- Asks for a yes/no before risky actions; saying \"undo\" right after "
    "some actions reverses them.\n"
    "- If staff turn them on: posts a bible verse when someone cites one, "
    "XP and level-up announcements, welcome and leave messages, hourly "
    "animals, a daily quote, full moon announcements, daily weather, server "
    "event logs, and the #do-not-type trap that bans scam bots.")

#: Asking about Corbin's abilities in so many words. Kept narrow on
#: purpose: a false positive pastes the command list into an ordinary
#: question. Anything subtler is the tool's job.
_COMMAND_QUESTION = re.compile(
    r"\b("
    r"(what|which) commands|"
    r"(your|corbin'?s) (commands|features)|"
    r"(list|show)( me)? (of |all |your |the )?commands|"
    r"(any|a|got a|have a) commands? (for|to|that)|"
    r"what (all )?can (you|corbin) do|"
    r"what are you (able|capable)|"
    r"how do i (use|get|make|ask|tell) (you|corbin)|"
    r"can (you|corbin) (do|help with) (anything|stuff|things)"
    r")\b", re.IGNORECASE)

#: Words that say "this is a question about commands" but not which one.
_STOP = frozenset("""
a about all an and any anything are ask be can command commands corbin could
do does feature features for from get got have help how i is it list make me
my of on or people person please run show some somebody someone something
stuff tell that the there these thing things this to use what whats which
with would you your yours
""".split())

LOOKUP_COMMANDS_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_commands",
        "description": (
            "Look up the things you can actually do for people here: music, "
            "radio, reminders, weather, ranks and XP, moderation, welcome "
            "messages, bible verses and more. Use it whenever someone asks "
            "what you can do, whether you can do X, how to get you to do "
            "something, or how a feature works, e.g. \"can you play "
            "music?\", \"how do I set a reminder?\", \"what commands do you "
            "have?\". You do NOT know your own commands by heart, so never "
            "describe one without looking it up. Not for ordinary chat."),
        "parameters": {
            "type": "object",
            "properties": {"topic": {
                "type": "string",
                "description": (
                    "What they want to do, in a few words (\"reminders\", "
                    "\"play music\", \"ban someone\"). Leave empty for an "
                    "overview of everything.")}},
        },
    },
}


def is_command_question(text: str) -> bool:
    return bool(_COMMAND_QUESTION.search(text or ""))


def _norm_tokens(text: str) -> set[str]:
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    # Same singularisation as the registry's matcher: "reminders" finds
    # "reminder", "bans" finds "ban".
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def _overlap(wanted: set[str], have: set[str]) -> int:
    """Shared words, counting a common stem ("remind" ~ "reminder").

    The stem rule allows at most a two-letter ending, or "time" would
    match "timeout".
    """
    return sum(1 for w in wanted
               if w in have or (len(w) >= 4 and any(
                   len(h) >= 4 and abs(len(h) - len(w)) <= 2
                   and (h.startswith(w) or w.startswith(h))
                   for h in have)))


def _stem(spec: CommandSpec) -> str:
    module = getattr(spec.handler, "__module__", "") or ""
    return module.rsplit(".", 1)[-1]


def category_of(spec: CommandSpec) -> str:
    stem = _stem(spec)
    return CATEGORIES.get(stem) or stem.replace("_cmds", "").replace(
        "_", " ").strip().capitalize() or "Other"


#: Permissions every ordinary member has. A command needing only these
#: (joining voice for music, say) is for everyone, not a staff tool.
MEMBER_PERMS = (Perm.VIEW_CHANNEL | Perm.SEND_MESSAGES | Perm.CONNECT
                | Perm.SPEAK | Perm.READ_MESSAGE_HISTORY)


def is_staff(spec: CommandSpec) -> bool:
    return bool(int(spec.permission) & ~int(MEMBER_PERMS))


def perm_label(required: Perm) -> str:
    names = [p.name.replace("_", " ").title() for p in Perm
             if p and p.name and (required & p) == p]
    return ", ".join(names) or "special permission"


class CommandHelp:
    """Renders the registry into a reference block for one asker."""

    def __init__(self, registry: Registry, *,
                 bot_owner_id: Optional[str] = None,
                 grants: Optional[dict[str, AbstractSet[str]]] = None) -> None:
        self.registry = registry
        self.bot_owner_id = str(bot_owner_id) if bot_owner_id else None
        self.grants = dict(grants or {})

    # -- who can run what --------------------------------------------------

    def _is_owner(self, invoker: Any) -> bool:
        return (self.bot_owner_id is not None
                and str(getattr(invoker, "id", "")) == self.bot_owner_id)

    def _granted(self, spec: CommandSpec, invoker: Any) -> bool:
        mine = self.grants.get(str(getattr(invoker, "id", "")), frozenset())
        return (spec.name in mine or spec.name.replace("-", "_") in mine)

    def restriction(self, spec: CommandSpec, invoker: Any,
                    in_guild: bool) -> Optional[str]:
        """Why this person can't run it, or None when they can.

        Mirrors the hard gates in ``core.risk.assess`` so the help never
        promises a command the dispatcher would then deny.
        """
        if spec.guild_only and not in_guild:
            return "only works in a server, not in DMs"
        if self._is_owner(invoker) or self._granted(spec, invoker):
            return None
        effective = getattr(invoker, "permissions", Perm.NONE)
        if not has(effective, spec.permission):
            return f"needs {perm_label(spec.permission)}"
        return None

    def _offered(self, guild_id: Optional[str] = None,
                 owner: bool = False) -> Iterable[CommandSpec]:
        # Owner-only commands are shown to the bot owner and nobody else.
        return (s for s in self.registry.ai_invocable()
                if owner or not s.owner_only)

    # -- rendering ---------------------------------------------------------

    def _detail(self, spec: CommandSpec, invoker: Any, in_guild: bool,
                indent: str = "", examples: int = 3) -> str:
        blocked = self.restriction(spec, invoker, in_guild)
        head = f"{indent}- {spec.name}: {spec.description}"
        if blocked:
            head += f" [{blocked}; this person can't run it]"
        lines = [head]
        sub = indent + "  "
        if spec.params:
            bits = []
            for p in spec.params:
                bits.append(f"{p.name}{'' if p.required else ' (optional)'}"
                            f" = {p.description}")
            lines.append(f"{sub}takes: " + "; ".join(bits))
        if spec.examples and examples:
            said = " / ".join(f'"{e}"'
                              for e in list(spec.examples)[:examples])
            lines.append(f"{sub}people say: {said}")
        return "\n".join(lines)

    def overview(self, invoker: Any, in_guild: bool,
                 guild_id: Optional[str] = None) -> str:
        """Everything this person can use, grouped by who it is for.

        A member gets the everyday commands and a one-line count of the
        staff tools. Someone who holds staff permissions gets those tools
        FIRST, in detail (arguments, an example, and the category notes a
        one-liner can't carry), then the everyday list in brief. Same
        question, different person, different answer.
        """
        sections: dict[bool, dict[str, list[CommandSpec]]] = {
            False: {}, True: {}}
        hidden = 0
        owner = self._is_owner(invoker)
        owner_tools: list[CommandSpec] = []
        for spec in self._offered(guild_id, owner=owner):
            if spec.owner_only:
                owner_tools.append(spec)
                continue
            if self.restriction(spec, invoker, in_guild):
                hidden += 1
                continue
            sections[is_staff(spec)].setdefault(
                category_of(spec), []).append(spec)

        lines: list[str] = []
        if owner_tools:
            lines.append("BOT OWNER ONLY (this person is the bot owner)")
            lines.extend(self._detail(s, invoker, in_guild, indent="  ",
                                      examples=1) for s in owner_tools)
            lines.append("")
        staff = sections[True]
        if staff:
            lines.append("MODERATOR AND ADMIN TOOLS (this person can use "
                         "these)")
            for cat, specs in sorted(staff.items()):
                lines.append(f"{cat}:")
                stem = _stem(specs[0])
                for note in (CATEGORY_NOTES.get(stem),
                             STAFF_NOTES.get(stem)):
                    if note:
                        lines.append(f"  ({note})")
                lines.extend(self._detail(s, invoker, in_guild, indent="  ",
                                          examples=1) for s in specs)
            lines.append("")

        everyday = sections[False]
        if everyday:
            lines.append("FOR EVERYONE")
            for cat, specs in sorted(everyday.items()):
                lines.append(f"{cat}:")
                stem = _stem(specs[0])
                note = CATEGORY_NOTES.get(stem)
                if note and not (stem in {_stem(x) for v in staff.values()
                                          for x in v}):
                    lines.append(f"  ({note})")
                lines.extend(f"  - {s.name}: {s.description}" for s in specs)
        if hidden:
            lines.append(f"(Plus {hidden} moderator, admin or server-only "
                         f"commands this person can't run here.)")
        return "\n".join(lines)

    def search(self, topic: str, guild_id: Optional[str] = None,
               owner: bool = False) -> list[CommandSpec]:
        wanted = _norm_tokens(topic) - _STOP
        if not wanted:
            return []
        scored = []
        for spec in self._offered(guild_id, owner=owner):
            name = _norm_tokens(spec.name.replace("-", " "))
            aliases = _norm_tokens(" ".join(spec.aliases))
            cat = _norm_tokens(category_of(spec))
            desc = _norm_tokens(spec.description)
            said = _norm_tokens(" ".join(spec.examples))
            score = (3 * _overlap(wanted, name | aliases)
                     + 2 * _overlap(wanted, cat)
                     + _overlap(wanted, desc) + _overlap(wanted, said))
            if score:
                scored.append((-score, spec.name, spec))
        scored.sort(key=lambda t: t[:2])
        if not scored:
            return []
        # Drop the long tail: a stray word shared with a description should
        # not ride along behind a real name match.
        floor = -scored[0][0] / 2
        return [spec for neg, _, spec in scored[:MAX_MATCHES] if -neg >= floor]

    def lookup(self, topic: str, invoker: Any, *, in_guild: bool,
               guild_id: Optional[str] = None) -> str:
        """The reference block to hand the model."""
        topic = (topic or "").strip()
        matches = self.search(topic, guild_id,
                              owner=self._is_owner(invoker))
        if matches:
            notes = []
            for spec in matches:
                stem = _stem(spec)
                runnable = not self.restriction(spec, invoker, in_guild)
                for note in (CATEGORY_NOTES.get(stem),
                             STAFF_NOTES.get(stem)
                             if runnable and is_staff(spec) else None):
                    if note and note not in notes:
                        notes.append(note)
            body = "\n".join(
                [f"({n})" for n in notes]
                + [self._detail(s, invoker, in_guild) for s in matches])
            title = f"Commands matching {topic!r}"
        else:
            body = (self.overview(invoker, in_guild, guild_id)
                    + "\n\n" + AUTOMATIC)
            title = ("Nothing matched " + repr(topic) + "; everything this "
                     "person can use" if (_norm_tokens(topic) - _STOP)
                     else "Everything this person can use")
        text = f"{title}:\n{body}"
        if len(text) > MAX_REFERENCE_CHARS:
            text = text[:MAX_REFERENCE_CHARS].rsplit("\n", 1)[0] + "\n…"
        return text


def reference_block(reference: str) -> str:
    """How the lookup is framed in the user turn."""
    return (
        "[Tool:CommandReference] Your real commands, looked up for this "
        "question. People use them by simply asking you in plain words "
        "(\"Corbin, play some lofi\"); there are no slash or ! commands, so "
        "never tell anyone to type one. Answer only from this list, in your "
        "own voice, and don't paste it verbatim. If what they want isn't "
        "here, say you can't do that rather than inventing a command. If a "
        "command is marked as one this person can't run, say who can. For "
        "a broad \"what can you do?\", give a short tour by section and "
        "category with a few highlights each, not every command; offer "
        "detail on any area they want. If the moderator and admin section "
        "comes first, this person is staff: lead with those tools and "
        "explain them properly. If it is absent, just say briefly that "
        "there are staff tools too.\n"
        f"{reference}")
