"""Offline suite for reaction controls, the verse switcher and music card."""
from __future__ import annotations
import asyncio, logging, time
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.reactions import (Controls, Panel, ReactionPanels, legend,
                                   normalise)

#: This bot's folder (verse parsing and the Gill DB live in it).
REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)


class _Port:
    def __init__(self, can_remove=True):
        self.added, self.removed = [], []
        self.can_remove = can_remove

    async def add_reaction(self, channel_id, message_id, emoji):
        self.added.append((message_id, emoji))
        return True

    async def remove_user_reaction(self, channel_id, message_id, emoji,
                                   user_id):
        self.removed.append((message_id, emoji, user_id))
        return self.can_remove


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def _press(panels, **kw):
    base = dict(message_id="m1", channel_id="c1", user_id="u1",
                emoji="📜", added=True, bot_user_id="bot")
    base.update(kw)
    return asyncio.run(panels.press(**base))


def suite_panels(r: Results) -> None:
    port, clock = _Port(), _Clock()
    panels = ReactionPanels(port, clock=clock)
    seen = []

    async def handler(panel, mid, emoji, uid):
        seen.append((mid, emoji, uid))
    panels.on("verse", handler)

    ctl = Controls(kind="verse", buttons=(("📜", "KJV"), ("📘", "NASB95")),
                   state={"x": 1})
    asyncio.run(panels.attach("m1", "c1", "g1", ctl))
    r.check("attaching adds every reaction, in order",
            port.added, [("m1", "📜"), ("m1", "📘")])
    r.ok("...and registers the panel", panels.get("m1") is not None)

    r.ok("a press runs the handler", _press(panels))
    r.check("...with the message, emoji and presser", seen[-1],
            ("m1", "📜", "u1"))
    r.check("...then takes the presser's reaction off",
            port.removed[-1], ("m1", "📜", "u1"))
    r.ok("our own removal echo is not a press",
         not _press(panels, added=False))

    clock.t += 5
    r.ok("the bot's own reactions are ignored",
         not _press(panels, user_id="bot"))
    r.ok("an emoji the panel doesn't offer is ignored",
         not _press(panels, emoji="🍕"))
    r.ok("an unknown message is ignored",
         not _press(panels, message_id="nope"))
    r.ok("a press goes through", _press(panels, emoji="📘"))
    r.ok("...but the same person again within a second is debounced",
         not _press(panels, emoji="📘"))
    clock.t += 2
    r.ok("...and allowed after", _press(panels, emoji="📘"))

    clock.t += 2
    r.ok("the variation selector doesn't matter",
         _press(panels, emoji="📜️"))
    r.check("...and the handler sees the panel's own spelling",
            seen[-1][1], "📜")

    # Without Manage Messages the reaction stays, so un-reacting presses.
    port2 = _Port(can_remove=False)
    p2 = ReactionPanels(port2, clock=clock)
    hits = []

    async def h2(panel, mid, emoji, uid):
        hits.append(emoji)
    p2.on("verse", h2)
    asyncio.run(p2.attach("m1", "c1", "g1", ctl))
    r.check("un-react is not a press while removal works",
            (_press(p2, added=False), hits), (False, []))
    _press(p2)
    clock.t += 2
    r.ok("after removal fails, un-reacting is a press too",
         _press(p2, added=False))

    # Expiry, and one music panel per guild.
    p3 = ReactionPanels(_Port(), clock=clock, ttl=60)
    asyncio.run(p3.attach("old", "c", "g", ctl))
    clock.t += 61
    r.check("panels expire", p3.get("old"), None)
    music = Controls(kind="music", buttons=(("⏯️", "pause"),),
                     state={"guild_id": "g"}, exclusive=True)
    asyncio.run(p3.attach("card1", "c", "g", music))
    asyncio.run(p3.attach("card2", "c", "g", music))
    asyncio.run(p3.attach("other", "c", "g2", music))
    r.ok("an exclusive panel replaces the guild's previous one",
         p3.get("card1") is None and p3.get("card2") is not None)
    r.ok("...but not another guild's", p3.get("other") is not None)

    # A forgotten panel can be rebuilt by a resolver.
    p4 = ReactionPanels(_Port(), clock=clock)
    p4.on("verse", handler)

    async def resolver(channel_id, message_id, emoji):
        return Panel(kind="verse", channel_id=channel_id, guild_id=None,
                     emojis=("📜",), state={})
    p4.resolver(resolver)
    r.ok("a resolver brings back a panel lost to a restart",
         _press(p4, message_id="lost"))
    r.ok("...and it is registered again", p4.get("lost") is not None)

    from corbin.core.reactions import reaction_emoji

    class _PE:
        def __init__(self, name, unicode=None, id=None):
            self.name, self.unicode, self.id = name, unicode, id
    r.check("the unicode character wins over fluxer.py's demojized name",
            reaction_emoji(_PE(":magnifying_glass_tilted_left:", "🔍")), "🔍")
    r.check("custom emoji are never controls",
            reaction_emoji(_PE("corbin", None, 123)), None)
    r.check("no emoji, no result", reaction_emoji(None), None)
    try:
        from fluxer.models.reaction import PartialEmoji
        real = PartialEmoji.from_data({"name": "🔍", "id": None})
        r.check("...and against the real SDK model", reaction_emoji(real),
                "🔍")
    except ImportError:
        pass

    r.check("legend renders for a footer",
            legend((("📜", "KJV"), ("📘", "NASB95"))), "📜 KJV · 📘 NASB95")
    r.check("normalise strips the variation selector",
            normalise("⏯️"), "⏯")


