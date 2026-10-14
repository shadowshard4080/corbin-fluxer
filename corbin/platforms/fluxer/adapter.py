"""Fluxer adapter.

Every unverified assumption about fluxer.py lives in this file, on purpose.
The library is young (47 stars, 55 commits at time of writing) and its exact
attribute names have not been checked against a running server, so:

* attribute access goes through :func:`_attr`, which tries the plausible
  spellings rather than hard-coding one;
* :func:`probe` verifies the bindings at boot and reports what is missing.

This mirrors the parent repo's ``tools/check_xai_sdk.py`` rule: a dependency
that degrades silently is worse than one that crashes, so we make it loud.

PHASE-0 UNKNOWNS, all flagged inline:
  - reaction gateway events (model exists; dispatch unconfirmed)
  - DM creation for the ephemeral fallback
  - whether voice accepts a *stream* or only local files  <-- blocks radio
  - audit-log events (assumed absent)
"""

from __future__ import annotations

import asyncio as _asyncio
import datetime as _dt
import logging
import re as _re
import time as _time
from typing import Any, Iterable, Mapping, Optional, Sequence

from ...core.emoji import SHORTHAND as EMOJI_SHORTHAND
from ...core.emoji import render as render_bot_emoji
from ...core.context import (EMBED_DESCRIPTION_LIMIT, EMBED_FIELD_NAME_LIMIT,
                             EMBED_FIELD_VALUE_LIMIT, EMBED_MAX_FIELDS,
                             EMBED_TITLE_LIMIT, Attachment, Channel, Embed,
                             Guild, Member, Message, Response, Role, User,
                             clip)
from ...core.permissions import Perm
from ...core.risk import TargetProfile
from ...core.stores import ActivityStore, WarningStore
from ..base import FLUXER_CAPABILITIES, Capabilities

logger = logging.getLogger("corbin.fluxer")

_MISSING = object()

#: Web client base, used to link a DM back to where it was asked. The SDK
#: exposes no jump_url helper, so this is constructed. PHASE-0-LIVE: the URL
#: shape mirrors Discord's and the route resolves, but a SPA returns 200 for
#: anything -- worth eyeballing once in the app.
WEB_BASE = "https://web.fluxer.app"


def channel_url(guild_id: Optional[str], channel_id: str,
                message_id: Optional[str] = None) -> Optional[str]:
    if not guild_id or not channel_id:
        return None
    url = f"{WEB_BASE}/channels/{guild_id}/{channel_id}"
    return f"{url}/{message_id}" if message_id else url


#: How long to stop trying typing on a channel after it errors.
TYPING_COOLDOWN = 15 * 60.0
#: Refresh interval; the server-side indicator lapses at ~10s.
TYPING_REFRESH = 8.0
#: How long a guild's role table is trusted.
ROLE_CACHE_TTL = 300.0
#: Pages of 100 when indexing member names. Enough for a guild of a few
#: thousand without walking a huge server on every cache miss.
MEMBER_INDEX_PAGES = 20
#: Message ids per bulk-delete request.
BULK_DELETE_CHUNK = 100
#: Messages per history request.
MESSAGE_PAGE = 50
#: `<@id>`, `<@!id>`, `<@&id>`, `<#id>` -- a mention already holds the id.
_MENTION_ID = _re.compile(r"^<[@#][!&]?(\d+)>$")
#: Channels change more often than roles, so a shorter window.
CHANNEL_INDEX_TTL = 120.0
#: A message send slower than this is logged; slower than the second is
#: abandoned. Fluxer normally answers in well under a second.
SEND_SLOW_WARN = 10.0
SEND_TIMEOUT = 60.0
TYPING_TIMEOUT = 5.0


class _Typing:
    """Keeps a typing indicator alive for the duration of a block."""

    def __init__(self, adapter: "FluxerAdapter", channel_id: str) -> None:
        self._adapter = adapter
        self._channel_id = str(channel_id)
        self._task: Optional[_asyncio.Task] = None

    async def __aenter__(self) -> "_Typing":
        ok = await self._adapter.trigger_typing(self._channel_id)
        if ok:
            self._task = _asyncio.create_task(self._loop())
        return self

    async def _loop(self) -> None:
        try:
            while True:
                await _asyncio.sleep(TYPING_REFRESH)
                if not await self._adapter.trigger_typing(self._channel_id):
                    return
        except _asyncio.CancelledError:
            pass

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (_asyncio.CancelledError, Exception):
                pass


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """First attribute (or mapping key) of ``obj`` among ``names``.

    Handles both objects and raw dicts, because fluxer 0.4.2 is inconsistent:
    ``Message.attachments`` holds parsed ``Attachment`` objects but
    ``Message.embeds`` is ``list[dict]``. Reading one with ``getattr`` alone
    silently yields empty values rather than raising, which is exactly the
    kind of quiet data loss that is hard to spot in production.
    """
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        value = getattr(obj, name, _MISSING)
        if value is not _MISSING:
            return value
    if names:
        logger.debug("no attribute among %s on %s", names, type(obj).__name__)
    return default


