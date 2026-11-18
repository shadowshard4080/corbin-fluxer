"""Giving the bot access to a channel it just made.

Live 2026-11-18 he made #level-ups, denied @everyone posting, and then
couldn't post there himself: the member overwrite for him didn't take,
and its failure only reached an INFO line. Role overwrites are what the
@everyone ones prove work, so he gets both: a member overwrite and one
on each of his own roles. #reaction-roles is made the same way.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("corbin.channel_access")


async def grant_self(adapter: Any, http: Any, guild_id: str,
                     channel_id: str, allow: int) -> bool:
    """Allow ``allow`` for him in ``channel_id``, as himself and on each
    of his roles. True if any overwrite for him went through; every
    refusal is a warning."""
    bits = str(int(allow))
    user = getattr(getattr(adapter, "client", None), "user", None)
    bot_id = str(user.id) if getattr(user, "id", None) else None
    if not bot_id:
        logger.warning("don't know my own id; can't give myself access to "
                       "%s", channel_id)
        return False
    ok = False
    try:
        await http.edit_channel_permissions(channel_id, bot_id, allow=bits,
                                            deny="0", type=1)
        ok = True
    except Exception:
        logger.warning("member overwrite for myself refused in %s",
                       channel_id, exc_info=True)
    try:
        me = await adapter.fetch_member(guild_id, bot_id)
    except Exception:
        logger.warning("couldn't read my own roles in %s", guild_id,
                       exc_info=True)
        me = None
    for role in getattr(me, "roles", ()) or ():
        rid = str(getattr(role, "id", "") or "")
        if not rid or rid == str(guild_id):
            continue
        try:
            await http.edit_channel_permissions(channel_id, rid, allow=bits,
                                                deny="0", type=0)
            ok = True
        except Exception:
            logger.warning("overwrite for my role %s refused in %s", rid,
                           channel_id, exc_info=True)
    return ok
