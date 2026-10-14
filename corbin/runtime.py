"""Executes what the dispatcher decided.

Separated from :mod:`corbin.core.dispatch` so the decision logic stays pure
and unit-testable: the dispatcher says *what* should happen, this performs it
and handles the reporting, undo registration, and error surfacing.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from .core.context import (CommandContext, Embed, Guild, Member, Platform,
                           Response, Surface)
from .core.dispatch import DispatchResult, Dispatcher
from .core.registry import CommandSpec, Risk
from .core.risk import Decision

logger = logging.getLogger("corbin.runtime")


class Executor:
    """Runs a command handler and reports the outcome."""

    def __init__(self, adapter: Any, dispatcher: Dispatcher, *,
                 platform: Platform = Platform.FLUXER) -> None:
        self.adapter = adapter
        self.dispatcher = dispatcher
        self.platform = platform
        #: Extra services handlers can reach via ``ctx.extras`` (settings, …).
        self.extras: dict[str, Any] = {}

    async def run(
        self,
        result: DispatchResult,
        *,
        invoker: Member,
        channel,
        guild: Optional[Guild],
        surface: Surface = Surface.AI,
        mention_ids: Optional[Mapping[str, str]] = None,
        raw_text: Optional[str] = None,
        attachments: Sequence[Any] = (),
    ) -> Optional[str]:
        """Perform ``result`` and return the text Corbin should say.

        ``raw_text`` is the message as the person wrote it (mentions
        resolved). Handlers that must reproduce text exactly, like
        ``echo``, read it from ``extras`` rather than trusting the
        classifier's copy, which can trim or rephrase.
        """
        if result.undo is not None:
            try:
                await result.undo.undo()
            except Exception as exc:
                logger.exception("undo failed for %s", result.undo.command)
                return f"I couldn't reverse that: {type(exc).__name__}: {exc}"
            return result.message or "Reversed."

        if result.decision is not Decision.EXECUTE or result.spec is None:
            return result.message or None

        spec = result.spec

        # Resolve written role/channel names to ids once, here, rather than
        # in each handler. The classifier emits what the user said ("the
        # Artist role"), and a handler receiving that string would pass a
        # name where an id is required and fail confusingly.
        args = dict(result.args)
        # Names the user actually @-mentioned in this message, keyed by
        # what they render as. The id is right there in the raw message;
        # resolving the display name back to an id through a member
        # listing is both slower and able to fail, which is how
        # "give awooo to @Robin" came back as "Robin isn't a member".
        mentions = {k.lower(): v for k, v in (mention_ids or {}).items()}
        if mentions:
            for param in spec.params:
                if param.kind != "user":
                    continue
                raw = str(args.get(param.name) or "").strip().lstrip("@")
                if raw and not raw.isdigit() and raw.lower() in mentions:
                    args[param.name] = mentions[raw.lower()]

        if guild is not None:
            for param in spec.params:
                if param.kind not in ("role", "channel", "user"):
                    continue
                # Already resolved from a mention above.
                if param.kind == "user" and \
                        str(args.get(param.name) or "").isdigit():
                    continue
                raw = args.get(param.name)
                if not raw:
                    continue
                try:
                    args[param.name] = await self.adapter.resolve_named(
                        guild.id, param.kind, str(raw))
                except Exception:
                    logger.debug("could not resolve %s %r", param.kind, raw,
                                 exc_info=True)

        ctx = CommandContext(
            platform=self.platform, surface=surface, invoker=invoker,
            channel=channel, guild=guild, responder=_AdapterResponder(
                self.adapter, channel.id, invoker.id),
            args=args,
            extras={"adapter": self.adapter, **self.extras,
                    "raw_text": raw_text,
                    "attachments": tuple(attachments or ()),
                    "mention_ids": dict(mention_ids or {})},
        )

        try:
            outcome = await self._invoke(spec, ctx, args)
        except PermissionError as exc:
            logger.info("command %s blocked by platform: %s", spec.name, exc)
            return f"The server wouldn't let me do that — {exc}"
        except Exception as exc:
            # Report failure. A moderation command that silently no-ops while
            # replying "done" is the exact bug the parent repo fixed in /warn.
            logger.exception("command %s failed", spec.name)
            return f"That failed: {type(exc).__name__}: {exc}"

        if spec.risk is Risk.DESTRUCTIVE:
            self._register_undo(spec, ctx, args)

        # Handlers may return a Response to control delivery (private, files,
        # auto-delete). Send through ctx.responder, never adapter.send
        # directly: the responder is what tags a private message with its
        # recipient, and without that the DM fallback has nobody to DM.
        if isinstance(outcome, Response):
            await ctx.responder.send(outcome)
            return None
        if isinstance(outcome, Embed):
            await ctx.responder.send(Response(embed=outcome))
            return None
        # No em-dash: this is Corbin speaking.
        return str(outcome) if outcome is not None else f"Done: {spec.name}."

    @staticmethod
    async def _invoke(spec: CommandSpec, ctx: CommandContext,
                      args: Mapping[str, Any]) -> Any:
        signature = inspect.signature(spec.handler)
        accepted = {
            name for name, p in signature.parameters.items()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        } - {"ctx", "self"}
        payload = {k: v for k, v in args.items() if k in accepted}
        return await spec.handler(ctx, **payload)

    def _register_undo(self, spec: CommandSpec, ctx: CommandContext,
                       args: Mapping[str, Any]) -> None:
        """Offer a short undo window on destructive actions.

        Natural language is fuzzier than a slash command, so auto-execution
        is only defensible if it is cheaply reversible.
        """
        undo = self._undo_for(spec, ctx, args)
        if undo is None:
            return
        self.dispatcher.store.put_undo(
            guild_id=ctx.guild_id, channel_id=ctx.channel.id,
            user_id=ctx.invoker.id, command=spec.name,
            summary=f"{spec.name} {args.get('user', '')}".strip(), undo=undo,
        )

    def _undo_for(self, spec: CommandSpec, ctx: CommandContext,
                  args: Mapping[str, Any]) -> Optional[Callable[[], Awaitable[None]]]:
        target = args.get("user")
        if spec.name == "ban" and target and ctx.guild_id:
            async def _unban() -> None:
                await self.adapter.unban(ctx.guild_id, str(target))
            return _unban
        # Kicks and deletions are not reversible; no undo is offered rather
        # than a fake one.
        return None


class _AdapterResponder:
    """Bridges CommandContext.respond() to the platform adapter."""

    def __init__(self, adapter: Any, channel_id: str, user_id: str) -> None:
        self.adapter = adapter
        self.channel_id = channel_id
        self.user_id = user_id

    async def send(self, response: Response) -> Optional[str]:
        # Tell the adapter who a private message belongs to, so its DM
        # fallback has somebody to DM.
        if response.private:
            setattr(response, "_target_user_id", self.user_id)
        return await self.adapter.send(self.channel_id, response)

    async def fetch_member(self, guild_id: str, user_id: str):
        return await self.adapter.fetch_member(guild_id, user_id)
