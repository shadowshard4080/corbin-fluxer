"""Configuration commands.

These are the first commands with persistent state, so they take the settings
store from ``ctx.extras`` rather than owning a global -- that keeps them
testable and keeps the store's lifetime owned by the bot.
"""

from __future__ import annotations

import re
import time
from typing import Mapping, Optional

from ..core.context import CommandContext, Embed
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command
from ..identity import NAME


@command(
    name="reply_channel",
    description=f"Toggle whether {NAME} answers every message in this channel",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("response_channel", "aichannel"),
    examples=(
        "set this as your response channel",
        f"{NAME.lower()}, always reply in here",
        "stop answering everything in this channel",
    ),
)
async def reply_channel(ctx: CommandContext) -> str:
    settings = ctx.extras["settings"]
    now_on = settings.toggle_ai_channel(ctx.guild_id, ctx.channel.id)
    if now_on:
        return ("Got it — I'll answer everything in here now, no mention "
                "needed. Say the same thing again to turn it back off.")
    return "Back to mention-only in here."


#: How long "presence or silence?" stays answerable.
SILENCE_QUESTION_TTL = 300.0

#: Commands that still run in a silenced channel (or with the mode off).
#: Without these there is no way back: the gate drops every message
#: before the dispatcher, so "unsilence" never reached a command.
#: silence is here so a misroute of "unsilence" still answers
#: ("already silenced") instead of vanishing. Registry names are
#: kebab-case (``spec.name`` is "chat-mode"); spelling these with
#: underscores is what made the first version drop every unsilence.
WORKS_WHILE_QUIET = frozenset({"unsilence", "chat-mode", "silence"})


def works_while_quiet(name: Optional[str]) -> bool:
    return bool(name) and \
        str(name).lower().replace("_", "-") in WORKS_WHILE_QUIET

#: Free pre-filter for a quiet channel: only messages that could be one
#: of WORKS_WHILE_QUIET are worth a classifier call there.
QUIET_ESCAPE = re.compile(
    r"(silen|mute|shush|quiet|talk|speak|respond|answer|come back|"
    r"back on|turn on|mode|global|mention)", re.IGNORECASE)

_ALL_CHANNELS = {"all", "everywhere", "every channel", "all channels"}

#: "unsilence" and the ways people actually type it ("unsilnce",
#: "un-silence", "unslience"), plus plain-English versions. Matched
#: without the classifier, because in a silenced channel a classifier
#: miss leaves no way out at all.
_UNSILENCE = re.compile(
    r"\bun[\s-]?(?:s\w{0,4}n[cs]e\w*|mute\w*|shush\w*|quiet\w*)"
    r"|\b(?:stop|quit) being (?:silen\w*|quiet|muted)"
    r"|\b(?:lift|end|remove|undo|cancel)\w* (?:the |your )?(?:silenc\w*|mute)"
    r"|\b(?:talk|speak|respond|answer|chat|reply)\w*"
    r"(?: in here| here| in this channel)? again\b",
    re.IGNORECASE)
_EVERYWHERE = re.compile(r"\b(everywhere|all (?:the )?channels|every channel)\b",
                         re.IGNORECASE)


def match_unsilence(text: str,
                    mentioned_channels=()) -> Optional[dict]:
    """``unsilence`` args if ``text`` asks to unsilence, else None."""
    if not _UNSILENCE.search(text or ""):
        return None
    if _EVERYWHERE.search(text):
        return {"channel": "all"}
    channels = list(mentioned_channels)
    if len(channels) == 1:
        return {"channel": str(channels[0])}
    return {}


def _target_channel(ctx: CommandContext,
                    channel: Optional[str]) -> Optional[str]:
    """The named channel, or this one when none was named.

    None means a channel WAS named and couldn't be found -- falling back
    to the current channel there is how "silence #art" silenced #general.
    """
    if not channel or _HERE.match(str(channel)):
        return str(ctx.channel.id)
    return _resolve_channel(ctx, channel)


def apply_silence(settings, guild_id, channel_id: str, scope: str) -> str:
    """Do it and say so. ``scope``: "presence" or "all"."""
    where = f"<#{channel_id}>"
    if scope == "presence":
        settings.set_presence_off(guild_id, channel_id, True)
        return (f"Okay, I'll stop chiming in on my own in {where}. I'll "
                "still answer if someone mentions me or replies to me there.")
    settings.set_silenced(guild_id, channel_id, True)
    return (f"Silenced in {where}. I won't respond there at all until "
            "someone tells me to unsilence.")