def suite_verse_controls(r: Results) -> None:
    from corbin.core import verses
    try:
        verses.load(REPO)
    except Exception:
        r.ok("versefinder module unavailable (skipped)", True)
        return
    import corbin.commands.verse_cmds as vc

    async def fake_passage(ref, slug):
        return [(16, f"text in {slug}")]
    real = verses.fetch_passage
    verses.fetch_passage = fake_passage
    try:
        ref = verses.parse("John 3:16")[0]
        e = asyncio.run(vc.build_verse_embed(ref, translation="kjv"))
        r.ok("a verse embed carries controls",
             e.controls is not None and e.controls.kind == "verse")
        r.check("...for KJV, NASB95 and Gill",
                [label for _, label in e.controls.buttons],
                ["KJV", "NASB95", "John Gill's Exposition"])
        r.ok("the footer says how to switch",
             "React to switch" in e.footer and "📖" in e.footer)
        r.ok("...under the translation's name",
             e.footer.startswith("King James Version"))
        n = asyncio.run(vc.build_verse_embed(ref, translation="nasb"))
        r.ok("NASB fetches NASB", "NASB" in n.description)
        r.check("...and remembers it in the panel state",
                n.controls.state["translation"], "nasb")

        class _Gill:
            available = True
            def lookup(self, ref): return [(16, "Gill says...")]
        c = asyncio.run(vc.build_verse_embed(ref, translation="commentary",
                                             gill=_Gill()))
        r.ok("commentary is Gill's", c.footer.startswith("John Gill"))
        r.ok("...with the switcher too", c.controls is not None)
        r.check("the reference reads back from a commentary title",
                vc.ref_from_title(c.title)["chapter"], 3)
        r.check("...and from a plain one", vc.ref_from_title(e.title)["verse"],
                16)
        r.check("a title that isn't a verse gives nothing",
                vc.ref_from_title("Now playing"), None)
    finally:
        verses.fetch_passage = real


def suite_music_card(r: Results) -> None:
    from corbin.commands.music_cmds import (MUSIC_BUTTONS, player_status,
                                            track_embed)
    from corbin.core.audio import Track
    from corbin.core.player import GuildPlayer

    t = Track(title="Song", url="u", webpage_url="w", duration=200)
    plain = track_embed(t, title="Added to the queue")
    r.check("a queue notice has no controls", plain.controls, None)
    card = track_embed(t, guild_id="g1")
    r.ok("a Now Playing card has controls",
         card.controls is not None and card.controls.exclusive)
    r.check("...all five", [e for e, _ in card.controls.buttons],
            [e for e, _ in MUSIC_BUTTONS])
    r.ok("...explained in the footer", "React:" in card.footer)
    with_video = track_embed(t, guild_id="g1", video=True)
    r.check("📺 comes first, so it's the first reaction to appear",
            [e for e, _ in with_video.controls.buttons][0], "📺")
    r.ok("...and leads the footer's legend too",
         with_video.footer.split("React: ")[1].startswith("📺 video"))

    p = GuildPlayer(guild_id="g1", current=t)
    p.loop_track = True
    p.paused_at = time.monotonic()
    r.check("status names pause and loop", player_status(p),
            "⏸️ Paused · 🔁 Looping track")
    shown = track_embed(t, guild_id="g1", player=p, note="Paused by Sam")
    r.ok("the note and status reach the footer",
         "Paused by Sam" in shown.footer and "Looping track" in shown.footer)


