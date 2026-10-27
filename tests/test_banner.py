"""Offline suite for banner rotation (corbin/core/banner.py and its
commands): what fits, how the rest is resized, the pick order, and the
whole rotate flow against fake Fluxer calls."""
from __future__ import annotations
import asyncio, io, logging, random, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from PIL import Image

from _harness import Results, run

from corbin.commands import banner_cmds as C
from corbin.core import banner as B
from corbin.core.context import (Channel, CommandContext, Guild, Member,
                                 Platform, Surface, User)
from corbin.core.settings import ChatSettings


def _png(w, h, mode="RGB", colour=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new(mode, (w, h), colour if mode == "RGB" else (*colour, 128)) \
        .save(buf, format="PNG")
    return buf.getvalue()


def _jpg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _gif(w, h, frames=4):
    imgs = [Image.new("RGB", (w, h), (i * 50, 0, 0)) for i in range(frames)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=80, loop=0)
    return buf.getvalue()


def _size(data):
    return Image.open(io.BytesIO(data)).size


def suite_fit(r: Results) -> None:
    ok = B.fit_image(_jpg(1920, 1080), "a.jpg")
    r.ok("16:9 at 1920x1080: used as it is", not ok.resized
         and ok.data == _jpg(1920, 1080))
    r.ok("32:9 (the widest the sidebar shows) fits too",
         not B.fit_image(_png(2400, 675), "w.png").resized)
    wide = B.fit_image(_png(4000, 1000), "pano.png")
    r.check("too wide: centre-cropped to 16:9, kept at its own size "
            "(not blurred up to 1920)",
            (wide.resized, _size(wide.data), wide.filename),
            (True, (1778, 1000), "pano-banner.jpg"))
    sq = B.fit_image(_png(1200, 1200), "sq.png")
    r.check("square: cropped to 16:9 at its own width",
            _size(sq.data), (1200, 675))
    tiny = B.fit_image(_png(400, 300), "tiny.png")
    r.check("too small: brought up to the 960x540 minimum",
            _size(tiny.data), (960, 540))
    huge = B.fit_image(_jpg(3840, 2160), "4k.jpg")
    r.check("over 2400 on a side: down to 1920x1080", _size(huge.data),
            (1920, 1080))
    alpha = B.fit_image(_png(1000, 1000, "RGBA"), "logo.png")
    r.ok("transparency kept as PNG", alpha.filename.endswith(".png")
         and Image.open(io.BytesIO(alpha.data)).mode == "RGBA")
    anim = B.fit_image(_gif(800, 800, 5), "dance.gif")
    im = Image.open(io.BytesIO(anim.data))
    r.ok("an animated GIF is resized frame by frame, still animated",
         anim.animated and im.size == (960, 540)
         and getattr(im, "n_frames", 1) == 5)
    r.ok("a banner-shaped GIF is left alone",
         not B.fit_image(_gif(960, 540, 2), "ok.gif").resized)
    try:
        B.fit_image(b"not an image", "x.png")
        bad = None
    except B.BannerError as exc:
        bad = str(exc)
    r.ok("garbage: refused by name", bad and "x.png" in bad)
    r.check("data uri knows its type", B.data_uri(_jpg(10, 10))[:23],
            "data:image/jpeg;base64,")
    r.ok("fits() judges from the attachment's own numbers",
         B.fits(1920, 1080, 500_000, "image/png")
         and not B.fits(1080, 1920) and not B.fits(None, None)
         and not B.fits(1920, 1080, 11 * 1024 * 1024)
         and not B.fits(1920, 1080, 1, "image/bmp"))


def _msg(mid, atts, author="u1"):
    return {"id": mid, "author": {"id": author}, "attachments": atts}


def _att(aid, url=None, w=1920, h=1080, ctype="image/png", **kw):
    return {"id": aid, "filename": f"{aid}.png",
            "url": url or f"http://cdn/{aid}", "width": w, "height": h,
            "content_type": ctype, "size": 1000, **kw}


def suite_pick(r: Results) -> None:
    msgs = [_msg("m1", [_att("a1"), _att("a2")]),
            _msg("m2", [{"id": "t", "filename": "notes.txt",
                         "url": "http://x", "content_type": "text/plain"}]),
            _msg("m3", [_att("a3", expired=True)]),
            _msg("m4", [_att("a4", url=None, proxy_url=None)
                        | {"url": None}]),
            _msg("m5", [_att("a5")], author="bot")]
    cands = B.candidates(msgs, bot_id="bot")
    r.check("images only; expired and link-less left out",
            [c.attachment_id for c in cands], ["a1", "a2", "a5"])
    r.ok("siblings and the bot's own posts are known",
         [s["id"] for s in cands[0].siblings] == ["a2"] and cands[2].by_bot)

    rng = random.Random(4)
    seen, used = [], []
    for _ in range(3):
        c, cycled = B.pick(cands, used, seen[-1] if seen else None, [], rng)
        seen.append(c.attachment_id)
        used.append(c.attachment_id)
    r.check("each image once before any repeats", sorted(seen),
            ["a1", "a2", "a5"])
    c, cycled = B.pick(cands, used, seen[-1], [], rng)
    r.ok("then a new round, never the one that's up", cycled
         and c.attachment_id != seen[-1])
    c, _ = B.pick(cands, [], None, ["a1", "a2"], rng)
    r.check("replaced originals are skipped", c.attachment_id, "a5")
    r.check("nothing left", B.pick(cands, [], None, ["a1", "a2", "a5"],
                                   rng), (None, False))

    for text, want in (("daily", 86400), ("every 12 hours", 43200),
                       ("30 minutes", 1800), ("2d", 172800),
                       ("weekly", 604800), ("every hour", 3600),
                       ("1.5 hours", 5400), ("whenever", None)):
        r.check(f"interval {text!r}", B.parse_interval(text), want)
    r.check("described", [B.describe_interval(s) for s in
                          (86400, 43200, 1800, 604800, 3600)],
            ["every day", "every 12 hours", "every 30 minutes",
             "every week", "every hour"])


class _Fake:
    """Fluxer, as the rotate hooks see it."""
    def __init__(self, messages, files, *, delete_fails=False,
                 banner_fails=None):
        self.messages, self.files = messages, files
        self.posts, self.deleted, self.banners = [], [], []
        self.delete_fails, self.banner_fails = delete_fails, banner_fails

    async def history(self, cid, limit):
        return self.messages

    async def download(self, url):
        return self.files.get(url)

    async def post(self, cid, content, files):
        self.posts.append((content, files))
        return "new"

    async def delete(self, cid, mid):
        if self.delete_fails:
            raise RuntimeError("403 Missing Permissions")
        self.deleted.append(mid)

    async def set_banner(self, gid, data):
        if self.banner_fails:
            raise RuntimeError(self.banner_fails)
        self.banners.append(data)

    def hooks(self):
        return B.Hooks(self.history, self.download, self.post, self.delete,
                       self.set_banner, bot_id="bot")


async def _inline(fn, *a):
    return fn(*a)


def _store(tmp):
    s = B.BannerSettings(ChatSettings(Path(tmp) / "chat.json"))
    s.update("g1", enabled=True, channel_id="chan", interval=3600)
    return s


def _rotate(store, fake, now=1000.0):
    return asyncio.run(B.rotate(store, "g1", fake.hooks(), now=now,
                                rng=random.Random(1), to_thread=_inline))


def suite_rotate(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        good = _jpg(1920, 1080)
        fake = _Fake([_msg("m1", [_att("a1", ctype="image/jpeg")])],
                     {"http://cdn/a1": good})
        res = _rotate(store, fake)
        r.ok("a banner-shaped image: straight on the banner, no repost",
             res.changed and fake.banners == [good] and not fake.posts
             and not fake.deleted)
        cfg = store.get("g1")
        r.ok("recorded: when, which, used", cfg["last_changed"] == 1000.0
             and cfg["used"] == ["a1"] and cfg["last_image"] == "m1")
        r.ok("not due again until the interval passes",
             not store.due("g1", 1000.0 + 3599) and store.due("g1", 4600.0))

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        fake = _Fake([_msg("m1", [_att("a1", w=4000, h=2000),
                                  _att("a2", w=500, h=500)], author="u9")],
                     {"http://cdn/a1": _png(4000, 2000),
                      "http://cdn/a2": _png(500, 500)})
        store.update("g1", used=["a2"])     # so a1 is the pick
        res = _rotate(store, fake)
        content, files = fake.posts[0]
        r.ok("needs resizing: reposted with credit, then the original "
             "deleted", res.reposted and "posted by <@u9>" in content
             and "1920×1080" in content and fake.deleted == ["m1"])
        r.check("the whole message reposts (its other image too, resized)",
                [_size(d) for _, d in files], [(1920, 1080), (960, 540)])
        r.check("the banner is the resized copy", _size(fake.banners[0]),
                (1920, 1080))
        r.ok("the originals won't be picked again",
             {"a1", "a2"} <= set(store.get("g1")["replaced"]))

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        fake = _Fake([_msg("m1", [_att("a1", w=800, h=800)])],
                     {"http://cdn/a1": _png(800, 800)}, delete_fails=True)
        res = _rotate(store, fake)
        r.ok("can't delete the original: still changes, says why, skips "
             "it from now on", res.changed and "Manage Messages" in
             res.message and "a1" in store.get("g1")["replaced"])

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        fake = _Fake([_msg("m1", [_att("a1")])], {"http://cdn/a1":
                                                  _png(1920, 1080)},
                     banner_fails="400 GUILD_BANNER_REQUIRES_FEATURE")
        res = _rotate(store, fake)
        r.ok("Fluxer refusing: explained, recorded, and it waits an "
             "interval rather than retrying every minute",
             not res.changed and "banner feature" in res.message
             and store.get("g1")["last_error"] and
             not store.due("g1", 1000.0 + 60))

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        res = _rotate(store, _Fake([], {}))
        r.ok("no images yet: says so", not res.changed and
             "no images" in res.message)
        r.ok("...and looks again in 15 minutes, not a whole interval",
             not store.due("g1", 1000.0 + 14 * 60)
             and store.due("g1", 1000.0 + 15 * 60))
        fake = _Fake([_msg("m1", [_att("a1")]), _msg("m2", [_att("a2")])],
                     {"http://cdn/a2": _png(1920, 1080)})
        res = _rotate(store, fake)
        r.ok("one won't download: the next one is used",
             res.changed and res.image == "m2")
        fake = _Fake([_msg("m1", [_att("a1")])],
                     {"http://cdn/a1": b"junk"})
        store.update("g1", used=[], replaced=[])
        res = _rotate(store, fake)
        r.ok("only an unreadable image: skipped and said, not crashed",
             not res.changed and "a1" in store.get("g1")["replaced"])


class _Http:
    def __init__(self, channels):
        self.channels, self.created, self.perms = channels, [], []

    async def get_guild_channels(self, gid):
        return self.channels

    async def create_guild_channel(self, gid, **kw):
        self.created.append(kw)
        return {"id": "new"}

    async def edit_channel_permissions(self, cid, oid, **kw):
        self.perms.append((cid, oid, kw))


def suite_channel(r: Results) -> None:
    http = _Http([{"id": "c9", "name": "banner-images", "type": 0}])
    r.check("an existing #banner-images is adopted untouched",
            asyncio.run(B.ensure_channel(http, "g1", "bot")),
            ("c9", False, []))
    r.ok("...nothing created or changed", not http.created
         and not http.perms)
    http = _Http([{"id": "c1", "name": "general", "type": 0}])
    cid, created, problems = asyncio.run(B.ensure_channel(http, "g1",
                                                          "bot"))
    everyone = http.perms[0]
    r.ok("made, readable by everyone, not postable",
         created and cid == "new" and everyone[1] == "g1"
         and int(everyone[2]["deny"]) & int(B.Perm.SEND_MESSAGES)
         and int(everyone[2]["allow"]) & int(B.Perm.VIEW_CHANNEL))
    r.ok("and he can repost and delete there",
         http.perms[1][1] == "bot"
         and int(http.perms[1][2]["allow"]) & int(B.Perm.MANAGE_MESSAGES))


class _R:
    async def send(self, r): return "m"


def suite_commands(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = ChatSettings(Path(tmp) / "chat.json")

        class Adapter:
            client = type("C", (), {"user": type("U", (), {"id": "bot"})})

            def _rest(self):
                return _Http([])

        calls = []

        async def rotate(gid):
            calls.append(gid)
            return B.Result(True, "Banner changed to x.png.")

        ctx = CommandContext(
            platform=Platform.FLUXER, surface=Surface.AI,
            invoker=Member(user=User(id="u1", name="Admin"),
                           guild_id="g1"),
            channel=Channel(id="c1", name="general", guild_id="g1"),
            guild=Guild(id="g1", name="Den"), responder=_R(),
            extras={"settings": settings, "adapter": Adapter(),
                    "banner_rotate": rotate})
        call = lambda fn, **kw: asyncio.run(fn(ctx, **kw))  # noqa: E731
        r.ok("not set up: every command says how to start",
             all("set up banner rotation" in call(fn, **kw) for fn, kw in (
                 (C.banner_interval, {"interval": "daily"}),
                 (C.banner_toggle, {"enabled": False}),
                 (C.banner_now, {}), (C.banner_status, {}))))
        r.ok("too often refused", "15 minutes" in call(
            C.banner_setup, interval="every 5 minutes"))
        r.ok("gibberish interval refused", "don't follow" in call(
            C.banner_setup, interval="whenever"))
        out = call(C.banner_setup, interval="every 12 hours")
        r.ok("set up: made the channel, said the interval and the "
             "resizing", "Made <#new>" in out and "every 12 hours" in out
             and "16:9" in out)
        cfg = B.BannerSettings(settings).get("g1")
        r.ok("stored, and due at once so it visibly works",
             cfg["enabled"] and cfg["interval"] == 43200
             and B.BannerSettings(settings).due("g1", 10))
        r.ok("interval change", "every 6 hours" in call(
            C.banner_interval, interval="every 6 hours"))
        r.ok("pause", "paused" in call(C.banner_toggle, enabled=False)
             and not B.BannerSettings(settings).guilds())
        r.ok("resume", "back on" in call(C.banner_toggle, enabled=True))
        r.check("change now", call(C.banner_now),
                "Banner changed to x.png.")
        r.check("...through the shared rotate", calls, ["g1"])
        fields = {f.name: f.value for f in call(C.banner_status).fields}
        r.ok("status", fields["Channel"] == "<#new>"
             and fields["Changes"] == "every 6 hours")

    from corbin.core.permissions import Perm
    from corbin.core.registry import registry
    specs = {s.name: s for s in registry.ai_invocable()}
    names = ("banner-setup", "banner-interval", "banner-toggle",
             "banner-now", "banner-status")
    r.ok("all registered, all need Manage Server",
         all(specs[n].permission == Perm.MANAGE_GUILD for n in names))
    r.ok("every example stays on topic", all(
        specs[n].on_topic(e) for n in names for e in specs[n].examples))
    r.ok("and 'rotate' alone doesn't reach them",
         not specs["banner-now"].on_topic("rotate the tires"))
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    r.ok("bot.py runs the job on the scheduler",
         'scheduler.add("banner_rotation", _banner_job)' in src)


SUITES = [
    ("banner: what fits and resizing", suite_fit),
    ("banner: picking and intervals", suite_pick),
    ("banner: the rotate flow", suite_rotate),
    ("banner: #banner-images", suite_channel),
    ("banner: commands", suite_commands),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
