"""The platform interface every adapter implements."""

from __future__ import annotations

from typing import Optional, Protocol, Sequence

from ..core.context import Member, Message, Response


class PlatformAdapter(Protocol):
    """What ``corbin.core`` needs from a chat platform."""

    name: str

    async def send(self, channel_id: str, response: Response) -> Optional[str]: ...
    async def send_dm(self, user_id: str, response: Response) -> Optional[str]: ...
    async def delete_message(self, channel_id: str, message_id: str) -> None: ...
    async def fetch_member(self, guild_id: str, user_id: str) -> Optional[Member]: ...
    async def ban(self, guild_id: str, user_id: str, *,
                  reason: Optional[str] = None,
                  delete_message_seconds: int = 0) -> None: ...
    async def unban(self, guild_id: str, user_id: str) -> None: ...
    async def kick(self, guild_id: str, user_id: str, *,
                   reason: Optional[str] = None) -> None: ...
    async def add_role(self, guild_id: str, user_id: str, role_id: str) -> None: ...
    async def remove_role(self, guild_id: str, user_id: str, role_id: str) -> None: ...

    def capabilities(self) -> "Capabilities": ...


class Capabilities:
    """What this platform can actually do.

    Commands consult this instead of branching on platform name, so a feature
    arriving later (Fluxer shipping components) is a flag flip, not a rewrite.
    """

    def __init__(self, **flags: bool) -> None:
        self.ephemeral = flags.get("ephemeral", False)
        self.buttons = flags.get("buttons", False)
        self.select_menus = flags.get("select_menus", False)
        self.modals = flags.get("modals", False)
        self.slash_commands = flags.get("slash_commands", False)
        self.reactions = flags.get("reactions", True)
        self.dms = flags.get("dms", True)
        self.file_upload = flags.get("file_upload", True)
        self.embeds = flags.get("embeds", True)
        self.voice = flags.get("voice", False)
        self.audit_log_events = flags.get("audit_log_events", False)
        self.message_edit_events = flags.get("message_edit_events", True)

    def __repr__(self) -> str:
        on = [k for k, v in vars(self).items() if v]
        off = [k for k, v in vars(self).items() if not v]
        return f"<Capabilities on={sorted(on)} off={sorted(off)}>"


#: What Fluxer supports as of 2026-09. Slash commands, components and modals
#: are 2026-roadmap items (#7) with no ship date -- flip these to True when
#: they land and the registry will start rendering them automatically.
FLUXER_CAPABILITIES = Capabilities(
    ephemeral=False, buttons=False, select_menus=False, modals=False,
    slash_commands=False, reactions=True, dms=True, file_upload=True,
    embeds=True, voice=True, audit_log_events=False,
    message_edit_events=True,
)

DISCORD_CAPABILITIES = Capabilities(
    ephemeral=True, buttons=True, select_menus=True, modals=True,
    slash_commands=True, reactions=True, dms=True, file_upload=True,
    embeds=True, voice=True, audit_log_events=True,
    message_edit_events=True,
)
