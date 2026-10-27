"""Reaction roles: react with an emoji on one of his messages, get a role.

Different from :mod:`corbin.core.reactions` on purpose. A reaction
*control* is a button: it expires after a week, ignores custom emoji, and
the bot takes your reaction back after each press. A reaction *role* is a
standing sign-up sheet: it must survive restarts, custom server emoji are
the usual choice, and your reaction staying put is the whole point
(removing it drops the role).

This module is platform-neutral: parsing, validation, placement and the
store. The command module (``commands/reaction_role_cmds.py``) does the
talking and the REST calls; ``bot.py`` routes reaction events here.

Stored per guild under ``reaction_roles`` in the chat settings::

    {message_id: {"channel_id": str, "created": float,
                  "roles": {emoji_key: {"role": role_id, "emoji": shown}}}}

``emoji_key`` is a custom emoji's id or a unicode emoji without its
variation selector, because that's all a reaction event reliably carries.
``shown`` is what to write in the embed and react with. A role entry may
also carry ``"dm": True``: an **opt-in DM role**, the only kind the
``messager`` command will DM (see below).

Opt-in DM roles exist because DMing everyone who holds a role is only
consent when holding it was their own choice. So consent is recorded
separately, under ``dm_optin`` (``{role_id: {user_id: timestamp}}``),
and only a member's own reaction writes it. A role handed out any other
way (a moderator, another bot, ``assignrole``) has no consent behind it:
``bot.py`` takes it back off on GUILD_MEMBER_UPDATE
(:meth:`ReactionRoleStore.unconsented`), and ``messager`` skips anyone
holding it without consent.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .permissions import Perm

logger = logging.getLogger("corbin.reaction_roles")

CHANNEL_NAME = "reaction-roles"
CHANNEL_TOPIC = ("React to the message here to give yourself a role. "
                 "Remove your reaction to drop it.")
STORE_KEY = "reaction_roles"
#: Who opted in to each DM role, by their own reaction.
CONSENT_KEY = "dm_optin"

#: How long "which roles and emoji?" stays answerable.
QUESTION_TTL = 600.0

#: @everyone in a channel he creates: read it and click what's there.
EVERYONE_ALLOW = Perm.VIEW_CHANNEL | Perm.READ_MESSAGE_HISTORY
#: ADD_REACTIONS denied is what stops new emoji while still letting people
#: click the ones already on the message (Discord's rule; not yet checked
#: on Fluxer). Fluxer's permission set has no thread bits to deny.
EVERYONE_DENY = (Perm.SEND_MESSAGES | Perm.ADD_REACTIONS | Perm.ATTACH_FILES
                 | Perm.EMBED_LINKS | Perm.MENTION_EVERYONE
                 | Perm.SEND_TTS_MESSAGES)
#: His own member overwrite, so the deny above can't lock him out of the
#: channel he has to post and react in.
BOT_ALLOW = (Perm.VIEW_CHANNEL | Perm.SEND_MESSAGES | Perm.ADD_REACTIONS
             | Perm.EMBED_LINKS | Perm.READ_MESSAGE_HISTORY)

#: A reaction is a click, so a role behind one must not carry power:
#: anyone who can see the channel could hand themselves moderation.
DANGEROUS = (Perm.ADMINISTRATOR | Perm.MANAGE_CHANNELS | Perm.MANAGE_GUILD
             | Perm.MANAGE_MESSAGES | Perm.MANAGE_NICKNAMES
             | Perm.MANAGE_ROLES | Perm.MANAGE_WEBHOOKS
             | Perm.MANAGE_EXPRESSIONS | Perm.BAN_MEMBERS
             | Perm.KICK_MEMBERS | Perm.MODERATE_MEMBERS)


# -- emoji --------------------------------------------------------------------

CUSTOM = re.compile(r"<(a?):([A-Za-z0-9_~]{1,64}):(\d{5,})>")
SHORTCODE = re.compile(r"(?<![<\w]):([A-Za-z0-9_~]{2,64}):(?!\d)")
_BASE = ("[©®‼⁉™ℹ↔-↪⌚-⏿"
         "Ⓜ▪-◾☀-➿⤴⤵⬅-⭕〰"
         "〽㊗㊙\U0001F000-\U0001FAFF]")
_MOD = "(?:️|[\U0001F3FB-\U0001F3FF])*"
#: One unicode emoji as people type it: keycaps, flags, and ZWJ sequences
#: ("👩‍🎨") count as one, or "👩‍🎨 Artist" would become three emoji.
UNICODE = re.compile(
    "(?:[0-9#*]️?⃣"
    "|[\U0001F1E6-\U0001F1FF]{2}"
    f"|{_BASE}{_MOD}(?:‍{_BASE}{_MOD})*)")
ROLE_MENTION = re.compile(r"<@&(\d+)>")


def normalise(emoji: str) -> str:
    """Unicode emoji without the variation selector (clients differ)."""
    return (emoji or "").replace("️", "").strip()


@dataclass(frozen=True)
class Emoji:
    #: What reaction events are matched on.
    key: str
    #: What goes in the embed and the add-reaction call.
    shown: str


def emoji_from_token(token: str) -> Optional[Emoji]:
    """An emoji written in a message (unicode or ``<:name:id>``)."""
    m = CUSTOM.fullmatch(token or "")
    if m:
        return Emoji(key=m.group(3), shown=m.group(0))
    if UNICODE.fullmatch(token or ""):
        return Emoji(key=normalise(token), shown=token)
    return None


def event_key(partial: Any) -> Optional[str]:
    """The store key for a gateway reaction's emoji.

    Not ``reactions.reaction_emoji``: that one drops custom emoji on
    purpose (they're never controls), and they are the common choice here.
    fluxer.py demojizes ``name`` for unicode emoji and keeps the character
    in ``unicode``.
    """
    if partial is None:
        return None
    emoji_id = getattr(partial, "id", None)
    if emoji_id:
        return str(emoji_id)
    char = getattr(partial, "unicode", None) or getattr(partial, "name", None)
    return normalise(char) if char else None


# -- parsing "🎮 Gamer" lines ----------------------------------------------

@dataclass(frozen=True)
class Pair:
    emoji: Emoji
    #: The role as written: a mention token, an id, or a name.
    role_text: str
    line: str


_FILLER = re.compile(r"^[\s\-–—:=>→|,;.]+|[\s\-–—:=>→|,;.]+$")
_LEADING_WORDS = re.compile(r"^(?:for|is|gets?|=)\s+", re.IGNORECASE)
_TRAILING_ROLE = re.compile(r"\s+role$", re.IGNORECASE)


def _clean_role(text: str) -> str:
    # A mention is already exact, and its closing ">" is in _FILLER.
    m = ROLE_MENTION.search(text or "")
    if m:
        return m.group(0)
    text = _FILLER.sub("", text or "")
    text = _LEADING_WORDS.sub("", text)
    text = _TRAILING_ROLE.sub("", text)
    text = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE)
    return _FILLER.sub("", text).strip()


def _tokens(line: str, custom: Mapping[str, str]
            ) -> tuple[list[tuple[int, int, Optional[Emoji], str]], str]:
    """Emoji spans in ``line`` as (start, end, emoji or None, raw)."""
    spans: list[tuple[int, int, Optional[Emoji], str]] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    for m in CUSTOM.finditer(line):
        spans.append((m.start(), m.end(), emoji_from_token(m.group(0)),
                      m.group(0)))
        taken.append((m.start(), m.end()))
    # The message text he gets has custom emoji flattened to ":name:".
    # Resolved against the guild's emoji; unknown ones are reported.
    for m in SHORTCODE.finditer(line):
        if not free(m.start(), m.end()):
            continue
        token = custom.get(m.group(1).lower())
        spans.append((m.start(), m.end(),
                      emoji_from_token(token) if token else None,
                      m.group(0)))
        taken.append((m.start(), m.end()))
    for m in UNICODE.finditer(line):
        if not free(m.start(), m.end()):
            continue
        spans.append((m.start(), m.end(), emoji_from_token(m.group(0)),
                      m.group(0)))
        taken.append((m.start(), m.end()))
    spans.sort()
    return spans, line


def parse_pairs(text: str, custom: Optional[Mapping[str, str]] = None
                ) -> tuple[list[Pair], list[str]]:
    """``(pairs, problems)`` from "🎮 Gamer" lines.

    One pair per line normally; "🎮 Gamer, 🎨 Artist" on one line works
    too. The emoji may come first or last ("Gamer 🎮"). ``custom`` maps a
    lowercased custom emoji name to its ``<:name:id>`` token, for text
    whose custom emoji were flattened to ``:name:``. A line with no emoji
    is ignored, not reported: it's usually "here you go:".
    """
    custom = {k.lower(): v for k, v in (custom or {}).items()}
    pairs: list[Pair] = []
    problems: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        spans, _ = _tokens(line, custom)
        if not spans:
            continue
        unknown = [raw for _, _, e, raw in spans if e is None]
        for raw in unknown:
            problems.append(f"{raw}: I can't find that emoji in this "
                            "server")
        # Emoji first, after any list bullet ("- 🎮 Gamer", "1. 🎮 Gamer").
        if not re.sub(r"[\s\-–—:=,;*•.\d]", "", line[:spans[0][0]]):
            # Emoji first: each emoji owns the text after it.
            for i, (start, end, emoji, raw) in enumerate(spans):
                stop = spans[i + 1][0] if i + 1 < len(spans) else len(line)
                role = _clean_role(line[end:stop])
                if emoji is None:
                    continue
                if not role:
                    problems.append(f"{raw}: which role?")
                    continue
                pairs.append(Pair(emoji, role, line))
        elif len(spans) == 1:
            # "Gamer 🎮" or "Gamer - 🎮 please".
            start, end, emoji, raw = spans[0]
            role = _clean_role(line[:start] + " " + line[end:])
            if emoji is None:
                continue
            if not role:
                problems.append(f"{raw}: which role?")
                continue
            pairs.append(Pair(emoji, role, line))
        else:
            problems.append(f"“{line}”: put one emoji before each role")
    return pairs, problems


# -- roles --------------------------------------------------------------------

def resolve_role(text: str, roles: Mapping[str, Any]
                 ) -> tuple[Optional[Any], Optional[str]]:
    """``(role, None)`` or ``(None, why)``. ``roles`` is ``{id: Role}``."""
    text = (text or "").strip()
    m = ROLE_MENTION.search(text)
    wanted_id = m.group(1) if m else (text if text.isdigit() else None)
    if wanted_id:
        role = roles.get(wanted_id)
        return (role, None) if role else (None, "no role with that id here")
    name = _clean_role(text.lstrip("@").strip())
    low = name.casefold()
    exact = [r for r in roles.values() if r.name.casefold() == low]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"there are {len(exact)} roles called “{name}”; " \
                     "mention the one you mean"
    partial = [r for r in roles.values() if low and low in r.name.casefold()]
    if len(partial) == 1:
        return partial[0], None
    if partial:
        shown = ", ".join(sorted(r.name for r in partial)[:5])
        return None, f"“{name}” could be {shown}; which one?"
    return None, f"there's no role called “{name}”"


def top_position(member: Any) -> int:
    return max((getattr(r, "position", 0) for r in
                getattr(member, "roles", ()) or ()), default=0)


def refusal(role: Any, *, guild_id: str, bot_top: Optional[int],
            invoker_top: int, invoker_exempt: bool) -> Optional[str]:
    """Why ``role`` can't be handed out by reaction, or None."""
    if str(role.id) == str(guild_id) or role.name == "@everyone":
        return "everyone already has @everyone"
    if Perm(int(role.permissions or 0)) & DANGEROUS:
        return (f"{role.name} has moderation or admin permissions, and "
                "anyone who can react could give it to themselves")
    if bot_top is not None and role.position >= bot_top:
        return (f"{role.name} is at or above my highest role, so I can't "
                "give it out. Move my role above it first")
    if not invoker_exempt and role.position >= invoker_top:
        return (f"{role.name} is at or above your highest role, and you "
                "can't hand out a role you couldn't assign yourself")
    return None


# -- placement ----------------------------------------------------------------

def _is_category(ch: Mapping[str, Any]) -> bool:
    return int(ch.get("type") or 0) == 4


def _sort_key(ch: Mapping[str, Any]) -> tuple:
    # Voice channels list after text within a category, as in the client.
    voice = int(ch.get("type") or 0) in (2, 13)
    return (voice, int(ch.get("position") or 0), int(ch.get("id") or 0))


def display_order(channels: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Non-category channels in the order the client shows them:
    uncategorised first, then each category's children."""
    chans = [dict(c) for c in channels or ()]
    cats = sorted((c for c in chans if _is_category(c)),
                  key=lambda c: (int(c.get("position") or 0),
                                 int(c.get("id") or 0)))
    out = sorted((c for c in chans if not _is_category(c)
                  and not c.get("parent_id")), key=_sort_key)
    for cat in cats:
        out += sorted((c for c in chans if not _is_category(c)
                       and str(c.get("parent_id") or "") == str(cat["id"])),
                      key=_sort_key)
    return out


def is_anchor(name: str) -> bool:
    low = (name or "").lower()
    return ("announcement" in low or "rules" in low
            or low.strip() == "do-not-type")


def pick_anchor(channels: Iterable[Mapping[str, Any]]) -> Optional[dict]:
    """The announcements/rules/do-not-type channel lowest on screen."""
    hits = [c for c in display_order(channels)
            if is_anchor(str(c.get("name") or ""))]
    return hits[-1] if hits else None


def placement(channels: Iterable[Mapping[str, Any]], new_id: str
              ) -> tuple[Optional[str], list[dict], Optional[dict]]:
    """Where ``new_id`` goes: ``(parent_id, positions, anchor)``.

    ``positions`` is the full ``[{id, position}]`` order of its siblings
    with the new channel right after the anchor (or first, with no
    anchor), for the bulk position endpoint. Renumbered from 0 because
    positions only order siblings; a single "anchor + 1" can tie with the
    next channel and land on the wrong side of it.
    """
    chans = [dict(c) for c in channels or ()
             if str(c.get("id")) != str(new_id)]
    anchor = pick_anchor(chans)
    parent = str(anchor.get("parent_id")) if anchor and \
        anchor.get("parent_id") else None
    siblings = [c for c in chans if not _is_category(c)
                and (str(c.get("parent_id") or "") or None) == parent]
    siblings.sort(key=lambda c: (int(c.get("position") or 0),
                                 int(c.get("id") or 0)))
    ids = [str(c["id"]) for c in siblings]
    at = ids.index(str(anchor["id"])) + 1 if anchor else 0
    ids.insert(at, str(new_id))
    return parent, [{"id": cid, "position": i} for i, cid in
                    enumerate(ids)], anchor


async def _reorder(http: Any, guild_id: str, positions: list[dict],
                   channel_id: str, parent: Optional[str],
                   anchor: Optional[dict]) -> bool:
    """Put the new channel in place among its siblings.

    fluxer.py 0.4.2 has no binding for the bulk channel-position endpoint
    (``PATCH /guilds/{id}/channels``), so it goes through the client's own
    ``request``. If that's refused, fall back to ``anchor + 1`` on the one
    channel, which can tie with the next channel and sort on either side.
    """
    request = getattr(http, "request", None)
    route = getattr(http, "_route", None)
    if request is not None and route is not None:
        body = [dict(p, **({"parent_id": parent} if p["id"] == channel_id
                           else {})) for p in positions]
        try:
            await request(route("PATCH", "/guilds/{guild_id}/channels",
                                guild_id=guild_id), json=body)
            return True
        except Exception:
            logger.info("bulk channel reorder refused; setting one position",
                        exc_info=True)
    try:
        position = int(anchor.get("position") or 0) + 1 if anchor else 0
        await http.modify_channel(channel_id, position=position,
                                  parent_id=parent)
        return True
    except Exception:
        logger.warning("could not position #%s", CHANNEL_NAME,
                       exc_info=True)
        return False


async def ensure_channel(http: Any, guild_id: str,
                         bot_id: Optional[str] = None
                         ) -> tuple[Optional[str], bool, Optional[dict],
                                    list[str]]:
    """Create or adopt #reaction-roles: ``(id, created, anchor, problems)``.

    An existing #reaction-roles is adopted **as it is**: the server may
    have made it and set its permissions on purpose. A new one goes under
    the lowest announcements/rules/do-not-type channel, locked so people
    can read and click reactions but not post or add new emoji.
    """
    try:
        channels = list(await http.get_guild_channels(guild_id) or [])
    except Exception as exc:
        return None, False, None, [f"couldn't list channels: {exc}"]
    for raw in channels:
        if str(raw.get("name") or "").lower() == CHANNEL_NAME \
                and not _is_category(raw):
            return str(raw["id"]), False, None, []

    parent, _, anchor = placement(channels, "")
    try:
        created = await http.create_guild_channel(
            guild_id, name=CHANNEL_NAME, type=0, topic=CHANNEL_TOPIC,
            parent_id=parent)
        channel_id = str(created.get("id"))
    except Exception as exc:
        return None, False, None, [f"couldn't create the channel: {exc}"]

    problems: list[str] = []
    # Permissions before position: a channel briefly in the wrong place is
    # harmless, one briefly open to posting is not.
    try:
        await http.edit_channel_permissions(
            channel_id, guild_id, allow=str(int(EVERYONE_ALLOW)),
            deny=str(int(EVERYONE_DENY)), type=0)
    except Exception as exc:
        problems.append(f"couldn't lock it for everyone: {exc}")
    if bot_id:
        try:
            await http.edit_channel_permissions(
                channel_id, bot_id, allow=str(int(BOT_ALLOW)), deny="0",
                type=1)
        except Exception:
            # Fine when his role already covers it; the post will say if not.
            logger.info("member overwrite for myself refused in %s",
                        channel_id, exc_info=True)
    _, positions, anchor = placement(channels, channel_id)
    if not await _reorder(http, guild_id, positions, channel_id, parent,
                          anchor):
        problems.append("couldn't move it into place")
    return channel_id, True, anchor, problems


# -- the store ----------------------------------------------------------------

class ReactionRoleStore:
    """Reaction-role messages, kept in the chat settings so they survive
    restarts. ``settings`` is a ``SettingsStore`` (``ChatSettings.store``).
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def _all(self, guild_id: str) -> dict:
        return dict(self.settings.get(str(guild_id), STORE_KEY, {}) or {})

    def _save(self, guild_id: str, data: dict) -> None:
        if data:
            self.settings.set(str(guild_id), STORE_KEY, data)
        else:
            self.settings.unset(str(guild_id), STORE_KEY)

    def messages(self, guild_id: str) -> dict:
        return self._all(guild_id)

    def get(self, guild_id: str, message_id: str) -> Optional[dict]:
        return self._all(guild_id).get(str(message_id))

    def find(self, message_id: str, guild_id: Optional[str] = None
             ) -> Optional[tuple[str, dict]]:
        """``(guild_id, entry)`` for a message, or None. Reaction events
        usually carry the guild; when one doesn't, every guild is
        searched (a handful of entries at most)."""
        guilds = [str(guild_id)] if guild_id else list(self.settings.guilds())
        for gid in guilds:
            entry = self._all(gid).get(str(message_id))
            if entry is not None:
                return gid, entry
        return None

    def target(self, guild_id: str, channel_id: Optional[str] = None
               ) -> Optional[str]:
        """The message new roles go on: the newest in ``channel_id`` when
        one is named, otherwise the newest anywhere in the guild."""
        found = [(e.get("created", 0), mid) for mid, e in
                 self._all(guild_id).items()
                 if channel_id is None
                 or str(e.get("channel_id")) == str(channel_id)]
        return max(found)[1] if found else None

    def create(self, guild_id: str, message_id: str, channel_id: str,
               roles: Mapping[str, Mapping[str, str]]) -> None:
        data = self._all(guild_id)
        data[str(message_id)] = {"channel_id": str(channel_id),
                                 "created": time.time(),
                                 "roles": {k: dict(v) for k, v in
                                           roles.items()}}
        self._save(guild_id, data)

    def merge(self, guild_id: str, message_id: str,
              pairs: Sequence[tuple[Emoji, str]], *, dm: bool = False
              ) -> tuple[list[str], list[str]]:
        """Add ``(emoji, role_id)`` pairs. Returns (added, replaced) keys.

        An emoji already on the message is re-pointed at the new role, not
        duplicated: one emoji, one role. ``dm`` marks them opt-in DM
        roles; re-adding a DM role as it was keeps the mark.
        """
        data = self._all(guild_id)
        entry = data.get(str(message_id))
        if entry is None:
            raise KeyError(message_id)
        roles = dict(entry.get("roles") or {})
        added, replaced = [], []
        for emoji, role_id in pairs:
            before = roles.get(emoji.key)
            (replaced if before else added).append(emoji.key)
            value = {"role": str(role_id), "emoji": emoji.shown}
            if dm or (before and before.get("dm")
                      and str(before.get("role")) == str(role_id)):
                value["dm"] = True
            roles[emoji.key] = value
        entry = {**entry, "roles": roles}
        data[str(message_id)] = entry
        self._save(guild_id, data)
        self._prune_consent(guild_id)
        return added, replaced

    def remove(self, guild_id: str, message_id: str, key: str
               ) -> Optional[dict]:
        """Drop one emoji's mapping. Returns what was removed."""
        data = self._all(guild_id)
        entry = data.get(str(message_id))
        if entry is None:
            return None
        roles = dict(entry.get("roles") or {})
        gone = roles.pop(key, None)
        if gone is not None:
            data[str(message_id)] = {**entry, "roles": roles}
            self._save(guild_id, data)
            self._prune_consent(guild_id)
        return gone

    def drop_message(self, message_id: str,
                     guild_id: Optional[str] = None) -> bool:
        """Forget a deleted message. True if it was one of ours."""
        found = self.find(message_id, guild_id)
        if found is None:
            return False
        gid, _ = found
        data = self._all(gid)
        data.pop(str(message_id), None)
        self._save(gid, data)
        self._prune_consent(gid)
        return True

    def role_for(self, guild_id: str, message_id: str,
                 key: Optional[str]) -> Optional[str]:
        entry = self.get(guild_id, message_id)
        if entry is None or not key:
            return None
        hit = (entry.get("roles") or {}).get(key)
        return str(hit["role"]) if hit else None

    # -- opt-in DM roles --------------------------------------------------

    def dm_roles(self, guild_id: str) -> dict[str, dict]:
        """``{role_id: {"message_id", "channel_id", "emoji"}}`` for every
        role marked opt-in DM on one of this guild's messages."""
        out: dict[str, dict] = {}
        for mid, entry in self._all(guild_id).items():
            for v in (entry.get("roles") or {}).values():
                if v.get("dm"):
                    out[str(v["role"])] = {
                        "message_id": str(mid),
                        "channel_id": str(entry.get("channel_id")),
                        "emoji": v.get("emoji")}
        return out

    def is_dm_role(self, guild_id: str, role_id: str) -> bool:
        return str(role_id) in self.dm_roles(guild_id)

    def _consents(self, guild_id: str) -> dict:
        return {k: dict(v) for k, v in
                (self.settings.get(str(guild_id), CONSENT_KEY, {})
                 or {}).items()}

    def _save_consents(self, guild_id: str, data: dict) -> None:
        data = {k: v for k, v in data.items() if v}
        if data:
            self.settings.set(str(guild_id), CONSENT_KEY, data)
        else:
            self.settings.unset(str(guild_id), CONSENT_KEY)

    def set_consent(self, guild_id: str, role_id: str, user_id: str,
                    given: bool) -> None:
        """Record (or withdraw) one member's opt-in. Only their own
        reaction may call this with ``given=True``."""
        data = self._consents(guild_id)
        people = data.setdefault(str(role_id), {})
        if given:
            people[str(user_id)] = time.time()
        else:
            people.pop(str(user_id), None)
        self._save_consents(guild_id, data)

    def consented(self, guild_id: str, role_id: str) -> set[str]:
        if not self.is_dm_role(guild_id, role_id):
            return set()
        return set(self._consents(guild_id).get(str(role_id)) or {})

    def clear_consent(self, guild_id: str, role_id: str) -> None:
        data = self._consents(guild_id)
        if data.pop(str(role_id), None) is not None:
            self._save_consents(guild_id, data)

    def _prune_consent(self, guild_id: str) -> None:
        """Forget consent for roles no longer marked DM anywhere, so a
        role unmarked and later re-marked starts from nobody."""
        data = self._consents(guild_id)
        if not data:
            return
        marked = self.dm_roles(guild_id)
        kept = {k: v for k, v in data.items() if k in marked}
        if kept != data:
            self._save_consents(guild_id, kept)

    def unconsented(self, guild_id: str, user_id: str,
                    role_ids: Iterable[Any]) -> list[str]:
        """The opt-in DM roles in ``role_ids`` this member holds without
        having reacted for them: given by someone else, to be taken back.
        """
        marked = self.dm_roles(guild_id)
        if not marked:
            return []
        consents = self._consents(guild_id)
        return [str(r) for r in role_ids
                if str(r) in marked
                and str(user_id) not in (consents.get(str(r)) or {})]


def lookup_pair(entry: Mapping[str, Any], text: str,
                roles: Mapping[str, Any]) -> Optional[str]:
    """The emoji key in ``entry`` that ``text`` names, by emoji or role."""
    mapping = entry.get("roles") or {}
    for raw in (m.group(0) for m in CUSTOM.finditer(text or "")):
        e = emoji_from_token(raw)
        if e and e.key in mapping:
            return e.key
    for m in UNICODE.finditer(text or ""):
        key = normalise(m.group(0))
        if key in mapping:
            return key
    for m in SHORTCODE.finditer(text or ""):
        for key, v in mapping.items():
            if f":{m.group(1).lower()}:" in str(v.get("emoji", "")).lower():
                return key
    role, _ = resolve_role(text, {rid: r for rid, r in roles.items()
                                  if any(str(v.get("role")) == rid
                                         for v in mapping.values())})
    if role is not None:
        for key, v in mapping.items():
            if str(v.get("role")) == str(role.id):
                return key
    return None
