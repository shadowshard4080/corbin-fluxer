"""Reaction roles: set up, add to, take from and list a sign-up message.

"Set up reaction roles" (optionally "in #channel") makes or picks the
channel, then asks which roles and emoji. The answer is the asker's next
message there, caught before dispatch by ``bot.py``
(``_answer_reaction_roles``, which calls :func:`answer`): the classifier
sees one message at a time, and "🎮 Gamer / 🎨 Artist" on its own reads as
chat, not a command. Same pattern as "which channel is your general chat?".

Adding more later edits the existing message rather than posting a
second one (owner's requirement): one sign-up sheet that grows.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping, Optional, Sequence

from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm, resolve
from ..core.channel_access import grant_self
from ..core.reaction_roles import (BOT_ALLOW, CHANNEL_NAME, QUESTION_TTL,
                                   Emoji, Pair,
                                   ReactionRoleStore, ensure_channel,
                                   lookup_pair, parse_pairs, refusal,
                                   resolve_role, top_position)
from ..core.registry import Param, Risk, command

logger = logging.getLogger("corbin.reaction_roles")

COLOUR = 0x7E57C2
TITLE = "Pick your roles"
FOOTER = ("React to give yourself a role. Take your reaction off to "
          "drop it.")
#: Shown beside an opt-in DM role, and the footer that explains it. The
#: reaction is the consent, so the message itself must say what it means.
DM_LABEL = "opt-in: DMs from staff"
DM_FOOTER = ("A role marked \"DMs from staff\" means staff may message "
             "you through me. Reacting for it is your yes; take the "
             "reaction off to stop.")
#: Member pages (1000 each) read when looking for a role's holders.
MEMBER_PAGES = 50
EXAMPLE = "🎮 Gamer\n🎨 @Artist\n:custom_emoji: Night Owls"
_CANCEL = re.compile(r"^\s*(cancel|never\s*mind|nevermind|stop|forget it)"
                     r"\b", re.IGNORECASE)


# -- helpers --------------------------------------------------------------------

def _store(extras: Mapping[str, Any]) -> ReactionRoleStore:
    store = extras.get("reaction_roles")
    if store is None:
        store = ReactionRoleStore(extras["settings"].store)
    return store


def _bot_id(adapter: Any) -> Optional[str]:
    user = getattr(getattr(adapter, "client", None), "user", None)
    return str(user.id) if getattr(user, "id", None) else None


async def custom_emoji(adapter: Any, guild_id: str) -> dict[str, str]:
    """``{name: <:name:id>}`` for the guild's emoji.

    The text a command gets has custom emoji flattened to ``:name:`` (no
    raw ids in model context), so this is how they're turned back.
    """
    try:
        http = adapter._rest()
        items = await http.get_guild_emojis(guild_id) or []
    except Exception:
        logger.debug("could not list emoji for %s", guild_id, exc_info=True)
        return {}
    out = {}
    for item in items:
        name, eid = (item or {}).get("name"), (item or {}).get("id")
        if name and eid:
            prefix = "a" if item.get("animated") else ""
            out[str(name).lower()] = f"<{prefix}:{name}:{eid}>"
    return out


def render(entry: Mapping[str, Any]) -> Embed:
    roles = (entry or {}).get("roles") or {}
    lines = [f"{v['emoji']}  <@&{v['role']}>"
             + (f"  ({DM_LABEL})" if v.get("dm") else "")
             for v in roles.values()]
    footer = FOOTER
    if any(v.get("dm") for v in roles.values()):
        footer += " " + DM_FOOTER
    return Embed(title=TITLE, colour=COLOUR, footer=footer,
                 description="\n".join(lines) or "No roles on here yet.")


async def role_holders(adapter: Any, guild_id: str,
                       role_id: str) -> Optional[list[str]]:
    """User ids holding ``role_id``, or None if the member list couldn't
    be read (never an empty list standing in for "couldn't tell")."""
    try:
        http = adapter._rest()
    except Exception:
        return None
    holders: list[str] = []
    after = "0"
    for _ in range(MEMBER_PAGES):
        try:
            page = await http.request(http._route(
                "GET", "/guilds/{guild_id}/members?limit=1000&after="
                + after, guild_id=str(guild_id))) or []
        except Exception:
            logger.warning("couldn't list members of %s", guild_id,
                           exc_info=True)
            return None
        last = after
        for m in page:
            uid = str((m.get("user") or {}).get("id") or "")
            if uid and str(role_id) in [str(r) for r in
                                        (m.get("roles") or [])]:
                holders.append(uid)
            if uid.isdigit() and int(uid) > int(last):
                last = uid
        if len(page) < 1000 or last == after:
            break
        after = last
    return sorted(set(holders))


def _exempt(invoker: Any) -> bool:
    """The server's owner outranks every role. Nobody else does: the bot
    owner gets no pass in other people's servers."""
    return bool(getattr(invoker, "is_guild_owner", False))