_ANSWER_PRESENCE = re.compile(
    r"\b(presence|chim\w*|on your own|unprompted|butt\w* in|jump\w* in|"
    r"join\w* in|first|1)\b", re.IGNORECASE)
_ANSWER_ALL = re.compile(
    r"\b(silen\w*|complete\w*|full\w*|entire\w*|everything|total\w*|"
    r"mute|shut up|all|second|2)\b", re.IGNORECASE)


def match_silence_answer(text: str) -> Optional[str]:
    """"presence", "all", or None for a reply to the silence question.

    Both or neither matching is None: the message is handled normally and
    the question stays open rather than guessing.
    """
    presence = bool(_ANSWER_PRESENCE.search(text or ""))
    whole = bool(_ANSWER_ALL.search(text or ""))
    if presence == whole:
        return None
    return "presence" if presence else "all"


@command(
    name="silence",
    description=(f"Silence {NAME} in a channel, or turn off just his "
                 "presence (chiming in on his own) there"),
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("shush", "quiet", "mute"),
    params=(
        Param("channel", "if not this one", kind="channel", required=False),
        Param("scope", "presence or all; only if said",
              choices=("presence", "all"), required=False),
    ),
    # Only the first three reach the classifier (build_vocabulary), so
    # the presence wording must be among them: with "stop chiming in"
    # fourth, "turn off presence in #x" was guessed as a command
    # called "presence" and refused.
    examples=("silence #art", "turn off presence in #general",
              "stop chiming in here", "be quiet in this channel",
              "stop responding here"),
)
async def silence(ctx: CommandContext, channel: Optional[str] = None,
                         scope: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    target = _target_channel(ctx, channel)
    if not target:
        return f"I couldn't find a channel called {channel}."
    if settings.is_silenced(ctx.guild_id, target):
        return (f"I'm already silenced in <#{target}>. Tell me to unsilence "
                "if you want me back.")
    if scope:
        return apply_silence(settings, ctx.guild_id, target, scope)
    # Presence only exists in global mode; outside it the only thing to
    # turn off is answering, so there is nothing to ask.
    if (settings.mode(ctx.guild_id) == "global"
            and not settings.is_presence_off(ctx.guild_id, target)):
        waiting = ctx.extras.get("silence_questions")
        if waiting is not None:
            waiting[(str(ctx.channel.id), str(ctx.invoker.id))] = (
                str(ctx.guild_id), target,
                time.monotonic() + SILENCE_QUESTION_TTL)
            return (f"Do you want me to just stop chiming in on my own in "
                    f"<#{target}> (I'd still answer when mentioned), or go "
                    "completely silent there? Presence or silence?")
    return apply_silence(settings, ctx.guild_id, target, "all")


@command(
    name="unsilence",
    description=("Undo silence, or turn his presence back on "
                 "(works inside a silenced channel)"),
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("unshush", "unmute", "unquiet"),
    params=(
        Param("channel", "if not this one; 'all' for every channel",
              kind="channel", required=False),
    ),
    examples=("unsilence #art", "turn presence back on in #general",
              "you can talk in here again", "unsilence",
              "start responding here again", "unsilence everywhere"),
)
async def unsilence(ctx: CommandContext,
                           channel: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    gid = ctx.guild_id
    off_note = ("" if settings.mode(gid) != "off" else
                " I'm still switched off server-wide, though; set my "
                "respond mode to mention or global to bring me back.")
    if channel and str(channel).strip().lstrip("#").lower() in _ALL_CHANNELS:
        targets = settings.silenced(gid) | settings.presence_off(gid)
        for cid in targets:
            settings.set_silenced(gid, cid, False)
            settings.set_presence_off(gid, cid, False)
        if not targets:
            return "I'm not silenced anywhere." + off_note
        return (f"Unsilenced in {len(targets)} channel"
                f"{'' if len(targets) == 1 else 's'}." + off_note)
    target = _target_channel(ctx, channel)
    if not target:
        return f"I couldn't find a channel called {channel}."
    was_silent = settings.set_silenced(gid, target, False)
    was_presence = settings.set_presence_off(gid, target, False)
    if not (was_silent or was_presence):
        return f"I wasn't silenced in <#{target}>." + off_note
    return f"Unsilenced. I'll answer in <#{target}> again." + off_note


#: How long "which channel is your general chat?" stays answerable.
GENERAL_QUESTION_TTL = 300.0

_HERE = re.compile(r"^\s*(here|this( one| channel)?|this is it|in here)\s*[.!]*\s*$",
                   re.IGNORECASE)


def _resolve_channel(ctx: CommandContext, value: Optional[str]) -> Optional[str]:
    """Turn a channel id or written name into an id, or None."""
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


def _ask_for_general(ctx: CommandContext) -> None:
    """Let the asker's next message in this channel answer the question.

    The classifier sees one message at a time, so a bare "#lounge" sent
    back would never reach a command. ``bot.py`` checks this map before
    dispatch; see :func:`match_general_answer`.
    """
    waiting = ctx.extras.get("general_questions")
    if waiting is not None:
        waiting[(str(ctx.channel.id), str(ctx.invoker.id))] = (
            str(ctx.guild_id), time.monotonic() + GENERAL_QUESTION_TTL)


def match_general_answer(text: str, mentioned: Mapping[str, str],
                         index: Mapping[str, str],
                         here: str) -> Optional[str]:
    """The channel id a reply to "which channel?" names, or None.

    ``mentioned`` is ``{id: name}`` for channels the message links,
    ``index`` is the guild's ``{name: id}``. Only a clear answer counts:
    one linked channel, "here", or a message that is little more than a
    channel name. Anything else falls through to normal handling, so an
    unrelated message is never eaten by the question.
    """
    if len(mentioned) == 1:
        return next(iter(mentioned))
    if mentioned:
        return None
    if _HERE.match(text or ""):
        return here
    whole = (text or "").strip().strip(".!").lstrip("#").lower()
    if whole in index:
        return index[whole]
    words = re.sub(r"[^\w\s'-]", " ", (text or "").lower()).split()
    filler = {"it's", "its", "it", "is", "the", "our", "that", "one",
              "channel", "chat", "general", "main", NAME.lower(), "in", "there"}
    hits = {index[w] for w in words if w in index}
    if len(hits) == 1 and all(w in index or w in filler for w in words):
        return hits.pop()
    return None


@command(
    name="chat_mode",
    description=f"Set how {NAME} decides when to speak server-wide",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    params=(
        Param("mode", "mention, global, or off", choices=("mention", "global", "off")),
        Param("channel", "the general chat, if named", kind="channel",
              required=False),
    ),
    examples=("set your mode to global", "only respond when mentioned"),
)
async def chat_mode(ctx: CommandContext, mode: str,
                         channel: Optional[str] = None) -> str:
    settings = ctx.extras["settings"]
    settings.set_mode(ctx.guild_id, mode)
    if mode != "global":
        return {
            "mention": "Mention-only now — ping me, reply to me, or use an AI channel.",
            "off": "Off. I'll stay quiet server-wide until you switch me back on.",
        }[mode]

    base = "Global mode. I'll chime in when something looks worth answering."
    target = _resolve_channel(ctx, channel)
    if target:
        settings.set_general_channel(ctx.guild_id, target)
        return (f"{base} When I've got something on my mind I'll bring it "
                f"up in <#{target}>.")
    current = settings.general_channel(ctx.guild_id)
    if current:
        return (f"{base} Anything I bring up on my own still goes in "
                f"<#{current}>.")
    _ask_for_general(ctx)
    return (f"{base} Which channel is your general chat? That's where I'll "
            "bring stuff up on my own.")


@command(
    name="general_channel",
    description=f"Set the general chat, where {NAME} posts his own thoughts",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("musings_channel",),
    params=(
        Param("channel", "the general chat channel", kind="channel"),
    ),
    examples=("#lounge is the general channel",
              "post your random thoughts in #general"),
)
async def general_channel(ctx: CommandContext, channel: str) -> str:
    settings = ctx.extras["settings"]
    target = _resolve_channel(ctx, channel)
    if not target:
        return f"I couldn't find a channel called {channel}."
    settings.set_general_channel(ctx.guild_id, target)
    if settings.mode(ctx.guild_id) != "global":
        return (f"Noted, <#{target}> is the general chat. I only bring "
                "things up on my own in global mode, though.")
    return f"Got it, <#{target}> it is."


@command(
    name="chat_status",
    description=f"Show {NAME}'s current configuration for this server",
    risk=Risk.SAFE,
    aliases=("status",),
    examples=("what's your config", "show your settings"),
)
async def chat_status(ctx: CommandContext) -> Embed:
    """Every per-server setting, configured or not (owner, 2026-11-17:
    ranking, banners and the quote channel were missing). A setting
    that's off says "off" or "not set" rather than leaving its field
    out, so an absent line can never be read as "this server has none".
    The adapter drops fields past ``EMBED_MAX_FIELDS``, and a test
    holds a fully configured server under it."""
    settings = ctx.extras["settings"]
    gid = str(ctx.guild_id) if ctx.guild_id else None
    embed = Embed(title=f"{NAME} — configuration",
                  description=(ctx.guild.name if ctx.guild else "Direct messages"))
    if not gid:
        embed.add_field("Mode", settings.mode(None), inline=True)
        return embed
    for name, value, inline in status_fields(ctx, settings, gid):
        embed.add_field(name, value, inline=inline)
    return embed


def _chan(cid: object, unset: str = "not set") -> str:
    return f"<#{cid}>" if cid else unset


def _channel_list(ids: list, cap: int = 10) -> str:
    if not ids:
        return "none"
    more = len(ids) - cap
    return "\n".join(f"<#{c}>" for c in ids[:cap]) + (
        f"\n…and {more} more" if more > 0 else "")


def _card_feature(label: str, cfg: dict) -> str:
    if not cfg.get("enabled"):
        return f"{label}: off"
    bits = [f"{label}: on in {_chan(cfg.get('channel_id'), 'no channel set')}"]
    bits.append("card" if cfg.get("card") else "no card")
    if cfg.get("card") and cfg.get("background"):
        bits.append("custom background")
    if cfg.get("message"):
        bits.append("custom message")
    return " · ".join(bits)


def status_fields(ctx: CommandContext, settings, gid: str
                  ) -> list[tuple[str, str, bool]]:
    """``(name, value, inline)`` for every server setting, in card order."""
    out: list[tuple[str, str, bool]] = []
    add = lambda n, v, i=False: out.append((n, v or "​", i))  # noqa: E731
    extras = ctx.extras

    # -- how he talks here ------------------------------------------------
    add("Mode", settings.mode(gid), True)
    add("General chat", _chan(settings.general_channel(gid)), True)
    here = ("silenced" if settings.is_silenced(gid, ctx.channel.id)
            else ("always-on" if settings.is_ai_channel(gid, ctx.channel.id)
                  else "mention-only"))
    if here != "silenced" and settings.is_presence_off(gid, ctx.channel.id):
        here += ", no chiming in"
    add("This channel", here, True)

    add("Verified", _verified_line(settings, extras, gid), True)
    features = extras.get("features")
    if features is not None:
        on = features.enabled(gid)
        premium = " · ".join(f"{f}: {'on' if f in on else 'off'}"
                             for f in ("voice", "streaming"))
    else:
        premium = "unknown (allowlist not loaded)"
    add("Voice & streaming", premium, True)

    add("Always-on channels", _channel_list(sorted(settings.ai_channels(gid))))
    add("Silenced", _channel_list(sorted(settings.silenced(gid))))
    add("No chiming in", _channel_list(sorted(settings.presence_off(gid))))

    # -- ranking ------------------------------------------------------------
    if settings.ranking_enabled(gid):
        rank = ("on · level-ups in "
                + _chan(settings.ranking_channel(gid),
                        "the channel they leveled up in"))
    else:
        rank = "off"
    from ..core.ranking import RankRoles
    roles = extras.get("rank_roles") or RankRoles(settings)
    tiers = roles.tiers(gid)
    rank += (f"\nRank roles: {'custom' if roles.custom(gid) else 'default'}"
             f", {len(tiers)} tier{'s' if len(tiers) != 1 else ''}")
    add("Ranking", rank)

    # -- members coming and going -------------------------------------------
    greet = settings.greeting(gid)
    greeting = ("AI-written welcomes: on" + (" (with extra info)"
                                             if greet.get("custom_info") else "")
                if greet.get("enabled") else "AI-written welcomes: off")
    add("Welcome & farewell", "\n".join((
        _card_feature("Welcome", settings.welcome(gid)),
        _card_feature("Farewell", settings.farewell(gid)),
        greeting)))

    from ..core.audit import CATEGORIES, normalise_categories
    audit = settings.audit(gid)
    if audit.get("enabled"):
        cats = normalise_categories(audit.get("categories"))
        add("Audit log", f"on in {_chan(audit.get('channel_id'), 'no channel set')}"
                         f" · {sum(cats.values())} of {len(CATEGORIES)} "
                         "event types")
    else:
        add("Audit log", "off")
    add("Spam honeypot", _chan(settings.honeypot(gid), "off"), True)
    add("Verse finder", "on" if settings.versefinder_enabled(gid) else "off",
        True)

    # -- scheduled posts ------------------------------------------------------
    quote = settings.daily_quote(gid)
    add("Daily quote",
        f"on in {_chan(quote.get('channel_id'), 'no channel set')} at "
        f"{int(quote.get('hour', 8)):02d}:{int(quote.get('minute', 0)):02d} UTC"
        if quote.get("enabled") else "off", True)
    animals = settings.hourly_animals(gid)
    add("Hourly animals",
        f"on in {_chan(animals.get('channel_id'), 'no channel set')}"
        f" ({animals.get('animal') or 'random'})"
        if animals.get("enabled") else "off", True)
    add("Full moon", _chan(settings.fullmoon(gid), "off"), True)
    alerts = settings.weather_alerts(gid)
    add("Weather alerts", "\n".join(
        f"{a.get('location', '?')} at {int(a.get('hour', 0)):02d}:"
        f"{int(a.get('minute', 0)):02d} {a.get('tz', 'UTC')} in "
        f"{_chan(a.get('channel_id'), 'no channel')}" for a in alerts)
        if alerts else "none")

    from ..core.banner import BannerSettings, describe_interval
    banners = BannerSettings(settings)
    bcfg = banners.get(gid)
    if bcfg.get("channel_id"):
        banner = (f"from <#{bcfg['channel_id']}>, "
                  f"{describe_interval(banners.interval(gid))}"
                  + ("" if bcfg.get("enabled") else " (paused)"))
        if bcfg.get("last_error"):
            banner += f"\n⚠️ {str(bcfg['last_error'])[:200]}"
    else:
        banner = "not set up"
    add("Banner rotation", banner)

    add("Events calendar", _events_line(extras, settings, gid))

    # -- roles ----------------------------------------------------------------
    from ..core.reaction_roles import ReactionRoleStore
    rr = extras.get("reaction_roles") or ReactionRoleStore(settings.store)
    msgs = rr.messages(gid)
    if msgs:
        pairs = sum(len(e.get("roles") or {}) for e in msgs.values())
        dm = len(rr.dm_roles(gid))
        chans = sorted({str(e.get("channel_id")) for e in msgs.values()
                        if e.get("channel_id")})
        add("Reaction roles",
            f"{len(msgs)} message{'s' if len(msgs) != 1 else ''}, {pairs} "
            f"role{'s' if pairs != 1 else ''} ({dm} opt-in DM) in "
            + ", ".join(f"<#{c}>" for c in chans[:5])
            + (f" and {len(chans) - 5} more" if len(chans) > 5 else ""))
    else:
        add("Reaction roles", "none")

    # -- TV and videos ----------------------------------------------------
    rules = extras.get("tv_rules")
    if rules is None:
        from ..core.tv_rules import TVRules
        rules = TVRules(settings)
    if rules.enabled(gid):
        allow, block = (rules.allowed_categories(gid),
                        rules.blocked_categories(gid))
        cats = (f"only {', '.join(allow)}" if allow else
                f"no {', '.join(block)}" if block else "all categories")
        limit = rules.max_minutes(gid)
        tv = (f"on · {cats} · "
              f"{'limit ' + str(limit) + ' min' if limit else 'no time limit'}"
              f" · {rules.quality(gid).name} · "
              f"{len(rules.favourites(gid))} favourite(s)")
    else:
        tv = "off"
    from ..core.video_player import VideoPlayer
    autoplay = settings.store.get(gid, VideoPlayer.AUTOPLAY, True) is not False
    tv += f"\nVideo library autoplay: {'on' if autoplay else 'off'}"
    if features is not None and "streaming" not in features.enabled(gid):
        tv += "\n(streaming isn't enabled for this server)"
    add("Live TV & videos", tv)
    return out


def _events_line(extras, settings, gid: str) -> str:
    from ..core.events import EventStore

    store = extras.get("events") or EventStore(settings)
    cfg = store.get(gid)
    if not cfg.get("channel_id"):
        return "not set up"
    upcoming = len(store.upcoming(gid))
    subs = len(cfg.get("subscribers") or {})
    return (f"<#{cfg['channel_id']}> · {upcoming} upcoming · {subs} "
            f"subscribed · timezone {cfg.get('tz') or 'not set'}")


def _verified_line(settings, extras, gid: str) -> str:
    """The owner's ``verify``."""
    return "yes" if settings.is_verified(gid) else "no"
