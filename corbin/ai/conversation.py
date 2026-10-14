"""The conversational bot.

This is the path ~95% of messages take. It carries the persona, and a few
rules that were learned the hard way:

* **Speaker labels.** Every transcript line is ``[Name]: text``. The prompt
  tells the model those labels are authoritative and never to invent a name
  that is not in them -- unlabelled transcripts made the bot greet one
  person using another's name.
* **No raw mention tokens.** ``<@123>`` is resolved to ``@DisplayName``
  before anything is stored. Storing the raw token causes attribution
  hallucinations in multi-user channels.
* **Never an empty turn.** A blank ``content`` makes the provider reject the
  *whole* request, wedging every later reply in that conversation, so blank
  turns are filtered at both the store and the send boundary.
* **Truncation is rolled back**, not posted. A reply cut mid-sentence is
  trimmed to the last complete sentence; if nothing survives, we say nothing.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from collections import deque
from pathlib import Path
from typing import Any, Deque, Iterable, Optional, Sequence

from corbin.identity import NAME as BOT_NAME

logger = logging.getLogger("corbin.conversation")

#: Fluxer enforces no client-side limit, so we enforce Discord-equivalent
#: ones. PHASE-0-LIVE: confirm the server's real ceiling.
MAX_MESSAGE_CHARS = 2000
#: Per-channel transcript depth fed to the model.
CHANNEL_HISTORY = 25
#: Per-user back-and-forth retained across turns.
USER_HISTORY_TURNS = 12
MAX_TOKENS_REPLY = 4096

_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_SENTENCE_END = re.compile(r"[.!?…\"')\]]\s*$")


#: A compact rule block for a model that carries its own persona.
#:
#: Used instead of the full persona file when LLM_PERSONA_IN_MODEL=1 (the
#: persona is baked into the model's own system prompt). Long rule
#: sections push a small model's prompt past what it acts on -- measured
#: on a 14B model, it spent the whole budget reasoning about the rules and
#: wrote nothing -- so this is the conclusions, not the reasoning. When a
#: new tic shows up, add ONE short line here, with the sentence that
#: failed as the example.
COMPACT_RULES = """\
## Also
These ADD to your own instructions. Where they differ, these win.

- When someone says what THEY are, that is about them, not you.
- You are on Fluxer. Do not mention Discord unless someone else does.
- You are a member here, not an assistant. Never "I'm here to help", \
never offer your services.
- You have never used a computer, phone, game or app, and have been \
nowhere. Never "I've tried it" or "it worked for me". Ask about theirs.
- You see an image only when it is attached to the message you are \
answering. One the transcript only mentions, you cannot see: say so or \
ask. Never guess what a picture shows.
- Never say you are doing something now. No "I'll do it now".
- Never ask for a password or secret, and never invent a way to check \
who someone is.
- No "as an AI".
- No "not X, but Y" or "X isn't the point, Y is". Say the thing once.
- No repeating a phrase down a sentence: "the way the fog settles, the \
way the light goes still, the way the sky stops" is three too many.
- No solemn abstraction about absence, silence or contrast. You are \
chatting, not writing poetry.
- Jokes are welcome. Land it and move on: never explain it or say it \
was a bit. Casual chat: one to three sentences.
"""


def extract_sections(markdown: str, names: Sequence[str]) -> str:
    """Pull named ``## Section`` blocks out of a markdown document.

    For tooling; matching is case-insensitive and a missing name is skipped silently, since SOUL.md
    is hand-edited and a renamed heading should not take the bot down.
    """
    if not markdown or not names:
        return ""
    wanted = {n.strip().lower() for n in names}
    out: list[str] = []
    current: Optional[list[str]] = None

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("###"):
            if current:
                out.append("\n".join(current).rstrip())
            heading = stripped[3:].strip().lower()
            current = [line] if heading in wanted else None
            continue
        if current is not None:
            current.append(line)
    if current:
        out.append("\n".join(current).rstrip())
    return "\n\n".join(out)


class ToolRequest:
    """The model asked for a tool rather than answering."""

    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: Optional[dict] = None) -> None:
        self.name = name
        self.arguments = arguments or {}

    def __bool__(self) -> bool:
        # Never mistaken for a sendable reply by an `if reply:` check.
        return False

    def __repr__(self) -> str:
        return f"ToolRequest({self.name!r}, {self.arguments!r})"


from corbin.ai.command_help import LOOKUP_COMMANDS_TOOL  # noqa: E402

#: Offered to the model on the plain-chat path. The description is the
#: routing policy: a small model follows pointed examples far better than
#: abstract rules.
REPLY_TOOLS = [LOOKUP_COMMANDS_TOOL]


def has_content(value: Any) -> bool:
    """True when a message turn carries something the provider will accept."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return any(has_content(v) for v in value)
    if isinstance(value, dict):
        return has_content(value.get("text") or value.get("content") or "")
    return bool(value)


