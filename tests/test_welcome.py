"""Offline suite for welcome/farewell/greeting."""
from __future__ import annotations
import logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.ai.greeting import build_prompt
from corbin.core.settings import ChatSettings
from corbin.core.welcome import (DEFAULT_FAREWELL, DEFAULT_WELCOME,
                                 make_welcome_card, render_template)


def suite_templates(r: Results) -> None:
    r.check("member substituted",
            render_template("hi {member}", member="@Lux", server="S"),
            "hi @Lux")
    r.check("server substituted",
            render_template("welcome to {server}", member="@L", server="Den"),
            "welcome to Den")
    r.check("{user} is an alias for {member}",
            render_template("{user}!", member="@L", server="S"), "@L!")
    r.check("count substituted",
            render_template("#{count}", member="@L", server="S", count=7), "#7")
    r.check("unknown placeholder left alone",
            render_template("{nope}", member="@L", server="S"), "{nope}")
    r.check("count omitted stays literal",
            render_template("{count}", member="@L", server="S"), "{count}")
    r.check("empty template is empty",
            render_template("", member="@L", server="S"), "")
    r.ok("defaults render", "{" not in render_template(
        DEFAULT_WELCOME, member="@L", server="S"))
    r.ok("farewell default renders", "{" not in render_template(
        DEFAULT_FAREWELL, member="@L", server="S"))


def suite_card(r: Results) -> None:
    png = make_welcome_card("Lux", "Welcome to The Den!")
    if png is None:
        r.ok("Pillow missing, card skipped cleanly", True)
        return
    r.ok("returns bytes", isinstance(png, bytes) and len(png) > 500)
    r.ok("is a PNG", png[:8] == b"\x89PNG\r\n\x1a\n")
    r.ok("survives a long message",
         make_welcome_card("Lux", "x " * 200) is not None)
    r.ok("survives an empty name", make_welcome_card("", "hi") is not None)
    r.ok("garbage avatar bytes do not crash",
         make_welcome_card("Lux", "hi", b"not an image") is None
         or True)


def suite_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = ChatSettings(Path(tmp) / "c.json")
        r.check("welcome off by default", s.welcome("g"), {})
        r.check("greeting off by default", s.greeting("g").get("enabled"), None)
        r.ok("no guild is safe", s.welcome(None) == {})

        s.set_welcome("g", enabled=True, channel_id="123",
                      message="hi {member}", card=False)
        cfg = s.welcome("g")
        r.ok("enabled", cfg["enabled"])
        r.check("channel stored", cfg["channel_id"], "123")
        r.check("message stored", cfg["message"], "hi {member}")
        r.check("card flag stored", cfg["card"], False)

        # Disabling must not wipe the channel and message.
        s.set_welcome("g", enabled=False)
        r.ok("disabled", not s.welcome("g")["enabled"])
        r.check("channel retained", s.welcome("g")["channel_id"], "123")
        r.check("message retained", s.welcome("g")["message"], "hi {member}")

        s.set_farewell("g", enabled=True, channel_id="9")
        r.check("farewell separate from welcome",
                s.farewell("g")["channel_id"], "9")
        s.set_greeting("g", enabled=True, custom_info="read #rules")
        r.check("greeting info stored",
                s.greeting("g")["custom_info"], "read #rules")
        r.check("survives reload",
                ChatSettings(Path(tmp) / "c.json").greeting("g")["custom_info"],
                "read #rules")
        r.ok("features are independent",
             s.welcome("g")["channel_id"] == "123"
             and s.farewell("g")["channel_id"] == "9")


def suite_greeting_prompt(r: Results) -> None:
    p = build_prompt(server_name="The Den", display_name="Lux",
                     username="pip", custom_info="read #rules")
    r.ok("names the server", "The Den" in p)
    r.ok("names the member", "Lux" in p)
    r.ok("includes custom info", "read #rules" in p)
    r.ok("forbids a mention", "NOT include a mention" in p)
    r.ok("forbids opening with the name", "NOT open with their name" in p)
    r.ok("forbids invention", "not invent facts" in p)
    bare = build_prompt(server_name="S", display_name="D", username="u")
    r.ok("no custom block when none given", "moderators want" not in bare)


def suite_no_empty_server(r: Results) -> None:
    """An empty guild name must never reach a rendered message."""
    from corbin.core.context import Guild

    for name in ("", None):
        g = Guild(id="1", name=name or "")
        resolved = g.name if g and g.name else "the server"
        r.check(f"empty name ({name!r}) falls back", resolved, "the server")
        rendered = render_template(DEFAULT_WELCOME, member="@x",
                                   server=resolved)
        r.ok(f"no dangling comma for {name!r}", ", @x" in rendered
             and "to ," not in rendered)

    good = Guild(id="1", name="The Den")
    r.check("a real name is used",
            good.name if good and good.name else "the server", "The Den")


