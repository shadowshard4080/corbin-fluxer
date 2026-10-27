"""Rotating the server banner from #banner-images (owner, 2026-10-19).

A port of the idea behind the Discord ``discord-banner-bot``: every
``interval`` he picks an image posted in the server's #banner-images
channel and makes it the server banner. What Fluxer wants, read from its
source (``fluxerapp/fluxer`` at 2026-10-19):

* ``PATCH /guilds/{id}`` with ``banner`` as a data URI; needs Manage
  Server, and the ``BANNER`` guild feature (every new guild has it;
  ``ANIMATED_BANNER`` for a GIF).
* Up to **10 MB**. The server stores the image as it is: **no resize or
  crop**. Its own settings screen crops to **16:9, at least 960x540**
  (up to 2048x1152), and the sidebar clamps the shown ratio to between
  16:9 and 32:9.

So an image "fits" when it's 960x540 or larger, no side over 2400, a
ratio between 16:9 and 32:9, a format Fluxer takes, and under 10 MB.
One that doesn't is **centre-cropped to 16:9 and scaled to 1920x1080**
(or up to 960x540 if it's smaller than that). Per the owner: the resized
copy is **reposted** in #banner-images and the original post deleted, so
it's ready for every later pick. If he can't delete the original (no
Manage Messages), its images are remembered as replaced and skipped.

Picks are random among images not yet used, so each one comes round
before any repeats, and never the same one twice running. Platform
neutral: the Fluxer specifics come in as plain functions.
"""

from __future__ import annotations

import base64
import io
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .permissions import Perm

logger = logging.getLogger("corbin.banner")

CHANNEL_NAME = "banner-images"
CHANNEL_TOPIC = ("Images posted here become the server banner, one at a "
                 "time. Anything that isn't banner-shaped is resized to "
                 "16:9 and reposted.")
KEY = "banner_rotation"

TARGET = (1920, 1080)
MIN_SIZE = (960, 540)
MAX_SIDE = 2400
MAX_BYTES = 10 * 1024 * 1024
#: The sidebar shows banners between 16:9 and 32:9; a hair of slack for
#: images that are 16:9 to the pixel but round oddly (1366x768).
RATIO_MIN, RATIO_MAX = 16 / 9 * 0.99, 32 / 9 * 1.01
FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}
CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
#: Animated banners are resized frame by frame; past this many frames a
#: GIF is too heavy for the one-core host, and is skipped instead.
MAX_FRAMES = 300

MIN_INTERVAL = 15 * 60
MAX_INTERVAL = 30 * 24 * 3600
DEFAULT_INTERVAL = 24 * 3600
#: How far back #banner-images is read, in messages.
HISTORY_LIMIT = 500

EVERYONE_ALLOW = Perm.VIEW_CHANNEL | Perm.READ_MESSAGE_HISTORY
#: Members can look but not post: whatever's posted here goes on the
#: banner, so it's for whoever the admins allow (admins always can).
EVERYONE_DENY = Perm.SEND_MESSAGES | Perm.ADD_REACTIONS
BOT_ALLOW = (Perm.VIEW_CHANNEL | Perm.SEND_MESSAGES | Perm.ATTACH_FILES
             | Perm.MANAGE_MESSAGES | Perm.READ_MESSAGE_HISTORY
             | Perm.EMBED_LINKS)


class BannerError(RuntimeError):
    """Why the banner couldn't change, worded for an admin."""


# --------------------------------------------------------------- settings