async def apply(adapter: Any, store: ReactionRoleStore, *, guild_id: str,
                channel_id: str, message_id: Optional[str],
                pairs: Sequence[Pair], invoker: Any,
                problems: Sequence[str] = (),
                dm: bool = False) -> tuple[str, int]:
    """Validate ``pairs`` and put them on the message. ``(reply, added)``.

    With ``message_id`` None (or a message the store no longer has) a new
    sign-up message is posted; otherwise the existing one is edited and
    only the new reactions are added. ``dm`` marks them opt-in DM roles.
    """
    problems = list(problems)
    # Fresh positions, not the 5-minute cache: "move my role above it" is
    # the usual fix for a refusal, and a retry right after must see it.
    forget = getattr(adapter, "forget_roles", None)
    if callable(forget):
        forget(guild_id)
    roles = await adapter.role_index(guild_id)
    if not roles:
        return ("I couldn't read this server's roles, so I can't check "
                "those. Try again in a moment.", 0)
    bot_top: Optional[int] = None
    bot_id = _bot_id(adapter)
    if bot_id:
        try:
            me = await adapter.fetch_member(guild_id, bot_id)
            bot_top = top_position(me) if me is not None else None
        except Exception:
            logger.debug("could not fetch my own member", exc_info=True)
    if bot_top is None:
        problems.append("I couldn't check my own role's position, so if a "
                        "role doesn't get handed out, move my role above it")
    exempt = _exempt(invoker)
    invoker_top = top_position(invoker)

    valid: dict[str, tuple[Emoji, Any]] = {}
    for pair in pairs:
        role, why = resolve_role(pair.role_text, roles)
        if role is None:
            problems.append(f"{pair.emoji.shown} {pair.role_text}: {why}")
            continue
        why = refusal(role, guild_id=guild_id, bot_top=bot_top,
                      invoker_top=invoker_top, invoker_exempt=exempt)
        if why:
            problems.append(f"{pair.emoji.shown}: {why}")
            continue
        dm_mark = store.dm_roles(guild_id).get(str(role.id))
        if dm_mark and dm_mark.get("emoji") != pair.emoji.shown:
            # A second, unlabelled way onto a DM list isn't consent.
            problems.append(f"{pair.emoji.shown}: <@&{role.id}> is an "
                            "opt-in DM role and keeps its one emoji, "
                            f"{dm_mark.get('emoji')}")
            continue
        valid[pair.emoji.key] = (pair.emoji, role)
    if not valid:
        return ("I couldn't use any of those:\n- " + "\n- ".join(problems)
                if problems else "I didn't find any emoji and roles in "
                "that.", 0)

    entry = store.get(guild_id, message_id) if message_id else None
    new_keys: list[str]
    if entry is None:
        start = {e.key: {"role": str(r.id), "emoji": e.shown,
                         **({"dm": True} if dm else {})}
                 for e, r in valid.values()}
        sent = await adapter.send(channel_id, Response(
            embed=render({"roles": start})))
        if not sent:
            return (f"I couldn't post in <#{channel_id}>. I need to be able "
                    "to send messages and embeds there.", 0)
        message_id = str(sent)
        store.create(guild_id, message_id, channel_id, start)
        new_keys = list(start)
    else:
        channel_id = str(entry.get("channel_id") or channel_id)
        new_keys, _ = store.merge(
            guild_id, message_id,
            [(e, str(r.id)) for e, r in valid.values()], dm=dm)
        if not await adapter.edit_message(
                channel_id, message_id,
                Response(embed=render(store.get(guild_id, message_id)))):
            problems.append("I couldn't edit the message to list them "
                            "(was it deleted?)")

    for key in new_keys:
        emoji = valid[key][0]
        if not await adapter.add_reaction(channel_id, message_id,
                                          emoji.shown):
            problems.append(f"I couldn't react with {emoji.shown} (a custom "
                            "emoji from another server needs Use External "
                            "Emojis)")
    done = ", ".join(f"{e.shown} <@&{r.id}>" for e, r in valid.values())
    logger.info("reaction roles in %s: %d set on %s", guild_id, len(valid),
                message_id)
    reply = f"Done in <#{channel_id}>: {done}."
    if dm:
        reply += (" Only people who react for it will get messager DMs; "
                  "if anyone else is given the role I'll take it back off.")
    if problems:
        reply += "\n\nSome didn't work:\n- " + "\n- ".join(problems)
    return reply, len(valid)


