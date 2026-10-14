"""Platform-neutral value types and the command invocation context.

Nothing in ``corbin.core`` may import discord.py or fluxer.py.  Adapters in
``corbin.platforms`` translate vendor objects into these shapes on the way in
and render :class:`Response` objects on the way out.

This is the seam that lets one command registry serve Discord slash commands,
Fluxer prefix commands, and the AI intent router without the handlers knowing
which one invoked them.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence

from .permissions import Perm


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Platform(str, Enum):
    DISCORD = "discord"
    FLUXER = "fluxer"


class Surface(str, Enum):
    """How a command was invoked. Feeds risk scoring and confirmation UX."""

    SLASH = "slash"          # Discord application command -- structured, precise
    PREFIX = "prefix"        # !command -- structured, explicit
    AI = "ai"                # natural language, routed by the intent classifier


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    permissions: int = 0
    position: int = 0


@dataclass(frozen=True)
class User:
    id: str
    name: str
    display_name: str = ""
    is_bot: bool = False
    created_at: Optional[_dt.datetime] = None

    def __post_init__(self) -> None:
        if not self.display_name:
            object.__setattr__(self, "display_name", self.name)

    @property
    def account_age(self) -> Optional[_dt.timedelta]:
        if self.created_at is None:
            return None
        return _utcnow() - self.created_at


@dataclass(frozen=True)
class Member:
    """A user within a guild."""

    user: User
    guild_id: str
    roles: Sequence[Role] = field(default_factory=tuple)
    joined_at: Optional[_dt.datetime] = None
    is_guild_owner: bool = False

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def display_name(self) -> str:
        return self.user.display_name

    @property
    def is_bot(self) -> bool:
        return self.user.is_bot

    @property
    def tenure(self) -> Optional[_dt.timedelta]:
        """How long this member has been in the guild."""
        if self.joined_at is None:
            return None
        return _utcnow() - self.joined_at

    @property
    def account_age(self) -> Optional[_dt.timedelta]:
        return self.user.account_age

    @property
    def top_role_position(self) -> int:
        return max((r.position for r in self.roles), default=0)

    @property
    def permissions(self) -> Perm:
        from .permissions import resolve

        return resolve(
            (r.permissions for r in self.roles),
            is_guild_owner=self.is_guild_owner,
        )


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    guild_id: Optional[str] = None
    is_dm: bool = False


@dataclass(frozen=True)
class Guild:
    id: str
    name: str
    owner_id: Optional[str] = None


@dataclass(frozen=True)
class Attachment:
    url: str
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None
    is_spoiler: bool = False


@dataclass(frozen=True)
class Message:
    id: str
    content: str
    author: User
    channel: Channel
    guild_id: Optional[str] = None
    attachments: Sequence[Attachment] = field(default_factory=tuple)
    reference_id: Optional[str] = None
    created_at: Optional[_dt.datetime] = None


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

#: Verified empirically against Fluxer 2026-09-23: these are hard 400s,
#: identical to Discord's. Exceeding any of them rejects the whole message.
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_MAX_FIELDS = 25


def clip(text: Optional[str], limit: int) -> Optional[str]:
    """Trim to ``limit``, marking the cut so truncation is visible."""
    if text is None:
        return None
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\u2026"


@dataclass
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass
class Embed:
    """Neutral embed. Adapters render to discord.Embed / fluxer.Embed."""

    title: Optional[str] = None
    description: Optional[str] = None
    colour: Optional[int] = None
    url: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    footer: Optional[str] = None
    author: Optional[str] = None
    timestamp: Optional[_dt.datetime] = None
    fields: list[EmbedField] = field(default_factory=list)

    def add_field(self, name: str, value: str, inline: bool = False) -> "Embed":
        self.fields.append(EmbedField(name=name, value=value, inline=inline))
        return self

    def add_list_field(self, name: str, lines: Sequence[str],
                       inline: bool = False) -> "Embed":
        """Add ``lines`` as one or more fields, splitting at the value limit.

        Truncating would silently drop somebody's data (this is what broke
        /my_memory with 21 facts), so overflow continues into "name (2)",
        "name (3)" rather than being cut.
        """
        part, length, index = [], 0, 1
        for line in lines:
            line = str(line)
            if len(line) > EMBED_FIELD_VALUE_LIMIT:
                line = clip(line, EMBED_FIELD_VALUE_LIMIT)
            if part and length + len(line) + 1 > EMBED_FIELD_VALUE_LIMIT:
                self.add_field(name if index == 1 else f"{name} ({index})",
                               "\n".join(part), inline)
                part, length, index = [], 0, index + 1
            part.append(line)
            length += len(line) + 1
        if part:
            self.add_field(name if index == 1 else f"{name} ({index})",
                           "\n".join(part), inline)
        return self


@dataclass
class Response:
    """What a command handler returns.

    ``private`` is the neutral form of Discord's ``ephemeral=True``.  Fluxer
    has no ephemeral messages, so its adapter degrades to a DM and then to
    post-and-delete -- see ``platforms/fluxer/adapter.py``.
    """

    content: Optional[str] = None
    embed: Optional[Embed] = None
    embeds: Sequence[Embed] = field(default_factory=tuple)
    private: bool = False
    #: Seconds after which the adapter should delete the message, if it can.
    delete_after: Optional[float] = None
    #: Posted in-channel when a ``private`` response is delivered by DM, so
    #: the user is not left looking at a typing indicator that went nowhere.
    #: Set to "" to suppress. Only used when the DM actually succeeds; the
    #: post-and-delete fallback needs no pointer, the content is right there.
    private_notice: Optional[str] = "Sent that to your DMs."
    #: Files to upload, as (filename, bytes).
    files: Sequence[tuple[str, bytes]] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.content is None and self.embed is None and not self.embeds:
            raise ValueError("Response must carry content, an embed, or embeds")


# --------------------------------------------------------------------------
# Invocation context
# --------------------------------------------------------------------------

class Responder(Protocol):
    """Platform-supplied callback set used by :class:`CommandContext`."""

    async def send(self, response: Response) -> Optional[str]: ...
    async def fetch_member(self, guild_id: str, user_id: str) -> Optional[Member]: ...


@dataclass
class CommandContext:
    """Everything a command handler needs, with no vendor types in sight."""

    platform: Platform
    surface: Surface
    invoker: Member
    channel: Channel
    guild: Optional[Guild]
    responder: Responder
    #: The message that triggered this, when there was one (AI/prefix surfaces).
    message: Optional[Message] = None
    #: Raw parsed arguments, already coerced by the registry.
    args: Mapping[str, Any] = field(default_factory=dict)
    #: Free-form bag for adapters to stash vendor handles on.
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def guild_id(self) -> Optional[str]:
        return self.guild.id if self.guild else None

    @property
    def is_ai(self) -> bool:
        return self.surface is Surface.AI

    async def respond(
        self,
        content: Optional[str] = None,
        *,
        embed: Optional[Embed] = None,
        private: bool = False,
        delete_after: Optional[float] = None,
    ) -> Optional[str]:
        return await self.responder.send(
            Response(
                content=content,
                embed=embed,
                private=private,
                delete_after=delete_after,
            )
        )

    async def respond_private(
        self, content: Optional[str] = None, *, embed: Optional[Embed] = None
    ) -> Optional[str]:
        return await self.respond(content, embed=embed, private=True)