class BannerSettings:
    """``{enabled, channel_id, interval, last_changed, last_image, used,
    replaced, last_error}`` per guild, in the chat settings."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def get(self, guild_id: str) -> dict:
        value = self.settings.store.get(str(guild_id), KEY, {})
        return dict(value) if isinstance(value, dict) else {}

    def update(self, guild_id: str, **changes: Any) -> dict:
        entry = self.get(guild_id)
        for k, v in changes.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        self.settings.store.set(str(guild_id), KEY, entry)
        return entry

    def guilds(self) -> list[str]:
        out = []
        for gid in self.settings.store.guilds():
            if gid.startswith("_"):
                continue
            if self.get(gid).get("enabled") and self.get(gid).get(
                    "channel_id"):
                out.append(gid)
        return out

    def interval(self, guild_id: str) -> int:
        try:
            return int(self.get(guild_id).get("interval")
                       or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL

    def due(self, guild_id: str, now: float) -> bool:
        cfg = self.get(guild_id)
        if not cfg.get("enabled") or not cfg.get("channel_id"):
            return False
        last = float(cfg.get("last_changed") or 0)
        if not last:
            return True       # never changed: the first tick does it
        return now - last >= self.interval(guild_id)


# -------------------------------------------------------------- intervals

def parse_interval(text: str) -> Optional[int]:
    """"daily" -> 86400, "every 12 hours" -> 43200, "30 minutes" -> 1800,
    "2d" -> 172800, "weekly". None when unreadable. Not clamped."""
    import re

    low = (text or "").strip().lower()
    named = {"hourly": 3600, "daily": 86400, "every day": 86400,
             "nightly": 86400, "weekly": 604800, "every week": 604800,
             "twice a day": 43200, "twice daily": 43200}
    if low in named:
        return named[low]
    m = re.search(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?"
                  r"|d|days?|w|wks?|weeks?)\b", low)
    if not m:
        m2 = re.fullmatch(r"(?:every\s+)?(minute|hour|day|week)", low)
        if not m2:
            return None
        return {"minute": 60, "hour": 3600, "day": 86400,
                "week": 604800}[m2.group(1)]
    n, unit = float(m.group(1)), m.group(2)
    mult = 60 if unit.startswith("m") else 3600 if unit.startswith("h") \
        else 86400 if unit.startswith("d") else 604800
    return int(n * mult)


def describe_interval(seconds: int) -> str:
    if seconds % 604800 == 0:
        n, unit = seconds // 604800, "week"
    elif seconds % 86400 == 0:
        n, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        n, unit = seconds // 3600, "hour"
    else:
        n, unit = round(seconds / 60), "minute"
    return f"every {unit}" if n == 1 else f"every {n} {unit}s"


# ---------------------------------------------------------------- images

@dataclass
class Candidate:
    message_id: str
    attachment_id: str
    url: str
    filename: str
    author_id: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    size: int = 0
    content_type: str = ""
    #: The message's other image attachments (a repost carries them all).
    siblings: list = field(default_factory=list)
    by_bot: bool = False


def _is_image(att: dict) -> bool:
    ctype = str(att.get("content_type") or "").split(";")[0].lower()
    name = str(att.get("filename") or "").lower()
    return ctype.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif",
         ".heic", ".tiff"))


def candidates(messages: list, bot_id: str = "") -> list[Candidate]:
    """Every usable image attachment in a channel history (REST message
    payloads), newest first. Expired or link-less attachments are left
    out: Fluxer's ``url`` can be null or expired."""
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        atts = [a for a in (msg.get("attachments") or [])
                if isinstance(a, dict) and _is_image(a)
                and (a.get("url") or a.get("proxy_url"))
                and not a.get("expired")]
        author = (msg.get("author") or {})
        for att in atts:
            out.append(Candidate(
                message_id=str(msg.get("id")),
                attachment_id=str(att.get("id")),
                url=str(att.get("url") or att.get("proxy_url")),
                filename=str(att.get("filename") or "banner.png"),
                author_id=str(author.get("id") or ""),
                width=att.get("width"), height=att.get("height"),
                size=int(att.get("size") or 0),
                content_type=str(att.get("content_type") or ""),
                siblings=[a for a in atts if a is not att],
                by_bot=bool(bot_id) and str(author.get("id")) == str(bot_id)))
    return out


def fits(width: Optional[int], height: Optional[int], size: int = 0,
         content_type: str = "") -> bool:
    """Banner-shaped and banner-sized, judged from what the attachment
    says about itself. Unknown dimensions don't fit (until downloaded)."""
    if not width or not height:
        return False
    ratio = width / height
    ctype = (content_type or "").split(";")[0].lower()
    return (width >= MIN_SIZE[0] and height >= MIN_SIZE[1]
            and max(width, height) <= MAX_SIDE
            and RATIO_MIN <= ratio <= RATIO_MAX
            and (not size or size <= MAX_BYTES)
            and (not ctype or ctype in CONTENT_TYPES))