def ask(extras: Mapping[str, Any], *, channel_id: str, user_id: str,
        guild_id: str, target: str, message_id: Optional[str]) -> None:
    """Let the asker's next message in this channel answer."""
    waiting = extras.get("reaction_role_questions")
    if waiting is not None:
        waiting[(str(channel_id), str(user_id))] = {
            "guild": str(guild_id), "target": str(target),
            "message": message_id,
            "deadline": time.monotonic() + QUESTION_TTL}


async def answer(questions: dict, store: ReactionRoleStore, adapter: Any, *,
                 guild_id: Optional[str], channel_id: str, member: Any,
                 raw_content: str
                 ) -> Optional[str]:
    """The reply to an answer to "which roles and emoji?", or None when
    this message isn't one (it's then handled as normal)."""
    key = (str(channel_id), str(member.id))
    asked = questions.get(key)
    if asked is None:
        return None
    if time.monotonic() > asked["deadline"] or \
            str(asked["guild"]) != str(guild_id):
        questions.pop(key, None)
        return None
    if _CANCEL.match(raw_content or ""):
        questions.pop(key, None)
        return "Okay, dropped it. Nothing changed."
    custom = await custom_emoji(adapter, str(guild_id))
    pairs, problems = parse_pairs(raw_content, custom)
    if not pairs and not problems:
        return None
    if not pairs:
        return ("I couldn't use that:\n- " + "\n- ".join(problems)
                + "\n\nTry again, or say cancel.")
    # The message may have been made (or deleted) since the question.
    message_id = asked.get("message") or store.target(guild_id,
                                                      asked["target"])
    reply, added = await apply(
        adapter, store, guild_id=str(guild_id), channel_id=asked["target"],
        message_id=message_id, pairs=pairs, invoker=member,
        problems=problems)
    if added:
        questions.pop(key, None)
    else:
        reply += "\n\nTry again, or say cancel."
    return reply


def _channel_arg(ctx: CommandContext, value: Optional[str]
                 ) -> tuple[Optional[str], Optional[str]]:
    """``(channel_id, error)`` for an optional channel parameter."""
    if not value:
        return None, None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text, None
    found = (ctx.extras.get("channel_index") or {}).get(text.lower())
    if found:
        return str(found), None
    return None, f"I can't find a channel called #{text}."


