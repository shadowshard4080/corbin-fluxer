"""Small utilities: progress, birthday jams, literature clock, image search,
invite, leave, messager, verify, echo."""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..identity import NAME
from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

BAR_WIDTH = 24
#: Fluxer's message limit, same as Discord's.
MAX_ECHO_CHARS = 2000

_FENCED = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)
_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')


def _loose(text: str) -> str:
    """Compare-only form: case, whitespace and a typed \\n don't matter."""
    return " ".join(text.replace("\\n", " ").lower().split())


def verbatim(extracted: str, original: Optional[str]) -> str:
    """The text to post, taken from what was actually written if possible.

    The classifier returns the message as an argument, and a language
    model extracting an argument will happily tidy it. So, in order:
    a fenced code block in the original, then a quoted span that contains
    the extracted text, then the original's own spelling of the extracted
    text, and only then the extracted text itself.
    """
    extracted = (extracted or "").strip()
    text = original or ""
    fenced = _FENCED.search(text)
    if fenced and fenced.group(1).strip():
        return fenced.group(1).strip("\n")
    for match in _QUOTED.finditer(text):
        quoted = match.group(1) or match.group(2) or ""
        if extracted and _loose(extracted) in _loose(quoted):
            return quoted
    if extracted:
        at = text.lower().find(extracted.lower())
        if at >= 0:
            return text[at:at + len(extracted)]
    return extracted


def restore_mentions(text: str, mention_ids: dict) -> str:
    """Turn "@Name" back into a real ping for people mentioned in the ask.

    Mentions are resolved to names before anything else sees a message,
    so without this an echoed "@Robin" would be plain text. Longest names
    first, so "@Robin" never eats the start of "@Robinson".
    """
    for name in sorted(mention_ids, key=len, reverse=True):
        text = re.sub(rf"@{re.escape(name)}(?!\w)", f"<@{mention_ids[name]}>",
                      text, flags=re.IGNORECASE)
    return text


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(width * fraction)))
    return "█" * filled + "░" * (width - filled)


@command(
    name="progress",
    description="How much of the day, week, month or year has passed",
    risk=Risk.SAFE,
    guild_only=False,
    params=(Param("period", "day, week, month or year", required=False,
                  default="year",
                  choices=("day", "week", "month", "year")),),
    examples=("how much of the year is left", "progress of the day",
              "how far through the month are we"),
)
async def progress(ctx: CommandContext, period: str = "year") -> Any:
    now = datetime.now(timezone.utc)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        span = timedelta(days=1)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        span = timedelta(days=7)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        span = timedelta(days=calendar.monthrange(now.year, now.month)[1])
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0,
                            microsecond=0)
        span = timedelta(days=366 if calendar.isleap(now.year) else 365)

    fraction = (now - start) / span
    remaining = start + span - now
    embed = Embed(title=f"The {period} so far", colour=0x5865F2,
                  description=f"`{_bar(fraction)}`  **{fraction * 100:.1f}%**")
    days = remaining.days
    hours = remaining.seconds // 3600
    left = f"{days}d {hours}h" if days else f"{hours}h"
    embed.add_field("Remaining", left, inline=True)
    return embed


@command(
    name="birthdayjams",
    description="The number one song on a given date",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("birthday_jams", "number_one"),
    params=(Param("date", "a date like 1995-06-12"),),
    examples=("what was number one on 1995-06-12",
              "birthday jams for 2000-01-01"),
)
async def birthdayjams(ctx: CommandContext, date: str) -> Any:
    try:
        when = datetime.strptime(date.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return "Give me a date like `1995-06-12`."
    if when > datetime.now():
        return "That's in the future. The charts haven't happened yet."

    url = (f"https://www.birthdayjams.com/us/{when.year}/"
           f"{when.month:02d}/{when.day:02d}/")
    embed = Embed(
        title=f"Number one on {when:%d %B %Y}", url=url, colour=0x1DB954,
        description=f"[See the chart for that day]({url})")
    embed.footer = "birthdayjams.com"
    return embed


@command(
    name="literature_clock",
    description="A book quote that mentions the current time",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("litclock", "book_clock"),
    examples=("literature clock", "give me a book quote for right now"),
)
async def literature_clock(ctx: CommandContext) -> Any:
    import random

    import httpx

    now = datetime.now()
    code = f"{now.hour:02d}_{now.minute:02d}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://literature-clock.jenevoldsen.com/times/{code}.json")
        resp.raise_for_status()
        entries = resp.json()
    except Exception:
        return "Couldn't reach the literature clock just now."
    if not entries:
        return f"No passage catalogued for {now:%H:%M}. Try another minute."

    pick = random.choice(entries)
    quote = str(pick.get("quote") or "").strip()
    embed = Embed(title=f"{now:%H:%M}", description=quote, colour=0x8B6F47)
    author = str(pick.get("author") or "").strip()
    title = str(pick.get("title") or "").strip()
    if author or title:
        embed.add_field("From", " — ".join(x for x in (title, author) if x))
    embed.footer = "literature-clock.jenevoldsen.com"
    return embed


