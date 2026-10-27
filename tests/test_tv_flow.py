"""Offline suite for live TV's server rules, favourites and guide (T3/T4
of VOICE_ROADMAP.md), driven through the real commands.

The point is the *flow*: every command a member or admin can reach, in
the states that matter (TV off, a category limit, favourites, a watch
already on), and the risk gates on the real registered specs, so a
command that works in isolation can't be unreachable or ungated."""
from __future__ import annotations
import asyncio, gzip, logging, os, tempfile, time
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, member, run

from corbin.commands import stream_cmds as S
from corbin.commands import tv_cmds as T
from corbin.core import epg as E
from corbin.core import iptv as I
from corbin.core.context import (Channel, CommandContext, Embed, Guild,
                                 Member, Platform, Response, Surface, User)
from corbin.core.permissions import Perm
from corbin.core.reactions import Panel
from corbin.core.registry import registry
from corbin.core.risk import Decision, assess
from corbin.core.settings import ChatSettings
from corbin.core.tv_rules import RuleError, TVRules
from corbin.core.video import QUALITIES
from test_iptv import T0, _raw


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def _service(tmp):
    """The fixture catalogue, every stream passed except CBC's. Listed:
    BBC News (news), New Channel (movies), Telemundo Kids (kids), Chaîne
    Libre (no category: Other)."""
    svc = I.IptvCatalogue(tmp, clock=_Clock(T0))
    svc.catalogue = I.build_catalogue(_raw(), now=T0)
    for url in svc.catalogue.urls():
        svc.health.record(url, url != "https://c/x", T0)
    return svc


class _FakeTV:
    def __init__(self):
        self.watched, self.on, self.stopped = [], None, []

    async def watch(self, gid, vc, channel, notify_to=None):
        from corbin.core.tv import Watching
        self.watched.append(channel.id)
        self.on = Watching(guild_id=gid, voice_channel_id=vc,
                           channel=channel, stream=channel.streams[0],
                           mode="picture")
        return self.on

    def watching(self, gid):
        return self.on

    async def stop(self, gid, reason=""):
        self.stopped.append(reason)
        self.on = None
        return True


class _Panels:
    def __init__(self, found=None): self.found = found
    def latest(self, kind, gid): return self.found


class _R:
    async def send(self, r): return "m"


async def _no_logo(channel):
    return None


def _world(tmp, *, allow_1080p=False):
    svc = _service(tmp)
    rules = TVRules(ChatSettings(os.path.join(tmp, "chat.json")),
                    allow_1080p=allow_1080p)
    return svc, rules, _FakeTV()


def _ctx(svc, rules, tv, *, panels=None, guide=None, streams=None,
         channel="c1"):
    return CommandContext(
        platform=Platform.FLUXER, surface=Surface.AI,
        invoker=Member(user=User(id="u1", name="Viewer"), guild_id="g1"),
        channel=Channel(id=channel, name="tv", guild_id="g1"),
        guild=Guild(id="g1", name="Test"), responder=_R(),
        extras={"iptv": svc, "tv_rules": rules, "tv": tv, "panels": panels,
                "voice_states": {"u1": "vc1"}, "tv_logo": _no_logo,
                "tv_guide": guide, "streams": streams})


def _call(fn, ctx, **kw):
    return asyncio.run(fn(ctx, **kw))


def _text(out) -> str:
    """Everything a reply says, whatever shape it came back in."""
    if isinstance(out, str):
        return out
    if isinstance(out, Response):
        return " ".join(x for x in (out.content, _text(out.embed)
                                    if out.embed else "") if x)
    if isinstance(out, Embed):
        return " ".join([out.title or "", out.description or "",
                         *(f"{f.name}: {f.value}" for f in out.fields),
                         out.footer or ""])
    return str(out)


# ------------------------------------------------------------------ rules

