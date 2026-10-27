"""Offline suite: a DM whose channel fluxer.py hasn't cached is still a DM.

After a restart DM channels aren't cached, so messages arrived with
``is_dm=False`` and no guild, and every DM was dropped without a log line
(2026-10-17). ``FluxerAdapter.channel_is_dm`` asks the API instead.
"""
from __future__ import annotations
import asyncio, logging, types
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.platforms.fluxer.adapter import FluxerAdapter


class Client:
    """Nothing cached; ``fetch_channel`` answers like fluxer.py's model."""

    def __init__(self, kinds):
        self.kinds = kinds
        self.fetches = 0

    def get_channel(self, cid):
        return None

    async def fetch_channel(self, cid):
        self.fetches += 1
        kind = self.kinds.get(str(cid))
        if kind is None:
            return None
        return types.SimpleNamespace(id=cid, type=kind, is_dm=kind == 1,
                                     guild_id=None if kind in (1, 3)
                                     else "g1")


def suite_dm(r: Results) -> None:
    client = Client({"dm": 1, "group": 3, "text": 0})
    adapter = FluxerAdapter(client)

    async def main():
        r.ok("an uncached DM is a DM", await adapter.channel_is_dm("dm"))
        r.ok("a group DM counts", await adapter.channel_is_dm("group"))
        r.ok("a guild channel isn't",
             not await adapter.channel_is_dm("text"))
        before = client.fetches
        await adapter.channel_is_dm("dm")
        r.check("the answer is kept (types never change)", client.fetches,
                before)
        r.ok("an unknown channel isn't assumed a DM",
             not await adapter.channel_is_dm("gone"))
        await adapter.channel_is_dm("gone")
        r.check("and is asked again next time", client.fetches, before + 2)

    asyncio.run(main())

    # The model's type may be an enum rather than an int.
    class Kind:
        value = 1

    enum_client = Client({})

    async def fetch(cid):
        return types.SimpleNamespace(id=cid, type=Kind())

    enum_client.fetch_channel = fetch
    r.ok("an enum type of 1 is a DM", asyncio.run(
        FluxerAdapter(enum_client).channel_is_dm("x")))


SUITES = [("DM detection on a cold cache", suite_dm)]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
