"""The natural-language intent router.

Pipeline, cheapest stage first -- the ordering matters because the vast
majority of messages Corbin sees are social, and none of them should cost a
model call:

1. **Direct invocation gate** (free).  Only @mentions, AI channels and
   replies-to-Corbin can run commands at all.  Ambient chatter in an open
   channel is structurally incapable of triggering one, which removes most
   of the joke surface before any model runs.
2. **Shape gate** (free regex).  Does this even look like a request?
3. **Classifier** (one ``MODEL_FAST`` call).  Returns the parsed command
   *and* whether it was meant as an instruction -- two separate axes, in one
   call, so the joke screen costs no extra latency.
4. **Risk assessment** (free, deterministic).  See ``core.risk``.
5. Execute, confirm, refuse, or hand back to conversation.

The classifier is injected as a protocol so the whole pipeline is testable
offline with no network and no API key.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import AbstractSet, Any, Mapping, Optional, Protocol, Sequence

from .confirm import (PendingStore, Undoable, is_affirmative, is_negative,
                      is_undo)
from .context import CommandContext, Member, Message, Surface
from .permissions import Perm
from .registry import CommandSpec, ParamError, Registry, Risk
from .risk import Assessment, Decision, TargetProfile, assess

logger = logging.getLogger("corbin.dispatch")


# --------------------------------------------------------------------------
# Free gates
# --------------------------------------------------------------------------

#: Verbs that carry a request regardless of which commands exist.
_UNIVERSAL_VERBS = (
    "show", "tell", "give", "get", "set", "make", "add", "remove", "delete",
    "enable", "disable", "toggle", "start", "stop", "check", "look up",
    "find", "search", "list", "what", "who", "when", "where", "how",
)

_WORD = re.compile(r"[a-z]{3,}")


def _registry_keywords(registry: "Registry") -> frozenset[str]:
    """Build the shape-gate vocabulary from the registry itself.

    Deriving this rather than hardcoding it means dropping a command (say
    ``joke``, which the conversational path handles better) automatically
    stops its keywords from burning a classifier call on every "tell me a
    joke". A hardcoded list silently rots the moment scope changes.
    """
    words: set[str] = set()
    for spec in registry.ai_invocable():
        words.add(spec.name.replace("-", " "))
        words.update(_WORD.findall(spec.name.replace("-", " ")))
        for alias in spec.aliases:
            words.update(_WORD.findall(alias.lower()))
        for example in spec.examples:
            words.update(_WORD.findall(example.lower()))
    words.update(_UNIVERSAL_VERBS)
    # Drop words too generic to discriminate.
    return frozenset(words - {"the", "and", "for", "with", "from", "that",
                              "this", "your", "you", "are", "was", "their"})


#: Markers that a message is not a live instruction. These *inform* the
#: classifier prompt; the model makes the call, since regex cannot read tone.
_NON_DIRECTIVE_HINTS = re.compile(
    r"(\blol\b|\blmao\b|\bhaha\b|\bjk\b|/s\b|\bkidding\b|\bjoking\b|"
    r"\bsomeone should\b|\bwhat if\b|\bimagine\b|\bwould you ever\b|"
    r"\bcan you even\b|\bremember when\b|\bhypothetical)",
    re.IGNORECASE,
)


_gate_cache: dict[int, frozenset[str]] = {}


def looks_like_request(text: str, registry: "Registry" = None) -> bool:
    """Free pre-filter. False means: definitely just conversation.

    Permissive by design: a false positive costs one ~0.6s classifier call,
    while a false negative loses a command outright.
    """
    if not text or len(text.strip()) < 2:
        return False
    if len(text) > 1500:  # an essay is not a command
        return False
    if registry is None:
        return True
    key = id(registry)
    vocab = _gate_cache.get(key)
    if vocab is None or len(vocab) == 0:
        vocab = _registry_keywords(registry)
        _gate_cache[key] = vocab
    lowered = text.lower()
    return any(w in lowered for w in vocab)


def has_non_directive_markers(text: str) -> bool:
    return bool(_NON_DIRECTIVE_HINTS.search(text or ""))


# --------------------------------------------------------------------------
# Classifier interface
# --------------------------------------------------------------------------

@dataclass
class Intent:
    """What the classifier believed the user wanted."""

    command: Optional[str] = None
    args: Mapping[str, Any] = field(default_factory=dict)
    #: How sure it is that it parsed the *right command with the right args*.
    confidence: float = 0.0
    #: Whether this was an instruction to act, as opposed to banter, a
    #: hypothetical, a quote, or a question about capability.
    directive: bool = True
    #: Why not, when ``directive`` is False -- surfaced in logs only.
    why_not: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.command)


class IntentClassifier(Protocol):
    async def classify(
        self, text: str, *, registry: Registry, hint_non_directive: bool
    ) -> Intent: ...


class TargetResolver(Protocol):
    """Builds a :class:`TargetProfile` from platform + Corbin's own stores."""

    async def profile(
        self, guild_id: Optional[str], user_ref: str
    ) -> Optional[TargetProfile]: ...