def suite_rules_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, _ = _world(tmp)
        bbc = svc.catalogue.channels["BBCNews.uk"]
        loose = svc.catalogue.channels["Loose.fr"]
        r.ok("defaults: on, no limits, 720p, no favourites",
             rules.enabled("g1") and not rules.has_category_rules("g1")
             and rules.max_seconds("g1") is None
             and rules.quality("g1") is QUALITIES["720p"]
             and rules.favourites("g1") == [] and rules.the_usual("g1")
             is None)
        r.ok("no server: off for everything but harmless",
             rules.enabled(None) and rules.permits(None, bbc))

        rules.set_enabled("g1", False)
        r.ok("off", not rules.enabled("g1"))
        rules.set_enabled("g1", True)
        r.ok("back on (stored as absent)", rules.enabled("g1")
             and "enabled" not in rules._get("g1"))

        rules.set_categories("g1", "only", ["news"])
        r.ok("only news: news allowed, Other not",
             rules.permits("g1", bbc) and not rules.permits("g1", loose))
        rules.set_categories("g1", "block", [I.OTHER_CATEGORY])
        r.ok("blocking replaces the allow list, never both",
             rules.allowed_categories("g1") == []
             and rules.blocked_categories("g1") == [I.OTHER_CATEGORY])
        r.ok("Other can be blocked like any category",
             not rules.permits("g1", loose) and rules.permits("g1", bbc))
        two = I.Channel(id="x", name="x", categories=["news", "sports"])
        rules.set_categories("g1", "block", ["sports"])
        r.ok("a channel in a blocked category is blocked even if it's "
             "also news", not rules.permits("g1", two))
        for bad in (lambda: rules.set_categories("g1", "only", []),
                    lambda: rules.set_categories("g1", "block", []),
                    lambda: rules.set_categories("g1", "explode", ["a"])):
            try:
                bad()
                raised = False
            except RuleError:
                raised = True
            r.ok("an empty or unknown category change is refused", raised)
        rules.set_categories("g1", "clear")
        r.ok("clear", not rules.has_category_rules("g1"))

        rules.set_max_minutes("g1", 90)
        r.check("limit", rules.max_seconds("g1"), 5400.0)
        rules.set_max_minutes("g1", 0)
        r.check("0 removes it", rules.max_seconds("g1"), None)
        for n in (-1, 7 * 24 * 60 + 1):
            try:
                rules.set_max_minutes("g1", n)
                raised = False
            except RuleError:
                raised = True
            r.ok(f"limit {n} refused", raised)
        hosted = TVRules(rules.settings, default_max_minutes=120)
        r.check("an explicit 0 beats the host default",
                hosted.max_seconds("g1"), None)
        r.check("an unset server gets the host default",
                hosted.max_seconds("g2"), 7200.0)

        try:
            rules.set_quality("g1", "1080p")
            msg = None
        except RuleError as exc:
            msg = str(exc)
        r.ok("1080p on a host that hasn't allowed it: refused with the "
             "reason, not stored", msg and "one CPU core" in msg
             and "quality" not in rules._get("g1"))
        big = TVRules(rules.settings, allow_1080p=True)
        r.check("allowed: stored", big.set_quality("g1", "1080").name,
                "1080p")
        r.ok("the host turning 1080p off later: streams 720p, and says why",
             rules.quality("g1") is QUALITIES["720p"]
             and "turned off" in rules.quality_note("g1"))
        r.check("720p set", big.set_quality("g1", "HD").name, "720p")
        default1080 = TVRules(rules.settings, allow_1080p=True,
                              default_quality="1080p")
        r.check("...and remembered as a choice, even with a 1080p default",
                default1080.quality("g1").name, "720p")
        r.check("a server that never chose gets the 1080p default",
                default1080.quality("g9").name, "1080p")
        r.check("...but 720p where the host doesn't allow 1080p",
                TVRules(rules.settings, default_quality="1080p")
                .quality("g9").name, "720p")
        try:
            big.set_quality("g1", "4k")
            raised = False
        except RuleError:
            raised = True
        r.ok("anything else refused", raised)

        r.ok("favourite added", rules.add_favourite("g1", "BBCNews.uk"))
        r.ok("not twice", not rules.add_favourite("g1", "BBCNews.uk"))
        rules.add_favourite("g1", "New.us")
        r.check("the usual: the first favourite", rules.the_usual("g1"),
                "BBCNews.uk")
        rules.note_watched("g1", "New.us")
        r.check("...or the last watched, if it's a favourite",
                rules.the_usual("g1"), "New.us")
        rules.note_watched("g1", "Loose.fr")
        r.check("...not a non-favourite while favourites exist",
                rules.the_usual("g1"), "BBCNews.uk")
        r.check("no favourites: the last watched",
                rules.the_usual("g2") or (rules.note_watched("g2", "x")
                                          or rules.the_usual("g2")), "x")
        r.ok("removed", rules.remove_favourite("g1", "BBCNews.uk")
             and rules.favourites("g1") == ["New.us"])
        r.ok("removing a non-favourite says so",
             not rules.remove_favourite("g1", "nope"))

        class Broken:
            class store:
                @staticmethod
                def get(*a):
                    raise OSError("disk")
        b = TVRules(Broken())
        r.ok("unreadable settings read as the defaults, never a crash",
             b.enabled("g1") and b.favourites("g1") == [])


