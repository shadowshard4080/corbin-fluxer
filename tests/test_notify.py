"""Moderation notification: content, and the ordering that makes it work."""
from __future__ import annotations
import asyncio, logging
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.commands._notify import delivery_note, notify_target


class FakeAdapter:
    """Records the order of DM and removal calls."""

    def __init__(self, dm_ok=True):
        self.dm_ok = dm_ok
        self.order: list[str] = []
        self.dms: list = []

    async def send_dm(self, uid, response):
        self.order.append("dm")
        if not self.dm_ok:
            return None
        self.dms.append((uid, response))
        return "m1"

    async def ban(self, g, u, *, reason=None, delete_message_seconds=0):
        self.order.append("ban")

    async def kick(self, g, u, *, reason=None):
        self.order.append("kick")

    async def timeout(self, g, u, until):
        self.order.append("timeout")


def suite_content(r: Results) -> None:
    a = FakeAdapter()
    ok = asyncio.run(notify_target(a, "u1", action="warned",
                                   server_name="The Den", reason="spam",
                                   moderator="Mod", warn_id="abc123"))
    r.ok("delivered", ok)
    embed = a.dms[0][1].embed
    r.ok("names the server", "The Den" in embed.title)
    r.ok("says what happened", "warned" in embed.title)
    values = {f.name: f.value for f in embed.fields}
    r.check("reason included", values["Reason"], "spam")
    r.check("moderator included", values["Moderator"], "Mod")
    r.ok("tells them how to appeal", "appeal abc123" in values["Appealing"])

    b = FakeAdapter()
    asyncio.run(notify_target(b, "u", action="timed out",
                              server_name="S", reason="x", duration="2 hours"))
    r.check("duration shown",
            {f.name: f.value for f in b.dms[0][1].embed.fields}["Duration"],
            "2 hours")

    c = FakeAdapter()
    asyncio.run(notify_target(c, "u", action="banned", server_name="S"))
    vals = {f.name: f.value for f in c.dms[0][1].embed.fields}
    r.check("no reason is stated plainly", vals["Reason"], "No reason given")
    r.ok("bans explain how to get in touch", "moderator" in vals["Appealing"])

    # A closed DM must not raise; moderation has to proceed regardless.
    closed = FakeAdapter(dm_ok=False)
    r.ok("undelivered returns False",
         not asyncio.run(notify_target(closed, "u", action="banned",
                                       server_name="S")))

    class Broken(FakeAdapter):
        async def send_dm(self, uid, response):
            raise RuntimeError("network down")

    r.ok("an exception is swallowed, not raised",
         not asyncio.run(notify_target(Broken(), "u", action="banned",
                                       server_name="S")))

    r.ok("note reflects success", "were told" in delivery_note(True, "banned"))
    r.ok("note reflects failure",
         "couldn't DM" in delivery_note(False, "banned"))


def suite_ordering(r: Results) -> None:
    """The whole point: notify BEFORE removing.

    A ban or kick destroys the mutual guild, after which the user cannot be
    DM'd at all, so the wrong order silently means nobody is ever told.
    """
    from corbin.commands.moderation import ban, kick
    from corbin.core.context import (Channel, CommandContext, Guild, Member,
                                     Platform, Surface, User)

    class _R:
        async def send(self, x): return "m"
        async def fetch_member(self, g, u): return None

    def ctx(adapter):
        return CommandContext(
            platform=Platform.FLUXER, surface=Surface.AI,
            invoker=Member(user=User(id="mod", name="Mod"), guild_id="g"),
            channel=Channel(id="c", name="general", guild_id="g"),
            guild=Guild(id="g", name="The Den"), responder=_R(),
            extras={"adapter": adapter})

    a = FakeAdapter()
    out = asyncio.run(ban(ctx(a), "u1", "scam links"))
    r.check("ban DMs first, then bans", a.order, ["dm", "ban"])
    r.ok("reason given to the user",
         "scam links" in str(a.dms[0][1].embed.fields[0].value))
    r.ok("moderator told they were notified", "were told" in out)

    b = FakeAdapter()
    asyncio.run(kick(ctx(b), "u1", "spam"))
    r.check("kick DMs first, then kicks", b.order, ["dm", "kick"])

    # DMs closed: the action must still happen.
    c = FakeAdapter(dm_ok=False)
    out2 = asyncio.run(ban(ctx(c), "u1", "raiding"))
    r.check("ban proceeds despite a failed DM", c.order, ["dm", "ban"])
    r.ok("moderator is told the DM failed", "couldn't DM" in out2)


SUITES = [
    ("moderation dm content", suite_content),
    ("notify-before-remove ordering", suite_ordering),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
