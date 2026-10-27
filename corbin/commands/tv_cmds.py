"""Live TV: what he can put on. Track T1b of ``VOICE_ROADMAP.md``.

Browsing only, for now: watching is T2. Everything here lists **only
channels that passed the health check** (``IptvCatalogue.listed``), so he
never offers a channel he then can't play.

* "what channels are available?" -> the owner's opening answer
  (2026-10-17): how many are available, an offer to filter, and the
  categories, countries and languages there are, with counts; the totals
  (indexed and available) in the footer.
* "news channels", "channels from Canada", "Spanish ones", "news from the
  UK" -> a numbered, paged embed.
* "which countries / categories / languages?" -> the full list, paged.
* ⬅️ / ➡️ on any of those turn the page in place (the ``tv_list`` panel,
  handled in ``bot.py``); "next page" typed does the same as a new post.

Gated by the owner's ``streaming`` allowlist like the rest of streaming.
Inside an allowed server its admins have the rest (T3, T4,
:mod:`corbin.core.tv_rules`): TV off, category limits, a watch limit,
favourites. A server's category limits apply to **every** count and list
here, and to "put on X": he never lists a channel he'd then refuse.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from ..core import iptv as I
from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.reactions import Controls, legend
from ..core.registry import Param, Risk, command

COLOUR = 0x1F6FEB
PER_PAGE = 15
#: Countries and languages shown in the opening answer; the rest are
#: "and N more".
TOP_COUNTRIES = 10
TOP_LANGUAGES = 10
PAGE_BUTTONS = (("⬅️", "previous page"), ("➡️", "next page"))
PAGE_STEP = {"⬅️": -1, "➡️": 1}
PANEL_KIND = "tv_list"
VIEWS = ("channels", "categories", "countries", "languages")
#: How each list is named to people. "Categories" (owner, 2026-10-17),
#: as iptv-org says; "genres" is still understood when asked for.
VIEW_TITLES = {"categories": "categories", "countries": "countries",
               "languages": "languages"}


def _thousands(n: int) -> str:
    return f"{n:,}"


def _counts(items: list, limit: Optional[int] = None) -> str:
    shown = items if limit is None else items[:limit]
    return " · ".join(f"{name} ({_thousands(n)})" for _, name, n in shown)


# ---------------------------------------------------------------- rendering

def _top(items: list, limit: int, ask: str) -> str:
    text = _counts(items, limit) or "none yet"
    more = len(items) - limit
    if more > 0:
        text += f" · and {more} more (ask “{ask}”)"
    return text


def opener(ov: dict) -> Embed:
    """The answer to "what channels are available?" with no filter."""
    total = ov["total"]
    embed = Embed(
        title="📺 Live TV",
        description=(f"I have **{_thousands(total)}** channels available "
                     f"right now. I can filter them by category, country "
                     f"and language. Which ones would you like to see?"),
        colour=COLOUR)
    embed.add_field("Categories", _counts(ov["categories"]) or "none yet")
    embed.add_field("Countries", _top(ov["countries"], TOP_COUNTRIES,
                                      "which countries?"))
    embed.add_field("Languages", _top(ov["languages"], TOP_LANGUAGES,
                                      "which languages?"))
    # Indexed is everything in the catalogue; available is what passed
    # the health check, the only channels he offers.
    embed.footer = (f"Total channels indexed: "
                    f"{_thousands(ov.get('indexed', total))} · "
                    f"Available now: {_thousands(total)}")
    return embed


#: Country names that read wrong without "the": "from the United States".
_THE = ("United ", "Netherlands", "Philippines", "Bahamas", "Gambia",
        "Czech Republic", "Dominican Republic", "Central African",
        "Maldives", "Marshall Islands", "Solomon Islands", "Comoros",
        "Cayman Islands", "Faroe Islands", "Virgin Islands")


def describe_filters(state: dict, cat: I.Catalogue) -> str:
    """"news from the United Kingdom in Spanish" for a title or a reply."""
    parts = []
    if state.get("category"):
        cid = state["category"]
        parts.append("Other" if cid == I.OTHER_CATEGORY
                     else cat.category_names.get(cid, cid))
    if state.get("query"):
        parts.append(f"“{state['query']}”")
    text = " ".join(parts) or "all"
    if state.get("country"):
        name = cat.country_names.get(state["country"], state["country"])
        text += " from " + ("the " if name.startswith(_THE) else "") + name
    if state.get("language"):
        text += " in " + cat.language_names.get(state["language"],
                                                state["language"])
    return text


def channel_line(n: int, c: I.Channel, cat: I.Catalogue,
                 state: Optional[dict] = None) -> str:
    """One numbered line. A column the list is already filtered on is
    left out: the title says it, and fifteen "· United States · News"
    in a row is noise."""
    state = state or {}
    name = c.name
    # "Investiga" alone doesn't say it's Pluto TV; the network does.
    if c.network and I.normalise(c.network).replace(" ", "") not in \
            I.normalise(c.name).replace(" ", ""):
        name += f" ({c.network})"
    bits = [f"**{n}.** {name}"]
    if not state.get("country"):
        bits.append(cat.country_names.get(c.country, c.country or "?"))
    if c.categories and not state.get("category"):
        bits.append(cat.category_names.get(c.main_category, c.main_category))
    if c.part_time:
        bits.append("Not 24/7")
    return " · ".join(bits)


def _paged(embed: Embed, state: dict, page: int, pages: int) -> Embed:
    state["page"] = page
    turn = f"React {legend(PAGE_BUTTONS)}" if pages > 1 else ""
    watch = ("Say “watch 3” to put one on"
             if (state.get("view") or "channels") == "channels" else "")
    embed.footer = " · ".join(x for x in (f"Page {page} of {pages}", turn,
                                          watch) if x)
    # Always registered, buttons or not: "watch 3" reads the list it
    # picks from out of this state, and a one-page list is the commonest
    # thing to pick from. Page buttons only when there are pages to turn.
    embed.controls = Controls(kind=PANEL_KIND,
                              buttons=PAGE_BUTTONS if pages > 1 else (),
                              state=dict(state))
    return embed


def permit(rules: Any, guild_id: Any) -> Optional[Any]:
    """The server's category rules as a channel predicate, or None when
    it has none (or there's no server)."""
    if rules is None or not guild_id or \
            not rules.has_category_rules(str(guild_id)):
        return None
    return lambda c: rules.permits(str(guild_id), c)


def _both(a: Any, b: Optional[Any]) -> Any:
    return a if b is None else (lambda c: a(c) and b(c))


def listed_here(iptv: Any, permits: Optional[Any] = None) -> list:
    """Listed channels this server may see."""
    found = iptv.listed_channels()
    return found if permits is None else [c for c in found if permits(c)]


def matching(iptv: Any, state: dict, permits: Optional[Any] = None) -> list:
    """The channels a list state covers. Listed (passed the health check)
    unless the state says ``include: playable``: the "Which one?" list for
    "put on Pluto TV", where an unchecked channel is still worth trying.
    A favourites list (``ids``) is those channels, in order, as long as
    they're still in the catalogue."""
    if state.get("ids") is not None:
        chans = iptv.catalogue.channels
        found = [chans[i] for i in state["ids"] if i in chans]
        return found if permits is None else [c for c in found
                                              if permits(c)]
    include = (iptv.playable if state.get("include") == "playable"
               else iptv.listed)
    return I.search(iptv.catalogue, state.get("query") or None,
                    country=state.get("country"),
                    category=state.get("category"),
                    language=state.get("language"),
                    include=_both(include, permits))


def _on_now(guide: Any, c: I.Channel) -> str:
    """" · now: X (N min left)" from the cached guide, or ""."""
    from ..core.epg import short

    for s in c.streams:
        if guide.covers(s.url):
            found = guide.peek(s.url, c.country)
            if found:
                return f" · now: {short(found, guide.clock())}"
    return ""


def render(iptv: Any, state: dict, permits: Optional[Any] = None,
           guide: Any = None) -> Embed:
    """The embed for a listing ``state``: ``{"view", "page", "query",
    "category", "country", "language"}``. Clamps ``state["page"]`` in
    place, so a page turn past the end stays on the last page."""
    cat = iptv.catalogue
    view = state.get("view") or "channels"
    if view == "channels":
        found = matching(iptv, state, permits)
        rows, page, pages, first = I.page(found, int(state.get("page") or 1),
                                          PER_PAGE)
        heading = ("favourites" if state.get("ids") is not None
                   else describe_filters(state, cat))
        # A "what's on" list (state["guide"]) shows each channel's
        # programme, from the guide already downloaded (peek: no fetch,
        # so a page turn stays instant).
        with_guide = bool(state.get("guide")) and guide is not None
        embed = Embed(
            title=(f"📺 What's on: {heading}" if with_guide else
                   f"📺 Live TV: {heading} ({_thousands(len(found))})"),
            description="\n".join(
                channel_line(first + i, c, cat, state)
                + (_on_now(guide, c) if with_guide else "")
                for i, c in enumerate(rows)),
            colour=COLOUR)
        return _paged(embed, state, page, pages)

    listed = listed_here(iptv, permits)
    items = {"categories": lambda: I.category_index(listed,
                                                    cat.category_names),
             "countries": lambda: I.country_index(listed, cat.country_names),
             "languages": lambda: I.language_index(listed,
                                                   cat.language_names),
             }[view]()
    rows, page, pages, first = I.page(items, int(state.get("page") or 1),
                                      PER_PAGE)
    embed = Embed(
        title=f"📺 Live TV {VIEW_TITLES[view]} ({len(items)})",
        description="\n".join(f"**{first + i}.** {name} ({_thousands(n)})"
                              for i, (_, name, n) in enumerate(rows)),
        colour=COLOUR)
    return _paged(embed, state, page, pages)


# ------------------------------------------------------------------ helpers

def _service(ctx: CommandContext) -> Any:
    return ctx.extras.get("iptv")


def _rules(ctx: CommandContext) -> Any:
    return ctx.extras.get("tv_rules")


def _permits(ctx: CommandContext) -> Optional[Any]:
    return permit(_rules(ctx), ctx.guild_id)


TV_OFF = ("Live TV is turned off in this server. An admin can turn it "
          "back on with “turn the TV back on”.")


def _tv_off(ctx: CommandContext) -> bool:
    rules = _rules(ctx)
    return bool(rules is not None and ctx.guild_id
                and not rules.enabled(str(ctx.guild_id)))


def _refusal(ctx: CommandContext, channel: I.Channel) -> Optional[str]:
    """Why this server won't put ``channel`` on, or None."""
    rules = _rules(ctx)
    if rules is None or not ctx.guild_id or \
            rules.permits(str(ctx.guild_id), channel):
        return None
    cat = _service(ctx).catalogue
    names = lambda ids: ", ".join(  # noqa: E731
        "Other" if i == I.OTHER_CATEGORY else cat.category_names.get(i, i)
        for i in ids)
    allow = rules.allowed_categories(str(ctx.guild_id))
    what = names(channel.categories or [I.OTHER_CATEGORY])
    if allow:
        return (f"{channel.name} is {what}, and this server only allows "
                f"{names(allow)} on the TV.")
    return (f"{channel.name} is {what}, which this server has blocked on "
            f"the TV.")


def _gid(ctx: CommandContext) -> Optional[str]:
    """For the list's state: ``adapter.send`` registers the panel under
    ``state["guild_id"]``, and "watch 3" / "next page" look it up by the
    guild. Without it every list was registered under no guild and could
    never be found again."""
    return str(ctx.guild_id) if ctx.guild_id else None


def _not_ready(iptv: Any) -> Optional[str]:
    """Why there's nothing to list yet, or None."""
    if iptv is None:
        return "Live TV isn't set up on this bot."
    if not len(iptv.catalogue):
        return ("I haven't downloaded the TV channel list yet. "
                "Give me a few minutes.")
    counts = iptv.counts()
    if not counts["listed"]:
        checked = counts[I.OK] + counts[I.FAILED]
        total = checked + counts[I.UNCHECKED]
        return (f"I'm still checking which channels work: "
                f"{_thousands(checked)} of {_thousands(total)} streams so "
                f"far, none confirmed yet. Try again in a bit.")
    return None


def resolve_filters(cat: I.Catalogue, *, query: Optional[str],
                    category: Optional[str], country: Optional[str],
                    language: Optional[str]) -> tuple:
    """``(state, error)``. A word the classifier put in ``query`` that is
    really a category, country or language ("news", "Canada", "Spanish")
    is moved to that filter: "news channels" is a category, not a channel
    called News."""
    state: dict = {"view": "channels", "page": 1}
    if category and I.resolve_category(category, cat) is None:
        # Not a category: the classifier split a channel's name ("Pluto
        # TV Westerns" -> query "PlutoTV", category "Westerns"; live,
        # 2026-10-19). Search it as part of the name; the caller says
        # "unknown category" only if that finds nothing either.
        query = f"{query} {category}".strip() if query else category
        state["_was_category"] = category
        category = None
    for value, resolve, key, what in (
            (category, I.resolve_category, "category", "category"),
            (country, I.resolve_country, "country", "country"),
            (language, I.resolve_language, "language", "language")):
        if value:
            found = resolve(value, cat)
            if found is None:
                return None, (what, value)
            state[key] = found
    if query:
        # "PlutoTV channels" -> "PlutoTV" for the title; search drops the
        # filler words itself, case and all.
        q = re.sub(r"^(the|any|all)\s+|\s+(channels?|ones?|stations?)$",
                   "", query.strip(), flags=re.I).strip()
        for resolve, key in ((I.resolve_category, "category"),
                             (I.resolve_country, "country"),
                             (I.resolve_language, "language")):
            found = resolve(q, cat)
            if found and key not in state:
                state[key] = found
                q = ""
                break
        if q:
            state["query"] = q
    return state, None


def _unknown(what: str, value: str, iptv: Any,
             permits: Optional[Any] = None) -> str:
    cat = iptv.catalogue
    if what == "category":
        names = [n for _, n, _ in I.category_index(listed_here(iptv, permits),
                                                   cat.category_names)]
        return (f"I don't have a category called “{value}”. "
                f"Categories: {', '.join(names)}.")
    if what == "country":
        return (f"I don't know a country called “{value}”. "
                f"Ask “which countries?” for the list.")
    return (f"I don't have channels in a language called “{value}”. "
            f"Ask “which languages?” for the list.")


# ----------------------------------------------------------------- commands

@command(
    name="tv_channels",
    description="List live TV channels by category, country, language or "
                "name",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("tv_list", "list_tv_channels", "what_channels"),
    params=(
        Param("category", "category or genre, e.g. news, sports, kids",
              required=False),
        Param("country", "country, e.g. UK, Canada, Japan", required=False),
        Param("language", "language, e.g. Spanish, English",
              required=False),
        Param("query", "part of a channel's name, e.g. BBC",
              required=False),
    ),
    examples=("what's on TV?", "what's on TV right now?",
              "what do you have streaming on TV?",
              "what TV channels can you put on?",
              "what news channels are there?",
              "any Spanish channels?", "channels from Canada",
              "news channels from the UK", "is there a BBC channel?"),
)
async def tv_channels(ctx: CommandContext, category: Optional[str] = None,
                      country: Optional[str] = None,
                      language: Optional[str] = None,
                      query: Optional[str] = None) -> Any:
    iptv = _service(ctx)
    if _tv_off(ctx):
        return TV_OFF
    why = _not_ready(iptv)
    if why:
        return why
    cat = iptv.catalogue
    permits = _permits(ctx)
    if not any((category, country, language, query)):
        embed = opener(I.overview(listed_here(iptv, permits), cat))
        if permits is not None:
            embed.footer += " · This server limits the categories"
        return embed
    state, bad = resolve_filters(cat, query=query, category=category,
                                 country=country, language=language)
    if bad:
        return _unknown(*bad, iptv, permits)
    state["guild_id"] = _gid(ctx)
    was_category = state.pop("_was_category", None)
    if was_category and not I.search(cat, state.get("query"),
                                     country=state.get("country"),
                                     language=state.get("language")):
        return _unknown("category", was_category, iptv, permits)
    if state.get("category") and permits is not None and not any(
            permits(c) for c in I.search(cat, None,
                                         category=state["category"])):
        name = cat.category_names.get(state["category"], state["category"])
        return f"{name} channels aren't allowed on the TV in this server."
    if not matching(iptv, state, permits):
        what = describe_filters(state, cat)
        unconfirmed = I.search(cat, state.get("query") or None,
                               country=state.get("country"),
                               category=state.get("category"),
                               language=state.get("language"),
                               include=_both(iptv.playable, permits))
        if unconfirmed:
            return (f"No confirmed working channels for {what} yet. "
                    f"{len(unconfirmed)} haven't been checked yet, so ask "
                    f"again later.")
        return f"No working channels for {what} right now."
    guide = ctx.extras.get("tv_guide")
    if state.get("query"):
        # A channel named: "what's on Pluto TV Westerns" lands here as
        # often as on tv-now, so show what's on where there's a guide.
        await _add_guide(state, matching(iptv, state, permits), guide)
    return render(iptv, state, permits, guide)


@command(
    name="tv_filters",
    description="List the live TV categories, countries or languages",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("tv_categories", "tv_genres", "tv_countries", "tv_languages"),
    params=(Param("kind", "what to list",
                  choices=("categories", "genres", "countries",
                           "languages")),),
    examples=("which countries do you have TV channels from?",
              "what categories of TV are there?",
              "what languages are the channels in?"),
)
async def tv_filters(ctx: CommandContext, kind: str = "categories") -> Any:
    iptv = _service(ctx)
    if _tv_off(ctx):
        return TV_OFF
    why = _not_ready(iptv)
    if why:
        return why
    # "categories" is what he says; "genres" is understood too.
    view = {"genres": "categories"}.get(kind, kind)
    if view not in VIEWS:
        view = "categories"
    return render(iptv, {"view": view, "page": 1, "guild_id": _gid(ctx)},
                  _permits(ctx))


@command(
    name="tv_page",
    description="Next or previous page of a live TV list",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("tv_more", "more_tv_channels"),
    params=(Param("direction", "next, previous, or a page number",
                  required=False),),
    examples=("next page of channels", "more channels",
              "previous page", "show page 3 of the channels"),
)
async def tv_page(ctx: CommandContext,
                  direction: Optional[str] = None) -> Any:
    iptv = _service(ctx)
    if _tv_off(ctx):
        return TV_OFF
    panels = ctx.extras.get("panels")
    found = panels.latest(PANEL_KIND, ctx.guild_id) if panels else None
    if iptv is None or found is None or \
            found[1].channel_id != str(ctx.channel.id):
        return ("There's no TV channel list here to page through. "
                "Ask “what channels are available?” first.")
    state = dict(found[1].state)
    d = (direction or "next").strip().lower()
    current = int(state.get("page") or 1)
    if d.isdigit():
        state["page"] = int(d)
    elif d.startswith(("prev", "back", "last")):
        state["page"] = current - 1
    else:
        state["page"] = current + 1
    return render(iptv, state, _permits(ctx))


# ------------------------------------------------------------- watching

#: "watch 3", "number 12", "#5", "channel 7": a pick from the last list.
_NUMBER = re.compile(r"^(?:number|no\.?|#|channel)?\s*(\d{1,4})$", re.I)


def pick_from_list(iptv: Any, panels: Any, guild_id: Any, channel_id: Any,
                   number: int, permits: Optional[Any] = None) -> tuple:
    """``(channel, error)`` for "watch N" against the newest TV list in
    this text channel. Numbers run on across pages, as listed."""
    found = panels.latest(PANEL_KIND, guild_id) if panels else None
    if found is None or found[1].channel_id != str(channel_id):
        return None, ("There's no TV channel list here to pick from. Ask "
                      "“what channels are available?” first, or name the "
                      "channel.")
    state = found[1].state
    if (state.get("view") or "channels") != "channels":
        return None, ("That list is of "
                      f"{VIEW_TITLES.get(state.get('view'), 'filters')}, not "
                      "channels. Pick one first, like “news channels”.")
    items = matching(iptv, state, permits)
    if not 1 <= number <= len(items):
        return None, f"That list only goes up to {len(items)}."
    return items[number - 1], None


def now_watching(w: Any, where: str, *, guide_line: str = "",
                 logo: Optional[bytes] = None) -> Any:
    """The "Now watching" card. ``logo`` (PNG bytes) is uploaded with it
    rather than linked (T4): logo hosts hotlink-block and links rot, the
    same reason the hourly animals are uploaded. Without one, the link
    is the thumbnail as before."""
    from .stream_cmds import WATCH_HINT

    c = w.channel
    embed = Embed(title=f"📺 Now watching: {c.name}", colour=COLOUR)
    lines = [f"In **{where}**."]
    if w.mode in ("picture", "silent", "logo"):
        lines.append(WATCH_HINT)
    if w.note:
        lines.append(w.note)
    embed.description = " ".join(lines)
    if guide_line:
        embed.add_field("What's on", guide_line)
    if c.logo and logo is None:
        embed.thumbnail_url = c.logo
    footer = "Say “stop the TV” to turn it off."
    if getattr(w, "deadline", None) is not None:
        import time as _t
        mins = max(1, int((w.deadline - _t.monotonic() + 59) // 60))
        footer += f" · The server's limit turns it off in {mins} min."
    embed.footer = footer
    if logo is not None:
        return Response(embed=embed, files=(("logo.png", logo),))
    return embed


#: guide_for's reasons when there's no line.
NO_GUIDE, GUIDE_DOWN, NOT_LISTED = "unsupported", "unreachable", "unlisted"


async def guide_lookup(ctx: CommandContext, channel: I.Channel,
                       stream: Optional[I.Stream] = None) -> tuple:
    """``(line, why)``: "On now: X (N min left) · Next: Y" and None, or ""
    and why not: NO_GUIDE (not a Pluto channel), GUIDE_DOWN (the guide
    couldn't be downloaded), NOT_LISTED (downloaded, channel not in it).
    Those three must never be worded the same."""
    guide = ctx.extras.get("tv_guide")
    if guide is None:
        return "", GUIDE_DOWN
    from ..core.epg import GuideUnavailable, describe

    streams = [stream] if stream is not None else list(channel.streams)
    covered = [s for s in streams if guide.covers(s.url)]
    if not covered:
        return "", NO_GUIDE
    down = False
    for s in covered:
        try:
            found = await guide.lookup(s.url, channel.country)
        except GuideUnavailable:
            down = True
            continue
        if found:
            return describe(found, guide.clock()), None
    return "", GUIDE_DOWN if down else NOT_LISTED


async def guide_for(ctx: CommandContext, channel: I.Channel,
                    stream: Optional[I.Stream] = None) -> str:
    """The guide line, or "" (the Now watching card just leaves the
    field off)."""
    return (await guide_lookup(ctx, channel, stream))[0]


#: "the usual", "our usual", "the usual channel".
_USUAL = re.compile(r"^(?:the|our|my)?\s*usual(?:\s+channel)?$", re.I)
#: "favourite 2", "fav #3", "favorite number 1".
_FAV_NUMBER = re.compile(
    r"^(?:our\s+|my\s+)?fav(?:ou?rite)?s?\s*(?:number|no\.?|#)?\s*(\d{1,2})$",
    re.I)


def _channel_by_id(iptv: Any, cid: Optional[str]) -> Optional[I.Channel]:
    return iptv.catalogue.channels.get(cid) if cid else None


@command(
    name="tv_watch",
    description="Put a live TV channel on in voice",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("watch_tv", "put_on_tv_channel", "tv_play"),
    params=(
        Param("channel", "the TV channel's name, its number in the last "
                         "TV list, or “the usual”"),
        Param("voice_channel", "which voice channel", kind="channel",
              required=False),
    ),
    examples=("put on BBC News", "watch Al Jazeera", "watch 3",
              "put number 12 on", "put on the usual",
              "put on favourite 2", "play channel 5 from the TV list"),
)
async def tv_watch(ctx: CommandContext, channel: str,
                   voice_channel: Optional[str] = None) -> Any:
    from ..core.tv import TVError
    from ..core.video import StreamBusy
    from .radio_cmds import _voice_channel

    iptv, tv = _service(ctx), ctx.extras.get("tv")
    if iptv is None or tv is None:
        return "Live TV isn't set up on this bot."
    if _tv_off(ctx):
        return TV_OFF
    if not len(iptv.catalogue):
        return ("I haven't downloaded the TV channel list yet. Give me a "
                "few minutes.")
    target = _voice_channel(ctx, voice_channel)
    if not target:
        if voice_channel:
            return f"I couldn't find a voice channel called “{voice_channel}”."
        return ("You're not in a voice channel. Join one and ask again, or "
                "tell me which.")
    rules = _rules(ctx)
    permits = _permits(ctx)
    gid = str(ctx.guild_id) if ctx.guild_id else None
    text = (channel or "").strip()
    m = _NUMBER.match(text)
    fav = _FAV_NUMBER.match(text)
    if _USUAL.match(text):
        cid = rules.the_usual(gid) if rules is not None else None
        chosen = _channel_by_id(iptv, cid)
        if chosen is None:
            return ("There's no usual yet. Put something on first, or add "
                    "a favourite: “add BBC News to our favourite "
                    "channels”.")
    elif fav:
        favs = rules.favourites(gid) if rules is not None else []
        n = int(fav.group(1))
        if not favs:
            return ("This server has no favourite channels yet. “add BBC "
                    "News to our favourites” makes one.")
        if not 1 <= n <= len(favs):
            return f"There are only {len(favs)} favourites."
        chosen = _channel_by_id(iptv, favs[n - 1])
        if chosen is None:
            return ("That favourite isn't in the channel list any more. "
                    "“remove it from our favourites” tidies it up.")
    elif m:
        chosen, err = pick_from_list(iptv, ctx.extras.get("panels"),
                                     ctx.guild_id, ctx.channel.id,
                                     int(m.group(1)), permits)
        if err:
            return err
    else:
        cat = iptv.catalogue
        results = I.search(cat, text, include=iptv.playable)
        if not results:
            # Known, but every stream is marked failed. Asked for by name,
            # so try anyway: TV.watch probes marked streams last, and the
            # probe decides (a mark refused a working channel, live
            # 2026-10-20).
            results = I.search(cat, text)
            if not results:
                return (f"I don't have a channel called “{text}”. Ask "
                        f"“what channels are available?” to browse.")
        allowed = [c for c in results if permits is None or permits(c)]
        if not allowed:
            return _refusal(ctx, results[0]) or \
                f"{results[0].name} isn't allowed here."
        q = I.clean_query(text)
        if len(allowed) > 1 and I._name_tier(q, allowed[0]) != 0:
            # "put on Pluto TV": hundreds match. Show them, don't guess.
            state, _ = resolve_filters(cat, query=text, category=None,
                                       country=None, language=None)
            state["include"] = "playable"
            state["guild_id"] = _gid(ctx)
            return Response(content="Which one? Say “watch 3” to pick.",
                            embed=render(iptv, state, permits))
        chosen = allowed[0]
    refused = _refusal(ctx, chosen)
    if refused:
        return refused
    try:
        w = await tv.watch(ctx.guild_id, target, chosen, notify_to=target)
    except TVError as exc:
        return str(exc)
    except StreamBusy as exc:
        return str(exc)
    except PermissionError as exc:
        return f"{exc}."
    voice = ctx.extras.get("voice")
    session = voice.get(ctx.guild_id) if voice is not None else None
    where = getattr(session, "channel_name", "") or "the voice channel"
    from ..core.tv import fetch_logo_png

    # A seam: the offline tests must never reach a logo host.
    fetch_logo = ctx.extras.get("tv_logo") or fetch_logo_png
    guide_line, logo = await asyncio.gather(
        guide_for(ctx, chosen, w.stream), fetch_logo(chosen))
    return now_watching(w, where, guide_line=guide_line, logo=logo)


@command(
    name="tv_stop",
    description="Turn the live TV off",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    aliases=("stop_tv", "turn_off_tv"),
    examples=("stop the TV", "turn the TV off", "turn off the channel"),
)
async def tv_stop(ctx: CommandContext) -> str:
    tv = ctx.extras.get("tv")
    w = tv.watching(ctx.guild_id) if tv is not None else None
    if w is None:
        return "The TV isn't on here."
    await tv.stop(ctx.guild_id, f"stopped by {ctx.invoker.id}")
    # Same as stopping any stream: nothing else using the channel, leave.
    voice = ctx.extras.get("voice")
    chat = ctx.extras.get("voicechat")
    session = voice.get(ctx.guild_id) if voice is not None else None
    if session is not None and not session.playing and not (
            chat is not None and chat.active(ctx.guild_id)):
        await voice.disconnect(ctx.guild_id)
        return f"Turned off {w.channel.name}, and I've left the channel."
    return f"Turned off {w.channel.name}."


# ------------------------------------------------------------ what's on

@command(
    name="tv_now",
    description="What programme is on the TV channel that's playing, or a "
                "named one (not \"what channels are there\": that's "
                "tv-channels)",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("whats_on_now", "tv_guide", "tv_schedule"),
    params=(Param("channel", "which channel; the one that's on if not "
                             "given", required=False),),
    topic=r"\b(on|show|programme|program|playing|watching|guide|"
          r"schedule|next)\b",
    # Never "what's on (TV) right now?": that's asking what he can put on
    # (tv-channels), and routing it here answered "The TV isn't on" to a
    # plain "what's on TV right now?" (live, 2026-10-19).
    examples=("what show is on this channel?", "what's this show?",
              "what's on Pluto TV Star Trek right now?",
              "what's on next on this channel?"),
)
async def tv_now(ctx: CommandContext, channel: Optional[str] = None) -> Any:
    iptv, tv = _service(ctx), ctx.extras.get("tv")
    if iptv is None:
        return "Live TV isn't set up on this bot."
    if _tv_off(ctx):
        return TV_OFF
    w = tv.watching(ctx.guild_id) if tv is not None else None
    stream = None
    text = (channel or "").strip()
    number = _NUMBER.match(text) if text else None
    if number:
        # "what's on 3": a pick from the last list here.
        chosen, err = pick_from_list(iptv, ctx.extras.get("panels"),
                                     ctx.guild_id, ctx.channel.id,
                                     int(number.group(1)), _permits(ctx))
        if err:
            return err
    elif text:
        found = I.search(iptv.catalogue, text, include=iptv.playable) or \
            I.search(iptv.catalogue, text)
        if not found:
            return f"I don't have a channel called “{text}”."
        if len(found) > 1 and I._name_tier(I.clean_query(text),
                                           found[0]) != 0:
            # "what's on PlutoTV?": many channels match. Answer for all of
            # them, a page at a time, with each one's programme (owner,
            # 2026-10-19: whether or not anything is streaming). Picking
            # the first match (live: Investiga) answered a question nobody
            # asked.
            return await whats_on_list(ctx, iptv, text)
        chosen = found[0]
    if text and w is not None and w.channel.id == chosen.id:
        stream = w.stream
    elif not text and w is not None:
        chosen, stream = w.channel, w.stream
    if not text and w is None:
        # Nothing on and no channel named: whatever the router thought,
        # "what's on?" here means "what can you put on?". Answer that
        # rather than a dead end.
        listing = await tv_channels(ctx)
        if isinstance(listing, Embed):
            return Response(content="Nothing's on here right now. Here's "
                                    "what I can put on:", embed=listing)
        return listing
    line, why = await guide_lookup(ctx, chosen, stream)
    if not line:
        # Say what CAN be done with it, not just what can't.
        if w is not None and w.channel.id == chosen.id:
            tail = "It's on now: click my stream tile to watch."
        elif iptv.playable(chosen) and _refusal(ctx, chosen) is None:
            tail = f"Say “put on {chosen.name}” to watch it."
        else:
            tail = f"{chosen.name} isn't working right now, though."
        if why == GUIDE_DOWN:
            head = ("I couldn't reach the Pluto TV programme guide just now, "
                    "so I can't say what's on. Try again in a bit.")
        elif why == NOT_LISTED:
            head = (f"Pluto TV's guide doesn't list what's on "
                    f"{chosen.name} right now.")
        else:
            head = (f"I don't have a programme guide for {chosen.name} (I "
                    f"only get guides for Pluto TV channels).")
        return f"{head} {tail}"
    embed = Embed(title=f"📺 {chosen.name}", description=line,
                  colour=COLOUR)
    return embed


async def whats_on_list(ctx: CommandContext, iptv: Any, text: str) -> Any:
    """The channels matching ``text``, each with what's on now, as a
    ``tv_list`` panel: ⬅️/➡️ page through it, "watch 3" puts one on."""
    guide = ctx.extras.get("tv_guide")
    state, _ = resolve_filters(iptv.catalogue, query=text, category=None,
                               country=None, language=None)
    state["include"] = "playable"
    state["guild_id"] = _gid(ctx)
    permits = _permits(ctx)
    found = matching(iptv, state, permits)
    note = "Say “watch 3” to put one on."
    why = await _add_guide(state, found, guide)
    if why == GUIDE_DOWN:
        note = ("I couldn't reach the Pluto TV programme guide just now, "
                "so here are the channels without it. " + note)
    elif why == NO_GUIDE:
        note = ("These channels have no programme guide (I only get "
                "guides for Pluto TV channels). " + note)
    return Response(content=note,
                    embed=render(iptv, state, permits, guide))


async def _add_guide(state: dict, found: list, guide: Any) -> Optional[str]:
    """Turn on the "now:" column for a listing whose first page has
    guide-covered channels. Downloads the all-countries Pluto guide once
    (cached 6 h) so every page turn can ``peek``. Returns None when the
    column is on, else NO_GUIDE / GUIDE_DOWN."""
    if guide is None:
        return GUIDE_DOWN
    if not any(guide.covers(s.url) for c in found[:PER_PAGE]
               for s in c.streams):
        return NO_GUIDE
    if not await guide.warm():
        return GUIDE_DOWN
    state["guide"] = True
    return None


# ------------------------------------------------------------ favourites

@command(
    name="tv_favourite",
    description="Add a TV channel to this server's favourites, or take "
                "one off",
    risk=Risk.WRITE,
    permission=Perm.CONNECT,
    feature="streaming",
    aliases=("tv_favorite", "favourite_channel", "favorite_channel"),
    params=(
        Param("action", "add or remove", choices=("add", "remove")),
        Param("channel", "the channel's name, or “this” for what's on",
              required=False),
    ),
    examples=("add BBC News to our favourite channels",
              "favourite this channel", "remove CNN from the favourites",
              "take Al Jazeera off our favorite channels"),
)
async def tv_favourite(ctx: CommandContext, action: str,
                       channel: Optional[str] = None) -> str:
    iptv, rules, tv = _service(ctx), _rules(ctx), ctx.extras.get("tv")
    if iptv is None or rules is None:
        return "Live TV isn't set up on this bot."
    if _tv_off(ctx):
        return TV_OFF
    gid = str(ctx.guild_id)
    text = (channel or "").strip()
    w = tv.watching(ctx.guild_id) if tv is not None else None
    if not text or text.lower() in ("this", "this channel", "it", "that",
                                    "what's on", "the current channel"):
        if w is None:
            return "Which channel? Nothing's on right now."
        chosen = w.channel
    else:
        found = I.search(iptv.catalogue, text)
        if action == "remove":
            # Removing works on what's actually stored, including a
            # channel no longer in the catalogue.
            favs = [iptv.catalogue.channels.get(i) for i in
                    rules.favourites(gid)]
            names = {c.id: c for c in favs if c is not None}
            found = [c for c in found if c.id in names] or found
        if not found:
            return f"I don't have a channel called “{text}”."
        chosen = found[0]
    from ..core.tv_rules import RuleError

    if action == "remove":
        if rules.remove_favourite(gid, chosen.id):
            return f"Took {chosen.name} off the favourites."
        return f"{chosen.name} isn't one of the favourites."
    refused = _refusal(ctx, chosen)
    if refused:
        return refused
    try:
        added = rules.add_favourite(gid, chosen.id)
    except RuleError as exc:
        return str(exc)
    if not added:
        return f"{chosen.name} is already a favourite."
    n = len(rules.favourites(gid))
    return (f"Added {chosen.name} to the favourites (number {n}). "
            f"“put on favourite {n}” or “put on the usual” to watch.")


@command(
    name="tv_favourites",
    description="List this server's favourite TV channels",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("tv_favorites", "favourite_channels", "favorite_channels"),
    examples=("show our favourite channels", "what are our favorite TV "
                                             "channels?"),
)
async def tv_favourites(ctx: CommandContext) -> Any:
    iptv, rules = _service(ctx), _rules(ctx)
    if iptv is None or rules is None:
        return "Live TV isn't set up on this bot."
    if _tv_off(ctx):
        return TV_OFF
    gid = str(ctx.guild_id)
    favs = rules.favourites(gid)
    if not favs:
        return ("No favourite channels yet. “add BBC News to our "
                "favourites” makes one.")
    state = {"view": "channels", "page": 1, "ids": favs,
             "guild_id": _gid(ctx)}
    embed = render(iptv, state, _permits(ctx))
    usual = _channel_by_id(iptv, rules.the_usual(gid))
    if usual is not None:
        embed.description = (embed.description or "") + \
            f"\n\nThe usual: **{usual.name}**"
    return embed


# --------------------------------------------------- the server's rules

def _names(cat: I.Catalogue, ids: list) -> str:
    return ", ".join("Other" if i == I.OTHER_CATEGORY
                     else cat.category_names.get(i, i) for i in ids)


def settings_embed(ctx: CommandContext) -> Embed:
    from ..core.tv import _duration

    rules, iptv = _rules(ctx), _service(ctx)
    gid = str(ctx.guild_id)
    cat = iptv.catalogue if iptv is not None else I.Catalogue()
    embed = Embed(title="📺 Live TV settings", colour=COLOUR)
    embed.add_field("Live TV", "On" if rules.enabled(gid) else "Off",
                    inline=True)
    allow, block = rules.allowed_categories(gid), \
        rules.blocked_categories(gid)
    embed.add_field("Categories",
                    f"Only {_names(cat, allow)}" if allow else
                    f"All except {_names(cat, block)}" if block else "All",
                    inline=True)
    minutes = rules.max_minutes(gid)
    embed.add_field("Watch limit", _duration(minutes) if minutes else "None",
                    inline=True)
    q = rules.quality(gid)
    embed.add_field("Stream quality", f"{q.name}30"
                    + (f" ({rules.quality_note(gid)})"
                       if rules.quality_note(gid) else ""), inline=True)
    embed.add_field("Favourites", str(len(rules.favourites(gid))),
                    inline=True)
    embed.footer = ("Change with: “turn the TV off”, “only allow news on "
                    "the TV”, “limit the TV to 2 hours”")
    return embed


@command(
    name="tv_settings",
    description="Show this server's live TV settings",
    risk=Risk.SAFE,
    feature="streaming",
    aliases=("tv_status", "tv_config"),
    examples=("show the TV settings", "what are the TV rules here?"),
)
async def tv_settings(ctx: CommandContext) -> Any:
    if _rules(ctx) is None:
        return "Live TV isn't set up on this bot."
    return settings_embed(ctx)


@command(
    name="tv_toggle",
    description="Turn live TV on or off in this server",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    feature="streaming",
    aliases=("tv_enable", "tv_disable", "live_tv_toggle"),
    params=(Param("enabled", "on or off", kind="boolean"),),
    confirm_phrase=lambda a: ("turn live TV back on in this server"
                              if a.get("enabled") else
                              "turn live TV off in this server"),
    examples=("turn live TV off in this server", "turn the TV back on",
              "disable live TV here"),
)
async def tv_toggle(ctx: CommandContext, enabled: bool) -> str:
    rules, tv = _rules(ctx), ctx.extras.get("tv")
    if rules is None:
        return "Live TV isn't set up on this bot."
    gid = str(ctx.guild_id)
    rules.set_enabled(gid, enabled)
    if enabled:
        return "Live TV is on in this server."
    note = ""
    if tv is not None and tv.watching(gid) is not None:
        # Off means off: whatever's on stops now, not at the next pick.
        await tv.stop(gid, f"live TV turned off by {ctx.invoker.id}")
        note = " I've turned off what was on."
    return ("Live TV is off in this server. Music video still works; it "
            "isn't TV." + note)


@command(
    name="tv_restrict",
    description="Limit which TV categories this server can watch",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    feature="streaming",
    aliases=("tv_category_rules", "tv_allow", "tv_block"),
    params=(
        Param("mode", "only (allow just these), block (allow all but "
                      "these), or clear (remove the limit)",
              choices=("only", "block", "clear")),
        Param("categories", "the categories, e.g. news and documentaries",
              required=False),
    ),
    confirm_phrase=lambda a: (
        "remove the TV category limits" if a.get("mode") == "clear" else
        f"only allow {a.get('categories')} on the TV"
        if a.get("mode") == "only" else
        f"block {a.get('categories')} on the TV"),
    examples=("only allow news and documentaries on the TV",
              "block sports channels from the TV",
              "remove the TV category limits"),
)
async def tv_restrict(ctx: CommandContext, mode: str,
                      categories: Optional[str] = None) -> str:
    from ..core.tv_rules import RuleError

    rules, iptv, tv = _rules(ctx), _service(ctx), ctx.extras.get("tv")
    if rules is None or iptv is None:
        return "Live TV isn't set up on this bot."
    gid = str(ctx.guild_id)
    cat = iptv.catalogue
    if mode == "clear":
        rules.set_categories(gid, "clear")
        return "Every TV category is allowed again."
    words = [w.strip() for w in re.split(r",|\band\b|\bor\b|&|/",
                                         categories or "") if w.strip()]
    ids, unknown = [], []
    for word in words:
        found = I.resolve_category(word, cat)
        (ids.append(found) if found else unknown.append(word))
    if unknown:
        known = [n for _, n, _ in I.category_index(iptv.listed_channels(),
                                                   cat.category_names)]
        return (f"I don't have a category called "
                f"{', '.join(f'“{u}”' for u in unknown)}. Categories: "
                f"{', '.join(known) or 'none listed yet'}.")
    try:
        rules.set_categories(gid, mode, ids)
    except RuleError as exc:
        return str(exc)
    note = ""
    w = tv.watching(gid) if tv is not None else None
    if w is not None and not rules.permits(gid, w.channel):
        await tv.stop(gid, "category limit")
        note = f" {w.channel.name} was on and isn't allowed now, so it's off."
    # Named from what was stored, so this reads the same as the settings.
    if mode == "only":
        return (f"The TV here only shows "
                f"{_names(cat, rules.allowed_categories(gid))} now.{note}")
    return (f"{_names(cat, rules.blocked_categories(gid))} channels are "
            f"blocked on the TV here.{note}")


@command(
    name="tv_time_limit",
    description="Set how long the TV can stay on at a time in this server",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    feature="streaming",
    aliases=("tv_limit", "tv_max_time"),
    params=(Param("minutes", "the limit in minutes; 0 for no limit",
                  kind="integer", min_value=0,
                  max_value=7 * 24 * 60),),
    confirm_phrase=lambda a: ("remove the TV time limit"
                              if not a.get("minutes") else
                              f"limit the TV to {a.get('minutes')} minutes"),
    examples=("limit the TV to 2 hours", "TV time limit 90 minutes",
              "remove the TV time limit"),
)
async def tv_time_limit(ctx: CommandContext, minutes: int) -> str:
    from ..core.tv import _duration
    from ..core.tv_rules import RuleError

    rules = _rules(ctx)
    if rules is None:
        return "Live TV isn't set up on this bot."
    try:
        rules.set_max_minutes(str(ctx.guild_id), minutes)
    except RuleError as exc:
        return str(exc)
    if not minutes:
        return "No time limit on the TV here now."
    return (f"The TV turns itself off after {_duration(minutes)} here, "
            f"starting with the next channel put on.")
