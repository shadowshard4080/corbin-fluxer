"""Rotating the server banner (owner's request, 2026-10-19). See
:mod:`corbin.core.banner`.

"Set up banner rotation" makes (or adopts) #banner-images and starts
changing the banner from it on an interval; the rest change the interval,
pause it, change it right now, or show how it's set. All need Manage
Server, the same permission changing the banner itself takes.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..core.banner import (CHANNEL_NAME, DEFAULT_INTERVAL, MAX_INTERVAL,
                           MIN_INTERVAL, BannerSettings, describe_interval,
                           ensure_channel, parse_interval)
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

COLOUR = 0xB22222
#: The banner commands are all about one noun; "rotate" alone (or
#: "interval") matched other things in testing.
TOPIC = r"\bbanners?\b"


def _store(ctx: CommandContext) -> BannerSettings:
    return BannerSettings(ctx.extras["settings"])


def _bot_id(adapter: Any) -> Optional[str]:
    user = getattr(getattr(adapter, "client", None), "user", None)
    return str(user.id) if getattr(user, "id", None) else None


def _interval(text: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """``(seconds, error)``."""
    if not text:
        return None, None
    seconds = parse_interval(text)
    if seconds is None:
        return None, (f"I don't follow “{text}” as an interval. Try "
                      f"“every 12 hours”, “daily” or “every 30 minutes”.")
    if seconds < MIN_INTERVAL:
        return None, "The shortest interval is every 15 minutes."
    if seconds > MAX_INTERVAL:
        return None, "The longest interval is every 30 days."
    return seconds, None


def _next_change(store: BannerSettings, gid: str) -> str:
    cfg = store.get(gid)
    if not cfg.get("enabled"):
        return "paused"
    last = float(cfg.get("last_changed") or 0)
    if not last:
        return "within a minute"
    due = last + store.interval(gid)
    left = max(0, int(due - time.time()))
    if left < 90:
        return "within a minute or two"
    hours, rem = divmod(left, 3600)
    mins = rem // 60
    return (f"in {hours}h {mins}m" if hours else f"in {mins} minutes")


@command(
    name="banner_setup",
    description="Rotate the server banner through images posted in "
                "#banner-images",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("banner_rotation", "setup_banner", "rotate_banner"),
    params=(Param("interval", "how often to change it, e.g. daily, every "
                              "12 hours", required=False),),
    topic=TOPIC,
    confirm_phrase=lambda a: ("rotate the server banner from "
                              f"#{CHANNEL_NAME}"
                              + (f" {a['interval']}" if a.get("interval")
                                 else " once a day")),
    examples=("set up banner rotation", "change the server banner every "
              "12 hours from a channel", "rotate the banner daily"),
)
async def banner_setup(ctx: CommandContext,
                       interval: Optional[str] = None) -> str:
    adapter = ctx.extras.get("adapter")
    if adapter is None:
        return "I can't reach the server from here."
    seconds, err = _interval(interval)
    if err:
        return err
    store = _store(ctx)
    gid = str(ctx.guild_id)
    channel_id, created, problems = await ensure_channel(
        adapter._rest(), gid, _bot_id(adapter))
    if channel_id is None:
        return (f"I couldn't make #{CHANNEL_NAME}: {problems[0]}. I need "
                f"Manage Channels.")
    cfg = store.get(gid)
    every = seconds or int(cfg.get("interval") or DEFAULT_INTERVAL)
    store.update(gid, enabled=True, channel_id=channel_id, interval=every,
                 last_error=None,
                 # A new setup changes it at the next tick, so it's
                 # visibly working; a re-run keeps the schedule.
                 last_changed=cfg.get("last_changed") or 0)
    where = (f"Made <#{channel_id}>. Everyone can see it; only admins "
             f"(and anyone you give Send Messages there) can post, since "
             f"whatever's posted goes on the banner."
             if created else f"Using the existing <#{channel_id}>.")
    if problems:
        where += " (" + "; ".join(problems) + ")"
    return (f"{where} I'll change the banner {describe_interval(every)} "
            f"from the images there. Anything that isn't banner-shaped "
            f"(16:9, at least 960×540) I'll resize and repost, deleting "
            f"the original. Say “change the banner now” to try it.")


@command(
    name="banner_interval",
    description="Change how often the server banner rotates",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("banner_schedule", "banner_every"),
    params=(Param("interval", "e.g. daily, every 6 hours, every 30 "
                              "minutes"),),
    topic=TOPIC,
    confirm_phrase=lambda a: f"change the banner {a.get('interval')}",
    examples=("change the banner every 6 hours",
              "rotate the banner weekly",
              "set the banner interval to 30 minutes"),
)
async def banner_interval(ctx: CommandContext, interval: str) -> str:
    seconds, err = _interval(interval)
    if err or seconds is None:
        return err or "How often? “every 12 hours”, “daily”…"
    store = _store(ctx)
    gid = str(ctx.guild_id)
    if not store.get(gid).get("channel_id"):
        return "Banner rotation isn't set up here. “set up banner rotation” " \
               "first."
    store.update(gid, interval=seconds)
    return (f"I'll change the banner {describe_interval(seconds)}. Next "
            f"change {_next_change(store, gid)}.")


@command(
    name="banner_toggle",
    description="Pause or resume the server banner rotation",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("banner_pause", "banner_resume"),
    params=(Param("enabled", "on or off", kind="boolean"),),
    topic=TOPIC,
    confirm_phrase=lambda a: ("resume the banner rotation"
                              if a.get("enabled") else
                              "pause the banner rotation"),
    examples=("pause the banner rotation", "resume rotating the banner",
              "stop changing the banner"),
)
async def banner_toggle(ctx: CommandContext, enabled: bool) -> str:
    store = _store(ctx)
    gid = str(ctx.guild_id)
    if not store.get(gid).get("channel_id"):
        return "Banner rotation isn't set up here. “set up banner rotation” " \
               "first."
    store.update(gid, enabled=bool(enabled))
    if enabled:
        return (f"Banner rotation is back on. Next change "
                f"{_next_change(store, gid)}.")
    return ("Banner rotation is paused. The current banner stays; the "
            f"images in #{CHANNEL_NAME} are kept.")


@command(
    name="banner_now",
    description="Change the server banner to the next image right now",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("next_banner", "banner_next", "shuffle_banner"),
    topic=TOPIC,
    examples=("change the banner now", "next banner please",
              "shuffle the banner"),
)
async def banner_now(ctx: CommandContext) -> str:
    rotate = ctx.extras.get("banner_rotate")
    store = _store(ctx)
    gid = str(ctx.guild_id)
    if not store.get(gid).get("channel_id"):
        return "Banner rotation isn't set up here. “set up banner rotation” " \
               "first."
    if rotate is None:
        return "I can't change the banner from here."
    result = await rotate(gid)
    return result.message


@command(
    name="banner_status",
    description="Show how the server banner rotation is set up",
    risk=Risk.SAFE,
    permission=Perm.MANAGE_GUILD,
    aliases=("banner_settings", "banner_info"),
    topic=TOPIC,
    examples=("show the banner rotation settings",
              "when does the banner change next?"),
)
async def banner_status(ctx: CommandContext) -> Any:
    store = _store(ctx)
    gid = str(ctx.guild_id)
    cfg = store.get(gid)
    if not cfg.get("channel_id"):
        return ("Banner rotation isn't set up here. “set up banner "
                "rotation” makes #banner-images and starts it.")
    embed = Embed(title="Banner rotation", colour=COLOUR)
    embed.add_field("Channel", f"<#{cfg['channel_id']}>", inline=True)
    embed.add_field("Changes", describe_interval(store.interval(gid)),
                    inline=True)
    embed.add_field("Next change", _next_change(store, gid), inline=True)
    embed.add_field("Shown so far this round",
                    str(len(cfg.get("used") or [])), inline=True)
    if cfg.get("last_error"):
        embed.add_field("Last problem", str(cfg["last_error"])[:1000])
    embed.footer = ("“change the banner now” · “change the banner every 6 "
                    "hours” · “pause the banner rotation”")
    return embed