def _can_make_channels(invoker: Any) -> bool:
    if getattr(invoker, "is_guild_owner", False):
        return True
    perms = resolve((getattr(r, "permissions", 0)
                     for r in getattr(invoker, "roles", ()) or ()))
    return bool(perms & Perm.MANAGE_CHANNELS)


# -- commands -------------------------------------------------------------------

@command(
    name="reaction_roles",
    description="Set up reaction roles (react with an emoji, get a role)",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_ROLES,
    aliases=("setup_reaction_roles", "reactionroles"),
    params=(Param("channel", f"where; default: a new #{CHANNEL_NAME}",
                  kind="channel", required=False),),
    confirm_phrase=lambda a: ("set up reaction roles"
                              + (f" in {a['channel']}" if a.get("channel")
                                 else f" in a new #{CHANNEL_NAME} channel")),
    examples=("set up reaction roles", "set up reaction roles in #roles",
              "make a reaction roles channel"),
)
async def reaction_roles(ctx: CommandContext,
                         channel: Optional[str] = None) -> Any:
    if not ctx.guild_id:
        return "Reaction roles only work in a server."
    adapter = ctx.extras["adapter"]
    store = _store(ctx.extras)
    target, error = _channel_arg(ctx, channel)
    if error:
        return error
    note = ""
    if target is None:
        if not (_can_make_channels(ctx.invoker)
                or _exempt(ctx.invoker)):
            return (f"Making #{CHANNEL_NAME} needs Manage Channels, which "
                    "you don't have. Name a channel that already exists "
                    "and I'll put it there.")
        target, created, anchor, problems = await ensure_channel(
            adapter._rest(), ctx.guild_id, _bot_id(adapter))
        if target is None:
            return (f"I couldn't make #{CHANNEL_NAME}: {problems[0]}. I need "
                    "Manage Channels and Manage Roles.")
        if created:
            # ensure_channel's member overwrite alone didn't take for
            # #level-ups (live 2026-11-18): the bot's roles get one too,
            # and a failure is said here rather than in the log alone.
            if not await grant_self(adapter, adapter._rest(), ctx.guild_id,
                                    target, BOT_ALLOW):
                problems.append("I couldn't give myself permission to post "
                                "there; give my role Send Messages and Add "
                                "Reactions in it")
            where = (f" under <#{anchor['id']}>" if anchor else
                     " at the top")
            note = (f"Made <#{target}>{where}. People can read it and click "
                    "the reactions, but not post or add new emoji.")
        else:
            note = f"Using the existing <#{target}>."
        if problems:
            note += " (" + "; ".join(problems) + ")"
    existing = store.target(ctx.guild_id, target)
    ask(ctx.extras, channel_id=ctx.channel.id, user_id=ctx.invoker.id,
        guild_id=ctx.guild_id, target=target, message_id=existing)
    if existing:
        intro = (f"<#{target}> already has a reaction-roles message, so "
                 "I'll add to that one.")
    else:
        intro = f"I'll post the message in <#{target}>."
    return (f"{note}\n\n{intro} Which roles? Send them here, one per line, "
            f"emoji first:\n{EXAMPLE}\n\nOr say cancel.").strip()