def resolve_mentions(content: str, *, users: Optional[dict[str, str]] = None,
                     roles: Optional[dict[str, str]] = None,
                     channels: Optional[dict[str, str]] = None) -> str:
    """Replace raw mention tokens with readable names.

    Unknown ids degrade to ``@someone`` / ``#channel`` rather than being left
    as a raw id -- a stray ``<@9182…>`` in the prompt is what makes the model
    attribute a message to the wrong person.
    """
    users, roles, channels = users or {}, roles or {}, channels or {}

    def user_sub(m: re.Match) -> str:
        return "@" + users.get(m.group(1), "someone")

    def role_sub(m: re.Match) -> str:
        return "@" + roles.get(m.group(1), "a role")

    def chan_sub(m: re.Match) -> str:
        # A placeholder like "#a channel" is worse than the raw token: the
        # classifier will happily extract it as a real channel name. Keep
        # the id when we cannot resolve a name.
        known = channels.get(m.group(1))
        return f"#{known}" if known else f"#{m.group(1)}"

    out = _MENTION.sub(user_sub, content or "")
    out = _ROLE_MENTION.sub(role_sub, out)
    return _CHANNEL_MENTION.sub(chan_sub, out)


def _norm_para(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


def drop_repeats(text: str, earlier: Sequence[str],
                 min_chars: int = 40) -> str:
    """Remove paragraphs the bot already posted, word for word.

    A small model copies its most recent turn: asked "what is bro yapping
    about", it re-sent the whole previous paragraph before adding to it.
    Short lines ("lol", "fair") are allowed to recur; only a paragraph of
    real length that already appeared in one of his recent messages goes.

    Fenced code is never touched. Re-sending code is the whole point when
    someone asks for it again, and a CSS block is blank-line-separated
    rules, so splitting it into "paragraphs" silently deleted every rule
    he had posted before -- asked for "the full css", he sent the prose
    around the block and no code, three times running.
    """
    blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"\x00CODE{len(blocks) - 1}\x00"

    masked = _FENCED_BLOCK.sub(_stash, text or "")
    seen = [_norm_para(e) for e in earlier if e]
    kept = []
    for para in re.split(r"\n\s*\n", masked):
        norm = _norm_para(para)
        if ("\x00CODE" not in para and len(norm) >= min_chars
                and any(norm in s for s in seen)):
            continue
        kept.append(para)
    out = "\n\n".join(p for p in kept if p.strip()).strip()
    return re.sub(r"\x00CODE(\d+)\x00", lambda m: blocks[int(m.group(1))],
                  out)


#: A closed fenced code block, with its language tag in group 1.
_FENCED_BLOCK = re.compile(r"```([\w+#.-]*)[^\S\n]*\n(.*?)```", re.DOTALL)

#: A code block at least this long is posted as a file, not inline: a long
#: block split across 2000-char messages can't be copied in one go, and a
#: file can.
CODE_FILE_MIN_LINES = 10
CODE_FILE_MIN_CHARS = 500

_LANG_EXTENSIONS = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts", "java": ".java", "c": ".c",
    "cpp": ".cpp", "c++": ".cpp", "csharp": ".cs", "cs": ".cs",
    "go": ".go", "rust": ".rs", "ruby": ".rb", "php": ".php",
    "html": ".html", "css": ".css", "scss": ".scss", "sql": ".sql",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "json": ".json",
    "yaml": ".yaml", "yml": ".yaml", "xml": ".xml", "markdown": ".md",
    "md": ".md", "lua": ".lua", "r": ".r", "swift": ".swift",
    "kotlin": ".kt", "scala": ".scala", "perl": ".pl",
    "powershell": ".ps1",
}


