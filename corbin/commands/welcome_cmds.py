"""Welcome, farewell, and AI greeting configuration."""

from __future__ import annotations

import re
from typing import Any, Optional

from ..identity import NAME
from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..core.welcome import (DEFAULT_FAREWELL, DEFAULT_FAREWELL_CARD,
                            DEFAULT_WELCOME, DEFAULT_WELCOME_CARD,
                            background_path, prepare_background,
                            render_template)


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


@command(
    name="setup_welcomer",
    description=("Post a message and an image card when someone joins the "
                 "server; also changes the message or card text later"),
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("welcomer", "setup_welcome"),
    params=(
        Param("enabled", "on or off; leave out to keep it as it is",
              kind="boolean", required=False),
        Param("channel", "where to post", kind="channel", required=False),
        Param("message", "the text; {member}, {server} and {count} (member "
              "count) are filled in", required=False, max_length=900),
        Param("card", "attach the welcome image card", kind="boolean",
              required=False),
        Param("card_text", "the line on the card itself; same placeholders",
              required=False, max_length=120),
    ),
    confirm_phrase=lambda a: ("turn welcome messages off"
                              if a.get("enabled") is False
                              else "set up welcome messages"),
    examples=("turn on welcome messages in this channel",
              "welcome new people in #general",
              "change the welcome card text to Welcome to the pack!",
              "stop posting welcome messages"),
)
async def setup_welcomer(ctx: CommandContext,
                         enabled: Optional[bool] = None,
                         channel: Optional[str] = None,
                         message: Optional[str] = None,
                         card: Optional[bool] = None,
                         card_text: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    if enabled is False:
        settings.set_welcome(ctx.guild_id, enabled=False)
        return "Welcome messages are off."
    current = settings.welcome(ctx.guild_id)
    target = (_resolve_channel(ctx, channel) or current.get("channel_id")
              or ctx.channel.id)
    cfg = settings.set_welcome(
        ctx.guild_id, enabled=True, channel_id=target,
        message=message if message is not None
        else (None if current.get("message") else DEFAULT_WELCOME),
        card=card, card_text=card_text)
    # Never render an empty server name: "Welcome to , @them!" reads as a
    # bug to every new member who sees it.
    server_name = (ctx.guild.name if ctx.guild and ctx.guild.name
                   else "the server")
    fill = dict(member="@them", server=server_name, count=123)
    preview = render_template(cfg.get("message") or DEFAULT_WELCOME, **fill)
    lines = [f"Welcome messages on in <#{target}>. Preview: “{preview}”"]
    if cfg.get("card", True):
        lines.append("Card: “" + render_template(
            cfg.get("card_text") or DEFAULT_WELCOME_CARD, **fill) + "”"
            + (" on your background." if cfg.get("background")
               else ". Say “set this image as the welcomer background” "
                    "with an image to give it one."))
    return "\n".join(lines)


@command(
    name="setup_leavemessage",
    description=("Post a message, and optionally an image card, when "
                 "someone leaves the server"),
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("farewell", "setup_farewell", "leavemessage"),
    params=(
        Param("enabled", "on or off; leave out to keep it as it is",
              kind="boolean", required=False),
        Param("channel", "where to post", kind="channel", required=False),
        Param("message", "the text; {member}, {server} and {count} are "
              "filled in", required=False, max_length=900),
        Param("card", "attach a farewell image card", kind="boolean",
              required=False),
        Param("card_text", "the line on the card itself", required=False,
              max_length=120),
    ),
    confirm_phrase=lambda a: ("turn leave messages off"
                              if a.get("enabled") is False
                              else "set up leave messages"),
    examples=("post a message when people leave",
              "add a farewell card to leave messages",
              "turn off leave messages"),
)
async def setup_leavemessage(ctx: CommandContext,
                             enabled: Optional[bool] = None,
                             channel: Optional[str] = None,
                             message: Optional[str] = None,
                             card: Optional[bool] = None,
                             card_text: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    if enabled is False:
        settings.set_farewell(ctx.guild_id, enabled=False)
        return "Leave messages are off."
    current = settings.farewell(ctx.guild_id)
    target = (_resolve_channel(ctx, channel) or current.get("channel_id")
              or ctx.channel.id)
    cfg = settings.set_farewell(
        ctx.guild_id, enabled=True, channel_id=target,
        message=message if message is not None
        else (None if current.get("message") else DEFAULT_FAREWELL),
        card=card, card_text=card_text)
    card_note = " with a card" if cfg.get("card") else ""
    return f"Leave messages on in <#{target}>{card_note}."


@command(
    name="greeting",
    description=f"Have {NAME} write each welcome instead of using a "
                "fixed template",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("ai_greeting", "dynamic_greeting"),
    params=(
        Param("enabled", "on or off", kind="boolean"),
        Param("info", f"anything {NAME} should mention, e.g. read the rules",
              required=False, max_length=600),
    ),
    confirm_phrase=lambda a: ("write my own welcome messages"
                              if a.get("enabled")
                              else "stop writing my own welcome messages"),
    examples=("write your own welcome messages",
              "greet new members yourself and tell them to read #rules"),
)
async def greeting(ctx: CommandContext, enabled: bool,
                   info: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    settings.set_greeting(ctx.guild_id, enabled=enabled, custom_info=info)
    if not enabled:
        return "I'll go back to the plain welcome template."
    extra = f" I'll work in: “{info}”." if info else ""
    welcome = settings.welcome(ctx.guild_id)
    if not welcome.get("enabled"):
        return ("I'll write the welcomes myself, but welcome messages are "
                "switched off, so nothing will post yet. Turn those on and "
                "pick a channel first.")
    return f"I'll write each welcome myself from now on.{extra}"


_CLEAR = frozenset({"none", "clear", "remove", "reset", "off", "default"})


_OG_IMAGE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::secure_url)?|"""
    r"""twitter:image)["'][^>]*content=["']([^"']+)["']"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]*(?:property|name)="""
    r"""["'](?:og:image|twitter:image)["']""", re.IGNORECASE)


def page_image(html: str) -> Optional[str]:
    """The picture a web page says it is about (og:image / twitter:image).

    Lets a page link -- Imgur, Tenor, a gallery -- work where only a direct
    image link did before.
    """
    match = _OG_IMAGE.search(html or "")
    url = (match.group(1) or match.group(2)) if match else None
    return url.replace("&amp;", "&") if url else None


async def _download(url: str) -> Optional[bytes]:
    import httpx

    try:
        async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=True,
                headers={"User-Agent": f"Mozilla/5.0 ({NAME})"}) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            kind = resp.headers.get("content-type", "").lower()
            if "html" in kind:
                target = page_image(resp.text[:200_000])
                if not target:
                    return None
                resp = await client.get(target)
                if resp.status_code != 200:
                    return None
            return resp.content
    except Exception:
        return None