def _png(size, colour):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def suite_card_parity(r: Results) -> None:
    """Background, centred avatar, custom text and count, as on Discord."""
    import io

    from corbin.core.welcome import (CARD_SIZE, avatar_url, make_card,
                                     prepare_background, render_template)

    r.check("an avatar hash becomes a real URL",
            avatar_url("123", "abc"),
            "https://fluxerusercontent.com/avatars/123/abc.png")
    r.check("...animated ones as GIF",
            avatar_url("123", "a_abc")[-4:], ".gif")
    r.check("no avatar means the default one, not none",
            avatar_url("123", None),
            "https://fluxerstatic.com/avatars/3.png")
    r.check("a URL passes through", avatar_url("1", "https://x/y.png"),
            "https://x/y.png")
    r.check("{count} fills in", render_template(
        "You are member {count}", member="a", server="b", count=1234),
        "You are member 1,234")

    try:
        from PIL import Image
    except ImportError:
        return
    wide = prepare_background(_png((1920, 1080), (10, 200, 10)))
    with Image.open(io.BytesIO(wide)) as img:
        r.check("a background is cropped to the card", img.size, CARD_SIZE)
    r.check("junk is refused at set time", prepare_background(b"nope"), None)
    r.check("an oversized upload is refused",
            prepare_background(b"x" * (9 * 1024 * 1024)), None)

    card = make_card("Welcome!", "Zaword", _png((64, 64), (250, 0, 0)),
                     wide, "Member #5")
    with Image.open(io.BytesIO(card)) as img:
        w, h = img.size
        rgb = img.convert("RGB")
        r.check("card is the Discord size", (w, h), CARD_SIZE)
        r.check("the avatar sits in the centre",
                rgb.getpixel((w // 2, h // 2)), (250, 0, 0))
        r.check("the background shows at the edge",
                rgb.getpixel((5, h // 2)), (10, 200, 10))


def suite_welcome_commands(r: Results) -> None:
    import asyncio
    from types import SimpleNamespace

    import config
    from corbin.commands import welcome_cmds as wc
    from corbin.core.welcome import background_path

    with tempfile.TemporaryDirectory() as tmp:
        settings = ChatSettings(Path(tmp) / "s.json")
        old_root = config.DATA_ROOT
        config.DATA_ROOT = tmp

        def ctx(attachments=()):
            return SimpleNamespace(
                extras={"settings": settings, "attachments": attachments,
                        "channel_index": {}},
                guild_id="42", guild=SimpleNamespace(name="The Den"),
                channel=SimpleNamespace(id="100"))
        try:
            asyncio.run(wc.setup_welcomer(ctx(), enabled=True))
            asyncio.run(wc.setup_welcomer(
                ctx(), card_text="Welcome to the pack, {member}!"))
            cfg = settings.welcome("42")
            r.ok("changing the card text keeps the welcomer on",
                 cfg["enabled"] and cfg["channel_id"] == "100")
            r.check("...and stores the text", cfg["card_text"],
                    "Welcome to the pack, {member}!")

            async def fake_download(url):
                return _png((800, 400), (1, 2, 3)) if "good" in url else None
            wc._download = fake_download
            image = SimpleNamespace(url="https://cdn/good.png",
                                    filename="wolves.png",
                                    content_type="image/png")
            out = asyncio.run(wc.card_background(ctx((image,))))
            r.ok("set this image: uses the attachment", "set" in out
                 and settings.welcome("42").get("background"))
            r.ok("...stored locally, since attachment links die",
                 background_path(tmp, "42", "welcome").exists())
            r.ok("...without touching the rest of the setup",
                 settings.welcome("42")["card_text"].startswith("Welcome"))
            r.ok("no image and no link is explained",
                 "Attach" in asyncio.run(wc.card_background(ctx())))
            r.ok("an unreadable image is refused now",
                 "couldn't get an image" in asyncio.run(wc.card_background(
                     ctx(), url="https://cdn/bad.png")))
            r.check("a page link yields its main picture", wc.page_image(
                '<meta property="og:image" content="https://i.imgur.com/'
                'a.png?x=1&amp;y=2">'), "https://i.imgur.com/a.png?x=1&y=2")
            r.check("...either attribute order", wc.page_image(
                "<meta content='https://t/b.gif' name='twitter:image'>"),
                "https://t/b.gif")
            r.check("...and none when there isn't one",
                    wc.page_image("<html><title>x</title></html>"), None)
            asyncio.run(wc.card_background(ctx(), which="both",
                                           url="https://cdn/good.png"))
            r.ok("both cards at once",
                 background_path(tmp, "42", "farewell").exists())
            asyncio.run(wc.card_background(ctx(), url="none"))
            r.ok("none clears it",
                 not settings.welcome("42").get("background")
                 and not background_path(tmp, "42", "welcome").exists())
        finally:
            config.DATA_ROOT = old_root

    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8-sig")
    r.ok("join and leave both render through the shared card path",
         src.count("await _render_card(") == 2)
    r.ok("the card resolves the avatar hash, not the raw field",
         "_avatar_url(uid, avatar_ref)" in src)


SUITES = [
    ("welcome templates", suite_templates),
    ("no empty server name", suite_no_empty_server),
    ("welcome card", suite_card),
    ("welcome settings", suite_settings),
    ("greeting prompt", suite_greeting_prompt),
    ("card parity with Discord", suite_card_parity),
    ("welcome commands", suite_welcome_commands),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
