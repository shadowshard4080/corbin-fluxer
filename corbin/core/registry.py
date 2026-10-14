"""The command registry.

One declarative descriptor per command, rendered into three front-ends:

* Discord application commands (generated -- Discord keeps its slash commands)
* Fluxer prefix commands (``!ban @x``) -- deterministic, always available
* The AI intent router (the registry becomes the classifier's vocabulary)

Handlers take a :class:`~corbin.core.context.CommandContext` and never see a
vendor object, so adding a platform does not touch a single handler.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence,
)

from .context import CommandContext
from .permissions import Perm

Handler = Callable[..., Awaitable[Any]]


class Risk(Enum):
    """How much damage a wrong invocation does.

    The tier sets the *base* confidence a natural-language invocation must
    clear.  Contextual signals then move that bar -- see ``core.risk``.
    """

    SAFE = "safe"                # read-only or trivially repeatable
    WRITE = "write"              # changes state, but reversible
    DESTRUCTIVE = "destructive"  # removes people or content


#: Base natural-language confidence required, per tier.
BASE_THRESHOLD: Mapping[Risk, float] = {
    Risk.SAFE: 0.60,
    Risk.WRITE: 0.80,
    Risk.DESTRUCTIVE: 0.90,
}


class ParamError(ValueError):
    """Raised when a supplied argument cannot be coerced or is invalid."""


def _norm(text: str) -> str:
    """Lowercase, punctuation-free, singularised token string."""
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w
                    for w in words)


def match_choice(value: str, choices: Sequence[str]) -> Optional[str]:
    """Map a natural phrase onto one of ``choices``.

    The classifier is a language model: asked for a category it answers
    "voice activity" or "message edits", not ``voice`` or ``message_edit``.
    Demanding the internal identifier made perfectly clear requests fail,
    so matching is done on normalised tokens instead.

    Returns None when nothing matches, or when the phrase is ambiguous
    between several choices -- guessing there would run the wrong command.
    """
    if value in choices:
        return value
    target = _norm(value)
    if not target:
        return None

    normalised = {c: _norm(c) for c in choices}
    for choice, norm in normalised.items():
        if norm == target:
            return choice

    # Every word of the choice appears in the phrase ("voice" in "voice
    # activity"), or vice versa ("message edits" for "message edit").
    hits = [c for c, norm in normalised.items()
            if norm and (set(norm.split()) <= set(target.split())
                         or set(target.split()) <= set(norm.split()))]
    return hits[0] if len(hits) == 1 else None


def humanise(value: str) -> str:
    return str(value).replace("_", " ").replace("-", " ")


@dataclass(frozen=True)
class Param:
    """A single command parameter."""

    name: str
    description: str
    kind: str = "string"          # string|integer|number|boolean|user|role|channel
    required: bool = True
    default: Any = None
    choices: Optional[Sequence[str]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    max_length: Optional[int] = None

    def coerce(self, raw: Any) -> Any:
        """Validate and convert a raw argument into its Python form."""
        if raw is None:
            if self.required:
                raise ParamError(f"'{self.name}' is required")
            return self.default

        if self.kind == "boolean":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("true", "yes", "y", "on", "1", "enable", "enabled"):
                return True
            if text in ("false", "no", "n", "off", "0", "disable", "disabled"):
                return False
            raise ParamError(f"'{self.name}' must be yes or no, got {raw!r}")

        if self.kind in ("integer", "number"):
            try:
                value = int(raw) if self.kind == "integer" else float(raw)
            except (TypeError, ValueError):
                raise ParamError(f"'{self.name}' must be a number, got {raw!r}")
            if self.min_value is not None and value < self.min_value:
                raise ParamError(
                    f"'{self.name}' must be at least {self.min_value:g}"
                )
            if self.max_value is not None and value > self.max_value:
                raise ParamError(
                    f"'{self.name}' must be at most {self.max_value:g}"
                )
            return value

        if self.kind in ("user", "role", "channel"):
            # Adapters resolve these to real objects; core only normalises the
            # reference. Accept a bare id, a mention token, or a name.
            text = str(raw).strip()
            match = re.fullmatch(r"<[@#]?[!&]?(\d+)>", text)
            return match.group(1) if match else text

        text = str(raw)
        if self.max_length is not None and len(text) > self.max_length:
            text = text[: self.max_length]
        if self.choices:
            matched = match_choice(text, self.choices)
            if matched is None:
                options = ", ".join(humanise(c) for c in self.choices)
                raise ParamError(
                    f"I'm not sure which {humanise(self.name)} you mean by "
                    f"\u201c{text}\u201d. I can do: {options}."
                )
            return matched
        return text

    def json_schema(self) -> dict[str, Any]:
        """JSON-schema fragment, used to brief the intent classifier."""
        type_map = {
            "string": "string", "integer": "integer", "number": "number",
            "boolean": "boolean", "user": "string", "role": "string",
            "channel": "string",
        }
        node: dict[str, Any] = {
            "type": type_map.get(self.kind, "string"),
            "description": self.description,
        }
        if self.choices:
            node["enum"] = list(self.choices)
        if self.kind == "user":
            node["description"] += " (user id, or the name as written)"
        return node


@dataclass
class CommandSpec:
    """A registered command."""

    name: str
    description: str
    handler: Handler
    params: Sequence[Param] = field(default_factory=tuple)
    permission: Perm = Perm.NONE
    risk: Risk = Risk.SAFE
    guild_only: bool = True
    owner_only: bool = False
    aliases: Sequence[str] = field(default_factory=tuple)
    #: Natural-language phrasings that should map here. Fed to the classifier
    #: so it learns this command's shape rather than guessing from the name.
    examples: Sequence[str] = field(default_factory=tuple)
    #: Exposed to the AI router at all? Some commands are prefix/slash only.
    ai_invocable: bool = True
    #: Callable(args) -> int estimating how many entities this affects.
    #: Large blast radius always forces confirmation regardless of confidence.
    blast_radius: Optional[Callable[[Mapping[str, Any]], int]] = None
    #: Callable(args) -> str giving a natural-language restatement for the
    #: confirmation prompt. Without it the prompt falls back to a mechanical
    #: "name (arg=value)" form, which reads like a stack trace to a user.
    confirm_phrase: Optional[Callable[[Mapping[str, Any]], str]] = None

    def describe_call(self, args: Mapping[str, Any]) -> Optional[str]:
        if self.confirm_phrase is None:
            return None
        try:
            return self.confirm_phrase(args)
        except Exception:
            return None

    def param(self, name: str) -> Optional[Param]:
        return next((p for p in self.params if p.name == name), None)

    @property
    def required_params(self) -> Sequence[Param]:
        return tuple(p for p in self.params if p.required)

    def coerce_args(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Coerce every declared param, raising ParamError on the first fault."""
        out: dict[str, Any] = {}
        for p in self.params:
            out[p.name] = p.coerce(raw.get(p.name))
        unknown = set(raw) - {p.name for p in self.params}
        if unknown:
            raise ParamError(f"unknown argument(s): {', '.join(sorted(unknown))}")
        return out

    def estimate_blast_radius(self, args: Mapping[str, Any]) -> int:
        if self.blast_radius is None:
            return 1
        try:
            return max(1, int(self.blast_radius(args)))
        except Exception:
            return 1

    def json_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {p.name: p.json_schema() for p in self.params},
                "required": [p.name for p in self.params if p.required],
            },
        }

    def usage(self, prefix: str = "!") -> str:
        parts = [f"{prefix}{self.name}"]
        for p in self.params:
            parts.append(f"<{p.name}>" if p.required else f"[{p.name}]")
        return " ".join(parts)