#: (channel, user) -> (query, results, index). Pagination without
#: components: the follow-up is a command, same as "leaderboard page 2".
#: Memory only -- a stale result set is not worth persisting.
_IMAGE_RESULTS: dict = {}
IMAGE_RESULTS_MAX = 10


def _image_embed(query: str, item: dict, index: int, total: int) -> Embed:
    embed = Embed(title=str(item.get("title") or query)[:200],
                  url=(item.get("image") or {}).get("contextLink"),
                  image_url=item.get("link"), colour=0x5865F2)
    embed.footer = f"Google Images \u00b7 {index + 1} of {total}"
    return embed


async def _fetch_images(query: str):
    import asyncio

    import config

    key = getattr(config, "GOOGLE_API_KEY", "")
    cse = getattr(config, "CSE_ID", "")
    if not key or not cse:
        return [], "Image search isn't configured (needs a Google API key)."

    def _run() -> list:
        from googleapiclient.discovery import build

        service = build("customsearch", "v1", developerKey=key)
        result = service.cse().list(q=query, cx=cse, searchType="image",
                                    num=IMAGE_RESULTS_MAX,
                                    safe="active").execute()
        return result.get("items") or []

    try:
        return await asyncio.to_thread(_run), None
    except Exception as exc:
        return [], f"Image search failed: {type(exc).__name__}"


@command(
    name="image_search",
    description="Search the web for an image",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("image", "img"),
    params=(Param("query", "what to look for", max_length=200),),
    examples=("find a picture of a raven", "image search northern lights"),
)
async def image_search(ctx: CommandContext, query: str):
    items, error = await _fetch_images(query)
    if error:
        return error
    if not items:
        return f"Nothing came back for \u201c{query}\u201d."

    _IMAGE_RESULTS[(ctx.channel.id, ctx.invoker.id)] = (query, items, 0)
    embed = _image_embed(query, items[0], 0, len(items))
    if len(items) > 1:
        embed.description = "Say \u201cnext image\u201d for another."
    return embed


@command(
    name="next_image",
    description="Show the next result from your last image search",
    risk=Risk.SAFE,
    guild_only=False,
    aliases=("another_image", "next_picture", "another_one"),
    examples=("next image", "show me another one", "next picture"),
)
async def next_image(ctx: CommandContext):
    key = (ctx.channel.id, ctx.invoker.id)
    stored = _IMAGE_RESULTS.get(key)
    if not stored:
        return ("You haven't searched for an image here yet. Try "
                "\u201cimage search northern lights\u201d.")
    query, items, index = stored
    if len(items) < 2:
        return "That search only turned up the one result."

    index = (index + 1) % len(items)
    _IMAGE_RESULTS[key] = (query, items, index)
    embed = _image_embed(query, items[index], index, len(items))
    if index == 0:
        embed.description = f"Back to the start of \u201c{query}\u201d."
    return embed


@command(
    name="invite",
    description=f"Get a link to add {NAME} to another server",
    risk=Risk.SAFE,
    guild_only=False,
    examples=("how do I add you to my server", "invite link"),
)
async def invite(ctx: CommandContext) -> str:
    import config

    bot_id = getattr(ctx.extras.get("bot_user_id", None), "id", None) \
        or ctx.extras.get("bot_user_id") or ""
    base = getattr(config, "FLUXER_WEB_BASE", "https://web.fluxer.app")
    if not bot_id:
        return "I don't know my own id yet, oddly. Try again in a moment."
    return (f"Add me to a server: {base}/oauth2/authorize"
            f"?client_id={bot_id}&scope=bot")


@command(
    name="leave",
    description=f"Make {NAME} leave this server",
    risk=Risk.DESTRUCTIVE,
    owner_only=True,
    confirm_phrase=lambda a: "leave this server",
    examples=("leave this server",),
)
async def leave(ctx: CommandContext) -> str:
    adapter = ctx.extras["adapter"]
    if ctx.guild_id is None:
        return "I'm not in a server here."
    http = getattr(adapter.client, "_http", None)
    if http is None:
        return "I can't reach the API right now."
    try:
        await http.request(http._route(
            "DELETE", "/users/@me/guilds/{guild_id}",
            guild_id=ctx.guild_id))
    except Exception as exc:
        return f"I couldn't leave: {type(exc).__name__}: {exc}"
    return "Leaving. It's been a pleasure."