# ---------------------------------------------------------------- TV off

def suite_tv_off(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, tv = _world(tmp)
        ctx = _ctx(svc, rules, tv)
        _call(T.tv_watch, ctx, channel="BBC News")
        out = _call(T.tv_toggle, ctx, enabled=False)
        r.ok("turning it off stops what's on", tv.on is None
             and tv.stopped and "turned off what was on" in out)
        for name, fn, kw in (
                ("listing", T.tv_channels, {}),
                ("filters", T.tv_filters, {}),
                ("paging", T.tv_page, {}),
                ("watching", T.tv_watch, {"channel": "BBC News"}),
                ("the usual", T.tv_watch, {"channel": "the usual"}),
                ("what's on", T.tv_now, {}),
                ("favouriting", T.tv_favourite, {"action": "add",
                                                 "channel": "BBC News"}),
                ("favourites", T.tv_favourites, {})):
            r.check(f"off: {name} says so", _call(fn, ctx, **kw), T.TV_OFF)
        r.check("stopping still works when off (nothing's on)",
                _call(T.tv_stop, ctx), "The TV isn't on here.")
        r.ok("settings still show it, as Off",
             "Live TV: Off" in _text(_call(T.tv_settings, ctx)))
        r.check("back on", _call(T.tv_toggle, ctx, enabled=True),
                "Live TV is on in this server.")
        r.ok("and watching works again",
             "Now watching" in _text(_call(T.tv_watch, ctx,
                                           channel="BBC News")))


# -------------------------------------------------------------- categories

def suite_categories(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, tv = _world(tmp)
        ctx = _ctx(svc, rules, tv)
        # A list made before the limit, to pick from after it.
        before = _call(T.tv_channels, ctx, category="kids")
        old_panel = Panel(kind="tv_list", channel_id="c1", guild_id="g1",
                          emojis=(), state=dict(before.controls.state))

        out = _call(T.tv_restrict, ctx, mode="only",
                    categories="news and documentaries")
        r.check("only news and documentaries", out,
                "The TV here only shows Documentary, News now.")
        op = _call(T.tv_channels, ctx)
        cats = {f.name: f.value for f in op.fields}["Categories"]
        r.check("the opener counts only what's allowed", cats, "News (1)")
        r.ok("...and says the server limits it",
             "limits the categories" in op.footer)
        r.check("a blocked category asked for by name",
                _call(T.tv_channels, ctx, category="kids"),
                "Kids channels aren't allowed on the TV in this server.")
        r.ok("filters list only allowed categories",
             "Kids" not in _call(T.tv_filters, ctx).description)
        r.check("putting on a disallowed channel by name",
                _call(T.tv_watch, ctx, channel="Telemundo Kids"),
                "Telemundo Kids is Kids, and this server only allows "
                "Documentary, News on the TV.")
        r.check("...nothing was put on", tv.watched, [])
        ctx_old = _ctx(svc, rules, tv, panels=_Panels(("m", old_panel)))
        r.check("'watch 1' from a list made before the limit can't sneak "
                "it through", _call(T.tv_watch, ctx_old, channel="1"),
                "That list only goes up to 0.")
        r.ok("an allowed channel still plays",
             "Now watching: BBC News" in _text(_call(
                 T.tv_watch, ctx, channel="BBC News")))

        out = _call(T.tv_restrict, ctx, mode="block", categories="news")
        r.ok("blocking what's on turns it off",
             tv.on is None and "was on and isn't allowed now" in out)
        r.check("blocked, worded as blocked",
                _call(T.tv_watch, ctx, channel="BBC News"),
                "BBC News is News, which this server has blocked on the TV.")
        r.check("a favourite can't be added past the limit either",
                _call(T.tv_favourite, ctx, action="add", channel="BBC News"),
                "BBC News is News, which this server has blocked on the TV.")
        r.ok("unknown category: says which, lists the real ones",
             "“curling”" in _call(T.tv_restrict, ctx, mode="only",
                                  categories="news, curling"))
        r.check("unknown leaves the rule as it was",
                rules.blocked_categories("g1"), ["news"])
        r.check("clear", _call(T.tv_restrict, ctx, mode="clear"),
                "Every TV category is allowed again.")
        r.ok("everything back", "Kids (1)" in {
            f.name: f.value for f in _call(T.tv_channels, ctx).fields
        }["Categories"])


# -------------------------------------------------------------- favourites

def suite_favourites(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, tv = _world(tmp)
        ctx = _ctx(svc, rules, tv)
        r.ok("no usual yet", "There's no usual yet" in
             _call(T.tv_watch, ctx, channel="the usual"))
        r.ok("no favourites yet", "no favourite channels yet" in
             _call(T.tv_watch, ctx, channel="favourite 1"))
        r.ok("favourite 'this' with nothing on", "Nothing's on" in
             _call(T.tv_favourite, ctx, action="add", channel="this"))
        r.check("add by name", _call(T.tv_favourite, ctx, action="add",
                                     channel="BBC News"),
                "Added BBC News to the favourites (number 1). “put on "
                "favourite 1” or “put on the usual” to watch.")
        r.check("not twice", _call(T.tv_favourite, ctx, action="add",
                                   channel="BBC"),
                "BBC News is already a favourite.")
        _call(T.tv_watch, ctx, channel="New Channel")
        r.ok("add 'this channel' while watching",
             "Added New Channel" in _call(T.tv_favourite, ctx, action="add",
                                          channel="this channel"))
        lst = _call(T.tv_favourites, ctx)
        r.ok("the list, numbered, with the usual",
             "**1.** BBC News" in lst.description
             and "**2.** New Channel" in lst.description
             and "The usual: **BBC News**" in lst.description)
        rules.note_watched("g1", "New.us")      # what the real TV does
        r.ok("the usual follows the last favourite watched",
             "The usual: **New Channel**" in
             _call(T.tv_favourites, ctx).description)
        panel = Panel(kind="tv_list", channel_id="c1", guild_id="g1",
                      emojis=(), state=dict(lst.controls.state))
        tv.watched.clear()
        _call(T.tv_watch, _ctx(svc, rules, tv,
                               panels=_Panels(("m", panel))), channel="1")
        r.check("'watch 1' picks from the favourites list", tv.watched,
                ["BBCNews.uk"])
        tv.watched.clear()
        rules.note_watched("g1", "BBCNews.uk")
        _call(T.tv_watch, ctx, channel="favourite 2")
        _call(T.tv_watch, ctx, channel="the usual")
        _call(T.tv_watch, ctx, channel="our usual channel")
        r.check("'favourite 2', then 'the usual' (the last watched "
                "favourite) in any wording", tv.watched,
                ["New.us", "BBCNews.uk", "BBCNews.uk"])
        r.check("past the end", _call(T.tv_watch, ctx, channel="fav #9"),
                "There are only 2 favourites.")
        rules._put("g1", favourites=["Gone.xx", "BBCNews.uk"])
        r.ok("a favourite no longer in the catalogue says so",
             "isn't in the channel list any more" in
             _call(T.tv_watch, ctx, channel="favourite 1"))
        r.check("remove", _call(T.tv_favourite, ctx, action="remove",
                                channel="BBC News"),
                "Took BBC News off the favourites.")
        r.check("remove one that isn't", _call(
            T.tv_favourite, ctx, action="remove", channel="New Channel"),
            "New Channel isn't one of the favourites.")
        r.check("unknown name", _call(T.tv_favourite, ctx, action="add",
                                      channel="Zorbo"),
                "I don't have a channel called “Zorbo”.")


# ------------------------------------------------------------ what's on

class _Guide:
    def __init__(self, found=None, down=False):
        self.found, self.asked, self.down = found, [], down
        self.clock = lambda: T0
        self.warmed = 0

    def covers(self, url):
        return "plu-" in url

    async def lookup(self, url, country=""):
        self.asked.append(url)
        if self.down:
            raise E.GuideUnavailable("down")
        return self.found

    def peek(self, url, country=""):
        return None if self.down else self.found

    async def warm(self, country=""):
        self.warmed += 1
        return not self.down


def suite_whats_on(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, tv = _world(tmp)
        pluto = I.Channel(id="Trek.us", name="Star Trek", network="Pluto TV",
                          country="US", categories=["series"],
                          streams=[I.Stream(
                              url="https://jmp2.uk/plu-5efbd39f8c4ce900075d7698.m3u8",
                              channel="Trek.us")])
        svc.catalogue.channels[pluto.id] = pluto
        svc.health.record(pluto.streams[0].url, True, T0)
        now = E.Programme("The Cage", T0 - 600, T0 + 1500)
        nxt = E.Programme("Where No Man Has Gone Before", T0 + 1500,
                          T0 + 4500)
        guide = _Guide((now, nxt))
        ctx = _ctx(svc, rules, tv, guide=guide)
        idle = _call(T.tv_now, ctx)
        r.ok("nothing on, no channel named: the channel overview, not a "
             "dead end (live: \"what's on TV right now?\")",
             isinstance(idle, Response) and "Nothing's on here" in
             idle.content and "channels available" in idle.embed.description)
        out = _call(T.tv_now, ctx, channel="Star Trek")
        r.check("a named Pluto channel's guide", out.description,
                "On now: **The Cage** (25 min left) · Next: Where No Man "
                "Has Gone Before")
        card = _call(T.tv_watch, ctx, channel="Star Trek")
        r.ok("the Now watching card carries it",
             "What's on: On now: **The Cage**" in _text(card))
        r.ok("what's on for the channel that's on",
             "The Cage" in _text(_call(T.tv_now, ctx)))
        r.check("a channel with no guide says so, and how to watch it",
                _call(T.tv_now, ctx, channel="BBC News"),
                "I don't have a programme guide for BBC News (I only get "
                "guides for Pluto TV channels). Say “put on BBC News” to "
                "watch it.")
        r.ok("...or that it's dead, when it is",
             "isn't working right now" in _call(
                 T.tv_now, ctx, channel="CBC News Network"))
        r.ok("...without asking the guide for a stream it can't cover",
             all("plu-" in u for u in guide.asked))
        r.check("unknown channel", _call(T.tv_now, ctx, channel="Zorbo"),
                "I don't have a channel called “Zorbo”.")

        # Live, 2026-10-19: "What's on PlutoTV?" answered about one
        # arbitrary Pluto channel. Many matches: all of them, with what's
        # on each, whether or not anything is streaming.
        for i in range(3):
            c = I.Channel(id=f"P{i}.us", name=f"Pluto TV Show {i}",
                          network="Pluto TV", country="US",
                          categories=["series"], streams=[I.Stream(
                              url=f"https://jmp2.uk/plu-{i:024x}.m3u8",
                              channel=f"P{i}.us")])
            svc.catalogue.channels[c.id] = c
            svc.health.record(c.streams[0].url, True, T0)
        idle = _ctx(svc, rules, _FakeTV(), guide=guide)
        many = _call(T.tv_now, idle, channel="Pluto TV")
        r.ok("'what's on Pluto TV?': every match, each with what's on",
             isinstance(many, Response)
             and many.embed.title.startswith("📺 What's on")
             and many.embed.description.count("now: The Cage") >= 3)
        r.ok("...a registered list, so 'watch 3' works from it",
             many.embed.controls is not None
             and many.embed.controls.state.get("guide") is True)
        page = T.render(svc, dict(many.embed.controls.state), None, guide)
        r.ok("a page turn keeps the column (from the cached guide)",
             "now: The Cage" in page.description)
        down = _call(T.tv_now, _ctx(svc, rules, _FakeTV(),
                                    guide=_Guide(down=True)),
                     channel="Pluto TV")
        r.ok("guide unreachable: the list anyway, and says why",
             "couldn't reach the Pluto TV programme guide" in down.content
             and "now:" not in down.embed.description)
        r.check("one channel, guide unreachable: never 'no guide for it'",
                _call(T.tv_now, _ctx(svc, rules, _FakeTV(),
                                     guide=_Guide(down=True)),
                      channel="Star Trek"),
                "I couldn't reach the Pluto TV programme guide just now, "
                "so I can't say what's on. Try again in a bit. Say “put on "
                "Star Trek” to watch it.")
        r.ok("downloaded, but the channel isn't in it: worded as that",
             "guide doesn't list what's on Star Trek" in _call(
                 T.tv_now, _ctx(svc, rules, _FakeTV(), guide=_Guide()),
                 channel="Star Trek"))

        # Live: "What's on PlutoTV Westerns, US English?" -> category
        # "Westerns" -> "I don't have a category called Westerns".
        west = I.Channel(id="West.us", name="Pluto TV Westerns",
                         network="Pluto TV", country="US",
                         categories=["movies"], streams=[I.Stream(
                             url="https://jmp2.uk/plu-" + "e" * 24 + ".m3u8",
                             channel="West.us")])
        svc.catalogue.channels[west.id] = west
        svc.health.record(west.streams[0].url, True, T0)
        out = _call(T.tv_channels, idle, query="PlutoTV",
                    category="Westerns", country="US")
        r.ok("a non-category in the category slot joins the name search",
             isinstance(out, Embed) and "Pluto TV Westerns" in
             out.description)
        r.ok("...and a named channel shows what's on",
             "now: The Cage" in out.description)
        r.ok("a real nonsense category still says so",
             "category called “Curling”" in _call(
                 T.tv_channels, idle, category="Curling"))
        r.ok("no guide service at all: says it can't reach one, no crash",
             "couldn't reach" in _call(T.tv_now, _ctx(svc, rules, tv),
                                       channel="Star Trek"))


# ------------------------------------------------------- the watch card

def suite_card(r: Results) -> None:
    from corbin.core.tv import Watching

    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, tv = _world(tmp)
        bbc = svc.catalogue.channels["BBCNews.uk"]
        w = Watching(guild_id="g1", voice_channel_id="vc1", channel=bbc,
                     stream=bbc.streams[0], mode="picture")
        plain = T.now_watching(w, "Lounge")
        r.ok("no uploaded logo: the link is the thumbnail",
             isinstance(plain, Embed) and plain.thumbnail_url == bbc.logo)
        up = T.now_watching(w, "Lounge", logo=b"\x89PNG")
        r.ok("an uploaded logo: attached, not linked",
             isinstance(up, Response) and up.files == (("logo.png",
                                                        b"\x89PNG"),)
             and not up.embed.thumbnail_url)
        w.deadline = time.monotonic() + 1800
        r.ok("the watch limit is in the footer",
             "turns it off in 30 min" in T.now_watching(w, "L").footer)

        async def logo(channel):
            return b"png"
        ctx = _ctx(svc, rules, tv)
        ctx.extras["tv_logo"] = logo
        out = _call(T.tv_watch, ctx, channel="BBC News")
        r.ok("the command uploads the logo it fetched",
             isinstance(out, Response) and out.files)


# ---------------------------------------------------- settings commands

def suite_settings(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc, rules, tv = _world(tmp)
        ctx = _ctx(svc, rules, tv)
        r.check("time limit", _call(T.tv_time_limit, ctx, minutes=120),
                "The TV turns itself off after 2 hours here, starting with "
                "the next channel put on.")
        r.check("no limit", _call(T.tv_time_limit, ctx, minutes=0),
                "No time limit on the TV here now.")
        _call(T.tv_time_limit, ctx, minutes=90)
        _call(T.tv_restrict, ctx, mode="only", categories="news")
        fields = {f.name: f.value for f in _call(T.tv_settings, ctx).fields}
        r.check("the settings card", fields,
                {"Live TV": "On", "Categories": "Only News",
                 "Watch limit": "1 hour 30 minutes",
                 "Stream quality": "720p30", "Favourites": "0"})

        r.check("quality: asked", _call(S.stream_quality, ctx),
                "I stream at 720p30 here.")
        r.ok("1080p refused on this host, with the reason",
             "one CPU core" in _call(S.stream_quality, ctx,
                                     quality="1080p"))
        svc2, rules2, tv2 = _world(tmp, allow_1080p=True)

        class Active:
            guild_id, quality = "g1", QUALITIES["720p"]
        streams = type("S", (), {"active": Active()})()
        ctx2 = _ctx(svc2, rules2, tv2, streams=streams)
        r.check("allowed: set, and a running stream keeps its size",
                _call(S.stream_quality, ctx2, quality="1080p"),
                "I'll stream at 1080p30 here from now on. The stream "
                "that's on keeps its size until it's restarted.")
        r.ok("garbage quality", "720p or 1080p" in _call(
            S.stream_quality, ctx2, quality="potato"))


# ----------------------------------------------------- gates on real specs

NEW = ("tv-now", "tv-favourite", "tv-favourites", "tv-settings",
       "tv-toggle", "tv-restrict", "tv-time-limit", "stream-quality")
ADMIN = ("tv-toggle", "tv-restrict", "tv-time-limit", "stream-quality")
EVERYONE = (Perm.VIEW_CHANNEL | Perm.SEND_MESSAGES | Perm.CONNECT
            | Perm.SPEAK | Perm.READ_MESSAGE_HISTORY)


def suite_gates(r: Results) -> None:
    specs = {s.name: s for s in registry.ai_invocable()}
    r.ok("every new command is registered and routable",
         all(n in specs for n in NEW))
    r.ok("all of them sit behind the owner's streaming allowlist",
         all(specs[n].feature == "streaming" for n in NEW))
    r.ok("every admin setting needs Manage Server",
         all(specs[n].permission == Perm.MANAGE_GUILD for n in ADMIN))
    r.ok("and every one that changes something confirms its phrasing",
         all(specs[n].confirm_phrase is not None for n in ADMIN))
    r.ok("first examples are user-facing and sensible",
         all(specs[n].examples and len(specs[n].examples[0]) > 8
             for n in NEW))
    names = [a for s in registry.ai_invocable() for a in
             (s.name, *(x.replace("_", "-") for x in s.aliases))]
    r.ok("no name or alias is shared between two commands",
         len(names) == len(set(names)))

    alice = member("alice", perms=EVERYONE)
    admin = member("admin", perms=EVERYONE | Perm.MANAGE_GUILD)
    on = frozenset({"streaming"})
    v = assess(specs["tv-toggle"], surface=Surface.AI, invoker=alice,
               args={"enabled": False}, features=on)
    r.check("a member can't turn the TV off", v.decision, Decision.DENIED)
    v = assess(specs["tv-toggle"], surface=Surface.AI, invoker=admin,
               args={"enabled": False})
    r.check("nor can an admin where streaming isn't allowlisted",
            v.decision, Decision.REFUSED)
    v = assess(specs["tv-toggle"], surface=Surface.AI, invoker=admin,
               args={"enabled": False}, features=on, spoken=True)
    r.check("nor by voice", v.decision, Decision.REFUSED)
    v = assess(specs["tv-toggle"], surface=Surface.AI, invoker=admin,
               args={"enabled": False}, features=on)
    r.ok("an admin typing it: runs or confirms",
         v.decision in (Decision.EXECUTE, Decision.CONFIRM))
    v = assess(specs["tv-favourite"], surface=Surface.AI, invoker=alice,
               args={"action": "add"}, features=on)
    r.ok("a member can add a favourite",
         v.decision in (Decision.EXECUTE, Decision.CONFIRM))
    v = assess(specs["tv-now"], surface=Surface.AI, invoker=alice,
               args={}, features=on, spoken=True)
    r.check("'what's on' works by voice", v.decision, Decision.EXECUTE)
    tv_now = specs["tv-now"]
    listing = specs["tv-channels"]
    r.ok("'what's on TV right now?' is the listing's example, never "
         "what's-on-this-channel's",
         "what's on TV right now?" in listing.examples[:3]
         and not any("right now" in e and "Pluto" not in e
                     for e in tv_now.examples))
    r.ok("what's on stays on topic: its examples pass",
         all(tv_now.on_topic(e) for e in tv_now.examples))
    r.ok("...and unrelated chat doesn't", not tv_now.on_topic(
        "what do you think about ravens?"))


# ----------------------------------------------------------------- guide

GUIDE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="5efbd39f8c4ce900075d7698"><display-name>Trek</display-name></channel>
  <programme start="20261019120000 +0000" stop="20261019130000 +0000"
             channel="5efbd39f8c4ce900075d7698">
    <title>The Cage</title><sub-title>Pilot</sub-title></programme>
  <programme start="20261019130000 +0000" stop="20261019140000 +0000"
             channel="5efbd39f8c4ce900075d7698"><title>Next One</title></programme>
  <programme start="20261019060000 +0000" stop="20261019070000 +0000"
             channel="5efbd39f8c4ce900075d7698"><title>Long Gone</title></programme>
  <programme start="garbage" stop="20261019140000 +0000"
             channel="5efbd39f8c4ce900075d7698"><title>Broken</title></programme>
</tv>"""
NOON_30 = 1792413000.0      # 2026-10-19 12:30:00 UTC


def suite_guide(r: Results) -> None:
    r.check("pluto id from an iptv-org stream url", E.pluto_id(
        "https://jmp2.uk/plu-5EFBD39F8C4CE900075D7698.m3u8"),
        "5efbd39f8c4ce900075d7698")
    r.check("not a pluto url", E.pluto_id("http://b/720"), None)
    r.check("xmltv time with offset", E.parse_time("20261019143000 +0200"),
            NOON_30)
    r.check("garbage time is skipped, not guessed",
            E.parse_time("tomorrow"), None)
    g = E.parse_guide(gzip.compress(GUIDE_XML), NOON_30)
    progs = g["5efbd39f8c4ce900075d7698"]
    r.check("gzip parsed; the broken and long-gone entries dropped",
            [p.title for p in progs], ["The Cage", "Next One"])
    cur, nxt = E.now_next(progs, NOON_30)
    r.check("now and next", (cur.title, nxt.title), ("The Cage",
                                                     "Next One"))
    r.check("described", E.describe((cur, nxt), NOON_30),
            "On now: **The Cage: Pilot** (30 min left) · Next: Next One")
    r.check("nothing found describes as nothing", E.describe(None, 0), "")

    async def go():
        asked = []

        async def fetch(url):
            asked.append(url)
            if url.endswith("/us.xml.gz"):
                return gzip.compress(b"<tv></tv>")   # not in this one
            if url.endswith("/all.xml.gz"):
                return GUIDE_XML
            return None
        guide = E.Guide(fetch=fetch, clock=lambda: NOON_30)
        url = "https://jmp2.uk/plu-5efbd39f8c4ce900075d7698.m3u8"
        first = await guide.lookup(url, "US")
        second = await guide.lookup(url, "US")
        none = await guide.lookup("http://b/720", "GB")
        return asked, first, second, none
    asked, first, second, none = asyncio.run(go())
    r.ok("the country's guide first, then every country's",
         asked[:2] == [E.PLUTO_GUIDE.format(region="us"),
                       E.PLUTO_GUIDE.format(region="all")])
    r.check("found in the all-countries file", first[0].title, "The Cage")
    r.check("cached: no second download", len(asked), 2)
    r.check("a non-Pluto stream never downloads anything", none, None)

    async def failing():
        calls = []

        async def fetch(url):
            calls.append(url)
            return None
        guide = E.Guide(fetch=fetch, clock=lambda: NOON_30)
        url = "https://jmp2.uk/plu-5efbd39f8c4ce900075d7698.m3u8"
        raised = 0
        for _ in range(2):
            try:
                await guide.lookup(url, "")
            except E.GuideUnavailable:
                raised += 1
        return calls, raised
    calls, raised = asyncio.run(failing())
    r.check("a failed download says so (raises), never 'not listed'",
            raised, 2)
    r.check("...and isn't downloaded again straight away", len(calls), 1)



SUITES = [
    ("tv rules: the store", suite_rules_store),
    ("tv rules: turned off", suite_tv_off),
    ("tv rules: categories", suite_categories),
    ("tv favourites and the usual", suite_favourites),
    ("tv what's on", suite_whats_on),
    ("tv now-watching card", suite_card),
    ("tv settings commands", suite_settings),
    ("tv commands: gates on the real specs", suite_gates),
    ("tv guide (epg)", suite_guide),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