def extract_code_files(text: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Move long fenced code blocks out of a reply and into file uploads.

    Returns ``(text, [(filename, bytes), ...])``. Each extracted block is
    replaced by a pointer to its file, so the text is never left empty
    and reads naturally around the gap. Short blocks stay inline.
    """
    text = text or ""
    files: list[tuple[str, bytes]] = []

    def _sub(m: re.Match) -> str:
        lang, code = m.group(1).lower(), m.group(2)
        if (code.count("\n") + 1 < CODE_FILE_MIN_LINES
                and len(code) < CODE_FILE_MIN_CHARS):
            return m.group(0)
        name = f"code_{len(files) + 1}{_LANG_EXTENSIONS.get(lang, '.txt')}"
        files.append((name, code.encode("utf-8")))
        return f"*See attached file: `{name}`*"

    return _FENCED_BLOCK.sub(_sub, text), files


def trim_incomplete_tail(text: str) -> str:
    """Roll a truncated reply back to its last complete sentence.

    Returns ``""`` when nothing complete survives, so the caller drops the
    message rather than posting a fragment ending in a comma.
    """
    text = (text or "").strip()
    if not text or _SENTENCE_END.search(text):
        return text
    parts = re.split(r"(?<=[.!?…])\s+", text)
    if len(parts) <= 1:
        return ""
    kept = " ".join(parts[:-1]).strip()
    return kept if _SENTENCE_END.search(kept) else ""


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split a reply into sendable chunks, preferring natural boundaries.

    Fenced code blocks are re-opened across a split so a chunk boundary
    cannot leave one half-open.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)

    # Re-balance unterminated code fences across chunk boundaries.
    fixed: list[str] = []
    open_fence = False
    for chunk in chunks:
        body = ("```\n" if open_fence else "") + chunk
        if body.count("```") % 2:
            body += "\n```"
            open_fence = True
        else:
            open_fence = False
        fixed.append(body)
    return fixed


class Conversation:
    """Per-channel transcript plus per-user history, and the reply call."""

    def __init__(self, llm: Any, *, soul_path: str | Path,
                 additions_path: Optional[str | Path] = None,
                 memory: Any = None, extractor: Any = None,
                 bot_name: str = BOT_NAME) -> None:
        self.llm = llm
        #: Optional MemoryStore and MemoryExtractor.
        self.memory = memory
        self.extractor = extractor
        self.bot_name = bot_name
        self.soul = self._load_soul(soul_path)
        #: Always sent, baked persona or not: a Modelfile persona covers
        #: identity but not these specifics.
        self.soul_additions = (self._load_soul(additions_path, required=False)
                               if additions_path else "")
        #: Sent instead of SOUL.md to a model with its own baked persona.
        self.compact_rules = COMPACT_RULES
        #: channel -> entries. Dicts, not tuples: presence needs timestamps
        #: (30-minute freshness) and a bot flag (never reply to yourself).
        self._channels: dict[str, Deque[dict]] = {}
        self._users: dict[str, Deque[dict]] = {}

    @staticmethod
    def _load_soul(path: str | Path, *, required: bool = True) -> str:
        try:
            # utf-8-sig: tolerate a BOM from editors that add one.
            return Path(path).read_text(encoding="utf-8-sig")
        except OSError:
            if required:
                logger.error("could not read persona file at %s -- %s "
                             "will sound generic", path, BOT_NAME)
            else:
                logger.info("optional persona file not found: %s", path)
            return ""

    # -- history ----------------------------------------------------------

    def record(self, channel_id: str, speaker: str, content: str, *,
               is_bot: bool = False, message_id: Optional[str] = None,
               user_id: Optional[str] = None,
               media: Sequence[str] = ()) -> dict:
        """Append one line to a channel transcript.

        Recorded when there is text **or** media. An image with no
        caption used to be dropped entirely, so the transcript had a
        hole where the picture was -- and asked about it, the bot filled
        the hole with a guess rather than saying he had not seen one.
        """
        media = [str(m).strip() for m in media if str(m).strip()]
        if not has_content(content) and not media:
            return {}
        entry = {
            "author": speaker,
            "content": content.strip(),
            "media": media,
            "is_bot": is_bot,
            "message_id": message_id,
            "user_id": user_id,
            "timestamp": _dt.datetime.now(_dt.timezone.utc),
        }
        buf = self._channels.setdefault(channel_id,
                                        deque(maxlen=CHANNEL_HISTORY))
        buf.append(entry)
        return entry

    def entries(self, channel_id: str) -> list[dict]:
        return list(self._channels.get(channel_id, ()))

    def last_bot_message(self, channel_id: str) -> str:
        """The bot's most recent line in a channel, or "".

        Exists so callers never reach into ``_channels`` directly. When the
        entry shape changed from tuples to dicts, a caller doing its own
        unpacking broke silently and took the whole conversational path
        with it.
        """
        for entry in reversed(self._channels.get(channel_id, ())):
            if entry.get("is_bot"):
                return str(entry.get("content") or "")
        return ""

    def transcript(self, channel_id: str, *, highlight: Optional[str] = None,
                   omit_latest: Optional[str] = None) -> str:
        """Render the channel transcript with authoritative speaker labels.

        ``omit_latest`` drops the newest line when it is that text: the
        message being answered is recorded before the reply is built, and
        it already arrives as the final turn.
        """
        entries = list(self._channels.get(channel_id, ()))
        if omit_latest is not None and entries and not entries[-1].get(
                "is_bot") and entries[-1]["content"].strip() == \
                str(omit_latest).strip():
            entries = entries[:-1]
        lines = []
        for entry in entries:
            line = f"[{entry['author']}]: {entry['content']}".rstrip()
            for label in entry.get("media") or ():
                # The bot cannot see these. Naming them is what lets him
                # say "you posted a picture, I can't see it" instead of
                # inventing what is in it.
                line += f" [posted {label}, which you cannot see]"
            if highlight is not None and entry["content"] == highlight:
                line += "   <<< YOU ARE REPLYING TO THIS MESSAGE"
            lines.append(line)
        return "\n".join(lines)

    def user_history(self, user_id: str) -> list[dict]:
        return list(self._users.get(user_id, ()))

    def remember_turn(self, user_id: str, role: str, content: str) -> None:
        if not has_content(content):
            return
        buf = self._users.setdefault(
            user_id, deque(maxlen=USER_HISTORY_TURNS * 2))
        buf.append({"role": role, "content": content.strip()})

    def clear_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)

    # -- prompting --------------------------------------------------------

    def system_prompt(self, *, guild_name: Optional[str],
                      channel_name: Optional[str],
                      speaker: str,
                      known_names: Sequence[str] = (),
                      include_soul: bool = True,
                      memory_block: str = "",
                      channel_context: str = "") -> str:
        """Build the system prompt.

        ``include_soul=False`` omits the persona file for a model that
        already carries its own (baked into an Ollama Modelfile).
        The situational context below is always sent either way -- without it
        the model has no speaker labels, no server name, and no label rules.
        """
        if self.soul and include_soul:
            parts = [self.soul]
        else:
            # A model with its own persona gets the short rule block: the
            # full sections are too long for a small model to act on.
            parts = [self.compact_rules] if self.compact_rules else []
        # Additions go to every provider, baked persona or not.
        if self.soul_additions:
            parts.append(self.soul_additions)
        # Stated flatly, and with the negation: models know Discord far
        # better than Fluxer, and a passive "you are on Fluxer" loses to
        # that -- he starts explaining that "Discord blocks the
        # assignment" on a server that has no Discord in it.
        context = ["## Current Context",
                   "You are on Fluxer, a chat app. This is NOT Discord. "
                   "Never say Discord, or refer to Discord features, "
                   "rules or limits, unless someone else brings it up "
                   "first."]
        if guild_name:
            context.append(f"Server: {guild_name}")
        if channel_name:
            context.append(f"Channel: #{channel_name}")
        context.append(f"The person addressing you is: {speaker}")
        if known_names:
            context.append("People present: " + ", ".join(known_names))
        parts.append("\n".join(context))
        if memory_block:
            parts.append(memory_block)
        parts.append(
            "## Transcript rules\n"
            "Lines in the transcript are prefixed with [Name]: and those "
            "labels are authoritative. Never invent or guess a name that is "
            "not in the labels or the context above. Your own lines are "
            f"labelled [{self.bot_name}].\n"
            "Reply as yourself in plain chat prose. Do not prefix your reply "
            "with a speaker label, and do not narrate actions."
        )
        if channel_context:
            # Here, not in a user turn. Folded into the question's turn,
            # the model answered the transcript: asked what "SO" means, it
            # also explained two other people's jokes nobody asked about,
            # and read the acronym through one of them. In the system
            # prompt it reads as background, which is what it is.
            parts.append(
                "## Recent channel messages (background only)\n"
                "Oldest first. These were said to each other, not to you, "
                "and none of them is a question for you. Answer ONLY the "
                "latest message, the one you were just sent. Mention these "
                "only if that message asks about them, and never repeat "
                "what you already said here.\n"
                f"{channel_context}")
        return "\n\n".join(p for p in parts if p.strip())

    def build_messages(self, *, user_id: str, user_content: str,
                       channel_id: str) -> list[dict]:
        """Assemble the message list, guaranteeing no empty turns."""
        # No invented assistant turn. There used to be one after the
        # transcript -- "Understood — I'm caught up." -- to keep the roles
        # alternating, and the model learned it as a thing the bot
        # says. The transcript is not here at all now: it goes in the
        # system prompt (see ``system_prompt(channel_context=)``), so the
        # final turn is only the message being answered.
        messages: list[dict] = list(self.user_history(user_id))
        messages.append({"role": "user", "content": user_content})
        return [m for m in messages if has_content(m.get("content"))]

    def recent_bot_lines(self, channel_id: str, limit: int = 6) -> list[str]:
        """What the bot itself said most recently in a channel."""
        return [e["content"] for e in self._channels.get(channel_id, ())
                if e.get("is_bot")][-limit:]

    # -- generation -------------------------------------------------------

    async def reply(self, *, user_id: str, speaker: str, user_content: str,
                    channel_id: str, guild_name: Optional[str] = None,
                    channel_name: Optional[str] = None,
                    known_names: Sequence[str] = (),
                    guild_id: Optional[str] = None,
                    images: Sequence[dict] = (),
                    files: Sequence[tuple] = (),
                    tools: Optional[list] = None,
                    reference: Optional[str] = None) -> Any:
        """Generate one conversational reply, or None to stay quiet.

        ``images`` are image blocks (``core.vision.image_block``). They are
        only sent when the model has vision; the caller checks.

        ``tools`` lets the model ask for a tool instead of answering. When
        it does, this returns a :class:`ToolRequest` and stores nothing:
        the caller runs the tool and calls ``reply`` again, and only that
        answer is a turn.

        ``reference`` is a command lookup from ``command_help``.

        ``files`` are ``(filename, text)`` pairs from attached text files
        and documents.
        """
        if not has_content(user_content) and images:
            # An image with no caption is still something to answer.
            # Checking the text alone used to drop these silently. The
            # placeholder also keeps the stored history turn non-empty.
            user_content = "<User sent an image>"
        if not has_content(user_content) and files:
            user_content = "<User uploaded a file: {}>".format(
                ", ".join(name for name, _ in files))
        if not has_content(user_content):
            # Silence here is right (there is nothing to answer), but
            # it must not be indistinguishable from a failure.
            logger.info("nothing to reply to from %s: empty content",
                        speaker)
            return None

        constants = self.llm.constants
        payload = user_content
        if reference:
            from corbin.ai.command_help import reference_block

            payload = f"{payload}\n\n{reference_block(reference)}"
        if files:
            from corbin.core.documents import prompt_block

            payload = f"{payload}\n\n{prompt_block(files)}"
        messages = self.build_messages(
            user_id=user_id, user_content=payload, channel_id=channel_id)

        if images:
            # Attach to the final user turn as a block list. The text
            # goes last: a model reads the instruction better when the
            # picture is already in front of it.
            blocks = list(images) + [{"type": "text", "text": payload}]
            for turn in reversed(messages):
                if turn.get("role") == "user":
                    turn["content"] = blocks
                    break

        memory_block = ""
        if self.memory is not None:
            try:
                memory_block = self.memory.context_block(
                    user_id, guild_id, speaker)
            except Exception:
                logger.exception("memory lookup failed")

        system = self.system_prompt(
            guild_name=guild_name, channel_name=channel_name,
            speaker=speaker, known_names=known_names,
            memory_block=memory_block,
            channel_context=self.transcript(channel_id,
                                            omit_latest=user_content),
            include_soul=not getattr(self.llm, "persona_baked_in", False))

        try:
            result = await self.llm.call(
                model=constants.MODEL_MAIN, system=system,
                messages=messages, max_tokens=MAX_TOKENS_REPLY,
                **({"tools": tools} if tools else {}),
            )
        except Exception:
            logger.exception("conversational reply failed")
            return None

        calls = getattr(result, "tool_calls", None) or ()
        if tools and calls:
            known = {t["function"]["name"] for t in tools}
            for call in calls:
                if call.get("name") in known:
                    return ToolRequest(call["name"],
                                       dict(call.get("arguments") or {}))
            logger.warning("model asked for unknown tool(s) %s; ignoring",
                           [c.get("name") for c in calls])

        text = (result.text or "").strip()
        truncated = bool(getattr(result, "truncated", False))
        if truncated:
            text = trim_incomplete_tail(text)

        earlier = self.recent_bot_lines(channel_id) + [
            t["content"] for t in self.user_history(user_id)
            if t.get("role") == "assistant" and isinstance(t["content"], str)]
        fresh = drop_repeats(text, earlier)
        if text and not fresh:
            # The whole reply was a copy of something he already said.
            # One retry with the instruction spelled out, rather than
            # posting it again or going silent.
            logger.warning("reply for %s repeated an earlier message; "
                           "retrying once", speaker)
            retry = list(messages) + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    "[You just repeated a message you already sent. Answer "
                    "the last message again, in one or two new sentences, "
                    "without reusing that wording.]")}]
            try:
                again = await self.llm.call(
                    model=constants.MODEL_MAIN, system=system,
                    messages=retry, max_tokens=MAX_TOKENS_REPLY)
                fresh = drop_repeats((again.text or "").strip(), earlier)
            except Exception:
                logger.exception("repeat retry failed")
            text = fresh or text
        elif fresh != text:
            logger.info("dropped a repeated paragraph from the reply for %s",
                        speaker)
            text = fresh
        if not text:
            # Returning None here means the caller sends nothing at
            # all, which looks exactly like the bot ignoring someone.
            # Say which of the two happened.
            logger.warning(
                "no reply produced for %s (%s); staying silent",
                speaker,
                "truncated to nothing" if truncated
                else "model returned empty text")
            return None

        # Store the user's actual words, never the injected reference or
        # file contents -- only a note that a file was there.
        stored = user_content
        if files and not user_content.startswith("<User uploaded"):
            stored = "{}\n<User uploaded a file: {}>".format(
                user_content, ", ".join(name for name, _ in files))
        self.remember_turn(user_id, "user", stored)
        self.remember_turn(user_id, "assistant", text)

        # Extraction runs detached: it is a second model call and must never
        # sit between the user and their reply.
        if self.extractor is not None and self.memory is not None:
            import asyncio

            # Snapshot the history BEFORE this turn, so the extractor sees
            # what led up to it.
            prior = self.user_history(user_id)[:-2]
            asyncio.create_task(self._remember(
                user_id=user_id, display_name=speaker,
                guild_id=guild_id, user_content=user_content,
                bot_response=text, recent=prior))
        return text

    async def _remember(self, *, user_id: str, display_name: str,
                        guild_id: Optional[str], user_content: str,
                        bot_response: str,
                        recent: Sequence[dict] = ()) -> None:
        """Extract and persist durable facts from one exchange."""
        try:
            entry = self.memory.get_user(user_id)
            user_facts, server_facts = await self.extractor.extract(
                display_name=display_name or entry.display_name or "them",
                user_content=user_content, bot_response=bot_response,
                known_facts=entry.facts, include_server=bool(guild_id),
                recent=recent)

            entry.touch(display_name)
            changed = entry.apply(user_facts)
            self.memory.save_user(entry)
            if changed:
                logger.info("memory: %d new fact(s) for %s", changed, user_id)

            if guild_id and server_facts:
                server = self.memory.get_server(guild_id)
                if server.apply(server_facts):
                    self.memory.save_server(server)
                    logger.info("memory: server facts updated for %s",
                                guild_id)
        except Exception:
            logger.exception("memory update failed")
