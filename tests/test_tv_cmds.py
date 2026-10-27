"""Offline suite for the live TV listing commands (corbin/commands/tv_cmds.py,
Track T1b)."""
from __future__ import annotations
import asyncio, logging, tempfile
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.commands import tv_cmds as T
from corbin.core import iptv as I
from corbin.core.context import (Channel, CommandContext, Embed, Guild,
                                 Member, Platform, Surface, User)
from corbin.core.reactions import Panel
from test_iptv import T0, _raw


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def _service(tmp, *, passed=None):
    """A catalogue from the fixture with ``passed`` URLs checked ok (all
    of them by default) and the CBC stream failed."""
    svc = I.IptvCatalogue(tmp, clock=_Clock(T0))
    svc.catalogue = I.build_catalogue(_raw(), now=T0)
    for url in svc.catalogue.urls():
        if passed is None or url in passed:
            svc.health.record(url, url != "https://c/x", T0)
    return svc


class _Panels:
    def __init__(self, found=None): self.found = found
    def latest(self, kind, gid): return self.found


class _R:
    async def send(self, r): return "m"


def _ctx(svc, panels=None, channel="c1"):
    return CommandContext(
        platform=Platform.FLUXER, surface=Surface.AI,
        invoker=Member(user=User(id="u1", name="Viewer"), guild_id="g1"),
        channel=Channel(id=channel, name="tv", guild_id="g1"),
        guild=Guild(id="g1", name="Test"), responder=_R(),
        extras={"iptv": svc, "panels": panels})


def _call(fn, ctx, **kw):
    return asyncio.run(fn(ctx, **kw))


