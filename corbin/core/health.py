"""Boot-time health numbers: how long, how fast, what's answering.

Two different latencies, because they fail independently:

* **Gateway** -- the websocket heartbeat round trip. High here means the
  connection to Fluxer is slow, and events arrive late.
* **API** -- a REST round trip. High here means requests are slow, so
  Corbin is slow to *act* even when he hears instantly.

fluxer.py tracks neither. ``_last_heartbeat_ack`` is a bare bool, and
the ACK is handled inside a ``match`` in the receive loop with no seam
to hook. So the heartbeat is timed from the **send** side only: the
original is called through untouched and the ack flag is then polled.
Nothing in the connection path is replaced -- a failure here yields no
number, never a dropped gateway.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("corbin.health")

#: How long to wait for a heartbeat ACK before giving up on timing it.
ACK_TIMEOUT = 5.0
#: Poll granularity while waiting. Fine enough not to distort the number.
ACK_POLL = 0.002


class GatewayPing:
    """Times the heartbeat round trip, without touching the receive loop."""

    def __init__(self) -> None:
        self.latency: Optional[float] = None
        self._installed = False

    def install(self, client: Any) -> bool:
        gateway = getattr(client, "_gateway", None)
        original = getattr(gateway, "_send_heartbeat", None)
        if gateway is None or original is None or self._installed:
            return self._installed

        async def timed() -> None:
            # The loop sets _last_heartbeat_ack False immediately before
            # calling this, so a True reading after the send is genuinely
            # this beat's ACK and not a stale one.
            started = time.monotonic()
            await original()
            deadline = started + ACK_TIMEOUT
            while time.monotonic() < deadline:
                if getattr(gateway, "_last_heartbeat_ack", False):
                    self.latency = time.monotonic() - started
                    return
                await asyncio.sleep(ACK_POLL)
            # No ACK in time: report unknown rather than a wrong number.
            self.latency = None

        try:
            gateway._send_heartbeat = timed
        except Exception:
            logger.debug("could not time heartbeats", exc_info=True)
            return False
        self._installed = True
        return True

    @property
    def ms(self) -> Optional[float]:
        return self.latency * 1000.0 if self.latency is not None else None


async def api_latency(adapter: Any, channel_id: str) -> Optional[float]:
    """Seconds for one REST round trip, or None if it could not be timed.

    Uses a GET the bot already has permission for; nothing is created or
    posted just to take a measurement.
    """
    http = None
    try:
        http = adapter._rest()
    except Exception:
        return None
    fetch = getattr(http, "get_channel", None)
    if fetch is None or not channel_id:
        return None
    started = time.monotonic()
    try:
        await fetch(str(channel_id))
    except Exception:
        return None
    return time.monotonic() - started


def human_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s"


def human_latency(seconds: Optional[float]) -> str:
    return "unknown" if seconds is None else f"{seconds * 1000:.0f} ms"


async def local_model_state(llm: Any) -> tuple[str, bool]:
    """``(model name, reachable)`` for the local Ollama model.

    Reports the name even when the host is unreachable -- "Corbin:latest
    (offline)" says which model is missing, where a bare "offline" does
    not.
    """
    local = getattr(llm, "local", None)
    if local is None:
        return ("", False)
    name = str(getattr(local, "model", "") or "")
    try:
        up = bool(await local.available(force=True))
    except TypeError:
        try:
            up = bool(await local.available())
        except Exception:
            up = False
    except Exception:
        up = False
    return (name, up)
