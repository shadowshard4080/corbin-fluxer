"""Offline suite for the profile status ladder.

Mirrors the Discord build's ``update_presence``: a playing track beats
the radio, the radio beats the server count, and the fallback goes idle
once the place has been quiet for a while.
"""
from __future__ import annotations
import asyncio, logging, time
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.status import (LISTENING, MAX_ACTIVITY_CHARS, WATCHING,
                                StatusUpdater, clip, track_label)


class Gateway:
    def __init__(self): self.sent = []
    async def update_presence(self, *, status="online", activity_name=None,
                              activity_type=0):
        self.sent.append((activity_type, activity_name, status))


class Bot:
    def __init__(self, gw): self._gateway = gw


class Track:
    def __init__(self, title, uploader=""):
        self.title, self.uploader = title, uploader


class Players:
    def __init__(self, current=None):
        self._players = {"g": type("P", (), {"current": current})()}


class Voice:
    def __init__(self, on_air=False):
        session = type("S", (), {"source_url": "http://s",
                                 "playing": True})()
        self._sessions = {"g": session} if on_air else {}


def updater(track=None, on_air=False, radio="KZLF - Something",
            guilds=3, active=True):
    gw = Gateway()
    u = StatusUpdater(Bot(gw), players=Players(track), voice=Voice(on_air),
                      radio_label=lambda: radio, guild_count=lambda: guilds)
    u.last_active = time.monotonic() if active else 0.0
    return u, gw


def suite_ladder(r: Results) -> None:
    from corbin.core.status import PLAYING, playing_label

    # Music in voice always wins, whichever half of the rotation it is.
    for radio_tick in (False, True):
        u, gw = updater(track=Track("The Raven", "Rotting Christ"),
                        on_air=True)
        u.show_radio = radio_tick
        asyncio.run(u.push(force=True))
        kind, text, _status = gw.sent[-1]
        r.check(f"music wins (radio tick={radio_tick})", kind, PLAYING)
        r.ok("...as Title by Artist",
             text.startswith("The Raven by Rotting Christ"))

    r.check("a Topic channel is not named twice",
            playing_label(Track("The Raven", "Rotting Christ - Topic")),
            "The Raven by Rotting Christ")
    r.check("no artist, just the title",
            playing_label(Track("Just A Title")), "Just A Title")

    u, gw = updater(on_air=False)
    asyncio.run(u.push(force=True))
    r.check("otherwise the first tick is the server count",
            gw.sent[-1][0], WATCHING)
    r.ok("...pluralised", gw.sent[-1][1].startswith("3 servers"))

    u.show_radio = True
    asyncio.run(u.push(force=True))
    kind, text, _ = gw.sent[-1]
    r.check("the next tick is the radio", kind, LISTENING)
    r.ok("...showing what's on, even with no stream open",
         text.startswith("KZLF - Something"))

    u, gw = updater(on_air=False, guilds=1)
    asyncio.run(u.push(force=True))
    r.ok("...and singular for one", gw.sent[-1][1].startswith("1 server ")
         or gw.sent[-1][1] == "1 server")

    u, gw = updater(radio=None)
    u.show_radio = True
    asyncio.run(u.push(force=True))
    r.check("no radio info: the radio tick shows the count instead",
            gw.sent[-1][0], WATCHING)


def suite_rotation(r: Results) -> None:
    """Alternates every tick, and the station is not asked every tick."""
    from corbin.core import status as st

    r.check("10 second cadence", st.REFRESH_SECONDS, 10.0)
    calls = []

    def radio():
        calls.append(1)
        return "KZLF - Song"

    gw = Gateway()
    u = StatusUpdater(Bot(gw), players=Players(), voice=Voice(),
                      radio_label=radio, guild_count=lambda: 3)
    u.last_active = time.monotonic()

    async def ticks(n):
        for _ in range(n):
            await u.push()
            u.show_radio = not u.show_radio
    asyncio.run(ticks(6))
    r.check("watching and listening alternate",
            [k for k, _t, _s in gw.sent],
            [WATCHING, LISTENING] * 3)
    r.check("the radio lookup is cached, not repeated each tick",
            len(calls), 1)