# --------------------------------------------------------------------------
# Outcome
# --------------------------------------------------------------------------

@dataclass
class DispatchResult:
    handled: bool
    decision: Optional[Decision] = None
    assessment: Optional[Assessment] = None
    spec: Optional[CommandSpec] = None
    args: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""
    #: True when the caller should fall through to normal conversation.
    converse: bool = False
    #: Set when the user asked to undo; the executor performs it.
    undo: Optional["Undoable"] = None


def _summarise(spec: CommandSpec, args: Mapping[str, Any],
               target: Optional[TargetProfile]) -> str:
    """A short, human-checkable restatement of the parsed intent.

    Only surfaces what the *user* actually specified. Echoing defaulted
    arguments back ("delete_days=0") makes the prompt read like a form and
    buries the one detail they need to check.
    """
    who = ""
    if target is not None:
        who = f" @{target.display_name or target.user_id}"
    elif "user" in args:
        who = f" {args['user']}"
    reason = args.get("reason")
    tail = f" for \"{reason}\"" if isinstance(reason, str) and reason.strip() else ""

    shown = []
    for key, value in args.items():
        if key in ("user", "reason") or value is None:
            continue
        param = spec.param(key)
        if param is not None and not param.required and value == param.default:
            continue  # user never asked for it; it's just the default
        shown.append(f"{key}={value}")
    if shown:
        tail += f" ({', '.join(shown)})"
    return f"{spec.name}{who}{tail}"


