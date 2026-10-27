"""The owner's per-server allowlist for voice chat and streaming.

Owner-only, and meant to be used in a DM to the bot:

    Add 123456789012345678 to the allowlist for voice
    Remove 1234567890 from the streaming allowlist
    Which servers have voice?

See ``corbin/core/features.py`` and ``VOICE_ROADMAP.md`` (Phase G).
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.context import CommandContext
from ..core.features import FEATURES, FeatureAllowlist, label, parse_features
from ..core.registry import Param, Risk, command


def _phrase(args: Any) -> str:
    on = args.get("enabled", True) is not False
    wanted = parse_features(str(args.get("features") or "")) or ("?",)
    what = " and ".join(label(f).lower() for f in wanted)
    server = args.get("server") or "this server"
    return (f"{'turn on' if on else 'turn off'} {what} for {server}")


def _allowlist(ctx: CommandContext) -> FeatureAllowlist:
    found = ctx.extras.get("features")
    if found is None:
        raise RuntimeError("the feature allowlist isn't loaded")
    return found


@command(
    name="feature_allowlist",
    description="Owner only: voice/streaming on or off for a server",
    risk=Risk.WRITE,
    owner_only=True,
    guild_only=False,
    always_confirm=True,
    params=(
        Param("features", "voice, streaming or both"),
        Param("server", "its id or \"this server\"", required=False),
        Param("enabled", "on or off", kind="boolean", required=False,
              default=True),
    ),
    confirm_phrase=_phrase,
    examples=("add 123456789012345678 to the allowlist for voice",
              "remove 1234567890 from the streaming allowlist"),
)
async def feature_allowlist(ctx: CommandContext, features: str,
                            server: Optional[str] = None,
                            enabled: bool = True) -> str:
    allow = _allowlist(ctx)
    wanted = parse_features(features)
    if not wanted:
        return ("Which feature? I can allowlist "
                + " or ".join(f"`{f}`" for f in FEATURES) + ".")

    raw = str(server or "").strip()
    if not raw or raw.lower() in ("this server", "here", "this one"):
        if not ctx.guild_id:
            return "Which server? Give me its id; this is a DM."
        gid = str(ctx.guild_id)
    elif raw.isdigit():
        gid = raw
    else:
        return f"“{raw}” isn't a server id. Give me the number."

    # A typo in an 18-digit id must not silently allow the wrong server,
    # so the server has to be one he's actually in, and the reply names
    # it rather than echoing the id back.
    adapter = ctx.extras.get("adapter")
    guild = await adapter.guild_info(gid) if adapter is not None else None
    if guild is None and enabled:
        return (f"I'm not in a server with id `{gid}`, so I haven't "
                "added anything. Check the id?")
    name = guild.name if guild is not None else "(not a member)"

    allow.set(gid, wanted, enabled, by=f"owner {ctx.invoker.id}")

    stopped = ""
    if not enabled and "voice" in wanted:
        # Revoking takes effect now, not at the next join.
        chat = ctx.extras.get("voicechat")
        if chat is not None and await chat.stop(gid):
            voice = ctx.extras.get("voice")
            session = voice.get(gid) if voice else None
            if session is not None and not session.playing:
                await voice.disconnect(gid)
            stopped = " I've stopped listening there."

    what = " and ".join(label(f).lower() for f in wanted)
    if enabled:
        return f"Done: {what} is on for **{name}** (`{gid}`)."
    return f"Done: {what} is off for **{name}** (`{gid}`).{stopped}"


@command(
    name="feature_allowlist_list",
    description="Owner only: show the allowlist",
    risk=Risk.SAFE,
    owner_only=True,
    guild_only=False,
    examples=("which servers have voice?",),
)
async def feature_allowlist_list(ctx: CommandContext) -> str:
    entries = _allowlist(ctx).entries()
    if not entries:
        return ("Nothing's allowlisted. Voice chat and streaming are off "
                "everywhere.")
    adapter = ctx.extras.get("adapter")
    lines = []
    for gid, on in sorted(entries.items()):
        guild = await adapter.guild_info(gid) if adapter is not None else None
        name = guild.name if guild is not None else "(not a member)"
        lines.append(f"- **{name}** (`{gid}`): "
                     + ", ".join(label(f).lower() for f in sorted(on)))
    return "Allowlisted servers:\n" + "\n".join(lines)