def suite_presence_state(r: Results) -> None:
    u, gw = updater(on_air=False, active=True)
    asyncio.run(u.push(force=True))
    r.check("recent activity reads as online", gw.sent[-1][2], "online")

    u, gw = updater(on_air=False, active=False)
    asyncio.run(u.push(force=True))
    r.check("a long quiet spell reads as idle", gw.sent[-1][2], "idle")

    # Music keeps him online regardless of how quiet chat has been.
    u, gw = updater(track=Track("Song"), active=False)
    asyncio.run(u.push(force=True))
    r.check("playing audio stays online", gw.sent[-1][2], "online")


def suite_churn(r: Results) -> None:
    """Presence updates are rate limited; don't resend the same line."""
    u, gw = updater(on_air=False)
    asyncio.run(u.push(force=True))
    sent_once = len(gw.sent)
    r.ok("first push is sent", sent_once == 1)
    r.ok("an identical second push is suppressed",
         asyncio.run(u.push()) is False and len(gw.sent) == 1)
    r.ok("force overrides the suppression",
         asyncio.run(u.push(force=True)) is True)

    u.players = Players(Track("A New Song"))
    r.ok("a genuine change is sent", asyncio.run(u.push()) is True)


def suite_labels(r: Results) -> None:
    r.check("artist and title joined",
            track_label(Track("The Raven", "Rotting Christ")),
            "Rotting Christ - The Raven")
    # YouTube auto-channels are named "<Artist> - Topic", which would
    # otherwise render the artist twice.
    r.check("a Topic channel is not doubled up",
            track_label(Track("The Raven", "Rotting Christ - Topic")),
            "Rotting Christ - The Raven")
    r.check("an artist already in the title is not repeated",
            track_label(Track("Rotting Christ - The Raven",
                              "Rotting Christ")),
            "Rotting Christ - The Raven")
    r.check("no uploader leaves the title alone",
            track_label(Track("Just A Title", "")), "Just A Title")

    r.check("a short label is untouched", clip("hi", " · 9% load"),
            "hi · 9% load")
    long = clip("X" * 300, " · 42% load")
    r.check("an over-long label is clipped to the limit",
            len(long), MAX_ACTIVITY_CHARS)
    r.ok("...keeping the suffix", long.endswith(" · 42% load"))


def suite_degrades(r: Results) -> None:
    class Bare:
        pass

    r.ok("no gateway is a no-op, not a crash",
         asyncio.run(StatusUpdater(Bare()).push()) is False)

    class Broken:
        _gateway = type("G", (), {
            "update_presence": staticmethod(
                lambda **_k: (_ for _ in ()).throw(RuntimeError("nope")))})()

    r.ok("a gateway that raises is swallowed",
         asyncio.run(StatusUpdater(Broken()).push()) is False)

    # A radio lookup that fails must not take the status down with it.
    gw = Gateway()
    u = StatusUpdater(Bot(gw), players=Players(), voice=Voice(True),
                      radio_label=lambda: 1 / 0, guild_count=lambda: 2)
    asyncio.run(u.push(force=True))
    r.check("a failing radio lookup falls through to the count",
            gw.sent[-1][0], WATCHING)

    gw = Gateway()
    u = StatusUpdater(Bot(gw), players=Players(), voice=Voice(),
                      guild_count=lambda: 1 / 0)
    asyncio.run(u.push(force=True))
    r.ok("a failing guild count still says something",
         gw.sent[-1][1].startswith("0 server"))