class Registry:
    """Name -> CommandSpec, with alias resolution."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(self, spec: CommandSpec) -> CommandSpec:
        key = self._normalise(spec.name)
        if key in self._commands:
            raise ValueError(f"duplicate command name: {spec.name}")
        for alias in spec.aliases:
            akey = self._normalise(alias)
            if akey in self._aliases or akey in self._commands:
                raise ValueError(f"duplicate alias: {alias}")
            self._aliases[akey] = key
        self._commands[key] = spec
        return spec

    @staticmethod
    def _normalise(name: str) -> str:
        # Commands register as kebab-case, but people (and the classifier)
        # write snake_case at least as often. Treat them as the same name.
        return name.lower().lstrip("/!").replace("_", "-").strip()

    def get(self, name: str) -> Optional[CommandSpec]:
        if not name:
            return None
        key = self._normalise(name)
        if key in self._commands:
            return self._commands[key]
        target = self._aliases.get(key)
        return self._commands.get(target) if target else None

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._commands)

    def all(self) -> Sequence[CommandSpec]:
        return tuple(self._commands[k] for k in sorted(self._commands))

    def ai_invocable(self) -> Sequence[CommandSpec]:
        return tuple(c for c in self.all() if c.ai_invocable)

    def command(self, **kwargs: Any) -> Callable[[Handler], Handler]:
        """Decorator form: ``@registry.command(name=..., risk=...)``."""

        def decorator(fn: Handler) -> Handler:
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"command handler {fn.__name__} must be async")
            name = kwargs.pop("name", fn.__name__).replace("_", "-")
            description = kwargs.pop(
                "description", (inspect.getdoc(fn) or "").split("\n")[0]
            )
            if not description:
                raise ValueError(f"command {name} needs a description")
            self.register(
                CommandSpec(
                    name=name, description=description, handler=fn, **kwargs
                )
            )
            return fn

        return decorator


#: The process-wide registry. Command modules import and decorate against it.
registry = Registry()
command = registry.command