@command(
    name="reaction_roles_add",
    description="Add emoji and roles to the reaction roles",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_ROLES,
    aliases=("add_reaction_role",),
    params=(
        Param("pairs", "the emoji and role pairs, e.g. 🎨 Artist"),
        Param("channel", "the channel whose message to add to",
              kind="channel", required=False),
    ),
    confirm_phrase=lambda a: f"add {a.get('pairs')} to the reaction roles",
    examples=("add 🎨 Artist to the reaction roles",
              "put 🐺 Wolves on the reaction roles message"),
)
async def reaction_roles_add(ctx: CommandContext, pairs: str,
                             channel: Optional[str] = None) -> str:
    if not ctx.guild_id:
        return "Reaction roles only work in a server."
    adapter = ctx.extras["adapter"]
    store = _store(ctx.extras)
    where, error = _channel_arg(ctx, channel)
    if error:
        return error
    message_id = store.target(ctx.guild_id, where)
    if message_id is None:
        place = f" in <#{where}>" if where else ""
        return (f"There's no reaction-roles message{place} yet. Say "
                "\"set up reaction roles\" first.")
    custom = await custom_emoji(adapter, ctx.guild_id)
    found, problems = parse_pairs(pairs, custom)
    if not found:
        # The classifier's copy can lose an emoji; the person's own words
        # still have it.
        raw = ctx.extras.get("raw_text") or ""
        raw = re.sub(r"\b(?:to|on|onto|in)\s+(?:the\s+)?reaction[\s-]*roles?"
                     r"\b.*$", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"^.*?\b(?:add|put)\b", "", raw, flags=re.IGNORECASE)
        found, problems = parse_pairs(raw, custom)
    if not found:
        return ("Tell me the emoji and the role, like \"add 🎨 Artist to "
                "the reaction roles\"."
                + ("\n- " + "\n- ".join(problems) if problems else ""))
    entry = store.get(ctx.guild_id, message_id)
    reply, _ = await apply(
        adapter, store, guild_id=ctx.guild_id,
        channel_id=str(entry["channel_id"]), message_id=message_id,
        pairs=found, invoker=ctx.invoker,
        problems=problems)
    return reply


@command(
    name="reaction_roles_dm",
    description="Add an opt-in DM role to the reaction roles: the only "
                "kind of role the messager will DM, and only to people "
                "who reacted for it themselves",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_ROLES,
    aliases=("dm_optin_role", "messager_role"),
    params=(
        Param("pairs", "one emoji and a role nobody holds yet, e.g. "
                       "📬 Announcements"),
        Param("channel", "the channel whose message to add to",
              kind="channel", required=False),
    ),
    confirm_phrase=lambda a: (f"add {a.get('pairs')} to the reaction roles "
                              "as an opt-in DM role"),
    examples=("add 📬 Announcements to the reaction roles as an opt-in "
              "DM role",
              "make a messager opt-in role on the reaction roles"),
)
async def reaction_roles_dm(ctx: CommandContext, pairs: str,
                            channel: Optional[str] = None) -> str:
    if not ctx.guild_id:
        return "Reaction roles only work in a server."
    adapter = ctx.extras["adapter"]
    store = _store(ctx.extras)
    where, error = _channel_arg(ctx, channel)
    if error:
        return error
    message_id = store.target(ctx.guild_id, where)
    if message_id is None:
        place = f" in <#{where}>" if where else ""
        return (f"There's no reaction-roles message{place} yet. Say "
                "\"set up reaction roles\" first, then add the DM role.")
    custom = await custom_emoji(adapter, ctx.guild_id)
    found, problems = parse_pairs(pairs, custom)
    if not found:
        raw = ctx.extras.get("raw_text") or ""
        raw = re.sub(r"\b(?:to|on|onto|in)\s+(?:the\s+)?reaction[\s-]*roles?"
                     r"\b.*$", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"^.*?\b(?:add|put|make)\b", "", raw,
                     flags=re.IGNORECASE)
        found, problems = parse_pairs(raw, custom)
    if len(found) != 1:
        return ("Give me exactly one emoji and one role, like \"add 📬 "
                "Announcements to the reaction roles as an opt-in DM role\"."
                + ("\n- " + "\n- ".join(problems) if problems else ""))
    roles = await adapter.role_index(ctx.guild_id)
    role, why = resolve_role(found[0].role_text, roles)
    if role is None:
        return f"{found[0].emoji.shown} {found[0].role_text}: {why}"
    # Everyone on a DM role must have chosen it. Someone who already
    # holds this one didn't, so it has to be a role nobody has yet.
    holders = await role_holders(adapter, ctx.guild_id, str(role.id))
    if holders is None:
        return ("I couldn't read the member list, so I can't check that "
                "nobody already has that role. Try again in a moment.")
    me = _bot_id(adapter)
    holders = [h for h in holders if h != me]
    if holders:
        return (f"{len(holders)} {'person already has' if len(holders) == 1 else 'people already have'} "
                f"<@&{role.id}> without having opted in. Make a new role "
                "nobody has yet, so everyone on it chose to be.")
    entry = store.get(ctx.guild_id, message_id)
    store.clear_consent(ctx.guild_id, str(role.id))
    reply, _ = await apply(
        adapter, store, guild_id=ctx.guild_id,
        channel_id=str(entry["channel_id"]), message_id=message_id,
        pairs=found, invoker=ctx.invoker,
        problems=problems,
        dm=True)
    return reply