def suite_identify(r: Results) -> None:
    """Presence is re-sent right after IDENTIFY.

    Some gateways only read a bot's presence from the handshake. We
    cannot tell yet whether Fluxer is one, so we send it both ways; an
    unknown extra frame costs nothing on a gateway that ignores it.
    """
    from corbin.core.status import install_identify_presence

    order = []

    class Gateway:
        async def _send_identify(self):
            order.append("identify")
        async def update_presence(self, *, status, activity_name,
                                  activity_type):
            order.append(("presence", activity_type, activity_name))

    def client():
        return type("C", (), {"_gateway": Gateway()})()

    async def describe():
        return (LISTENING, "a song", "online")

    c = client()
    r.ok("installs", install_identify_presence(c, describe))
    r.ok("twice is idempotent", install_identify_presence(c, describe))
    asyncio.run(c._gateway._send_identify())
    r.check("the handshake still goes first", order[0], "identify")
    r.check("...then the presence", order[1][0], "presence")
    r.check("...carrying the real activity", order[1][1], LISTENING)

    # A broken describe must never take the handshake with it.
    order.clear()
    c2 = client()

    async def boom():
        raise RuntimeError("no")

    install_identify_presence(c2, boom)
    asyncio.run(c2._gateway._send_identify())
    r.check("a failing status leaves IDENTIFY intact", order, ["identify"])

    r.ok("a client with no gateway is a no-op",
         install_identify_presence(type("X", (), {})(), describe) is False)


def suite_custom_status(r: Results) -> None:
    """Fluxer drops ``activities``; only ``custom_status`` renders."""
    import json

    from corbin.core.status import (OP_PRESENCE_UPDATE, presence_frame,
                                    status_text)

    class Ws:
        closed = False
        def __init__(self): self.frames = []
        async def send_str(self, raw): self.frames.append(json.loads(raw))

    class SocketGateway(Gateway):
        def __init__(self):
            super().__init__()
            self._ws = Ws()

    gw = SocketGateway()
    u = StatusUpdater(Bot(gw), players=Players(), voice=Voice(),
                      guild_count=lambda: 3)
    u.last_active = time.monotonic()
    asyncio.run(u.push(force=True))
    r.ok("a live socket gets the raw frame, not update_presence",
         len(gw._ws.frames) == 1 and not gw.sent)
    frame = gw._ws.frames[0]
    r.check("...as op 3", frame["op"], OP_PRESENCE_UPDATE)
    r.ok("...carrying a custom status with the verb spelled out",
         frame["d"]["custom_status"]["text"].startswith("Watching 3 servers"))
    r.check("...and the online state", frame["d"]["status"], "online")

    r.check("listening gets its verb",
            status_text(LISTENING, "Rotting Christ - The Raven"),
            "Listening to Rotting Christ - The Raven")
    long = status_text(LISTENING, "X" * 300 + " · 42% load")
    r.check("a long line fits Fluxer's 128-char limit",
            len(long), MAX_ACTIVITY_CHARS)
    r.ok("...keeping the load suffix", long.endswith(" · 42% load"))
    r.ok("the gateway requires status, so it is always present",
         presence_frame("idle", "", WATCHING)["status"] == "idle")
    r.ok("no text clears the custom status rather than sending \"\"",
         presence_frame("online", "", WATCHING)["custom_status"] is None)

    # A closed socket must not raise into the loop; it degrades.
    gw = SocketGateway()
    gw._ws.closed = True
    u = StatusUpdater(Bot(gw), players=Players(), voice=Voice(),
                      guild_count=lambda: 1)
    asyncio.run(u.push(force=True))
    r.ok("a closed socket falls back to update_presence",
         len(gw.sent) == 1 and not gw._ws.frames)


SUITES = [
    ("status priority ladder", suite_ladder),
    ("status rotation", suite_rotation),
    ("status online/idle", suite_presence_state),
    ("status update churn", suite_churn),
    ("status labels", suite_labels),
    ("status degradation", suite_degrades),
    ("presence at identify", suite_identify),
    ("fluxer custom status", suite_custom_status),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
