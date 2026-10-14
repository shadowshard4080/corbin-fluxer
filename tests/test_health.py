"""Offline suite for the boot health numbers."""
from __future__ import annotations
import asyncio, logging, time
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

import corbin.core.health as H
from corbin.core.health import (GatewayPing, api_latency, human_duration,
                                human_latency, local_model_state)


def suite_formatting(r: Results) -> None:
    r.check("sub-second reads in ms", human_duration(0.42), "420 ms")
    r.check("seconds get one decimal", human_duration(3.74), "3.7s")
    r.check("over a minute splits", human_duration(95), "1m 35s")
    r.check("latency in ms", human_latency(0.0184), "18 ms")
    # Unknown must not render as 0 ms, which reads as a perfect result.
    r.check("unmeasured latency says so", human_latency(None), "unknown")


def suite_heartbeat(r: Results) -> None:
    class Gateway:
        def __init__(self, ack_after=0.02):
            self._last_heartbeat_ack = False
            self.ack_after, self.sends = ack_after, 0

        async def _send_heartbeat(self):
            self.sends += 1
            if self.ack_after is None:
                return

            async def ack():
                await asyncio.sleep(self.ack_after)
                self._last_heartbeat_ack = True
            asyncio.get_running_loop().create_task(ack())

    def client(gw):
        return type("C", (), {"_gateway": gw})()

    gw = Gateway(0.02)
    ping = GatewayPing()
    r.ok("installs onto a real gateway", ping.install(client(gw)))
    r.ok("installing twice is idempotent", ping.install(client(gw)))

    asyncio.run(gw._send_heartbeat())
    r.ok("a round trip is measured", ping.latency is not None)
    r.ok("...and is in the right ballpark", 0.01 < ping.latency < 0.2)
    r.check("the original heartbeat still went out", gw.sends, 1)
    r.ok("exposed in ms", ping.ms is not None and ping.ms > 10)

    # A missed ACK must not hang the heartbeat loop or invent a number.
    saved, H.ACK_TIMEOUT = H.ACK_TIMEOUT, 0.05
    try:
        dead = Gateway(ack_after=None)
        p2 = GatewayPing()
        p2.install(client(dead))
        started = time.monotonic()
        asyncio.run(dead._send_heartbeat())
        waited = time.monotonic() - started
    finally:
        H.ACK_TIMEOUT = saved
    r.check("no ACK reports unknown, not zero", p2.latency, None)
    r.ok("...and gives up at the cap", waited < 0.5)

    r.ok("a client with no gateway is a no-op",
         GatewayPing().install(type("X", (), {})()) is False)


def suite_api(r: Results) -> None:
    class Adapter:
        def __init__(self, fn): self.fn = fn
        def _rest(self):
            return type("H", (), {"get_channel": staticmethod(self.fn)})()

    async def ok(_cid):
        await asyncio.sleep(0.01)

    def boom(_cid):
        raise RuntimeError("500")

    got = asyncio.run(api_latency(Adapter(ok), "c1"))
    r.ok("a successful call is timed", got is not None and got >= 0.005)
    r.check("a failing call reports unknown rather than a fake number",
            asyncio.run(api_latency(Adapter(boom), "c1")), None)
    r.check("no channel id, no measurement",
            asyncio.run(api_latency(Adapter(ok), "")), None)

    class NoRest:
        def _rest(self): raise RuntimeError("gone")
    r.check("an adapter with no REST client is unknown",
            asyncio.run(api_latency(NoRest(), "c1")), None)


def suite_model(r: Results) -> None:
    def llm(local):
        return type("L", (), {"local": local})()

    class Up:
        model = "Corbin:latest"
        async def available(self, force=False): return True

    class Down:
        model = "Corbin:latest"
        async def available(self, force=False): raise RuntimeError("no route")

    class Legacy:
        model = "Corbin:latest"
        async def available(self): return True      # no force= kwarg

    r.check("a reachable model reports up",
            asyncio.run(local_model_state(llm(Up()))), ("Corbin:latest", True))
    # The name matters when it is down: "offline" alone does not say
    # which model failed to answer.
    r.check("an unreachable model keeps its name",
            asyncio.run(local_model_state(llm(Down()))),
            ("Corbin:latest", False))
    r.check("an older provider signature still works",
            asyncio.run(local_model_state(llm(Legacy()))),
            ("Corbin:latest", True))
    r.check("no local model configured", asyncio.run(local_model_state(llm(None))),
            ("", False))


SUITES = [
    ("health formatting", suite_formatting),
    ("gateway heartbeat timing", suite_heartbeat),
    ("api latency", suite_api),
    ("local model state", suite_model),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