def pick(cands: list[Candidate], used: list, last: Optional[str],
         replaced: list, rng: random.Random = random.Random()
         ) -> tuple[Optional[Candidate], bool]:
    """``(candidate, cycled)``: a random image not used yet this round.
    ``cycled`` means every image had been used and the round restarted.
    Never the image that's up now, unless it's the only one."""
    skip = set(replaced)
    pool = [c for c in cands if c.attachment_id not in skip]
    if not pool:
        return None, False
    fresh = [c for c in pool if c.attachment_id not in set(used)]
    cycled = not fresh
    if cycled:
        fresh = [c for c in pool if c.attachment_id != last] or pool
    return rng.choice(fresh), cycled


def _target_size(w: int, h: int) -> tuple[int, int, tuple]:
    """The centre 16:9 crop of a ``w`` x ``h`` image, and the size to
    scale it to: 1920x1080 at most, 960x540 at least. A crop between the
    two keeps its own size: upscaling 1778 px to 1920 only blurs it."""
    ratio = 16 / 9
    if w / h > ratio:
        cw, ch = int(round(h * ratio)), h
    else:
        cw, ch = w, int(round(w / ratio))
    left, top = (w - cw) // 2, (h - ch) // 2
    box = (left, top, left + cw, top + ch)
    if cw > TARGET[0]:
        size = TARGET
    elif cw < MIN_SIZE[0]:
        size = MIN_SIZE
    else:
        size = (cw - cw % 2, int(round((cw - cw % 2) / ratio)))
    return size[0], size[1], box


@dataclass
class Fitted:
    data: bytes
    filename: str
    width: int
    height: int
    resized: bool
    animated: bool = False


def fit_image(data: bytes, filename: str = "banner.png") -> Fitted:
    """The image as a banner: unchanged if it already fits, else cropped
    to 16:9 and scaled. Blocking (Pillow): call through ``to_thread``.
    Raises :class:`BannerError` for something that can't be a banner."""
    from PIL import Image, ImageSequence

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise BannerError(f"“{filename}” isn't an image I can read") \
            from exc
    w, h = img.size
    fmt = (img.format or "").upper()
    frames = getattr(img, "n_frames", 1) or 1
    animated = frames > 1
    stem = filename.rsplit(".", 1)[0] or "banner"
    if fmt in FORMATS and len(data) <= MAX_BYTES and \
            fits(w, h, len(data)):
        return Fitted(data, filename, w, h, resized=False,
                      animated=animated)
    tw, th, box = _target_size(w, h)
    if animated and fmt in ("GIF", "WEBP", "PNG"):
        if frames > MAX_FRAMES:
            raise BannerError(f"“{filename}” has {frames} frames; I resize "
                              f"animations of up to {MAX_FRAMES}")
        out_frames, durations = [], []
        for frame in ImageSequence.Iterator(img):
            out_frames.append(frame.convert("RGBA").crop(box).resize(
                (tw, th), Image.LANCZOS))
            durations.append(frame.info.get("duration", 100))
        buf = io.BytesIO()
        out_frames[0].save(buf, format="GIF", save_all=True,
                           append_images=out_frames[1:], loop=0,
                           duration=durations, disposal=2, optimize=True)
        if buf.tell() > MAX_BYTES:
            raise BannerError(f"“{filename}” is still over 10 MB as a "
                              f"banner-sized GIF")
        return Fitted(buf.getvalue(), f"{stem}-banner.gif", tw, th,
                      resized=True, animated=True)
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info)
    out = img.convert("RGBA" if has_alpha else "RGB").crop(box).resize(
        (tw, th), Image.LANCZOS)
    buf = io.BytesIO()
    if has_alpha:
        out.save(buf, format="PNG", optimize=True)
        name = f"{stem}-banner.png"
    else:
        quality = 92
        while True:
            buf = io.BytesIO()
            out.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_BYTES or quality <= 60:
                break
            quality -= 8
        name = f"{stem}-banner.jpg"
    if buf.tell() > MAX_BYTES:
        raise BannerError(f"“{filename}” won't fit under 10 MB")
    return Fitted(buf.getvalue(), name, tw, th, resized=True)