@command(
    name="card_background",
    description=("Set or clear the background image on the welcome or "
                 "farewell card, from an attached image or a link"),
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("welcomer_background", "welcome_background",
             "farewell_background"),
    params=(
        Param("which", "welcome, farewell or both", required=False,
              default="welcome", choices=("welcome", "farewell", "both")),
        Param("url", "an image link; leave out to use the attached image; "
              "\"none\" to go back to a plain colour", required=False),
    ),
    confirm_phrase=lambda a: f"change the {a.get('which') or 'welcome'} "
                             f"card background",
    examples=("set this image as the welcomer background",
              "use https://example.com/wolves.png as the farewell background",
              "remove the welcome card background"),
)
async def card_background(ctx: CommandContext, which: str = "welcome",
                          url: Optional[str] = None) -> str:
    import asyncio

    import config

    settings = ctx.extras["settings"]
    kinds = ("welcome", "farewell") if which == "both" else (which,)
    setter = {"welcome": settings.set_welcome,
              "farewell": settings.set_farewell}

    if url and url.strip().lower() in _CLEAR:
        for kind in kinds:
            path = background_path(config.DATA_ROOT, ctx.guild_id, kind)
            path.unlink(missing_ok=True)
            setter[kind](ctx.guild_id, background=False)
        return f"The {which} card is back to a plain colour."

    source = None
    if url and url.strip().startswith(("http://", "https://")):
        source = url.strip()
    else:
        for item in ctx.extras.get("attachments") or ():
            kind = str(getattr(item, "content_type", "") or "")
            name = str(getattr(item, "filename", "") or "").lower()
            if kind.startswith("image/") or name.endswith(
                    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                source = item.url
                break
    if not source:
        return ("Attach the image to your message, or give me a link to "
                "it.")

    raw = await _download(source)
    # Decoded and cropped now, while you're here to hear that it failed,
    # rather than at the next join. The file is kept locally because an
    # attachment link stops working once its message is deleted.
    prepared = await asyncio.to_thread(prepare_background, raw) if raw \
        else None
    if prepared is None:
        return ("I couldn't get an image from that. A direct image link, a "
                "page with a main picture (Imgur, Tenor), or an attached "
                "PNG, JPG, GIF or WebP under 8 MB works.")
    for kind in kinds:
        path = background_path(config.DATA_ROOT, ctx.guild_id, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(prepared)
        setter[kind](ctx.guild_id, background=True)
    note = ""
    if "welcome" in kinds and not settings.welcome(ctx.guild_id).get(
            "card", True):
        note = " The welcome card is switched off, so turn it on to see it."
    if "farewell" in kinds and not settings.farewell(ctx.guild_id).get(
            "card"):
        note += (" The farewell card is off; say “add a farewell card to "
                 "leave messages” to use it.")
    return f"Background set for the {which} card.{note}"


@command(
    name="welcome_status",
    description="Show the welcome, farewell and greeting configuration",
    risk=Risk.SAFE,
    aliases=("welcome_config",),
    examples=("show the welcome settings",),
)
async def welcome_status(ctx: CommandContext) -> Any:
    settings = ctx.extras["settings"]
    gid = ctx.guild_id
    if gid is None:
        return "That only works inside a server."
    w, f, g = (settings.welcome(gid), settings.farewell(gid),
               settings.greeting(gid))

    def _state(cfg: dict) -> str:
        if not cfg.get("enabled"):
            return "off"
        where = cfg.get("channel_id")
        return f"on in <#{where}>" if where else "on"

    embed = Embed(title="Welcome configuration")
    embed.add_field("Welcome", _state(w), inline=True)
    embed.add_field("Leave message", _state(f), inline=True)
    embed.add_field("AI greeting", "on" if g.get("enabled") else "off",
                    inline=True)
    def _card(cfg: dict, default_on: bool, default_text: str) -> str:
        if not cfg.get("card", default_on):
            return "off"
        bg = "custom background" if cfg.get("background") else "plain colour"
        return f"“{cfg.get('card_text') or default_text}” on a {bg}"

    if w.get("enabled"):
        embed.add_field("Welcome text", w.get("message") or DEFAULT_WELCOME)
        embed.add_field("Welcome card",
                        _card(w, True, DEFAULT_WELCOME_CARD))
    if f.get("enabled"):
        embed.add_field("Leave text", f.get("message") or DEFAULT_FAREWELL)
        embed.add_field("Farewell card",
                        _card(f, False, DEFAULT_FAREWELL_CARD))
    if g.get("enabled") and g.get("custom_info"):
        embed.add_field("Greeting notes", g["custom_info"])
    return embed