def _as_dt(value: Any) -> Optional[_dt.datetime]:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(value, _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
    return None


# --------------------------------------------------------------------------
# Vendor -> neutral conversion
# --------------------------------------------------------------------------

def to_user(raw: Any) -> User:
    return User(
        id=str(_attr(raw, "id", "user_id", default="")),
        name=str(_attr(raw, "username", "name", default="unknown")),
        display_name=str(_attr(raw, "display_name", "global_name", "nickname",
                               "username", "name", default="") or ""),
        is_bot=bool(_attr(raw, "bot", "is_bot", default=False)),
        created_at=_as_dt(_attr(raw, "created_at", "createdAt")),
    )


def to_role(raw: Any) -> Role:
    return Role(
        id=str(_attr(raw, "id", "role_id", default="")),
        name=str(_attr(raw, "name", default="")),
        permissions=int(_attr(raw, "permissions", "permission", default=0) or 0),
        position=int(_attr(raw, "position", "rank", default=0) or 0),
    )


def to_member(raw: Any, *, guild_id: str,
              guild_owner_id: Optional[str] = None,
              role_index: Optional[Mapping[str, Role]] = None) -> Member:
    """Build a neutral Member.

    fluxer 0.4.2's ``GuildMember.roles`` is ``list[int]`` -- bare role IDs,
    not Role objects. Without ``role_index`` to resolve them the member has
    no permission bits at all, so every non-owner is denied everything while
    the guild owner still works (owner short-circuits). That asymmetry is
    exactly the kind of bug that survives testing by one admin.
    """
    user_obj = _attr(raw, "user", default=raw)
    user = to_user(user_obj)

    raw_roles = _attr(raw, "roles", default=()) or ()
    roles: list[Role] = []
    for entry in raw_roles:
        if isinstance(entry, (str, int)):
            resolved = (role_index or {}).get(str(entry))
            roles.append(resolved if resolved
                         else Role(id=str(entry), name="", permissions=0))
        else:
            roles.append(to_role(entry))
    roles = tuple(roles)
    return Member(
        user=user,
        guild_id=str(guild_id),
        roles=roles,
        joined_at=_as_dt(_attr(raw, "joined_at", "joinedAt")),
        is_guild_owner=bool(guild_owner_id and user.id == str(guild_owner_id)),
    )


def to_channel(raw: Any) -> Channel:
    gid = _attr(raw, "guild_id", "guildId")
    return Channel(
        id=str(_attr(raw, "id", "channel_id", default="")),
        name=str(_attr(raw, "name", default="") or ""),
        guild_id=str(gid) if gid else None,
        # Do NOT infer "DM" from a missing guild_id -- an uncached channel
        # has no guild_id and is not a DM.
        is_dm=bool(_attr(raw, "is_dm", "dm", default=False)),
    )


def to_attachment(raw: Any) -> Attachment:
    filename = str(_attr(raw, "filename", "name", default="file"))
    return Attachment(
        url=str(_attr(raw, "url", "proxy_url", default="")),
        filename=filename,
        content_type=_attr(raw, "content_type", "contentType"),
        size=_attr(raw, "size"),
        is_spoiler=filename.startswith("SPOILER_"),
    )


def _message_guild_id(raw: Any, channel: Any) -> Optional[str]:
    """Resolve a message's guild id, tolerating an uncached channel.

    fluxer 0.4.2 computes ``Message.guild_id`` as ``self._guild.id if
    self._guild else None``, and ``_parse_message`` only populates ``_guild``
    when the channel is already in the client cache. On a cache miss every
    message therefore looks like a DM -- which silently files per-guild
    settings under the literal key "None". Check the other sources too.
    """
    for candidate in (
        _attr(raw, "guild_id"),
        _attr(_attr(raw, "_guild"), "id"),
        _attr(channel, "guild_id"),
        _attr(_attr(channel, "_guild"), "id"),
    ):
        if candidate:
            return str(candidate)
    return None


def to_message(raw: Any) -> Message:
    channel = _attr(raw, "channel")
    reference = _attr(raw, "reference", "referenced_message", "reply_to")
    return Message(
        id=str(_attr(raw, "id", default="")),
        content=str(_attr(raw, "content", "text", default="") or ""),
        author=to_user(_attr(raw, "author", "user")),
        channel=to_channel(channel) if channel is not None
        else Channel(id=str(_attr(raw, "channel_id", default="")), name=""),
        guild_id=_message_guild_id(raw, channel),
        attachments=tuple(to_attachment(a)
                          for a in (_attr(raw, "attachments", default=()) or ())),
        reference_id=(str(_attr(reference, "id", "message_id", default=""))
                      or None) if reference is not None else None,
        created_at=_as_dt(_attr(raw, "created_at", "timestamp")),
    )


# --------------------------------------------------------------------------
# Neutral -> vendor rendering
# --------------------------------------------------------------------------

def render_embed(embed: Embed, embed_cls: Any) -> Any:
    """Build a fluxer.Embed from our neutral one.

    Only ``title``/``description``/``colour`` are assumed to be constructor
    kwargs; everything else is set defensively, because an embed that loses
    its footer is survivable and one that raises is not.
    """
    # Clip at the boundary. These limits are hard 400s on Fluxer and one
    # over-long field rejects the ENTIRE message, so this is the last line
    # of defence regardless of what a command built.
    built = embed_cls(
        title=clip(embed.title, EMBED_TITLE_LIMIT),
        description=clip(embed.description, EMBED_DESCRIPTION_LIMIT),
        color=embed.colour if embed.colour is not None else None,
    )
    for field in embed.fields[:EMBED_MAX_FIELDS]:
        adder = getattr(built, "add_field", None)
        if not adder:
            break
        name = clip(field.name, EMBED_FIELD_NAME_LIMIT) or "\u200b"
        # An empty value is also a 400.
        value = clip(field.value, EMBED_FIELD_VALUE_LIMIT) or "\u200b"
        try:
            adder(name=name, value=value, inline=field.inline)
        except TypeError:
            adder(name, value)
    for attr, value, setter in (
        ("url", embed.url, "url"),
        ("image", embed.image_url, "set_image"),
        ("thumbnail", embed.thumbnail_url, "set_thumbnail"),
        ("footer", embed.footer, "set_footer"),
    ):
        if not value:
            continue
        method = getattr(built, setter, None)
        if callable(method):
            try:
                method(url=value) if setter in ("set_image", "set_thumbnail") \
                    else method(text=value)
                continue
            except TypeError:
                try:
                    method(value)
                    continue
                except Exception:
                    pass
        try:
            setattr(built, attr, value)
        except Exception:
            logger.debug("could not set embed.%s", attr)
    return built


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------

class FluxerAdapter:
    """Implements :class:`~corbin.platforms.base.PlatformAdapter` over fluxer.py."""

    name = "fluxer"

    def __init__(self, client: Any, *, embed_cls: Any = None,
                 file_cls: Any = None, data_root: str = ".",
                 capabilities: Capabilities = FLUXER_CAPABILITIES) -> None:
        self.client = client
        #: Corbin's custom emoji, resolved at boot; see set_emoji().
        self.emoji_id: Optional[str] = None
        self.emoji_animated = False
        self.embed_cls = embed_cls
        self.file_cls = file_cls
        self._caps = capabilities
        #: Read-only view for risk priors. The command layer writes through
        #: its own WarningStore over the same file, so a warning issued now
        #: lowers the confirmation bar on the next moderation request.
        self.warnings = WarningStore(data_root)
        self.activity = ActivityStore(data_root)
        #: Channels where a DM fallback already failed, so we stop retrying.
        self._dm_blocked: set[str] = set()
        #: Channel -> monotonic time until which typing is disabled.
        self._typing_disabled_until: dict[str, float] = {}
        #: guild id -> (expiry, {role_id: Role}); roles change rarely, and
        #: refetching them on every message would double our API traffic.
        self._role_cache: dict[str, tuple[float, dict[str, Role]]] = {}
        self._member_index_cache: dict[str, tuple[float, dict[str, str]]] = {}
        #: guild id -> (expiry, {name: channel id})
        self._channel_index_cache: dict[str, tuple[float, dict]] = {}

    def capabilities(self) -> Capabilities:
        return self._caps

    # -- typing -----------------------------------------------------------

    async def trigger_typing(self, channel_id: str) -> bool:
        """Fire one typing indicator. Never raises.

        Mirrors the Discord build's ``_safe_typing`` rule: the typing route is
        rate-limited per channel, and an unhandled 429 there used to abort the
        whole message handler. A failure trips a 15-minute per-channel circuit
        breaker so we stop keeping the rate-limit scope warm.
        """
        key = str(channel_id)
        until = self._typing_disabled_until.get(key, 0.0)
        if until and _time.monotonic() < until:
            return False
        try:
            channel = await self._channel(key)
            if channel is None:
                return False
            fn = getattr(channel, "trigger_typing", None)
            if fn is None:
                return False
            # Typing is cosmetic; it must never be what a reply waits on.
            await _asyncio.wait_for(fn(), TYPING_TIMEOUT)
            return True
        except Exception as exc:
            self._typing_disabled_until[key] = (
                _time.monotonic() + TYPING_COOLDOWN)
            logger.info("typing disabled for %s for %.0fs after %s",
                        key, TYPING_COOLDOWN, type(exc).__name__)
            return False

    def typing(self, channel_id: str) -> "_Typing":
        """Async context manager that keeps the indicator alive.

        The indicator expires server-side after ~10s, so it is refreshed on a
        timer for as long as the block runs -- scoped tightly around the model
        call, never around the whole handler.
        """
        return _Typing(self, channel_id)

    # -- sending ----------------------------------------------------------

    async def send(self, channel_id: str, response: Response) -> Optional[str]:
        """Send, honouring ``private`` as best the platform allows.

        Fluxer has no ephemeral messages, so a private response degrades:
        DM first, then post-and-delete in channel. Never silently public.
        """
        if response.private and self._caps.dms:
            author_id = getattr(response, "_target_user_id", None)
            if author_id and str(author_id) not in self._dm_blocked:
                # Corbin's DMs are not a conversation surface, so every DM
                # points back at the channel it came from.
                outgoing = await self._with_back_link(response, channel_id)
                sent = await self.send_dm(str(author_id), outgoing)
                if sent is not None:
                    # Say so in channel. Without this a private reply looks
                    # like the bot typed and then did nothing.
                    notice = response.private_notice
                    if notice:
                        try:
                            channel = await self._channel(channel_id)
                            if channel is not None:
                                await channel.send(content=notice)
                        except Exception:
                            logger.debug("private notice failed",
                                         exc_info=True)
                    return sent
                self._dm_blocked.add(str(author_id))
                logger.info("DM fallback failed for %s; posting with cleanup",
                            author_id)

        channel = await self._channel(channel_id)
        if channel is None:
            logger.warning("cannot resolve channel %s", channel_id)
            return None

        kwargs = self._payload(response)
        message = await self._watched(channel.send(**kwargs),
                                      f"send to {channel_id}")
        if message is None:
            return None
        message_id = str(_attr(message, "id", default="")) or None

        # Emulate ephemerality when we had to post publicly.
        delete_after = response.delete_after
        if response.private and delete_after is None:
            delete_after = 60.0
        if delete_after and message_id:
            import asyncio

            async def _cleanup() -> None:
                await asyncio.sleep(delete_after)
                await self.delete_message(channel_id, message_id)

            asyncio.create_task(_cleanup())
        return message_id

    async def _watched(self, coro: Any, what: str) -> Any:
        """Await an API call that must not hang silently.

        Warns at ``SEND_SLOW_WARN`` and gives up at ``SEND_TIMEOUT``,
        returning None. "Typed, then never answered" was a send stuck in
        the SDK with nothing in the log; now it is always one of three
        lines: sent, slow, or timed out. Safe to cancel only because
        ``http_guard`` makes a cancelled request release its bucket lock.
        """
        import asyncio

        task = asyncio.ensure_future(coro)
        try:
            return await asyncio.wait_for(asyncio.shield(task),
                                          SEND_SLOW_WARN)
        except asyncio.TimeoutError:
            logger.warning("%s still waiting on Fluxer after %.0fs (API slow "
                           "or rate limited)", what, SEND_SLOW_WARN)
        try:
            return await asyncio.wait_for(task,
                                          SEND_TIMEOUT - SEND_SLOW_WARN)
        except asyncio.TimeoutError:
            logger.error("%s gave up after %.0fs; nothing was posted", what,
                         SEND_TIMEOUT)
            return None

    def _rest(self) -> Any:
        """The SDK's REST client.

        It is ``_http``, not ``http`` -- getting that wrong once already
        produced "no HTTP client" five times in a row and looked like a
        rate limit. Raises rather than returning None: every caller needs
        it, and a None here turns into a plausible-looking failure
        message that sends the next hour in the wrong direction.
        """
        http = getattr(self.client, "_http", None)
        if http is None:
            raise RuntimeError(
                "fluxer.py Client has no _http — the SDK's REST client "
                "moved; adapter needs updating")
        return http

    async def guild_expressions(self, guild_id: str) -> set[str]:
        """Existing emoji/sticker names, as ``"<kind>:<name>"`` lowercased.

        Used to make a copy re-runnable. Names are the only key that can
        match across platforms -- each mints its own ids.
        """
        names: set[str] = set()
        http = self._rest()
        for kind, method in (("emoji", "get_guild_emojis"),
                             ("sticker", "get_guild_stickers")):
            fetch = getattr(http, method, None)
            if fetch is None:
                raise RuntimeError(f"this fluxer.py has no {method}")
            try:
                for item in await fetch(guild_id) or []:
                    name = str((item or {}).get("name") or "").strip()
                    if name:
                        names.add(f"{kind}:{name.lower()}")
            except Exception as exc:
                # Raised, not swallowed. An empty set here reads as "the
                # server has none", which would re-upload everything.
                raise RuntimeError(
                    f"could not list {kind}s: {type(exc).__name__}") from exc
        return names

    async def create_expression(self, guild_id: str, *, kind: str,
                                name: str, image: bytes) -> Optional[str]:
        """Create one emoji or sticker. Returns an error string, or None."""
        http = self._rest()
        method = ("create_guild_emoji" if kind == "emoji"
                  else "create_guild_sticker")
        create = getattr(http, method, None)
        if create is None:
            return f"this fluxer.py has no {method}"
        try:
            await create(guild_id, name=name, image=image,
                         reason="copied from Discord")
            return None
        except Exception as exc:
            # Surfaced to the caller rather than raised: one rejected
            # emoji must not abandon the other ninety.
            return f"{type(exc).__name__}: {str(exc)[:120]}"

    async def set_emoji(self, emoji_id: Optional[str],
                        guild_ids: Sequence[str] = ()) -> bool:
        """Configure the bot's own emoji, resolving animated from a guild.

        Whether it is animated decides between ``<:`` and ``<a:``, and
        the wrong prefix renders as literal text. Looked up rather than
        configured so it cannot drift from the actual emoji: the first of
        ``guild_ids`` that has it wins.
        """
        self.emoji_id = str(emoji_id) if emoji_id else None
        self.emoji_animated = False
        if not self.emoji_id or not guild_ids:
            return bool(self.emoji_id)
        fetch = getattr(self._rest(), "get_guild_emojis", None)
        for guild_id in guild_ids:
            try:
                for item in (await fetch(str(guild_id)) if fetch else []) \
                        or []:
                    if str((item or {}).get("id")) == self.emoji_id:
                        self.emoji_animated = bool(item.get("animated"))
                        logger.info("bot emoji %s resolved in %s (%s)",
                                    self.emoji_id, guild_id,
                                    "animated" if self.emoji_animated
                                    else "static")
                        return True
            except Exception:
                logger.debug("emoji lookup failed in %s", guild_id,
                             exc_info=True)
        logger.warning("bot emoji %s not found in any of %d server(s); %s "
                       "will post as plain text", self.emoji_id,
                       len(guild_ids), EMOJI_SHORTHAND)
        return False

    async def message_author(self, channel_id: str,
                             message_id: str) -> Optional[str]:
        """The author id of one message, or None if it cannot be read."""
        try:
            fetch = getattr(self._rest(), "get_message", None)
            if fetch is None:
                return None
            data = await fetch(str(channel_id), str(message_id)) or {}
        except Exception:
            logger.debug("could not fetch message %s", message_id,
                         exc_info=True)
            return None
        author = (data.get("author") or {}) if isinstance(data, dict) else {}
        found = author.get("id")
        return str(found) if found else None

    def channel_link(self, guild_id: Optional[str],
                     channel_id: Optional[str]) -> Optional[str]:
        """Public URL for a channel, or None if either id is missing.

        Exists so platform-neutral command modules can offer a jump-back
        link without importing anything from this package.
        """
        return channel_url(guild_id, str(channel_id or "")) \
            if channel_id else None

    async def _with_back_link(self, response: Response,
                              channel_id: str) -> Response:
        """Copy of ``response`` with a link back to the source channel.

        Returned as a copy, never mutated in place: the caller may reuse the
        Response, and a link appended twice reads as a bug.
        """
        import copy

        gid = await self.resolve_guild_id(channel_id)
        url = channel_url(gid, channel_id)
        if not url:
            return response

        out = copy.deepcopy(response)
        target = out.embed or (out.embeds[0] if out.embeds else None)
        if target is not None:
            # A field renders the link clickable; a footer does not.
            target.add_field("Asked in", f"[Jump back to the channel]({url})")
        else:
            suffix = f"\n\n[Back to the channel]({url})"
            out.content = f"{out.content or ''}{suffix}".strip()
        return out

    async def send_dm(self, user_id: str, response: Response) -> Optional[str]:
        ref = await self.send_dm_ref(user_id, response)
        return ref[1] if ref else None

    async def send_dm_ref(self, user_id: str,
                          response: Response) -> Optional[tuple[str, str]]:
        """Send a DM; return ``(dm_channel_id, message_id)`` or None.

        The channel id is what ``edit_message`` needs to edit it later --
        the message id alone cannot address a DM.
        """
        try:
            user = await self._user(user_id)
            if user is None:
                return None
            # PHASE-0: confirm the DM entry point. Try the common spellings.
            sender = (getattr(user, "send", None)
                      or getattr(user, "send_message", None))
            if sender is None:
                opener = (getattr(user, "create_dm", None)
                          or getattr(self.client, "create_dm", None))
                if opener is None:
                    return None
                channel = await opener(user) if opener is not getattr(
                    user, "create_dm", None) else await opener()
                sender = getattr(channel, "send", None)
            if sender is None:
                return None
            message = await sender(**self._payload(response))
            message_id = str(_attr(message, "id", default="") or "")
            if not message_id:
                return None
            return str(_attr(message, "channel_id", default="") or ""), \
                message_id
        except Exception:
            logger.info("DM to %s failed", user_id, exc_info=True)
            return None

    def _payload(self, response: Response) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if response.content:
            # The one place every outgoing message passes through, so
            # :corbin: renders wherever it was written -- replies,
            # presence, greetings, DMs, and anything added later.
            payload["content"] = render_bot_emoji(
                response.content, self.emoji_id,
                animated=self.emoji_animated)
        if response.files and self.file_cls is not None:
            built = [self.file_cls(data, filename=name)
                     for name, data in response.files]
            payload["file" if len(built) == 1 else "files"] = (
                built[0] if len(built) == 1 else built)
        embeds = list(response.embeds) + (
            [response.embed] if response.embed else []
        )
        if embeds and self.embed_cls is not None:
            rendered = [render_embed(e, self.embed_cls) for e in embeds]
            payload["embed" if len(rendered) == 1 else "embeds"] = (
                rendered[0] if len(rendered) == 1 else rendered
            )
        elif embeds:
            # No embed class wired: degrade to text rather than drop content.
            payload["content"] = "\n\n".join(
                filter(None, [payload.get("content"),
                              *(self._embed_as_text(e) for e in embeds)])
            )
        return payload

    @staticmethod
    def _embed_as_text(embed: Embed) -> str:
        lines = []
        if embed.title:
            lines.append(f"**{embed.title}**")
        if embed.description:
            lines.append(embed.description)
        for field in embed.fields:
            lines.append(f"**{field.name}**\n{field.value}")
        if embed.footer:
            lines.append(f"_{embed.footer}_")
        return "\n".join(lines)

    async def edit_message(self, channel_id: str, message_id: str,
                           response: Response) -> bool:
        """Edit a previously sent message.

        Embeds go straight to REST: fluxer.py's ``Message.edit`` forwards
        ``embed=`` to ``HTTPClient.edit_message``, which only accepts
        ``embeds=``, so every embed edit raised TypeError and was swallowed
        below. Text edits keep the original path.
        """
        payload = self._payload(response)
        if "embed" in payload or "embeds" in payload:
            try:
                embeds = payload.get("embeds") or [payload["embed"]]
                # _watched returns None on timeout; that is not an edit.
                return await self._watched(self._rest().edit_message(
                    str(channel_id), str(message_id),
                    content=payload.get("content"),
                    embeds=[e.to_dict() if hasattr(e, "to_dict") else e
                            for e in embeds]), "edit message") is not None
            except Exception:
                logger.info("embed edit failed for %s/%s", channel_id,
                            message_id, exc_info=True)
                return False
        try:
            channel = await self._channel(channel_id)
            if channel is None:
                return False
            fetch = getattr(channel, "fetch_message", None)
            if fetch is None:
                return False
            message = await fetch(message_id)
            editor = getattr(message, "edit", None)
            if editor is None:
                return False
            await editor(**self._payload(response))
            return True
        except Exception:
            logger.debug("edit failed for %s/%s", channel_id, message_id,
                         exc_info=True)
            return False

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        try:
            channel = await self._channel(channel_id)
            if channel is None:
                logger.warning("delete: channel %s not found", channel_id)
                return
            fetch = (getattr(channel, "fetch_message", None)
                     or getattr(channel, "get_message", None))
            if fetch is None:
                logger.warning("delete: channel %s has no message fetch",
                               channel_id)
                return
            message = await fetch(message_id)
            deleter = getattr(message, "delete", None)
            if deleter is None:
                logger.warning("delete: message %s has no delete()",
                               message_id)
                return
            await deleter()
        except Exception:
            # WARNING, not debug: a failed delete leaves a stale progress
            # note in the channel, which reads as Corbin going silent.
            logger.warning("delete failed for %s/%s", channel_id, message_id,
                           exc_info=True)

    async def purge(self, channel_id: str, count: int) -> int:
        """Delete up to ``count`` recent messages. Returns how many went.

        Collect ids first, then delete them. ``Channel.delete_messages``
        takes a **list of message ids** -- it is not a "purge the last
        N" helper, and calling it with a count raised ``'int' object is
        not iterable`` from inside the SDK.

        Bulk delete is attempted in chunks and falls back to deleting
        one at a time, because platforms in this family refuse to bulk
        delete messages older than about two weeks. The count returned
        is what actually went, never what was asked for.
        """
        http = self._rest()
        ids = await self._recent_message_ids(channel_id, count)
        if not ids:
            return 0

        deleted = 0
        bulk = getattr(http, "delete_messages", None)
        for chunk in [ids[i:i + BULK_DELETE_CHUNK]
                      for i in range(0, len(ids), BULK_DELETE_CHUNK)]:
            if bulk is not None and len(chunk) > 1:
                try:
                    await bulk(str(channel_id), list(chunk))
                    deleted += len(chunk)
                    continue
                except Exception:
                    logger.info("bulk delete refused for %d message(s); "
                                "falling back to one at a time",
                                len(chunk), exc_info=True)
            # The REST call, not self.delete_message: that one swallows
            # its errors by design (a failed tidy-up must not break a
            # command), so counting its "successes" reported messages
            # deleted that are still there.
            single = getattr(http, "delete_message", None)
            for message_id in chunk:
                if single is None:
                    break
                try:
                    await single(str(channel_id), str(message_id))
                    deleted += 1
                except Exception:
                    logger.debug("could not delete %s", message_id,
                                 exc_info=True)
        return deleted

    async def _recent_message_ids(self, channel_id: str,
                                  count: int) -> list[str]:
        """The newest ``count`` message ids, oldest last.

        Paginated: the REST call caps at ``MESSAGE_PAGE`` per request,
        so asking for 200 without paging silently deleted 50.
        """
        http = self._rest()
        fetch = getattr(http, "get_messages", None)
        if fetch is None:
            raise RuntimeError("this fluxer.py has no get_messages")

        found: list[str] = []
        before: Optional[str] = None
        while len(found) < count:
            want = min(MESSAGE_PAGE, count - len(found))
            try:
                rows = await fetch(str(channel_id), limit=want,
                                   before=before) or []
            except Exception:
                logger.info("could not list messages in %s", channel_id,
                            exc_info=True)
                break
            if not rows:
                break
            for row in rows:
                mid = str((row or {}).get("id") or "")
                if mid:
                    found.append(mid)
            before = found[-1] if found else None
            if len(rows) < want:
                break
        return found[:count]

    # -- lookups ----------------------------------------------------------

    @staticmethod
    async def _lookup(client: Any, cache_name: str, fetch_name: str,
                      ident: str) -> Any:
        """Cache-then-API lookup, tolerant of int/str id keying.

        Two fluxer 0.4.2 details make the naive version fail silently:

        * the caches are ``dict[int, ...]`` while ids arrive as strings, so a
          string key always misses;
        * ``get_guild`` EXISTS, so an ``or`` chain picks it, gets ``None`` from
          the miss, and never reaches the async ``fetch_guild``.

        Together those made every guild resolve to None -- which is what put
        per-guild settings under the literal key "None" and made the server
        owner look like they had no roles.
        """
        candidates: list[Any] = [ident]
        try:
            candidates.append(int(ident))
        except (TypeError, ValueError):
            pass

        getter = getattr(client, cache_name, None)
        if callable(getter):
            for key in candidates:
                try:
                    hit = getter(key)
                except Exception:
                    continue
                if hit is not None:
                    return await hit if hasattr(hit, "__await__") else hit

        fetcher = getattr(client, fetch_name, None)
        if callable(fetcher):
            for key in candidates:
                try:
                    got = fetcher(key)
                    got = await got if hasattr(got, "__await__") else got
                except Exception:
                    continue
                if got is not None:
                    return got
        return None

    async def _channel(self, channel_id: str) -> Any:
        return await self._lookup(self.client, "get_channel",
                                  "fetch_channel", str(channel_id))

    async def _user(self, user_id: str) -> Any:
        return await self._lookup(self.client, "get_user",
                                  "fetch_user", str(user_id))

    async def member_count(self, guild_id: str) -> Optional[int]:
        """How many members the guild has right now, or None.

        REST first: the gateway's cached guild keeps the count from
        GUILD_CREATE and is not guaranteed to move as people join, so a
        welcome card would say the same number all day. Falls back to
        the cached figure, then None -- never a guessed zero.
        """
        try:
            http = self._rest()
            data = await http.request(http._route(
                "GET", "/guilds/{guild_id}?with_counts=true",
                guild_id=str(guild_id)))
            if isinstance(data, dict):
                for key in ("approximate_member_count", "member_count"):
                    if isinstance(data.get(key), int):
                        return data[key]
        except Exception:
            logger.debug("member count via REST failed", exc_info=True)
        guild = await self._guild(guild_id)
        count = _attr(guild, "member_count") if guild is not None else None
        return count if isinstance(count, int) else None

    async def _guild(self, guild_id: str) -> Any:
        return await self._lookup(self.client, "get_guild",
                                  "fetch_guild", str(guild_id))

    async def role_index(self, guild_id: str) -> dict[str, Role]:
        """``{role_id: Role}`` for a guild, cached briefly."""
        key = str(guild_id)
        cached = self._role_cache.get(key)
        if cached and _time.monotonic() < cached[0]:
            return cached[1]

        index: dict[str, Role] = {}
        # REST first: it needs only the guild id. Going through a guild
        # object meant a cold cache produced an empty role list, which
        # reads as "this server has no roles" rather than as a failure.
        fetch = getattr(self._rest(), "get_guild_roles", None)
        try:
            if fetch is not None:
                for raw_role in (await fetch(key)) or ():
                    role = to_role(raw_role)
                    if role.id:
                        index[role.id] = role
            else:
                guild = await self._guild(key)
                fetcher = getattr(guild, "fetch_roles", None) if guild \
                    else None
                if not callable(fetcher):
                    raise RuntimeError(
                        f"no way to list roles for guild {key}")
                for raw_role in (await fetcher()) or ():
                    role = to_role(raw_role)
                    if role.id:
                        index[role.id] = role
        except Exception:
            logger.warning("could not fetch roles for guild %s", key,
                           exc_info=True)
            # Cache the failure briefly so we do not hammer the API.
            self._role_cache[key] = (_time.monotonic() + 30.0, {})
            return {}
        self._role_cache[key] = (_time.monotonic() + ROLE_CACHE_TTL, index)
        return index

    async def channel_names(self, ids: Iterable[str]) -> dict[str, str]:
        """``{channel_id: name}`` for the ids given, best effort."""
        out: dict[str, str] = {}
        for cid in {str(c) for c in ids if c}:
            try:
                channel = await self._channel(cid)
            except Exception:
                channel = None
            name = _attr(channel, "name") if channel is not None else None
            if name:
                out[cid] = str(name)
        return out

    async def guild_channel_index(self, guild_id: str) -> dict[str, str]:
        """``{lowercased name: id}`` so a user can say "in Music".

        fluxer 0.4.2's ``Guild`` model exposes neither ``channels`` nor
        ``fetch_channels``, so this reads the REST guild payload, which does
        carry a ``channels`` array. Going through the model instead silently
        returned an empty index and every channel name failed to resolve.
        """
        cached = self._channel_index_cache.get(str(guild_id))
        if cached and _time.monotonic() < cached[0]:
            return cached[1]

        index: dict[str, str] = {}
        http = getattr(self.client, "_http", None)
        if http is not None:
            try:
                data = await http.request(http._route(
                    "GET", "/guilds/{guild_id}", guild_id=str(guild_id)))
                for raw in (data.get("channels") or []):
                    name, cid = raw.get("name"), raw.get("id")
                    if name and cid:
                        index[str(name).lower().lstrip("#")] = str(cid)
            except Exception:
                logger.debug("channel listing failed for %s", guild_id,
                             exc_info=True)

        self._channel_index_cache[str(guild_id)] = (
            _time.monotonic() + CHANNEL_INDEX_TTL, index)
        return index

    async def role_name_index(self, guild_id: str) -> dict[str, str]:
        """``{lowercased role name: id}``."""
        return {r.name.lower(): rid
                for rid, r in (await self.role_index(guild_id)).items()
                if r.name}

    async def member_name_index(self, guild_id: str) -> dict[str, str]:
        """``{written name: member id}`` for a guild, cached briefly.

        Keyed several ways, because people refer to each other however
        they see them: username, ``username#1234``, display name and
        nickname all point at the same id.
        """
        key = str(guild_id)
        cached = self._member_index_cache.get(key)
        if cached and _time.monotonic() < cached[0]:
            return cached[1]

        index: dict[str, str] = {}
        fetch = getattr(self._rest(), "get_guild_members", None)
        if fetch is not None:
            after = None
            try:
                # Paginated: a guild larger than one page would
                # otherwise resolve only its oldest hundred members.
                for _page in range(MEMBER_INDEX_PAGES):
                    rows = await fetch(key, limit=100, after=after) or []
                    if not rows:
                        break
                    for row in rows:
                        user = (row or {}).get("user") or {}
                        uid = str(user.get("id") or "")
                        if not uid:
                            continue
                        name = str(user.get("username") or "")
                        disc = str(user.get("discriminator") or "")
                        for label in (name,
                                      f"{name}#{disc}" if disc else "",
                                      user.get("global_name"),
                                      user.get("display_name"),
                                      (row or {}).get("nick")):
                            if label:
                                index.setdefault(str(label).lower(), uid)
                        after = uid
                    if len(rows) < 100:
                        break
            except Exception:
                logger.info("member listing failed for %s", guild_id,
                            exc_info=True)

        if not index:
            # An empty index is why "@Robin" reached the API as a literal
            # name. Silence here reads as "that person does not exist".
            logger.warning("member name index for guild %s is EMPTY -- "
                           "names will not resolve to ids (is "
                           "GUILD_MEMBERS granted, and does this "
                           "fluxer.py have get_guild_members?)", key)
        else:
            logger.info("member name index for guild %s: %d name(s)",
                        key, len(index))
        self._member_index_cache[key] = (
            _time.monotonic() + ROLE_CACHE_TTL, index)
        return index

    async def resolve_named(self, guild_id: Optional[str], kind: str,
                            value: str) -> str:
        """Turn a written name into an id for a role, channel or user param.

        Returns the input unchanged when it is already an id or cannot be
        resolved -- the command then reports a real failure rather than
        this layer inventing a target.
        """
        text = str(value or "").strip().lstrip("#&")
        # A mention token carries the id already; take it before
        # stripping decoration off what would otherwise be a name.
        mention = _MENTION_ID.match(text)
        if mention:
            return mention.group(1)
        text = text.lstrip("@")
        if not text or not guild_id or text.isdigit():
            return text
        try:
            if kind == "role":
                index = await self.role_name_index(guild_id)
            elif kind == "channel":
                index = await self.guild_channel_index(guild_id)
            elif kind == "user":
                index = await self.member_name_index(guild_id)
            else:
                return text
        except Exception:
            return text
        found = index.get(text.lower())
        if found:
            return found
        # "alex#5928" with no exact hit: try the bare username. Fluxer
        # is dropping discriminators, so a stale one must not be fatal.
        if "#" in text:
            return index.get(text.split("#", 1)[0].lower(), text)
        return text

    async def resolve_guild_id(self, channel_id: str) -> Optional[str]:
        """Fetch the channel to learn its guild, when the cache missed."""
        try:
            channel = await self._channel(channel_id)
            gid = _attr(channel, "guild_id") if channel is not None else None
            return str(gid) if gid else None
        except Exception:
            logger.debug("guild resolution failed for channel %s", channel_id,
                         exc_info=True)
            return None

    async def guild_owner_id(self, guild_id: str) -> Optional[str]:
        guild = await self._guild(guild_id)
        if guild is None:
            return None
        owner = _attr(guild, "owner_id", "ownerId")
        return str(owner) if owner else None

    async def guild_info(self, guild_id: str) -> Optional[Guild]:
        """Resolve a guild, re-fetching when the cached copy is partial.

        The client caches guilds from the GUILD_CREATE gateway payload,
        which can be lighter than the REST object -- a cache hit may carry
        an id but no name. Returning that produced welcome messages reading
        "Welcome to , @them!". So a missing name triggers one REST fetch
        rather than being passed through as an empty string.
        """
        guild = await self._guild(guild_id)
        if guild is None:
            return None

        name = str(_attr(guild, "name", default="") or "")
        owner = _attr(guild, "owner_id", "ownerId")

        if not name:
            fetcher = getattr(self.client, "fetch_guild", None)
            if callable(fetcher):
                try:
                    fresh = await fetcher(guild_id)
                except Exception:
                    fresh = None
                if fresh is not None:
                    name = str(_attr(fresh, "name", default="") or "")
                    owner = _attr(fresh, "owner_id", "ownerId") or owner
            if not name:
                logger.info("guild %s has no name even after refetch",
                            guild_id)

        return Guild(id=str(guild_id), name=name,
                     owner_id=str(owner) if owner else None)

    async def fetch_member(self, guild_id: str, user_id: str) -> Optional[Member]:
        guild = await self._guild(guild_id)
        if guild is None:
            return None
        raw = await self._lookup(guild, "get_member", "fetch_member",
                                 str(user_id))
        if raw is None:
            return None
        return to_member(
            raw, guild_id=guild_id,
            guild_owner_id=_attr(guild, "owner_id", "ownerId"),
            role_index=await self.role_index(guild_id),
        )

    # -- moderation -------------------------------------------------------

    async def _moderate(self, route: str, guild_id: str, user_id: str,
                        *, fallbacks: "Sequence[str]" = (),
                        **kwargs: Any) -> Any:
        """Run a moderation action, preferring the REST route.

        Every one of these used to fetch a guild or member object first
        and, when that lookup came back empty, report "fluxer.py exposes
        no X method". That blames the SDK for a cold cache and is how a
        role assignment spent an evening being diagnosed as a missing
        binding. The REST routes need only ids, so the lookup -- and the
        failure mode -- goes away.
        """
        call = getattr(self._rest(), route, None)
        if call is not None:
            return await call(str(guild_id), str(user_id), **kwargs)

        guild = await self._guild(guild_id)
        if guild is None:
            raise RuntimeError(
                f"guild {guild_id} not found, and this fluxer.py has no "
                f"{route}")
        for name in fallbacks:
            fn = getattr(guild, name, None)
            if fn is None:
                continue
            try:
                return await fn(user_id, **kwargs)
            except TypeError:
                # Older signatures take fewer keywords.
                return await fn(user_id)
        raise RuntimeError(f"this fluxer.py has neither {route} nor "
                           f"{' / '.join(fallbacks)}")

    async def ban(self, guild_id: str, user_id: str, *,
                  reason: Optional[str] = None,
                  delete_message_seconds: int = 0) -> None:
        # delete_message_seconds was accepted and then dropped on the
        # floor -- the honeypot passes it to sweep a scammer's posts.
        await self._moderate(
            "ban_guild_member", guild_id, user_id,
            fallbacks=("ban", "ban_member"), reason=reason,
            delete_message_seconds=int(delete_message_seconds or 0))

    async def unban(self, guild_id: str, user_id: str) -> None:
        await self._moderate("unban_guild_member", guild_id, user_id,
                             fallbacks=("unban", "unban_member"))

    async def kick(self, guild_id: str, user_id: str, *,
                   reason: Optional[str] = None) -> None:
        await self._moderate("kick_guild_member", guild_id, user_id,
                             fallbacks=("kick", "kick_member"),
                             reason=reason)

    async def _member_role(self, guild_id: str, user_id: str, role_id: str,
                           *, add: bool) -> None:
        """Add or remove one role, by REST rather than via the member.

        ``GuildMember.add_role`` exists, but getting a member object
        first is an extra lookup that can miss -- and when it did, the
        old code reported "fluxer.py exposes no member add_role method",
        blaming the SDK for what was actually a cold cache. The REST
        route needs only the three ids, so there is nothing to miss.
        """
        http = self._rest()
        method = ("add_guild_member_role" if add
                  else "remove_guild_member_role")
        call = getattr(http, method, None)
        if call is not None:
            try:
                await call(str(guild_id), str(user_id), str(role_id))
            except Exception as exc:
                raise RuntimeError(await self._explain_role_failure(
                    guild_id, user_id, role_id, exc)) from exc
            return

        # Older SDK: fall back to the member object.
        member = await self._raw_member(guild_id, user_id)
        if member is None:
            raise RuntimeError(
                f"member {user_id} not found in guild {guild_id}")
        fn = getattr(member, "add_role" if add else "remove_role", None)
        if fn is None:
            raise RuntimeError(f"this fluxer.py has neither {method} nor "
                               f"GuildMember.{'add' if add else 'remove'}"
                               f"_role")
        await fn(int(role_id))

    async def _explain_role_failure(self, guild_id: str, user_id: str,
                                    role_id: str, exc: Exception) -> str:
        """Say which of the three ids the server could not find.

        A bare ``404 UNKNOWN_MEMBER`` does not distinguish "wrong user"
        from "wrong guild", and the reply then blames permissions for
        what is actually a bad id. Probing costs one or two requests on
        a path that has already failed.
        """
        detail = f"{type(exc).__name__}: {str(exc)[:120]}"
        try:
            roles = await self.role_index(guild_id)
        except Exception:
            roles = {}
        if roles and str(role_id) not in roles:
            names = sorted((r.name for r in roles.values()),
                           key=str.casefold)
            # Say how many there are and mark the cut. Listing eight of
            # twenty with no ellipsis reads as the whole set, and sent
            # someone looking for a role that was there all along.
            shown = ", ".join(names[:12])
            more = (f" …and {len(names) - 12} more"
                    if len(names) > 12 else "")
            return (f"there's no role {role_id} in this server "
                    f"(guild {guild_id}). The {len(names)} roles here: "
                    f"{shown}{more}")

        getter = getattr(self._rest(), "get_guild_member", None)
        if getter is not None:
            try:
                await getter(str(guild_id), str(user_id))
            except Exception:
                return (f"user {user_id} isn't a member of guild "
                        f"{guild_id} — check the id, and check that's "
                        f"the right server")
        return detail

    async def add_role(self, guild_id: str, user_id: str,
                       role_id: str) -> None:
        await self._member_role(guild_id, user_id, role_id, add=True)

    async def remove_role(self, guild_id: str, user_id: str,
                          role_id: str) -> None:
        await self._member_role(guild_id, user_id, role_id, add=False)

    async def ensure_rank_roles(self, guild_id: str,
                                tiers: "Mapping[int, tuple[str, int]]"
                                ) -> tuple[dict[str, str], list[str]]:
        """Create any missing milestone roles. Returns (index, problems).

        Idempotent: existing roles are adopted by name, never duplicated.
        Creating roles needs MANAGE_ROLES, and the bot's own role must sit
        above the ones it creates or assignment fails later -- so failures
        are collected and reported rather than swallowed.
        """
        index = await self.role_index(guild_id)
        by_name = {r.name.lower(): r.id for r in index.values() if r.name}
        guild = await self._guild(guild_id)
        if guild is None:
            return {}, [f"cannot resolve guild {guild_id}"]

        created: dict[str, str] = {}
        problems: list[str] = []
        for _level, (name, colour) in sorted(tiers.items()):
            existing = by_name.get(name.lower())
            if existing:
                created[name] = existing
                continue
            maker = getattr(guild, "create_role", None)
            if maker is None:
                problems.append("this Fluxer build exposes no create_role")
                break
            try:
                role = await maker(name=name, color=colour)
                created[name] = str(_attr(role, "id", default=""))
            except Exception as exc:
                problems.append(f"{name}: {type(exc).__name__}: {exc}")

        # A new role invalidates the cached index.
        self._role_cache.pop(str(guild_id), None)
        return created, problems

    async def apply_rank_role(self, guild_id: str, user_id: str,
                              target_name: Optional[str],
                              all_tier_names: "Iterable[str]") -> Optional[str]:
        """Give the member ``target_name`` and strip other milestone roles.

        Returns an error string, or None on success. Roles the member does
        not have are skipped, so this is safe to call repeatedly.
        """
        index = await self.role_index(guild_id)
        by_name = {r.name.lower(): rid for rid, r in index.items() if r.name}

        # Which milestone roles they already hold. A failed member
        # lookup means "unknown", not "none": treating it as none made
        # this return success having done nothing.
        member = await self._raw_member(guild_id, user_id)
        held = ({str(r) for r in (_attr(member, "roles", default=()) or ())}
                if member is not None else None)

        target_id = by_name.get((target_name or "").lower()) \
            if target_name else None

        problems: list[str] = []
        if target_id and (held is None or str(target_id) not in held):
            try:
                await self.add_role(guild_id, user_id, str(target_id))
            except Exception as exc:
                problems.append(f"add {target_name}: {exc}")

        for name in all_tier_names:
            if name == target_name:
                continue
            rid = by_name.get(name.lower())
            # Only strip what we know they hold. With an unknown role
            # list, skip rather than fire a removal per tier.
            if not rid or held is None or str(rid) not in held:
                continue
            try:
                await self.remove_role(guild_id, user_id, str(rid))
            except Exception as exc:
                problems.append(f"remove {name}: {exc}")

        return "; ".join(problems) if problems else None

    async def timeout(self, guild_id: str, user_id: str,
                      until: Optional[_dt.datetime]) -> None:
        """Time a member out, or lift it with ``until=None``."""
        call = getattr(self._rest(), "timeout_guild_member", None)
        if call is not None:
            await call(str(guild_id), str(user_id),
                       until=until.isoformat() if until else None)
            return

        member = await self._raw_member(guild_id, user_id)
        if member is None:
            raise RuntimeError(
                f"member {user_id} not found in guild {guild_id}")
        fn = getattr(member, "timeout", None)
        if fn is None:
            raise RuntimeError("this fluxer.py has neither "
                               "timeout_guild_member nor GuildMember.timeout")
        await fn(until)

    async def _raw_member(self, guild_id: str, user_id: str) -> Any:
        guild = await self._guild(guild_id)
        if guild is None:
            return None
        return await self._lookup(guild, "get_member", "fetch_member",
                                  str(user_id))

    # -- risk priors ------------------------------------------------------

    async def profile(self, guild_id: Optional[str],
                      user_ref: str) -> Optional[TargetProfile]:
        """Assemble a :class:`TargetProfile` -- the input to risk scoring."""
        if not guild_id:
            return None
        member = await self.fetch_member(guild_id, user_ref)
        if member is None:
            return None
        tenure = member.tenure
        age = member.account_age
        return TargetProfile(
            user_id=member.id,
            display_name=member.display_name,
            tenure_days=tenure.total_seconds() / 86400 if tenure else None,
            account_age_days=age.total_seconds() / 86400 if age else None,
            role_count=len([r for r in member.roles
                            if r.name not in ("@everyone", "everyone")]),
            message_count=self.activity.message_count(guild_id, member.id),
            warning_count=self.warnings.count(guild_id, member.id),
            is_bot=member.is_bot,
            is_guild_owner=member.is_guild_owner,
            permissions=member.permissions,
            top_role_position=member.top_role_position,
        )


# --------------------------------------------------------------------------
# Boot-time probe
# --------------------------------------------------------------------------

#: Verified present in fluxer.py 0.4.2 top-level exports. Attachment exists
#: but only as fluxer.models.message.Attachment, so it is not checked here.
REQUIRED_MODELS = ("Guild", "Channel", "Message", "User", "GuildMember",
                   "Role", "Embed", "Reaction", "File", "Permissions")
REQUIRED_TOPLEVEL = ("Bot", "Client", "Intents", "Cog")


def probe(module: Any = None) -> dict[str, list[str]]:
    """Verify the fluxer.py bindings this adapter depends on.

    Run at boot. An outdated or renamed SDK otherwise degrades silently --
    the same failure mode the parent repo's xai-sdk checker exists to catch.
    Returns ``{"ok": [...], "missing": [...]}``.
    """
    if module is None:
        try:
            import fluxer as module  # type: ignore
        except ImportError:
            return {"ok": [], "missing": ["fluxer (package not installed)"]}

    ok, miss = [], []
    for name in REQUIRED_TOPLEVEL + REQUIRED_MODELS:
        (ok if hasattr(module, name) else miss).append(name)

    perms = getattr(module, "Permissions", None)
    if perms is None:
        miss.append("Permissions")
    else:
        from ...core.permissions import verify_against

        vendor = {p.name: int(p.value) for p in perms
                  if getattr(p, "name", None)} if hasattr(perms, "__iter__") else {}
        for problem in verify_against(vendor):
            miss.append(f"permission bit drift -> {problem}")

    # The music publish profile reaches into VoiceClient._publish_track,
    # because play() builds TrackPublishOptions internally and hardcodes
    # the microphone source. If that private method is renamed the audio
    # quietly drops back to LiveKit's speech profile -- playable, but
    # mono and thin. Reported as a warning, not a missing binding: it
    # should not stop the bot from starting.
    warn: list[str] = []
    try:
        from fluxer.voice import VoiceClient  # type: ignore

        if not hasattr(VoiceClient, "_publish_track"):
            warn.append("VoiceClient._publish_track is gone — music will "
                        "publish with LiveKit's speech defaults")
        else:
            ok.append("VoiceClient._publish_track")
    except Exception:
        warn.append("fluxer.voice.VoiceClient not importable — no music "
                    "publish profile")

    return {"ok": ok, "missing": miss, "warn": warn}
