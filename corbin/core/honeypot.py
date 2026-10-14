"""The #do-not-type honeypot.

This is a **trap, not a detector**. Discord's AutoMod-equivalent rate limits
and raid protection cover ordinary spam; what they cannot do is catch a
compromised account *before* it reaches a real channel. Scam bots sweep a
server's writable channels top-down and post in the first one they find, so
a channel that sits at the very top and looks postable catches them on
their first message.

Two details are load-bearing and must not be "fixed":

* **``@everyone`` is granted SEND_MESSAGES.** Permission to post is the
  bait. A locked channel catches nothing.
* **``parent_id=None`` AND ``position=0``.** Uncategorised channels render
  above every category, so it needs both to actually sit at the top.

Everything else is denied, so a bot cannot use it to attach files, embed
links, or react.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .permissions import Perm

logger = logging.getLogger("corbin.honeypot")

CHANNEL_NAME = "do-not-type"
CHANNEL_TOPIC = ("Do not type here. Posting in this channel is an automatic "
                 "ban. It exists to catch compromised accounts and scam "
                 "bots.")
#: Seconds to suppress duplicate handling while the first ban lands. A bot
#: that dumps five messages must not queue five bans and five DMs.
PENDING_TTL = 5.0
#: Delete this much of a banned account's recent history.
BAN_DELETE_SECONDS = 24 * 3600

#: The bait. Nothing else.
ALLOW = Perm.VIEW_CHANNEL | Perm.SEND_MESSAGES
#: Everything a scam bot would actually want to use.
DENY = (Perm.ADD_REACTIONS | Perm.ATTACH_FILES | Perm.EMBED_LINKS
        | Perm.MANAGE_MESSAGES | Perm.MENTION_EVERYONE
        | Perm.USE_EXTERNAL_EMOJIS | Perm.SEND_TTS_MESSAGES
        | Perm.READ_MESSAGE_HISTORY)

#: Anyone with these is nudged rather than banned.
EXEMPT = (Perm.ADMINISTRATOR | Perm.MANAGE_GUILD | Perm.BAN_MEMBERS
          | Perm.KICK_MEMBERS | Perm.MODERATE_MEMBERS)

NOTICE_TITLE = "⛔  Do not type in this channel"
NOTICE_BODY = (
    "This channel is a trap for compromised accounts and scam bots.\n\n"
    "**Posting here bans you automatically.**\n\n"
    "If you landed here by accident, just leave. Nothing you need is in "
    "this channel."
)


def is_exempt(member: Any, *, bot_owner_id: Optional[str] = None) -> bool:
    """Staff and the bot owner get a nudge instead of a ban."""
    if member is None:
        return True  # unknown member: never ban on a guess
    if bot_owner_id and str(getattr(member, "id", "")) == str(bot_owner_id):
        return True
    if getattr(member, "is_guild_owner", False):
        return True
    try:
        return bool(member.permissions & EXEMPT)
    except Exception:
        return True


class HoneypotGuard:
    """Tracks in-flight bans so a burst produces exactly one."""

    def __init__(self, ttl: float = PENDING_TTL) -> None:
        self.ttl = ttl
        self._pending: dict[tuple[str, str], float] = {}

    def claim(self, guild_id: str, user_id: str) -> bool:
        """True if this caller should handle the ban."""
        import time

        key = (str(guild_id), str(user_id))
        now = time.monotonic()
        # Drop anything stale first so the dict cannot grow forever.
        for stale in [k for k, t in self._pending.items()
                      if now - t > self.ttl]:
            del self._pending[stale]
        if key in self._pending:
            return False
        self._pending[key] = now
        return True

    def release(self, guild_id: str, user_id: str) -> None:
        self._pending.pop((str(guild_id), str(user_id)), None)

    def __len__(self) -> int:
        return len(self._pending)


async def ensure_channel(http: Any, guild_id: str,
                         existing_id: Optional[str] = None
                         ) -> tuple[Optional[str], list[str]]:
    """Create or adopt #do-not-type. Returns (channel_id, problems).

    Idempotent: an existing channel is adopted and its overwrites, topic and
    position re-applied, rather than creating a duplicate.
    """
    problems: list[str] = []
    channel_id = None

    try:
        channels = await http.get_guild_channels(guild_id)
    except Exception as exc:
        return None, [f"couldn't list channels: {exc}"]

    for raw in channels or []:
        if str(raw.get("id")) == str(existing_id or ""):
            channel_id = str(raw["id"])
            break
        if str(raw.get("name") or "").lower() == CHANNEL_NAME:
            channel_id = str(raw["id"])
            break

    if channel_id is None:
        try:
            created = await http.create_guild_channel(
                guild_id, name=CHANNEL_NAME, type=0, topic=CHANNEL_TOPIC,
                position=0, parent_id=None)
            channel_id = str(created.get("id"))
        except Exception as exc:
            return None, [f"couldn't create the channel: {exc}"]
    else:
        try:
            await http.modify_channel(channel_id, topic=CHANNEL_TOPIC,
                                      position=0, parent_id=None)
        except Exception as exc:
            problems.append(f"couldn't reposition it: {exc}")

    # @everyone's overwrite id is the guild id, same convention as Discord.
    try:
        await http.edit_channel_permissions(
            channel_id, guild_id, allow=str(int(ALLOW)),
            deny=str(int(DENY)), type=0)
    except Exception as exc:
        problems.append(f"couldn't set permissions: {exc}")

    return channel_id, problems


async def has_notice(http: Any, channel_id: str) -> bool:
    try:
        messages = await http.get_channel_messages(channel_id, limit=20) \
            if hasattr(http, "get_channel_messages") else []
    except Exception:
        return False
    for message in messages or []:
        for embed in (message.get("embeds") or []):
            if str(embed.get("title") or "").strip() == NOTICE_TITLE:
                return True
    return False
