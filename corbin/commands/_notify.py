"""Telling a member about a moderation action against them.

**Order matters for removals.** A DM needs a mutual guild, so once someone
is banned or kicked they are unreachable. Every removal therefore notifies
FIRST and acts second -- the same rule the Discord build's honeypot
follows.

Delivery never blocks the action: a closed DM is not a reason to leave a
scammer in the server. The outcome is reported back to the moderator so
they know whether the person was actually told.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..core.context import Embed, Response

logger = logging.getLogger("corbin.notify")

COLOURS = {"warned": 0xE8A33D, "timed out": 0xE8A33D,
           "kicked": 0xD9534F, "banned": 0xD9534F}


async def notify_target(adapter: Any, user_id: str, *, action: str,
                        server_name: str, reason: Optional[str] = None,
                        moderator: Optional[str] = None,
                        warn_id: Optional[str] = None,
                        duration: Optional[str] = None) -> bool:
    """DM a member about an action. Returns True if it was delivered."""
    titles = {
        "warned": f"You've been warned in {server_name}",
        "timed out": f"You've been timed out in {server_name}",
        "kicked": f"You've been kicked from {server_name}",
        "banned": f"You've been banned from {server_name}",
    }
    embed = Embed(title=titles.get(action, f"Moderation action in "
                                          f"{server_name}"),
                  colour=COLOURS.get(action, 0xE8A33D))
    embed.add_field("Reason", reason or "No reason given")
    if duration:
        embed.add_field("Duration", duration, inline=True)
    if moderator:
        embed.add_field("Moderator", moderator, inline=True)
    if warn_id:
        embed.add_field(
            "Appealing",
            f"Reply in the server with `appeal {warn_id}` if you think this "
            "was a mistake. You can appeal once a week.")
    elif action == "banned":
        embed.add_field("Appealing",
                        "If you think this was a mistake, contact a "
                        "moderator through another channel.")

    try:
        sent = await adapter.send_dm(str(user_id), Response(embed=embed))
        return sent is not None
    except Exception:
        logger.info("could not DM %s about being %s", user_id, action,
                    exc_info=True)
        return False


def delivery_note(delivered: bool, action: str) -> str:
    """One line for the moderator about whether the person was told."""
    if delivered:
        return f"They were told they'd been {action}."
    return (f"I couldn't DM them, so they don't know why they were "
            f"{action} (their DMs are probably closed).")