class Dispatcher:
    """Routes natural language to registry commands."""

    def __init__(
        self,
        registry: Registry,
        classifier: IntentClassifier,
        *,
        resolver: Optional[TargetResolver] = None,
        store: Optional[PendingStore] = None,
        bot_owner_id: Optional[str] = None,
        grants: "Optional[Mapping[str, AbstractSet[str]]]" = None,
    ) -> None:
        self.registry = registry
        self.classifier = classifier
        self.resolver = resolver
        self.store = store or PendingStore()
        self.bot_owner_id = str(bot_owner_id) if bot_owner_id else None
        #: ``{user id: command names}`` -- extra commands a specific
        #: person may run without the guild permission the command
        #: normally needs. Narrower than granting them that permission.
        self.grants: Mapping[str, AbstractSet[str]] = dict(grants or {})

    # -- entry point ------------------------------------------------------

    async def handle(
        self,
        text: str,
        *,
        invoker: Member,
        channel_id: str,
        guild_id: Optional[str],
        directly_invoked: bool,
        mention_ids: Optional[Mapping[str, str]] = None,
    ) -> DispatchResult:
        """Decide what to do with one directly-invoked message.

        ``mention_ids`` maps the display names @-mentioned in the message to
        their ids. User arguments are resolved to ids with it BEFORE the
        risk check, because the check profiles the target by id: given a
        name, the lookup found nobody and every target gate (staff,
        hierarchy, owner, bot, self) was skipped.
        """
        user_id = invoker.id

        # A pending confirmation or undo owns the next word from this user.
        replied = await self._resume(text, channel_id=channel_id, user_id=user_id,
                                     invoker=invoker, guild_id=guild_id)
        if replied is not None:
            return replied

        # Gate 1: only direct invocation can run commands.
        if not directly_invoked:
            return DispatchResult(handled=False, converse=True,
                                  message="not directly invoked")

        # Gate 2: free shape filter.
        if not looks_like_request(text, self.registry):
            return DispatchResult(handled=False, converse=True,
                                  message="no request shape")

        # Gate 3: the one model call.
        intent = await self.classifier.classify(
            text, registry=self.registry,
            hint_non_directive=has_non_directive_markers(text),
        )
        if not intent.matched:
            return DispatchResult(handled=False, converse=True,
                                  message="no command matched")

        spec = self.registry.get(intent.command or "")
        if spec is None or not spec.ai_invocable:
            return DispatchResult(handled=False, converse=True,
                                  message=f"unknown command {intent.command!r}")

        try:
            args = spec.coerce_args(intent.args)
        except ParamError as exc:
            # A misparse should ask, not guess.
            return DispatchResult(
                handled=True, decision=Decision.CONFIRM, spec=spec,
                args=dict(intent.args), message=f"I need a bit more: {exc}",
            )

        args = await self._resolve_users(spec, args, guild_id, mention_ids)
        target = await self._target_for(spec, args, guild_id)
        verdict = assess(
            spec, surface=Surface.AI, invoker=invoker, args=args,
            confidence=intent.confidence, directive=intent.directive,
            target=target,
            is_bot_owner=(self.bot_owner_id is not None
                          and user_id == self.bot_owner_id),
            grants=self.grants.get(str(user_id), frozenset()),
        )
        logger.info("AI dispatch | %s | user=%s", verdict.explain(), user_id)

        return self._apply(verdict, spec, args, target,
                           channel_id=channel_id, user_id=user_id,
                           guild_id=guild_id, intent=intent)

    # -- internals --------------------------------------------------------

    async def _resolve_users(
        self, spec: CommandSpec, args: Mapping[str, Any],
        guild_id: Optional[str], mention_ids: Optional[Mapping[str, str]],
    ) -> dict[str, Any]:
        """User params as ids: the mention first, then a name lookup."""
        out = dict(args)
        mentions = {str(k).lower(): str(v)
                    for k, v in (mention_ids or {}).items()}
        for param in spec.params:
            if param.kind != "user":
                continue
            raw = str(out.get(param.name) or "").strip()
            if not raw or raw.isdigit():
                continue
            name = raw.lstrip("@")
            if name.lower() in mentions:
                out[param.name] = mentions[name.lower()]
                continue
            named = getattr(self.resolver, "resolve_named", None)
            if named is not None and guild_id:
                try:
                    out[param.name] = await named(guild_id, "user", raw)
                except Exception:
                    logger.debug("could not resolve user %r", raw,
                                 exc_info=True)
        return out

    async def _target_for(
        self, spec: CommandSpec, args: Mapping[str, Any], guild_id: Optional[str]
    ) -> Optional[TargetProfile]:
        param = next((p for p in spec.params if p.kind == "user"), None)
        if param is None or self.resolver is None:
            return None
        ref = args.get(param.name)
        if not ref:
            return None
        try:
            return await self.resolver.profile(guild_id, str(ref))
        except Exception:
            logger.exception("target resolution failed for %r", ref)
            return None

    def _apply(
        self, verdict: Assessment, spec: CommandSpec,
        args: Mapping[str, Any], target: Optional[TargetProfile], *,
        channel_id: str, user_id: str, guild_id: Optional[str],
        intent: Intent,
    ) -> DispatchResult:
        summary = spec.describe_call(args) or _summarise(spec, args, target)

        if verdict.decision is Decision.NOT_A_COMMAND:
            # Banter. Fall through to conversation -- never offer to execute.
            return DispatchResult(handled=False, converse=True,
                                  decision=verdict.decision, assessment=verdict,
                                  message=intent.why_not or "read as banter")

        if verdict.decision is Decision.DENIED:
            # Decline conversationally. Do not restate the action or confirm:
            # that would leak capability to someone not entitled to it.
            #
            # Naming the missing permission is safe -- they already know
            # which command they asked for -- and without it the reply
            # is indistinguishable from a bug. A vague "you don't have
            # permission" sent an admin looking for a broken grant that
            # was simply never configured.
            needed = getattr(verdict, "missing_permission", None)
            names = [p.name.replace("_", " ").title()
                     for p in Perm if p.value and needed and (needed & p)]
            detail = f" (needs {', '.join(names[:2])})" if names else ""
            return DispatchResult(
                handled=True, decision=verdict.decision, assessment=verdict,
                spec=spec, args=args,
                message=f"you don't have permission for that one{detail}",
            )

        if verdict.decision is Decision.REFUSED:
            return DispatchResult(
                handled=True, decision=verdict.decision, assessment=verdict,
                spec=spec, args=args, message=verdict.reason,
            )

        if verdict.decision is Decision.CONFIRM:
            self.store.put(
                guild_id=guild_id, channel_id=channel_id, user_id=user_id,
                command=spec.name, args=args, summary=summary,
            )
            return DispatchResult(
                handled=True, decision=verdict.decision, assessment=verdict,
                spec=spec, args=args,
                # No em-dash: SOUL.md forbids it and this is Corbin talking.
                message=f"Want me to {summary}? (yes/no)",
            )

        return DispatchResult(
            handled=True, decision=Decision.EXECUTE, assessment=verdict,
            spec=spec, args=args, message=summary,
        )

    async def _resume(
        self, text: str, *, channel_id: str, user_id: str,
        invoker: Member, guild_id: Optional[str],
    ) -> Optional[DispatchResult]:
        """Handle 'yes'/'no'/'undo' replies to our own earlier prompt."""
        if is_undo(text):
            entry = self.store.take_undo(channel_id, user_id)
            if entry is not None:
                return DispatchResult(handled=True, undo=entry,
                                      message=f"Reversing {entry.summary}.")
            return None

        pending = self.store.peek(channel_id, user_id)
        if pending is None:
            return None

        if is_negative(text):
            self.store.drop(channel_id, user_id)
            return DispatchResult(handled=True, message="Cancelled.")

        if is_affirmative(text):
            self.store.take(channel_id, user_id)
            spec = self.registry.get(pending.command)
            if spec is None:
                return DispatchResult(handled=True,
                                      message="That command vanished on me.")
            # Re-run the hard gates: permissions may have changed while the
            # prompt sat there, and a confirmation must never be a way to
            # bank an authorisation you no longer hold.
            # The real target, not None: re-checking hierarchy and owner
            # against nobody made a "yes" skip them.
            verdict = assess(
                spec, surface=Surface.PREFIX, invoker=invoker,
                args=pending.args, confidence=1.0, directive=True,
                target=await self._target_for(spec, pending.args, guild_id),
                is_bot_owner=(self.bot_owner_id is not None
                              and user_id == self.bot_owner_id),
                grants=self.grants.get(str(user_id), frozenset()),
            )
            if verdict.decision is not Decision.EXECUTE:
                return DispatchResult(
                    handled=True, decision=verdict.decision,
                    assessment=verdict, message=verdict.reason,
                )
            return DispatchResult(
                handled=True, decision=Decision.EXECUTE, assessment=verdict,
                spec=spec, args=pending.args, message=pending.summary,
            )

        return None