def data_uri(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:6] in (b"GIF89a", b"GIF87a"):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# -------------------------------------------------------------- rotating

@dataclass
class Hooks:
    """The platform side, as plain async functions."""
    history: Callable[[str, int], Awaitable[list]]
    download: Callable[[str], Awaitable[Optional[bytes]]]
    #: (channel_id, content, files) -> message id or None
    post: Callable[[str, str, list], Awaitable[Optional[str]]]
    #: (channel_id, message_id) -> raises on failure
    delete: Callable[[str, str], Awaitable[Any]]
    #: (guild_id, image bytes) -> raises BannerError-worthy failures
    set_banner: Callable[[str, bytes], Awaitable[Any]]
    bot_id: str = ""


@dataclass
class Result:
    changed: bool
    message: str
    image: Optional[str] = None       # the message id now on the banner
    reposted: bool = False


async def rotate(store: BannerSettings, guild_id: str, hooks: Hooks, *,
                 rng: random.Random = random.Random(),
                 now: Optional[float] = None,
                 to_thread: Callable[..., Awaitable[Any]] = None) -> Result:
    """Pick the next image and put it on the banner. Records the outcome
    (``last_changed``, ``used``, ``last_error``) either way: a guild with
    a broken setup waits a full interval before trying again, rather
    than every minute."""
    import asyncio

    run = to_thread or asyncio.to_thread
    gid = str(guild_id)
    cfg = store.get(gid)
    channel = cfg.get("channel_id")
    now = time.time() if now is None else now
    if not channel:
        return Result(False, "There's no #banner-images channel set up.")

    def fail(text: str, *, soon: bool = False) -> Result:
        # ``soon``: an empty channel is checked again in MIN_INTERVAL, not
        # a whole interval later: someone is probably about to post.
        stamp = (now - store.interval(gid) + MIN_INTERVAL) if soon else now
        store.update(gid, last_changed=stamp, last_error=text)
        logger.info("banner for %s: %s", gid, text)  # log-ok: status
        return Result(False, text)

    try:
        messages = await hooks.history(str(channel), HISTORY_LIMIT)
    except Exception as exc:
        return fail(f"I couldn't read <#{channel}>: {exc}")
    cands = candidates(messages, hooks.bot_id)
    used = [str(u) for u in cfg.get("used") or []]
    replaced = [str(u) for u in cfg.get("replaced") or []]
    tried: set = set()
    while True:
        choice, cycled = pick([c for c in cands
                               if c.attachment_id not in tried],
                              used, cfg.get("last_image_att"), replaced, rng)
        if choice is None:
            if not cands:
                return fail(f"<#{channel}> has no images to use yet.",
                            soon=True)
            return fail("None of the images in "
                        f"<#{channel}> would work as a banner.")
        if cycled:
            used = []
        tried.add(choice.attachment_id)
        data = await hooks.download(choice.url)
        if not data:
            logger.info("banner: couldn't download %s; trying another",
                        choice.url)
            continue
        try:
            fitted = await run(fit_image, data, choice.filename)
        except BannerError as exc:
            logger.info("banner: skipping %s: %s", choice.filename, exc)
            replaced.append(choice.attachment_id)
            store.update(gid, replaced=replaced[-1000:])
            continue
        break

    reposted = False
    note = ""
    if fitted.resized:
        # Owner: repost the resized copy, delete the original, so it's
        # ready next time. The whole message is reposted (its other
        # images too, resized where they need it), or deleting it would
        # lose them.
        files = [(fitted.filename, fitted.data)]
        for sib in choice.siblings:
            sdata = await hooks.download(str(sib.get("url")
                                             or sib.get("proxy_url")))
            if not sdata:
                continue
            try:
                sfit = await run(fit_image, sdata,
                                 str(sib.get("filename") or "image.png"))
                files.append((sfit.filename, sfit.data))
            except BannerError:
                files.append((str(sib.get("filename") or "image.png"),
                              sdata))
        credit = (f" (posted by <@{choice.author_id}>)"
                  if choice.author_id and not choice.by_bot else "")
        try:
            await hooks.post(str(channel),
                             f"Resized to {fitted.width}×{fitted.height} "
                             f"for the banner{credit}.", files)
            reposted = True
        except Exception as exc:
            note = f" (couldn't repost the resized copy: {exc})"
        if reposted:
            try:
                await hooks.delete(str(channel), choice.message_id)
            except Exception:
                note = (" (I couldn't delete the original post, so I'll "
                        "skip it from now on: I need Manage Messages in "
                        f"<#{channel}>)")
        # Either way the original's images aren't picked again: the
        # repost replaces them.
        replaced += [choice.attachment_id,
                     *(str(s.get("id")) for s in choice.siblings)]

    try:
        await hooks.set_banner(gid, fitted.data)
    except Exception as exc:
        store.update(gid, replaced=replaced[-1000:])
        return fail(f"Fluxer refused the banner: {_why(exc)}")
    used.append(choice.attachment_id)
    store.update(gid, last_changed=now, last_image=choice.message_id,
                 last_image_att=choice.attachment_id,
                 used=used[-1000:], replaced=replaced[-1000:],
                 last_error=None)
    logger.info("banner for %s: now %s (%dx%d%s)", gid, choice.filename,
                fitted.width, fitted.height,
                ", resized and reposted" if reposted else "")
    return Result(True, f"Banner changed to {choice.filename}"
                        f"{' (resized and reposted)' if reposted else ''}."
                        + note, image=choice.message_id, reposted=reposted)