@command(
    name="echo",
    description=(f"Owner only: post a message exactly as written, as {NAME}, "
                 "in any channel"),
    risk=Risk.WRITE,
    owner_only=True,
    aliases=("say", "announce_as_corbin"),
    params=(
        Param("channel", "where to post: a #channel, its name, or its id",
              kind="channel"),
        Param("message", "the exact text to post", max_length=MAX_ECHO_CHARS),
    ),
    confirm_phrase=lambda a: f"post that in <#{a.get('channel')}>",
    examples=('echo "Server maintenance tonight at 9" in #announcements',
              "say hello everyone in #general",
              "post this in 123456789012345678: welcome aboard"),
)
async def echo(ctx: CommandContext, channel: str, message: str) -> str:
    adapter = ctx.extras["adapter"]
    target = str(channel or "").strip().lstrip("#<>")
    if not target.isdigit():
        found = (ctx.extras.get("channel_index") or {}).get(target.lower())
        if not found:
            return f"I can't find a channel called “{channel}”."
        target = found
    text = verbatim(message, ctx.extras.get("raw_text"))
    # "\n" typed literally becomes a line break, as on Discord.
    text = text.replace("\\n", "\n")
    text = restore_mentions(text, ctx.extras.get("mention_ids") or {})
    if not text.strip():
        return "There's nothing to post."
    if len(text) > MAX_ECHO_CHARS:
        return (f"That's {len(text)} characters; the limit is "
                f"{MAX_ECHO_CHARS}.")
    sent = await adapter.send(target, Response(content=text))
    if not sent:
        return f"I couldn't post in <#{target}>. Check I can see and send there."
    return f"Posted in <#{target}>."


@command(
    name="messager",
    description="DM everyone with a given role",
    risk=Risk.DESTRUCTIVE,
    permission=Perm.MANAGE_GUILD,
    params=(
        Param("role", "which role", kind="role"),
        Param("message", "what to send", max_length=1500),
    ),
    # Blast radius is unknown until we count the role, so always confirm.
    blast_radius=lambda a: 50,
    confirm_phrase=lambda a: f"DM everyone with that role",
    examples=("DM everyone with the Roleplayers role about the event",),
)
async def messager(ctx: CommandContext, role: str, message: str) -> str:
    adapter = ctx.extras["adapter"]
    if not str(role).isdigit():
        return f"There's no role called “{role}” here."

    http = getattr(adapter.client, "_http", None)
    if http is None:
        return "I can't reach the API right now."
    try:
        members = await http.request(http._route(
            "GET", "/guilds/{guild_id}/members?limit=1000",
            guild_id=ctx.guild_id))
    except Exception as exc:
        return f"I couldn't list members: {type(exc).__name__}"

    targets = [m for m in (members or [])
               if str(role) in [str(r) for r in (m.get("roles") or [])]]
    if not targets:
        return "Nobody has that role."

    sent = failed = 0
    for member in targets:
        uid = str((member.get("user") or {}).get("id") or "")
        if not uid:
            continue
        try:
            ok = await adapter.send_dm(uid, Response(
                content=f"**From {ctx.guild.name if ctx.guild else 'the server'}:**\n{message}"))
            sent += bool(ok)
            failed += not ok
        except Exception:
            failed += 1
    tail = f" {failed} couldn't be reached." if failed else ""
    return f"Sent to {sent} of {len(targets)} people.{tail}"


@command(
    name="verify",
    # "Verify" also reads as "prove who I am", which is the claim
    # command. Say what this one is for so the two stop competing.
    description=f"Owner only: grant a guild or user unlimited {NAME} "
                "access. Nothing to do with proving someone's identity "
                "or with Discord accounts",
    risk=Risk.WRITE,
    owner_only=True,
    params=(
        Param("target", "a user or guild id"),
        Param("enabled", "on or off", kind="boolean", required=False,
              default=True),
    ),
    confirm_phrase=lambda a: f"verify {a.get('target')}",
    examples=("verify this server", "verify user 12345"),
)
async def verify(ctx: CommandContext, target: str,
                 enabled: bool = True) -> str:
    settings = ctx.extras["settings"]
    key = str(target).strip()
    if key.lower() in ("this server", "here") and ctx.guild_id:
        key = ctx.guild_id
    settings.set_verified(key, enabled)
    return f"{'Verified' if enabled else 'Unverified'} `{key}`."