def suite_pause(r: Results) -> None:
    from corbin.core.audio import Track
    from corbin.core.player import PlayerManager

    class _Client:
        def __init__(self): self.calls = []
        def pause(self): self.calls.append("pause")
        def resume(self): self.calls.append("resume")

    class _Session:
        def __init__(self): self.client, self.channel_id = _Client(), "v"

    class _Voice:
        def __init__(self): self.session = _Session()
        def get(self, gid): return self.session

    voice = _Voice()
    mgr = PlayerManager(voice, extractor=None)
    r.check("nothing playing: no toggle", mgr.toggle_pause("g"), None)
    p = mgr.player("g")
    p.current = Track(title="Song", url="u", duration=200)
    p.mark_started()
    p.started_at -= 10
    r.ok("pausing pauses the client",
         mgr.toggle_pause("g") is True and voice.session.client.calls ==
         ["pause"])
    p.paused_at -= 30           # pretend it sat paused for 30s...
    p.started_at -= 30          # ...so the track began 30s earlier too
    r.ok("time paused doesn't count as elapsed", 9 <= p.elapsed <= 11)
    r.ok("resuming resumes", mgr.toggle_pause("g") is False
         and voice.session.client.calls[-1] == "resume")
    r.ok("...and elapsed carries on from where it was",
         9 <= p.elapsed <= 11)
    p.mark_started()
    r.ok("a new track starts unpaused at zero",
         not p.paused and p.elapsed < 1)


def suite_stale_finish(r: Results) -> None:
    """A retried stream's first "finished" must not start the next song.

    It used to: the dead attempt's after-callback advanced the queue,
    starting the next track with its own Now Playing card.
    """
    from corbin.core.player import PlayerManager

    mgr = PlayerManager(voice=None, extractor=None)
    advanced = []

    async def fake_advance(gid):
        advanced.append(gid)
    mgr.advance = fake_advance
    p = mgr.player("g")
    p.generation = 3

    async def go():
        mgr._schedule_advance("g", 2)
        await asyncio.sleep(0)
        mgr._schedule_advance("g", 3)
        await asyncio.sleep(0)
    asyncio.run(go())
    r.check("only the current attempt's finish advances", advanced, ["g"])


def suite_reply_to(r: Results) -> None:
    """🔍 answers thread onto the message that was searched."""
    from corbin.core.context import Response
    from corbin.platforms.fluxer.adapter import FluxerAdapter

    sent = []

    class _Channel:
        async def send(self, **kw):
            sent.append(kw)
            return type("M", (), {"id": "new"})()

    adapter = FluxerAdapter(type("C", (), {"_http": None})())

    async def _channel(cid):
        return _Channel()
    adapter._channel = _channel
    asyncio.run(adapter.send("c1", Response(content="hi", reply_to="m9")))
    r.check("a reply carries a message reference",
            sent[-1].get("message_reference"),
            {"message_id": "m9", "channel_id": "c1"})
    asyncio.run(adapter.send("c1", Response(content="hi")))
    r.ok("a plain message carries none", "message_reference" not in sent[-1])
    r.ok("...and edits never do: _payload has no reference",
         "message_reference" not in adapter._payload(
             Response(content="x", reply_to="m9")))


SUITES = [
    ("reaction panels", suite_panels),
    ("verse switcher", suite_verse_controls),
    ("music card", suite_music_card),
    ("pause and resume", suite_pause),
    ("stale finish callbacks", suite_stale_finish),
    ("replies", suite_reply_to),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
