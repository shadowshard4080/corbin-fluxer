"""Platform-neutral permission model.

This module is the *authorization* boundary. Nothing here ever consults a
language model, and no model output is ever an input to these functions.
The AI layer decides what a user *meant*; this decides what they may *do*.

Bit positions and names are VERIFIED against fluxer.py 0.4.2: all 36 of its
``Permissions`` members share our bit values exactly (checked 2026-09-23 via
``verify_against``).  ``verify_against()`` re-runs that check at boot, because
a silent bit drift here is a privilege bug, not a crash.
"""

from __future__ import annotations

from enum import IntFlag
from typing import Iterable, Mapping, Optional, Protocol, Sequence


class Perm(IntFlag):
    """Permission bits. Names match Discord/Fluxer conventions."""

    NONE = 0

    CREATE_INVITE = 1 << 0
    KICK_MEMBERS = 1 << 1
    BAN_MEMBERS = 1 << 2
    ADMINISTRATOR = 1 << 3
    MANAGE_CHANNELS = 1 << 4
    MANAGE_GUILD = 1 << 5
    ADD_REACTIONS = 1 << 6
    VIEW_AUDIT_LOG = 1 << 7
    PRIORITY_SPEAKER = 1 << 8
    STREAM = 1 << 9
    VIEW_CHANNEL = 1 << 10
    SEND_MESSAGES = 1 << 11
    SEND_TTS_MESSAGES = 1 << 12
    MANAGE_MESSAGES = 1 << 13
    EMBED_LINKS = 1 << 14
    ATTACH_FILES = 1 << 15
    READ_MESSAGE_HISTORY = 1 << 16
    MENTION_EVERYONE = 1 << 17
    USE_EXTERNAL_EMOJIS = 1 << 18
    CONNECT = 1 << 20
    SPEAK = 1 << 21
    MUTE_MEMBERS = 1 << 22
    DEAFEN_MEMBERS = 1 << 23
    MOVE_MEMBERS = 1 << 24
    USE_VOICE_ACTIVITY_DETECTION = 1 << 25
    CHANGE_NICKNAME = 1 << 26
    MANAGE_NICKNAMES = 1 << 27
    MANAGE_ROLES = 1 << 28
    MANAGE_WEBHOOKS = 1 << 29
    MANAGE_EXPRESSIONS = 1 << 30
    USE_EXTERNAL_STICKERS = 1 << 33
    MODERATE_MEMBERS = 1 << 40
    CREATE_EXPRESSIONS = 1 << 43
    PIN_MESSAGES = 1 << 51
    BYPASS_SLOWMODE = 1 << 52
    UPDATE_RTC_REGION = 1 << 53


#: Permissions that, in Corbin's own policy, mark somebody as staff.
STAFF_PERMS = (
    Perm.ADMINISTRATOR
    | Perm.MANAGE_GUILD
    | Perm.BAN_MEMBERS
    | Perm.KICK_MEMBERS
    | Perm.MANAGE_MESSAGES
    | Perm.MODERATE_MEMBERS
    | Perm.MANAGE_ROLES
)


class HasPermissions(Protocol):
    """Minimal shape a role-like object must satisfy."""

    @property
    def permissions(self) -> int: ...


def resolve(
    role_permissions: Iterable[int],
    *,
    is_guild_owner: bool = False,
) -> Perm:
    """Fold a member's role permission bits into one effective mask.

    Mirrors fluxer.py's own computation: OR every role's bits together, then
    treat guild owner and ADMINISTRATOR as holding everything.
    """
    if is_guild_owner:
        return ~Perm.NONE

    acc = 0
    for bits in role_permissions:
        acc |= int(bits)

    effective = Perm(acc)
    if effective & Perm.ADMINISTRATOR:
        return ~Perm.NONE
    return effective


def has(effective: Perm, required: Perm) -> bool:
    """True when ``effective`` satisfies every bit in ``required``.

    ``Perm.NONE`` means "no permission required" and is always satisfied.
    """
    if required == Perm.NONE:
        return True
    return (effective & required) == required


def missing(effective: Perm, required: Perm) -> Perm:
    """The subset of ``required`` that ``effective`` lacks."""
    return Perm(int(required) & ~int(effective))


def describe(perm: Perm) -> str:
    """Human-readable permission list, for telling a user what they lack."""
    if perm == Perm.NONE:
        return "no special permissions"
    names = [
        p.name.replace("_", " ").title()
        for p in Perm
        if p is not Perm.NONE and p.name and perm & p
    ]
    return ", ".join(names) if names else "unknown permissions"


def verify_against(vendor_flags: Mapping[str, int]) -> Sequence[str]:
    """Compare our bit values with the platform library's own enum.

    Call this once at boot with ``{name: value}`` pulled from fluxer.py so a
    silent bit-position drift becomes a loud startup error instead of a
    moderation bug. Returns a list of human-readable mismatches (empty = OK).
    """
    problems = []
    for name, vendor_value in vendor_flags.items():
        ours: Optional[Perm] = getattr(Perm, name.upper(), None)
        if ours is None:
            continue
        if int(ours) != int(vendor_value):
            problems.append(
                f"{name}: ours={int(ours)} vendor={int(vendor_value)}"
            )
    return problems