@command(
    name="reaction_roles_remove",
    description="Take an emoji and its role off the reaction roles",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_ROLES,
    aliases=("remove_reaction_role",),
    params=(Param("target", "the emoji or role to take off"),
            Param("channel", "the channel whose message to change",
                  kind="channel", required=False)),
    confirm_phrase=lambda a: f"take {a.get('target')} off the reaction roles",
    examples=("remove 🎨 from the reaction roles",
              "take the Artist role off reaction roles"),
)
async def reaction_roles_remove(ctx: CommandContext, target: str,
                                channel: Optional[str] = None) -> str:
    if not ctx.guild_id:
        return "Reaction roles only work in a server."
    adapter = ctx.extras["adapter"]
    store = _store(ctx.extras)
    where, error = _channel_arg(ctx, channel)
    if error:
        return error
    roles = await adapter.role_index(ctx.guild_id)
    text = " ".join(p for p in (target, ctx.extras.get("raw_text") or "")
                    if p)
    messages = sorted(store.messages(ctx.guild_id).items(),
                      key=lambda kv: -kv[1].get("created", 0))
    for message_id, entry in messages:
        if where and str(entry.get("channel_id")) != where:
            continue
        key = lookup_pair(entry, target, roles) or \
            lookup_pair(entry, text, roles)
        if key is None:
            continue
        gone = store.remove(ctx.guild_id, message_id, key)
        channel_id = str(entry["channel_id"])
        bot_id = _bot_id(adapter)
        if bot_id:
            await adapter.remove_user_reaction(channel_id, message_id,
                                               gone["emoji"], bot_id)
        await adapter.edit_message(channel_id, message_id, Response(
            embed=render(store.get(ctx.guild_id, message_id))))
        tail = (" It's no longer an opt-in DM role, so the messager "
                "won't DM it." if gone.get("dm") else "")
        return (f"Took {gone['emoji']} <@&{gone['role']}> off. People who "
                f"already have the role keep it.{tail}")
    return ("That isn't on any reaction-roles message here. Say \"list the "
            "reaction roles\" to see what is.")


@command(
    name="reaction_roles_list",
    description="List the reaction roles in this server",
    risk=Risk.SAFE,
    permission=Perm.NONE,
    examples=("list the reaction roles", "what reaction roles are there"),
)
async def reaction_roles_list(ctx: CommandContext) -> Any:
    if not ctx.guild_id:
        return "Reaction roles only work in a server."
    messages = _store(ctx.extras).messages(ctx.guild_id)
    if not messages:
        return "No reaction roles here. Staff can say \"set up reaction roles\"."
    adapter = ctx.extras["adapter"]
    # Field names render as plain text, so a <#id> link there would show
    # as the raw token; the name reads fine.
    names = await adapter.channel_names(
        [str(e.get("channel_id")) for e in messages.values()])
    embed = Embed(title="Reaction roles", colour=COLOUR)
    for entry in messages.values():
        cid = str(entry.get("channel_id"))
        lines = [f"{v['emoji']}  <@&{v['role']}>"
                 + (f"  ({DM_LABEL})" if v.get("dm") else "")
                 for v in (entry.get("roles") or {}).values()]
        embed.add_list_field(f"#{names.get(cid) or cid}",
                             lines or ["(none yet)"])
    return embed