def _why(exc: BaseException) -> str:
    text = str(exc)
    if "BANNER_REQUIRES_FEATURE" in text or "BANNER" in text.upper() and \
            "FEATURE" in text.upper():
        return ("this server doesn't have the banner feature (animated "
                "ones need ANIMATED_BANNER)")
    if "403" in text or "Missing Permissions" in text or "50013" in text:
        return "I need Manage Server to change the banner"
    return text or type(exc).__name__


async def ensure_channel(http: Any, guild_id: str,
                         bot_id: Optional[str] = None
                         ) -> tuple[Optional[str], bool, list[str]]:
    """Create or adopt #banner-images: ``(id, created, problems)``.

    An existing one is adopted as it is (the server may have set it up on
    purpose). A new one is readable by everyone and postable only by the
    admins and whoever they allow: whatever lands here goes on the
    banner. He gives himself what he needs to repost and delete.
    """
    try:
        channels = list(await http.get_guild_channels(guild_id) or [])
    except Exception as exc:
        return None, False, [f"couldn't list channels: {exc}"]
    for raw in channels:
        if str(raw.get("name") or "").lower() == CHANNEL_NAME and \
                int(raw.get("type") or 0) == 0:
            return str(raw["id"]), False, []
    try:
        created = await http.create_guild_channel(
            guild_id, name=CHANNEL_NAME, type=0, topic=CHANNEL_TOPIC)
        channel_id = str(created.get("id"))
    except Exception as exc:
        return None, False, [f"couldn't create the channel: {exc}"]
    problems = []
    try:
        await http.edit_channel_permissions(
            channel_id, guild_id, allow=str(int(EVERYONE_ALLOW)),
            deny=str(int(EVERYONE_DENY)), type=0)
    except Exception as exc:
        problems.append(f"couldn't stop everyone posting in it: {exc}")
    if bot_id:
        try:
            await http.edit_channel_permissions(
                channel_id, bot_id, allow=str(int(BOT_ALLOW)), deny="0",
                type=1)
        except Exception:
            logger.info("member overwrite for myself refused in %s",
                        channel_id, exc_info=True)
    return channel_id, True, problems