def suite_opener(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        out = _call(T.tv_channels, _ctx(svc))
        r.ok("no filter: the opening answer, an embed", isinstance(out, Embed))
        r.check("owner's wording", out.description,
                "I have **4** channels available right now. I can filter "
                "them by category, country and language. Which ones would "
                "you like to see?")
        fields = {f.name: f.value for f in out.fields}
        r.check("fields: categories, countries, languages",
                list(fields), ["Categories", "Countries", "Languages"])
        r.check("categories with counts, only listed channels",
                fields["Categories"], "Kids (1) · Movies (1) · News (1) · "
                "Other (1)")
        r.check("languages with counts", fields["Languages"],
                "English (1) · Spanish (1)")
        r.check("footer: indexed and available totals", out.footer,
                "Total channels indexed: 5 · Available now: 4")
        r.ok("countries with counts", "United Kingdom (1)" in
             fields["Countries"] and "Canada" not in fields["Countries"])
        r.ok("no channel list in the opener", out.controls is None)

        ov = I.overview(svc.listed_channels(), svc.catalogue)
        ov["countries"] = [(f"C{i}", f"Land {i:02}", 50 - i)
                           for i in range(25)]
        countries = {f.name: f.value for f in T.opener(ov).fields
                     }["Countries"]
        r.ok("long country list: top 10 and the rest counted",
             "Land 09" in countries and "Land 10" not in countries
             and "and 15 more (ask “which countries?”)" in countries)
        ov["languages"] = [(f"l{i}", f"Tongue {i:02}", 30 - i)
                           for i in range(12)]
        r.ok("long language list: the same",
             "and 2 more (ask “which languages?”)" in
             {f.name: f.value for f in T.opener(ov).fields}["Languages"])


def suite_listing(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        out = _call(T.tv_channels, _ctx(svc), category="news")
        r.ok("a genre lists its working channels",
             "BBC News" in out.description
             and "CBC" not in out.description)
        r.check("filtered by category: the category column is left out",
                out.description, "**1.** BBC News · United Kingdom")
        out = _call(T.tv_channels, _ctx(svc), query="bbc")
        r.check("unfiltered: country and category shown", out.description,
                "**1.** BBC News · United Kingdom · News")
        out = _call(T.tv_channels, _ctx(svc), country="UK")
        r.check("filtered by country: no country column",
                out.description, "**1.** BBC News · News")
        r.ok("'the' where the country needs it",
             "from the United Kingdom" in out.title)
        r.ok("one page: no page buttons, but says how to watch",
             not out.controls.buttons and out.footer ==
             "Page 1 of 1 · Say “watch 3” to put one on")
        r.ok("...and is still registered, so 'watch 1' can find it",
             out.controls.kind == "tv_list"
             and out.controls.state["country"] == "GB")
        r.check("registered under this guild (the adapter reads it from "
                "the state)", out.controls.state.get("guild_id"), "g1")

        out = _call(T.tv_channels, _ctx(svc), query="news")
        r.ok("a genre word in the name slot is treated as a genre",
             out.title.startswith("📺 Live TV: News "))
        out = _call(T.tv_channels, _ctx(svc), query="Spanish")
        r.ok("...and a language word as a language",
             "Telemundo Kids" in out.description and "in Spanish"
             in out.title)
        out = _call(T.tv_channels, _ctx(svc), query="bbc")
        r.ok("a real name searches by name", "BBC News" in out.description)
        svc.catalogue.channels["P"] = I.Channel(
            id="P", name="Investiga", network="Pluto TV", country="US",
            categories=["movies"],
            streams=[I.Stream(url="http://p/1", channel="P")])
        svc.health.record("http://p/1", True, T0)
        out = _call(T.tv_channels, _ctx(svc), query="PlutoTV channels")
        r.check("a network search lists its channels, network shown",
                out.description,
                "**1.** Investiga (Pluto TV) · United States · Movies")
        out = _call(T.tv_channels, _ctx(svc), category="kids",
                    country="Spain")
        r.ok("filters combine; only what's left is shown",
             "**1.** Telemundo Kids · Not 24/7" in out.description
             and "from Spain" in out.title)
        r.check("unknown category names the real ones",
                _call(T.tv_channels, _ctx(svc), category="cars"),
                "I don't have a category called “cars”. Categories: "
                "Movies, Kids, News, Other.")
        r.ok("unknown country", "which countries" in
             _call(T.tv_channels, _ctx(svc), country="Atlantis"))
        r.check("nothing working for a real filter",
                _call(T.tv_channels, _ctx(svc), country="Canada"),
                "No working channels for all from Canada right now.")

    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp, passed={"http://b/720"})
        r.ok("unchecked matches: 'not confirmed yet', not 'none'",
             "haven't been checked yet" in
             _call(T.tv_channels, _ctx(svc), category="movies"))


def suite_paging(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        # Forty listed channels in one genre, to get several pages.
        for i in range(40):
            c = I.Channel(id=f"N{i}", name=f"News {i:02}", country="GB",
                          categories=["news"],
                          streams=[I.Stream(url=f"http://n/{i}",
                                            channel=f"N{i}")])
            svc.catalogue.channels[c.id] = c
            svc.health.record(f"http://n/{i}", True, T0)
        out = _call(T.tv_channels, _ctx(svc), category="news")
        r.ok("several pages: page buttons and a footer",
             out.controls is not None and out.controls.kind == "tv_list"
             and out.footer.startswith("Page 1 of 3"))
        state = dict(out.controls.state)
        r.check("state carries the filter and page",
                (state["category"], state["page"]), ("news", 1))
        state["page"] = 2
        page2 = T.render(svc, state)
        r.ok("numbering runs on across pages",
             page2.description.startswith("**16.** "))
        state["page"] = 99
        T.render(svc, state)
        r.check("a page past the end clamps", state["page"], 3)

        panel = Panel(kind="tv_list", channel_id="c1", guild_id="g1",
                      emojis=("⬅️", "➡️"), state=dict(out.controls.state))
        nxt = _call(T.tv_page, _ctx(svc, _Panels(("m1", panel))))
        r.ok("typed 'next page' gives page 2", nxt.footer.startswith(
            "Page 2 of 3"))
        p3 = _call(T.tv_page, _ctx(svc, _Panels(("m1", panel))),
                   direction="3")
        r.ok("'page 3'", p3.footer.startswith("Page 3 of 3"))
        r.ok("a list in another channel isn't paged from here",
             "no TV channel list here" in _call(
                 T.tv_page, _ctx(svc, _Panels(("m1", panel)), "c2")))

        cats = _call(T.tv_filters, _ctx(svc), kind="categories")
        r.ok("which categories: the full list with counts",
             cats.title == "📺 Live TV categories (4)"
             and "**1.** News (41)" in cats.description)
        r.check("asking for genres gives the same list",
                _call(T.tv_filters, _ctx(svc), kind="genres").title,
                cats.title)
        langs = _call(T.tv_filters, _ctx(svc), kind="languages")
        r.ok("which languages", "English (1)" in langs.description)


def suite_not_ready(r: Results) -> None:
    r.check("no service", T._not_ready(None),
            "Live TV isn't set up on this bot.")
    with tempfile.TemporaryDirectory() as tmp:
        empty = I.IptvCatalogue(tmp, clock=_Clock(T0))
        r.ok("no catalogue yet", "haven't downloaded" in
             _call(T.tv_channels, _ctx(empty)))
        unchecked = _service(tmp, passed=set())
        r.ok("nothing confirmed yet says how far the check has got",
             "0 of 7 streams so far" in _call(T.tv_channels,
                                              _ctx(unchecked)))


class _FakeTV:
    def __init__(self, error=None):
        self.error, self.watched, self.on = error, [], None

    async def watch(self, gid, vc, channel, notify_to=None):
        from corbin.core.tv import TVError, Watching
        if self.error:
            raise TVError(self.error)
        self.watched.append((vc, channel.id))
        self.on = Watching(guild_id=gid, voice_channel_id=vc,
                           channel=channel, stream=channel.streams[0],
                           mode="picture")
        return self.on

    def watching(self, gid):
        return self.on

    async def stop(self, gid, reason=""):
        self.on = None
        return True


def _watch_ctx(svc, tv, panels=None, in_voice=True):
    ctx = _ctx(svc, panels)
    ctx.extras.update({"tv": tv,
                       "voice_states": {"u1": "vc1"} if in_voice else {}})
    return ctx


def suite_watch_cmd(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        tv = _FakeTV()
        out = _call(T.tv_watch, _watch_ctx(svc, tv), channel="BBC News")
        r.ok("by name: puts it on in the asker's voice channel",
             tv.watched == [("vc1", "BBCNews.uk")]
             and out.title == "📺 Now watching: BBC News"
             and "Click my stream tile" in out.description)
        r.check("not in voice", _call(T.tv_watch, _watch_ctx(
            svc, _FakeTV(), in_voice=False), channel="BBC News"),
            "You're not in a voice channel. Join one and ask again, or "
            "tell me which.")
        r.ok("unknown channel", "I don't have a channel called “Zorbo”"
             in _call(T.tv_watch, _watch_ctx(svc, _FakeTV()),
                      channel="Zorbo"))
        tv = _FakeTV()
        _call(T.tv_watch, _watch_ctx(svc, tv), channel="CBC News Network")
        r.check("known but marked dead: asked by name, so it's tried "
                "anyway (the probe decides, not the mark)", tv.watched,
                [("vc1", "CBC.ca")])
        r.check("the TV's own refusal is passed on", _call(
            T.tv_watch, _watch_ctx(svc, _FakeTV("BBC News isn't working "
                                                "right now.")),
            channel="BBC News"), "BBC News isn't working right now.")

        for i in range(3):
            c = I.Channel(id=f"P{i}", name=f"Show {i}", network="Pluto TV",
                          country="US", categories=["movies"],
                          streams=[I.Stream(url=f"http://pl/{i}",
                                            channel=f"P{i}")])
            svc.catalogue.channels[c.id] = c
        tv = _FakeTV()
        out = _call(T.tv_watch, _watch_ctx(svc, tv), channel="Pluto TV")
        r.ok("many matches: asks which, with the list, plays nothing",
             out.content == "Which one? Say “watch 3” to pick."
             and "Show 0 (Pluto TV)" in out.embed.description
             and not tv.watched)
        r.ok("...unchecked ones included: they can still be tried",
             out.embed.title.endswith("(3)"))

        listing = _call(T.tv_channels, _ctx(svc), category="kids")
        panel = Panel(kind="tv_list", channel_id="c1", guild_id="g1",
                      emojis=(), state=dict(listing.controls.state))
        tv = _FakeTV()
        _call(T.tv_watch, _watch_ctx(svc, tv, _Panels(("m", panel))),
              channel="1")
        r.check("'watch 1' picks from the last list here", tv.watched,
                [("vc1", "Telemundo.es")])
        r.check("past the end of the list", _call(T.tv_watch, _watch_ctx(
            svc, _FakeTV(), _Panels(("m", panel))), channel="number 9"),
            "That list only goes up to 1.")
        r.ok("no list here", "no TV channel list here" in _call(
            T.tv_watch, _watch_ctx(svc, _FakeTV()), channel="#2"))

        tv = _FakeTV()
        _call(T.tv_watch, _watch_ctx(svc, tv), channel="BBC News")
        ctx = _watch_ctx(svc, tv)
        r.check("stop the TV", _call(T.tv_stop, ctx), "Turned off BBC News.")
        r.check("...when it's off", _call(T.tv_stop, ctx),
                "The TV isn't on here.")


SUITES = [
    ("tv opening answer", suite_opener),
    ("tv listings", suite_listing),
    ("tv paging", suite_paging),
    ("tv not ready", suite_not_ready),
    ("tv watch command", suite_watch_cmd),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
