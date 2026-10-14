#!/usr/bin/env python3
"""Entry point. Configure in .env (see .env.example), then:

Boot order matters:
  1. validate secrets, and refuse to start without them
  2. probe the SDK bindings, and refuse to start on drift
  3. build adapter / conversation / dispatcher / executor
  4. register commands
  5. connect

Steps 1-2 exist because both failure modes are *silent*: a missing token or
a renamed SDK attribute produces a bot that connects, logs "ready", and then
answers nothing.

    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from time import monotonic as _monotonic
import re
import sys
from typing import Optional

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger("corbin")

from corbin.ai.classifier import ModelIntentClassifier          # noqa: E402
import corbin.ai.conversation as conversation_mod                # noqa: E402
from corbin.ai.conversation import (Conversation,               # noqa: E402
                                    extract_code_files,
                                    resolve_mentions, split_message)
from corbin.ai.extraction import MemoryExtractor                # noqa: E402
from corbin.ai.greeting import Greeter                          # noqa: E402
from corbin.ai.llm import LLM                                   # noqa: E402
from corbin.ai.ollama import OllamaProvider                     # noqa: E402
from corbin.ai.presence import PresenceEngine                   # noqa: E402
import corbin.ai.musings as musings_mod                        # noqa: E402
from corbin.ai.musings import Musings, MusingPlanner            # noqa: E402
from corbin.core.context import Response                        # noqa: E402
from corbin.core import vision                                 # noqa: E402
from corbin.core import documents                              # noqa: E402
from corbin.core.media import (media_labels,                    # noqa: E402
                               strip_emoji_ids)
from corbin.ai.command_help import (CommandHelp,                # noqa: E402
                                    is_command_question)
from corbin.core.dispatch import Dispatcher                     # noqa: E402
from corbin.core.registry import registry                       # noqa: E402
from corbin.core.audit import (CATEGORIES, COLOURS,             # noqa: E402
                               CachedMessage, MessageCache,
                               truncate)
from corbin.core.memory import MemoryStore                      # noqa: E402
from corbin.core.moon import (MoonCalculator, find_ephemeris,   # noqa: E402
                              moon_name)
from corbin.core.welcome import (DEFAULT_FAREWELL,              # noqa: E402
                                 DEFAULT_FAREWELL_CARD,
                                 DEFAULT_WELCOME,
                                 DEFAULT_WELCOME_CARD,
                                 render_template)
from corbin.core import verses as verse_lookup               # noqa: E402
from corbin.core.digest import DigestStore                      # noqa: E402
from corbin.core.audio import Extractor                         # noqa: E402
from corbin.core.spotify import Spotify                         # noqa: E402
from corbin.core.status import (StatusUpdater,                  # noqa: E402
                                install_identify_presence)
from corbin.core.health import (GatewayPing, api_latency,       # noqa: E402
                                human_duration, human_latency,
                                local_model_state)
from corbin.core.bans import BanStore, ChatbanStore, humanise   # noqa: E402
from corbin.core.honeypot import (BAN_DELETE_SECONDS,           # noqa: E402
                                  HoneypotGuard, is_exempt)
from corbin.core.player import PlayerManager                    # noqa: E402
from corbin.core.reminders import ReminderStore                 # noqa: E402
from corbin.core.voice import VoiceManager                      # noqa: E402
from corbin.core.scheduled import (Scheduler, animal_image_url, # noqa: E402
                                   fetch_quote, is_due,
                                   pick_animal)
from corbin.core.warnings import WarningStore                   # noqa: E402
from corbin.core.ranking import (ROLE_TIERS, RankingStore,      # noqa: E402
                                 XPAccumulator, role_for_level)
from corbin.core.settings import ChatSettings                   # noqa: E402
from corbin.core.risk import Decision                           # noqa: E402
from corbin.platforms.base import FLUXER_CAPABILITIES           # noqa: E402
from corbin.platforms.fluxer.adapter import (FluxerAdapter,     # noqa: E402
                                             probe, to_member, to_message)
from corbin.commands._notify import notify_target               # noqa: E402
from corbin.commands.config_cmds import match_general_answer     # noqa: E402
from corbin.runtime import Executor                             # noqa: E402

import corbin.commands.audit_cmds   # noqa: E402,F401 (registers commands)
import corbin.commands.config_cmds  # noqa: E402,F401
import corbin.commands.digest_cmds  # noqa: E402,F401
import corbin.commands.expression_cmds  # noqa: E402,F401
import corbin.commands.general     # noqa: E402,F401
import corbin.commands.info        # noqa: E402,F401
import corbin.commands.memory_cmds  # noqa: E402,F401
import corbin.commands.moon_cmds    # noqa: E402,F401
import corbin.commands.music_cmds   # noqa: E402,F401
import corbin.commands.radio_cmds    # noqa: E402,F401
import corbin.commands.reminder_cmds # noqa: E402,F401
import corbin.commands.ranking_cmds  # noqa: E402,F401
import corbin.commands.scheduled_cmds  # noqa: E402,F401
import corbin.commands.spamblock_cmds  # noqa: E402,F401
import corbin.commands.util_cmds    # noqa: E402,F401
import corbin.commands.verse_cmds    # noqa: E402,F401
import corbin.commands.warn_cmds     # noqa: E402,F401
import corbin.commands.welcome_cmds  # noqa: E402,F401
import corbin.commands.moderation  # noqa: E402,F401


def main() -> int:
    boot_started = _monotonic()
    problems = config.validate()
    if problems:
        logger.error("configuration is incomplete:")
        for problem in problems:
            logger.error("  - %s", problem)
        return 2
    logger.info("configuration:\n%s", config.summary())

    report = probe()
    if report["missing"]:
        logger.error("fluxer.py binding check FAILED: %s",
                     ", ".join(report["missing"]))
        logger.error("refusing to start -- a silent binding mismatch looks "
                     "like a healthy bot that ignores everything")
        return 3
    for problem in report.get("warn") or ():
        logger.warning("fluxer.py binding drift: %s", problem)
    logger.info("fluxer.py bindings OK (%d verified)", len(report["ok"]))

    import fluxer

    # Intents.default() EXCLUDES the privileged flags. The bot reads every
    # message and needs member data for risk scoring, so with default() he
    # would receive blank content and never resolve a member.
    intents = (fluxer.Intents.default()
               | fluxer.Intents.MESSAGE_CONTENT
               | fluxer.Intents.GUILD_MEMBERS)
    if config.PRESENCE_INTENT:
        # Opt-in: if Fluxer treats this as privileged, an unauthorised
        # intent makes IDENTIFY fail and nothing starts.
        intents |= fluxer.Intents.GUILD_PRESENCES
        logger.info("requesting GUILD_PRESENCES (PRESENCE_INTENT=1); "
                    "if the bot does not connect, unset it")

    bot_kwargs = {"command_prefix": "!", "intents": intents}
    if config.FLUXER_API_URL:
        bot_kwargs["api_url"] = config.FLUXER_API_URL
    bot = fluxer.Bot(**bot_kwargs)

    adapter = FluxerAdapter(bot, embed_cls=fluxer.Embed, file_cls=fluxer.File,
                            data_root=config.DATA_ROOT,
                            capabilities=FLUXER_CAPABILITIES)
    def _provider(model: str) -> OllamaProvider:
        return OllamaProvider(
            config.LLM_URL, model,
            persona_baked_in=config.LLM_PERSONA_IN_MODEL,
            vision=config.LLM_VISION,
            temperature=config.LLM_TEMPERATURE,
            presence_penalty=config.LLM_PRESENCE_PENALTY,
            probe_timeout=config.LLM_PROBE_TIMEOUT,
        )

    main_model = _provider(config.LLM_MODEL)
    llm = LLM(main_model, (_provider(config.LLM_FAST_MODEL)
                           if config.LLM_FAST_MODEL != config.LLM_MODEL
                           else None))
    memory = MemoryStore(config.DATA_ROOT)
    conversation = Conversation(llm, soul_path=config.PERSONA_PATH,
                                additions_path=config.PERSONA_ADDITIONS_PATH,
                                memory=memory,
                                extractor=MemoryExtractor(llm))
    owner_id = config.BOT_OWNER_ID or None
    dispatcher = Dispatcher(
        registry, ModelIntentClassifier(llm.classifier_call()),
        resolver=adapter, bot_owner_id=owner_id,
    )
    executor = Executor(adapter, dispatcher)
    # Same owner id as the dispatcher, so the help never offers a command
    # the risk gates would then refuse.
    command_help = CommandHelp(registry, bot_owner_id=owner_id)
    greeter = Greeter(llm)

    settings = ChatSettings(config.SETTINGS_PATH)
    ranking = RankingStore(config.DATA_ROOT)
    xp = XPAccumulator(ranking)
    warnings_store = WarningStore(config.DATA_ROOT)
    moon_calc = MoonCalculator(
        config.EPHEMERIS_PATH
        or find_ephemeris(config.DATA_ROOT, config.HERE))
    reminders_store = ReminderStore(config.DATA_ROOT)
    bans = BanStore(config.DATA_ROOT)
    chatbans = ChatbanStore(config.DATA_ROOT)
    if len(bans) or len(chatbans):
        logger.info("loaded %d timed ban(s), %d chatban(s)",
                    len(bans), len(chatbans))
    digest_store = DigestStore(config.DATA_ROOT)

    voice = None
    try:
        import fluxer.voice  # noqa: F401  (the [voice] extra)

        voice = VoiceManager(bot)
        logger.info("voice support ready")
    except ImportError:
        logger.info("voice extra not installed; radio and playback are off "
                    "(pip install 'fluxer.py[voice]')")
    spotify = Spotify(config.SPOTIPY_CLIENT_ID, config.SPOTIPY_CLIENT_SECRET)
    if not spotify.configured:
        logger.info("Spotify links disabled (no SPOTIPY_CLIENT_ID/SECRET)")
    extractor = Extractor(spotify=spotify)
    player_mgr = None
    if voice is not None:
        async def _announce_track(guild_id: str, track) -> None:
            """Post the now-playing card in the voice channel's own chat."""
            player = player_mgr.get(guild_id) if player_mgr else None
            where = (player.announce_to if player and player.announce_to
                     else None)
            if not where:
                session = voice.get(guild_id)
                where = session.channel_id if session else None
            if not where:
                return
            from corbin.commands.music_cmds import track_embed

            try:
                await adapter.send(where, Response(embed=track_embed(track)))
            except Exception:
                logger.debug("track announce failed", exc_info=True)
            # Don't make the status wait out the next tick: a song that
            # starts now should show now.
            try:
                await status.push()
            except Exception:
                logger.debug("status refresh failed", exc_info=True)

        player_mgr = PlayerManager(voice, extractor, announce=_announce_track)

    #: user id -> voice channel id, so "play the radio" can mean "here".
    voice_states: dict[str, str] = {}

    # GUILD_CREATE carries a voice_states snapshot that fluxer.py drops on
    # the floor, and there is no REST endpoint for it. Without seeding from
    # it, anyone already sitting in a channel before the bot started is
    # invisible and Corbin asks "which voice channel?" pointlessly.
    _inner_dispatch = bot._dispatch

    async def _dispatch_with_voice(event_name, data):
        # Corbin's own status does not render on Fluxer even though the
        # gateway accepts the payload. fluxer.py models no presence at
        # all, so there is no reference for the shape Fluxer actually
        # uses -- log the first few inbound ones and read it off a real
        # user who has a status set.
        if event_name == "PRESENCE_UPDATE" and _presence_samples[0] < 3:
            _presence_samples[0] += 1
            logger.info("PRESENCE_UPDATE sample %d: %r",
                        _presence_samples[0], data)

        if event_name == "GUILD_CREATE" and isinstance(data, dict):
            seeded = 0
            for state in (data.get("voice_states") or []):
                uid = state.get("user_id") or \
                    (state.get("member") or {}).get("id")
                cid = state.get("channel_id")
                if uid and cid:
                    voice_states[str(uid)] = str(cid)
                    seeded += 1
            if seeded:
                logger.info("seeded %d voice state(s) from GUILD_CREATE",
                            seeded)
        return await _inner_dispatch(event_name, data)

    bot._dispatch = _dispatch_with_voice
    gill = None
    try:
        verse_lookup.load()
        gill = verse_lookup.GillCommentary(config.GILL_DB_PATH)
        logger.info("verse lookup ready (Gill commentary: %s)",
                    "yes" if gill.available else "no, NET fallback")
    except Exception:
        logger.warning("verse lookup unavailable", exc_info=True)
    #: (channel, user) -> (guild, deadline): "which channel is your general
    #: chat?" waiting on an answer. See _answer_general_question.
    general_questions: dict = {}
    executor.extras = {"settings": settings, "ranking": ranking,
                       "memory": memory, "warnings": warnings_store,
                       "bans": bans, "chatbans": chatbans,
                       "moon": moon_calc, "reminders": reminders_store,
                       "gill": gill, "digest": digest_store,
                       "voice": voice, "voice_states": voice_states,
                       "player": player_mgr, "extractor": extractor,
                       "log_channel": config.LOG_CHANNEL_ID,
                       "general_questions": general_questions}

    #: Bounded counter; see the PRESENCE_UPDATE hook in _dispatch.
    _presence_samples = [0]
    status = StatusUpdater(bot)
    # Wired now rather than in on_ready: the IDENTIFY presence below is
    # built before on_ready runs, and without this it would announce
    # "0 servers" for the first few seconds.
    status.guild_count = lambda: len(getattr(bot, "guilds", ()) or ())
    gateway_ping = GatewayPing()
    # Installed here, not in on_ready: IDENTIFY has already gone out by
    # the time on_ready fires, so patching it there would only take
    # effect on a reconnect. fluxer.py builds the Gateway in Bot(), so
    # this is early enough for the first handshake.
    if not install_identify_presence(bot, status.describe):
        logger.debug("could not attach presence to IDENTIFY")

    msg_cache = MessageCache()
    honeypot_guard = HoneypotGuard()
    scheduler = Scheduler()

    async def _daily_quote_job() -> None:
        """One Zenquotes fetch per tick, fanned out to every due guild.

        Fetching per guild hammers the API from one IP and gets us rate
        limited -- this is the shared-fetch pattern from the Discord build.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        due = []
        for gid in settings.guilds_with("daily_quote"):
            cfg = settings.daily_quote(gid)
            if is_due(now, int(cfg.get("hour", 8)),
                      int(cfg.get("minute", 0)),
                      settings.scheduled_marker(gid, "daily_quote")):
                due.append((gid, cfg))
        if not due:
            return

        quote = await fetch_quote()
        if quote is None:
            # Skip the day rather than posting a placeholder; it retries
            # tomorrow.
            return
        from corbin.core.context import Embed as _E

        embed = _E(description=quote.render(), colour=0x9B59B6)
        for gid, cfg in due:
            try:
                await adapter.send(cfg["channel_id"], Response(embed=embed))
                settings.mark_scheduled_sent(gid, "daily_quote",
                                             now.date().isoformat())
            except Exception:
                logger.warning("daily quote failed for %s", gid,
                               exc_info=True)

    async def _hourly_animal_job() -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        marker = now.strftime("%Y-%m-%dT%H")
        from corbin.core.context import Embed as _E

        for gid in settings.guilds_with("hourly_animals"):
            if settings.scheduled_marker(gid, "hourly_animals") == marker:
                continue
            cfg = settings.hourly_animals(gid)
            key, label = pick_animal(cfg.get("animal"))
            # Fetch the bytes and upload them, rather than pointing an
            # embed at the URL. tinyfox serves a different animal on
            # every request, but the URL never changes -- so the CDN
            # caches the first one and every hour shows the same wolf.
            picture = await _fetch_bytes(animal_image_url(key),
                                     timeout=20.0)
            if picture is None:
                # Not marked sent, so the next tick tries again rather
                # than skipping the hour on one bad fetch.
                logger.info("tinyfox gave nothing for %s; will retry", key)
                continue
            try:
                await adapter.send(cfg["channel_id"], Response(
                    embed=_E(title=f"Here is your Hourly {label}.",
                             footer="Powered by tinyfox.dev",
                             colour=0x5CB85C),
                    files=((f"{key}.png", picture),)))
                settings.mark_scheduled_sent(gid, "hourly_animals", marker)
            except Exception:
                logger.warning("hourly animal failed for %s", gid,
                               exc_info=True)

    async def _weather_alerts_job() -> None:
        """Post each daily weather alert at its own local time.

        This job did not exist: ``weatheralerts`` saved alerts that were
        never delivered. One OpenWeather call per distinct place and unit
        per tick, fanned out, the same shared-fetch rule as the quotes.
        """
        from datetime import datetime, timezone

        from corbin.commands.info import weather_embed
        from corbin.core import weather_alerts as wa

        now = datetime.now(timezone.utc)
        fetched: dict = {}
        for gid in settings.guilds_with_weather():
            alerts = [wa.normalise(a) for a in settings.weather_alerts(gid)]
            changed = False
            for alert in alerts:
                today = wa.due(alert, now)
                if today is None:
                    continue
                key = (alert["location"].lower(), alert["units"])
                if key not in fetched:
                    fetched[key] = await weather_embed(alert["location"],
                                                       alert["units"])
                card = fetched[key]
                if isinstance(card, str):
                    # Not marked sent: a transient API failure retries on
                    # the next tick, within the lateness window.
                    logger.warning("weather alert %s (%s) in guild %s: %s",
                                   alert["id"], alert["location"], gid, card)
                    continue
                try:
                    await adapter.send(alert["channel_id"], Response(
                        content=f"Your daily weather for "
                                f"**{alert['location']}**:",
                        embed=card))
                except Exception:
                    logger.warning("weather alert %s failed for guild %s",
                                   alert["id"], gid, exc_info=True)
                    continue
                alert["last_sent"] = today
                changed = True
                logger.info("weather alert %s sent for %s in guild %s",
                            alert["id"], alert["location"], gid)
            if changed:
                settings.set_weather_alerts(gid, alerts)

    async def _digest_job() -> None:
        if not digest_store.is_due() or not config.LOG_CHANNEL_ID:
            return
        from corbin.core.digest import digest_now
        from corbin.commands.digest_cmds import render

        marker = digest_now().date().isoformat()
        posted = digest_store.snapshot_and_reset(marker)
        try:
            await adapter.send(config.LOG_CHANNEL_ID, Response(
                embed=render(posted, "overview",
                             title=f"{config.BOT_NAME} — daily digest "
                                   f"for {marker}")))
            if posted.users:
                await adapter.send(config.LOG_CHANNEL_ID, Response(
                    embed=render(posted, "users", title="Most active people")))
        except Exception:
            logger.warning("digest post failed", exc_info=True)

    async def _expire_bans_job() -> None:
        for ban in bans.due():
            try:
                await adapter.unban(ban.guild_id, ban.user_id)
                logger.info("timed ban expired for %s in %s", ban.user_id,
                            ban.guild_id)
                if ban.channel_id:
                    await adapter.send(ban.channel_id, Response(
                        content=f"<@{ban.user_id}>'s ban has expired."))
            except Exception:
                logger.warning("could not lift ban on %s", ban.user_id,
                               exc_info=True)
            finally:
                # Drop it either way: retrying a failed unban every minute
                # forever is worse than logging it once.
                bans.remove(ban.guild_id, ban.user_id)

    async def _full_moon_job() -> None:
        """Announce a full moon once, on the day it happens."""
        if not moon_calc.available:
            return
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc)
        marker = today.date().isoformat()
        if not moon_calc.is_full_today(today):
            return
        from corbin.core.context import Embed as _E

        for gid in settings.guilds_with_fullmoon():
            if settings.scheduled_marker(gid, "fullmoon") == marker:
                continue
            channel = settings.fullmoon(gid)
            if not channel:
                continue
            embed = _E(
                title=f"\U0001F315  The {moon_name(today)} is full tonight",
                colour=0x2C3E50,
                description="The moon is full. Make of that what you will.")
            try:
                await adapter.send(channel, Response(embed=embed))
                settings.mark_scheduled_sent(gid, "fullmoon", marker)
            except Exception:
                logger.warning("full moon announcement failed for %s", gid,
                               exc_info=True)

    async def _reminders_job() -> None:
        for item in reminders_store.pop_due():
            body = f"<@{item.user_id}> you asked me to remind you: {item.text}"
            try:
                await adapter.send(item.channel_id, Response(content=body))
            except Exception:
                # The channel may be gone; fall back to a DM rather than
                # dropping a reminder the user is relying on.
                try:
                    await adapter.send_dm(item.user_id,
                                          Response(content=body))
                except Exception:
                    logger.warning("could not deliver reminder %s", item.id)

    scheduler.add("reminders", _reminders_job)
    scheduler.add("full_moon", _full_moon_job)
    scheduler.add("expire_bans", _expire_bans_job)
    scheduler.add("digest", _digest_job)
    scheduler.add("daily_quote", _daily_quote_job)
    scheduler.add("hourly_animals", _hourly_animal_job)
    scheduler.add("weather_alerts", _weather_alerts_job)

    async def _audit(guild_id: Optional[str], category: str, title: str,
                     description: str = "", fields: tuple = ()) -> None:
        """Post one log entry, if the guild has that category enabled."""
        if not guild_id or not settings.audit_enabled(guild_id, category):
            return
        channel = settings.audit_channel(guild_id)
        if not channel:
            return
        from corbin.core.context import Embed as _E

        embed = _E(title=title, description=description or None,
                   colour=COLOURS.get(category))
        for name, value, inline in fields:
            if value:
                embed.add_field(name, truncate(str(value)), inline)
        try:
            await adapter.send(channel, Response(embed=embed))
        except Exception:
            logger.debug("audit post failed for %s", category, exc_info=True)

    def _presence_allowed(guild_id: str, channel_id: str) -> bool:
        """Organic chime-in is opt-in: it needs mode=global for the guild."""
        if settings.is_silenced(guild_id, channel_id):
            return False
        return settings.mode(guild_id) == "global"

    async def _presence_send(channel_id: str, text: str,
                             target: Optional[dict]) -> None:
        conversation.record(channel_id, conversation.bot_name, text,
                            is_bot=True)
        for chunk in split_message(text):
            sent_id = await adapter.send(channel_id, Response(content=chunk))
            # Presence lines are Corbin's too -- a reply to one is a
            # reply to him, and they are the messages most likely to
            # get replied to since nobody asked for them.
            _remember_own_message(sent_id, channel_id, chunk)

    presence = PresenceEngine(llm, conversation, send=_presence_send,
                              is_enabled=_presence_allowed)

    if config.MUSINGS_ENABLED:
        async def _guild_name(gid: str) -> str:
            info = await adapter.guild_info(gid)
            return info.name if info and info.name else ""

        presence.musings = Musings(
            llm, conversation,
            MusingPlanner(os.path.join(config.DATA_ROOT,
                                       musings_mod.STATE_FILE)),
            memory=memory, guild_name=_guild_name,
            general_channel=settings.general_channel,
            always_on=settings.is_ai_channel)

    greeter.soul = conversation.soul

    if not conversation.soul:
        logger.warning("the persona file is empty — %s will sound generic",
                       config.BOT_NAME)

    @bot.event
    async def on_ready() -> None:  # type: ignore[misc]
        # First, before anything sends: fluxer.py builds its HTTP client in
        # start(), so this is the earliest it exists. Idempotent across
        # reconnects. See http_guard for the two hangs it closes.
        from corbin.platforms.fluxer.http_guard import install_http_guards

        install_http_guards(bot)
        me = getattr(bot, "user", None)
        executor.extras["bot_user_id"] = str(getattr(me, "id", "") or "")
        logger.info("connected as %s (%s) | %d commands | %d chars of persona",
                    getattr(me, "username", "?"), getattr(me, "id", "?"),
                    len(registry), len(conversation.soul))
        # Resolve the bot's own emoji so its :shortcode: renders rather
        # than posting as literal text.
        await adapter.set_emoji(
            config.BOT_EMOJI_ID,
            [str(g.id) for g in (getattr(bot, "guilds", None) or ())])
        if not gateway_ping.install(bot):
            logger.info("gateway heartbeat timing unavailable; "
                        "bot latency will read as unknown")
        await _post_boot_diagnostics(me)
        presence.start()
        if player_mgr is not None:
            ready = await extractor.probe_pot(force=True)
            logger.info("PO token provider: %s",
                        "up" if ready else "down (YouTube may refuse)")
            # Log the cookiefile too. This server's IP gets
            # LOGIN_REQUIRED from every player client without cookies, so
            # a missing file is a guaranteed bot check that no PO token
            # can satisfy -- and "cookies.txt" resolves against the
            # working directory, which moved when this bot got its own.
            from pathlib import Path
            from corbin.core.audio import find_cookiefile
            cookiefile = find_cookiefile()
            if cookiefile:
                logger.info("YouTube cookies: %s", cookiefile)
            else:
                logger.info(
                    "YouTube cookies: none found (cwd=%s, home=%s). Fine on "
                    "many hosts; if playback fails YouTube's bot check, "
                    "export cookies and set YTDL_COOKIEFILE.",
                    Path.cwd(), Path.home())
        scheduler.start()

        # Profile status: "Listening to <track>" / "Watching N servers".
        from corbin.commands.radio_cmds import fetch_now_playing

        status.players = player_mgr
        status.voice = voice
        status.radio_label = fetch_now_playing
        status.guild_count = lambda: len(getattr(bot, "guilds", ()) or ())
        status.start()
        await status.push(force=True)

    async def _post_boot_diagnostics(me) -> None:
        """Announce in the log channel that the bot is back, with numbers.

        Four things, because they are what actually differ between a
        healthy start and a sick one: how long boot took, the two
        latencies (gateway and REST fail independently), and whether the
        model answered. Everything else is in the log file.
        """
        if not config.LOG_CHANNEL_ID:
            return
        from corbin.core.context import Embed

        took = _monotonic() - boot_started
        # The first heartbeat is scheduled at a random offset into the
        # interval, so at boot there is usually no reading yet. Take one
        # REST measurement, which is available immediately.
        api = await api_latency(adapter, config.LOG_CHANNEL_ID)
        model_name, model_up = await local_model_state(llm)
        model_name = model_name or config.LLM_MODEL

        embed = Embed(title=f"{config.BOT_NAME} is back", colour=0x2C3E50)
        embed.add_field("Startup", human_duration(took), inline=True)
        embed.add_field("Bot latency", human_latency(gateway_ping.latency),
                        inline=True)
        embed.add_field("API latency", human_latency(api), inline=True)
        embed.add_field(
            "Model",
            f"{model_name} — {'up' if model_up else 'NOT RESPONDING'}",
            inline=True)

        try:
            await adapter.send(config.LOG_CHANNEL_ID, Response(embed=embed))
        except Exception:
            logger.warning("could not post the startup notice to %s",
                           config.LOG_CHANNEL_ID, exc_info=True)

    @bot.event
    async def on_message(raw) -> None:  # type: ignore[misc]
        try:
            await _handle(raw)
        except Exception:
            # One bad message must never kill the gateway listener.
            logger.exception("on_message failed")

    async def _readable_content(raw, text: str):
        """``(content, users, channels)``: mentions and emoji ids resolved.

        ``users`` is ``{user_id: name}`` and ``channels`` is
        ``{channel_id: name}`` for what this message mentions.

        The one conversion every stored copy of a message goes through --
        the prompt, the message cache, and the edit log's comparison. The
        edit log once compared this against the RAW new content, so a
        message containing a mention "changed" from @Corbin to <@id> the
        moment its link preview loaded, and was logged as an edit nobody
        made.
        """
        name_map = {str(u.id): (getattr(u, "display_name", None)
                                or getattr(u, "username", "someone"))
                    for u in (getattr(raw, "mentions", None) or [])}
        # Resolve channel mentions too. Without this every <#id> became the
        # literal placeholder "#a channel", which the classifier then
        # extracted as a real channel name.
        mentioned_channels = re.findall(r"<#(\d+)>", text or "")
        chan_map = (await adapter.channel_names(mentioned_channels)
                    if mentioned_channels else {})
        content = resolve_mentions(text, users=name_map, channels=chan_map)
        # ``<:hug:123>`` -> ``:hug:``. The id is noise he cannot use;
        # the name is the only part that means anything. Same rule as
        # mentions: no raw ids in model context.
        return strip_emoji_ids(content), name_map, chan_map

    def _is_me(user_id) -> bool:
        me = str(getattr(getattr(bot, "user", None), "id", "") or "")
        return bool(me) and str(user_id) == me

    async def _handle(raw) -> None:
        # Any human traffic counts as Corbin being awake; the fallback
        # status shows idle once the server has been quiet for a while.
        status.last_active = _monotonic()
        message = to_message(raw)
        me_id = str(getattr(getattr(bot, "user", None), "id", "") or "")

        if message.author.id == me_id:
            # Cache his own echo before ignoring it. Progress notes and
            # post-and-delete replies never went through
            # _remember_own_message, so deleting one logged "Message
            # deleted ... predates my cache" every time.
            _remember_own_message(message.id, channel_id=message.channel.id,
                                  content=message.content or "")
            return
        if message.author.is_bot:
            return

        # Resolve mentions before ANYTHING is stored: a raw <@id> in the
        # prompt causes attribution hallucinations in multi-user channels.
        content, name_map, chan_map = await _readable_content(
            raw, message.content)
        # display name -> id, for the users mentioned in THIS message.
        # resolve_mentions turns <@id> into @Name for the prompt, so by
        # the time a command argument is extracted the id is gone. This
        # keeps it, so "@Robin" resolves to the person who was mentioned
        # rather than to a name lookup that can miss.
        mention_ids = {str(name): str(uid) for uid, name in name_map.items()}

        speaker = message.author.display_name or message.author.name
        channel_id = message.channel.id

        # The honeypot is checked before anything else: a scam payload must
        # never reach channel history, XP, or the presence dirty set.
        hp_guild = message.guild_id or await adapter.resolve_guild_id(
            channel_id)
        if hp_guild and settings.is_honeypot(hp_guild, channel_id):
            await _handle_honeypot(message, hp_guild, raw)
            return
        conversation.record(channel_id, speaker, content,
                            is_bot=False, message_id=message.id,
                            user_id=message.author.id,
                            media=media_labels(message))

        # XP accrues on EVERY message, not just ones addressed to Corbin --
        # it measures participation, not conversation with the bot.
        gid_for_xp = message.guild_id
        if gid_for_xp is None and not message.channel.is_dm:
            gid_for_xp = await adapter.resolve_guild_id(channel_id)
        msg_cache.put(CachedMessage(
            id=message.id, channel_id=channel_id,
            author_id=message.author.id, author_name=speaker,
            content=content,
            attachments=tuple(a.filename for a in message.attachments)))

        presence.mark_dirty(gid_for_xp, channel_id)

        if gid_for_xp and settings.ranking_enabled(gid_for_xp):
            xp.record(gid_for_xp, message.author.id, content)
            xp.flush_due()
            await _announce_level_up(gid_for_xp, channel_id,
                                     author=raw.author, name=speaker)

        # A cited verse pre-empts the chatbot: posting the passage AND a
        # chat reply to the same message is noise. Mirrors the Discord
        # build's will_handle guard, and runs after XP/history bookkeeping
        # so verse messages still count.
        if gid_for_xp and settings.versefinder_enabled(gid_for_xp) and gill \
                is not None:
            refs = verse_lookup.parse(content)
            if refs:
                presence.mark_spoke(gid_for_xp, channel_id)
                from corbin.commands.verse_cmds import (build_verse_embed,
                                                        detect_translation)

                # Honour "Romans 1:1 commentary" / "Genesis 1:1 in hebrew".
                # This path pre-empts the dispatcher, so if it ignored the
                # request the user could never reach anything but the KJV.
                wanted = detect_translation(content) or "kjv"
                for ref in refs[:2]:
                    try:
                        await adapter.send(channel_id, Response(
                            embed=await build_verse_embed(
                                ref, translation=wanted, gill=gill)))
                    except Exception:
                        logger.warning("verse post failed", exc_info=True)
                return

        gid = message.guild_id
        if gid is None and not message.channel.is_dm:
            # Channel cache miss: ask the API rather than treating a guild
            # channel as a DM and filing its settings under "None".
            gid = await adapter.resolve_guild_id(channel_id)

        if settings.is_silenced(gid, channel_id) or settings.mode(gid) == "off":
            return

        if await _answer_general_question(gid, channel_id, message.author.id,
                                          content, chan_map):
            return

        addressed = bool(me_id and me_id in message.content)
        # A reply only counts when it is a reply to CORBIN. Treating
        # every reply as an invocation meant two people replying to each
        # other pulled him into the middle of it.
        replying_to_corbin = await _reply_is_to_bot(message, channel_id,
                                                    me_id)
        ai_channel = settings.is_ai_channel(gid, channel_id)

        directly_invoked = (addressed or replying_to_corbin
                            or message.channel.is_dm or ai_channel)
        if not directly_invoked:
            return

        # An AI channel answers everything EXCEPT people talking to each
        # other in it. Without this he replies to both halves of a
        # conversation he was never part of.
        if (ai_channel and not addressed and not replying_to_corbin
                and not message.channel.is_dm
                and await _aimed_at_someone_else(message, raw,
                                                 channel_id, me_id)):
            # INFO, not debug: silence with no log line is
            # indistinguishable from the bot being broken, and this
            # filter is new enough to be the first suspect.
            logger.info("AI channel %s: skipping a message aimed at "
                        "another member", channel_id)
            return

        guild = await adapter.guild_info(gid) if gid else None

        # MESSAGE_CREATE does not carry a member object on Fluxer, and
        # `raw.author` is a bare User with NO roles -- using it makes every
        # permission compute to zero and denies even the server owner.
        member = None
        if gid:
            try:
                member = await adapter.fetch_member(gid, message.author.id)
            except Exception:
                logger.debug("member fetch failed for %s", message.author.id,
                             exc_info=True)
        if member is None:
            member = to_member(
                getattr(raw, "member", None) or raw.author,
                guild_id=gid or "",
                guild_owner_id=guild.owner_id if guild else None,
            )
            if gid:
                logger.warning(
                    "falling back to a roleless author for %s -- permission "
                    "checks will be conservative", message.author.id)

        # Let channel-typed params resolve "#ranking" back to an id.
        if gid:
            index = await adapter.guild_channel_index(gid)
            index.update({name.lower(): cid for cid, name in chan_map.items()})
            executor.extras["channel_index"] = index

        result = await dispatcher.handle(
            content, invoker=member, channel_id=channel_id,
            guild_id=gid, directly_invoked=True, mention_ids=mention_ids,
        )

        # Chatbanned people keep full use of commands; Corbin just stops
        # chatting with them. Same rule as the Discord build.
        if result.converse and message.author.id in chatbans:
            return

        # Typing is scoped tightly around the work that actually takes time
        # -- the model call or the command -- not the whole handler.
        if result.converse:
            reply = await _converse(content, member, speaker, channel_id,
                                    guild, message)
        elif result.decision is Decision.EXECUTE or result.undo is not None:
            async with adapter.typing(channel_id):
                reply = await executor.run(
                    result, invoker=member, channel=message.channel,
                    guild=guild, mention_ids=mention_ids,
                    raw_text=content,
                    attachments=tuple(message.attachments or ()))
        else:
            reply = result.message

        if not reply:
            return
        conversation.record(channel_id, conversation.bot_name, reply,
                            is_bot=True)
        # Both paths share the cooldown, so the loop will not also answer
        # a channel Corbin just replied in.
        presence.mark_spoke(gid, channel_id)
        digest_store.record("message", user_id=member.id, user_name=speaker,
                            guild_id=gid,
                            guild_name=guild.name if guild else "")
        # Long code goes up as a file. History above keeps the inline
        # version, so he still knows what he sent.
        body, code_files = reply, []
        if adapter.capabilities().file_upload:
            body, code_files = extract_code_files(reply)
        chunks = split_message(body)
        sent_ids = []
        for i, chunk in enumerate(chunks):
            files = tuple(code_files) if i == len(chunks) - 1 else ()
            sent_id = await adapter.send(channel_id,
                                         Response(content=chunk, files=files))
            _remember_own_message(sent_id, channel_id, chunk)
            sent_ids.append(sent_id)
        # The success path used to log nothing, so "generated a reply but
        # it never showed up" was indistinguishable from "never replied".
        if chunks and all(sent_ids):
            logger.info("replied to %s in %s (%d chars, %d message(s): %s) "
                        "| sent=%r", speaker, channel_id, len(reply),
                        len(chunks), ",".join(map(str, sent_ids)),
                        reply[:120])
        else:
            logger.warning("reply to %s in %s did not post: %d chunk(s), "
                           "ids=%s | text=%r", speaker, channel_id,
                           len(chunks), sent_ids, reply[:200])

    async def _answer_general_question(gid, channel_id: str, user_id: str,
                                       content: str, chan_map) -> bool:
        """Take a reply to "which channel is your general chat?".

        Asked by ``chat_mode`` when global mode has no general channel
        yet. Only the person who was asked, in the channel it was asked in,
        within the TTL, and only a clear channel answer; anything else is
        handled as normal and the question stays open until it expires.
        """
        key = (str(channel_id), str(user_id))
        asked = general_questions.get(key)
        if asked is None:
            return False
        asked_gid, deadline = asked
        if _monotonic() > deadline or str(asked_gid) != str(gid):
            general_questions.pop(key, None)
            return False
        index = await adapter.guild_channel_index(gid)
        target = match_general_answer(content, chan_map, index, channel_id)
        if not target:
            return False
        general_questions.pop(key, None)
        settings.set_general_channel(gid, target)
        reply = (f"Got it, <#{target}>. That's where I'll bring stuff up "
                 "on my own.")
        logger.info("general channel for guild %s set to %s (answered)",
                    gid, target)
        conversation.record(channel_id, conversation.bot_name, reply,
                            is_bot=True)
        presence.mark_spoke(gid, channel_id)
        sent_id = await adapter.send(channel_id, Response(content=reply))
        _remember_own_message(sent_id, channel_id, reply)
        return True

    def _member_bits(data) -> tuple[str, str, str, Optional[str]]:
        """(user_id, display_name, username, avatar_url) from a raw payload.

        GUILD_MEMBER_ADD/REMOVE arrive as raw dicts, not parsed models, so
        this reads defensively rather than assuming attributes.
        """
        get = (data.get if isinstance(data, dict)
               else lambda k, d=None: getattr(data, k, d))
        user = get("user") or {}
        uget = (user.get if isinstance(user, dict)
                else lambda k, d=None: getattr(user, k, d))
        uid = str(uget("id") or get("user_id") or get("id") or "")
        username = str(uget("username") or uget("name") or "someone")
        display = str(get("nick") or uget("global_name")
                      or uget("display_name") or username)
        avatar = uget("avatar_url") or uget("avatar")
        return uid, display, username, (str(avatar) if avatar else None)

    async def _fetch_bytes(url: Optional[str], *,
                           timeout: float = 8.0) -> Optional[bytes]:
        if not url or not str(url).startswith("http"):
            return None
        try:
            import httpx

            async with httpx.AsyncClient(timeout=timeout,
                                         follow_redirects=True) as client:
                resp = await client.get(str(url))
            return resp.content if resp.status_code == 200 else None
        except Exception:
            return None

    @bot.event
    async def on_message_edit(raw) -> None:  # type: ignore[misc]
        try:
            msg = to_message(raw)
            # His own edits are his business, not the server's audit trail:
            # he edits progress notes ("Searching…") as a matter of course.
            if _is_me(msg.author.id):
                return
            gid = msg.guild_id or await adapter.resolve_guild_id(
                msg.channel.id)
            before = msg_cache.get(msg.channel.id, msg.id)
            if before is not None and _is_me(before.author_id):
                return
            after, _users, _channels = await _readable_content(
                raw, msg.content)
            if not after.strip() and not msg.attachments:
                # A partial update (an embed resolving) can arrive with no
                # content at all. A real message can't be empty AND have
                # nothing attached, so this is not "edited to nothing".
                return
            # Re-cache regardless, so a second edit diffs against the first.
            author = before.author_name if before else msg.author.display_name
            msg_cache.put(CachedMessage(
                id=msg.id, channel_id=msg.channel.id,
                author_id=msg.author.id, author_name=author,
                content=after,
                attachments=tuple(before.attachments) if before else ()))
            # Compared in the same resolved form the cache holds. Fluxer
            # sends an update when a link preview loads, with the text
            # untouched; only a change to the text itself is an edit.
            if before is None or before.content.strip() == after.strip():
                return
            await _audit(gid, "message_edit", "Message edited",
                         f"By **{author}** in <#{msg.channel.id}>",
                         (("Before", before.content, False),
                          ("After", after, False)))
        except Exception:
            logger.debug("message_edit log failed", exc_info=True)

    @bot.event
    async def on_message_delete(data) -> None:  # type: ignore[misc]
        try:
            get = (data.get if isinstance(data, dict)
                   else lambda k, d=None: getattr(data, k, d))
            cid = str(get("channel_id") or "")
            mid = str(get("id") or get("message_id") or "")
            gid = str(get("guild_id") or "") or \
                await adapter.resolve_guild_id(cid)
            # The payload carries ids only, never content -- this is the
            # entire reason MessageCache exists.
            cached = msg_cache.pop(cid, mid)
            if cached is not None and _is_me(cached.author_id):
                # He deletes his own messages routinely (progress notes,
                # post-and-delete replies); logging those is pure noise.
                return
            if cached is None:
                await _audit(gid, "message_delete", "Message deleted",
                             f"In <#{cid}>. It predates my cache, so I have "
                             "no copy of the content.")
                return
            await _audit(gid, "message_delete", "Message deleted",
                         f"By **{cached.author_name}** in <#{cid}>",
                         (("Content", cached.content, False),))
        except Exception:
            logger.debug("message_delete log failed", exc_info=True)

    async def _channel_event(data, category: str, title: str) -> None:
        get = (data.get if isinstance(data, dict)
               else lambda k, d=None: getattr(data, k, d))
        gid = str(get("guild_id") or "")
        name = get("name") or "?"
        cid = get("id")
        await _audit(gid, category, title,
                     f"**#{name}**" + (f" (<#{cid}>)" if cid else ""))

    @bot.event
    async def on_channel_create(data) -> None:  # type: ignore[misc]
        await _channel_event(data, "channel_create", "Channel created")

    @bot.event
    async def on_channel_update(data) -> None:  # type: ignore[misc]
        await _channel_event(data, "channel_update", "Channel updated")

    @bot.event
    async def on_channel_delete(data) -> None:  # type: ignore[misc]
        try:
            _forget_deleted_honeypot(data)
        except Exception:
            logger.warning("honeypot cleanup failed", exc_info=True)
        await _channel_event(data, "channel_delete", "Channel deleted")

    def _forget_deleted_honeypot(data) -> None:
        """A deleted trap must not leave a dangling honeypot id behind.

        Otherwise honeypot-status reports a trap that no longer exists, and
        spamblock "adopts" an id pointing at nothing. Mirrors the Discord
        build's on_guild_channel_delete.
        """
        get = (data.get if isinstance(data, dict)
               else lambda k, d=None: getattr(data, k, d))
        gid, cid = str(get("guild_id") or ""), str(get("id") or "")
        if gid and cid and settings.is_honeypot(gid, cid):
            settings.set_honeypot(gid, None)
            logger.info("honeypot channel %s deleted in guild %s; spam "
                        "trap is now off", cid, gid)

    @bot.event
    async def on_voice_state_update(data) -> None:  # type: ignore[misc]
        try:
            get = (data.get if isinstance(data, dict)
                   else lambda k, d=None: getattr(data, k, d))
            gid = str(get("guild_id") or "")
            uid = str(get("user_id") or "")
            chan = get("channel_id")
            if uid:
                if chan:
                    voice_states[uid] = str(chan)
                else:
                    voice_states.pop(uid, None)
            await _audit(gid, "voice",
                         "Joined voice" if chan else "Left voice",
                         f"<@{uid}>" + (f" in <#{chan}>" if chan else ""))
        except Exception:
            logger.debug("voice log failed", exc_info=True)

    @bot.event
    async def on_member_join(data) -> None:  # type: ignore[misc]
        try:
            await _welcome(data)
        except Exception:
            logger.exception("welcome failed")

    async def _welcome(data) -> None:
        gid = str((data.get("guild_id") if isinstance(data, dict)
                   else getattr(data, "guild_id", "")) or "")
        _uid, _display, _user, _av = _member_bits(data)
        await _audit(gid, "member_join", "Member joined",
                     f"<@{_uid}> ({_display})")
        cfg = settings.welcome(gid)
        if not gid or not cfg.get("enabled"):
            return
        channel_id = cfg.get("channel_id")
        if not channel_id:
            return

        uid, display, username, avatar_url = _member_bits(data)
        if not uid:
            logger.warning("join event with no user id: %r", data)
            return
        guild = await adapter.guild_info(gid)
        server_name = (guild.name if guild and guild.name else "the server")

        count = await adapter.member_count(gid)
        text = None
        wants_ai = bool(settings.greeting(gid).get("enabled"))
        if wants_ai:
            text = await greeter.greet(
                server_name=server_name, display_name=display,
                username=username,
                custom_info=settings.greeting(gid).get("custom_info", ""))
        if not text:
            # Falling back to the template is correct, but "off" and
            # "the model gave nothing back" produce identical output,
            # so say which in the log or the next report is guesswork.
            if wants_ai:
                logger.warning("AI greeting produced nothing for guild %s; "
                               "sent the template instead", gid)
            else:
                logger.info("greeting is off for guild %s; sent the "
                            "template. Enable it with the greeting "
                            "command.", gid)
            text = render_template(cfg.get("message") or DEFAULT_WELCOME,
                                   member=f"<@{uid}>", server=server_name,
                                   count=count)
        else:
            logger.info("AI greeting sent for %s in guild %s", display, gid)
            text = f"<@{uid}> {text}"

        files: list = []
        if cfg.get("card", True):
            png = await _render_card(
                "welcome", gid, uid, display, avatar_url,
                render_template(cfg.get("card_text") or DEFAULT_WELCOME_CARD,
                                member=display, server=server_name,
                                count=count),
                bool(cfg.get("background")),
                subtitle=f"Member #{count:,}" if count else None)
            if png:
                files.append((f"welcome-{uid}.png", png))

        await adapter.send(channel_id,
                           Response(content=text, files=tuple(files)))

    async def _render_card(kind: str, gid: str, uid: str, display: str,
                           avatar_ref, top_text: str, has_background: bool,
                           subtitle=None):
        """One welcome/farewell card: avatar, stored background, text."""
        from corbin.core.welcome import avatar_url as _avatar_url
        from corbin.core.welcome import background_path, make_card

        url = _avatar_url(uid, avatar_ref)
        avatar = await _fetch_bytes(url)
        if avatar is None:
            # INFO: this is exactly the failure that shipped unnoticed --
            # every card went out without a face and nothing said why.
            logger.info("%s card for %s: no avatar (from %r)", kind, uid,
                        url)
        background = None
        if has_background:
            path = background_path(config.DATA_ROOT, gid, kind)
            try:
                background = path.read_bytes()
            except OSError:
                logger.warning("%s background for guild %s is set but %s "
                               "is missing; using a plain colour", kind,
                               gid, path)
        return await asyncio.to_thread(make_card, top_text, display, avatar,
                                       background, subtitle)

    @bot.event
    async def on_member_remove(data) -> None:  # type: ignore[misc]
        try:
            gid = str((data.get("guild_id") if isinstance(data, dict)
                       else getattr(data, "guild_id", "")) or "")
            cfg = settings.farewell(gid)
            if not gid:
                return
            if not cfg.get("enabled") or not cfg.get("channel_id"):
                uid, display, _u, _a = _member_bits(data)
                await _audit(gid, "member_leave", "Member left",
                             f"**{display}** ({uid})")
                return
            uid, display, _username, avatar_ref = _member_bits(data)
            await _audit(gid, "member_leave", "Member left",
                         f"**{display}** ({uid})")
            guild = await adapter.guild_info(gid)
            server_name = (guild.name if guild and guild.name
                           else "the server")
            count = await adapter.member_count(gid)
            files: list = []
            if cfg.get("card"):
                png = await _render_card(
                    "farewell", gid, uid, display, avatar_ref,
                    render_template(
                        cfg.get("card_text") or DEFAULT_FAREWELL_CARD,
                        member=display, server=server_name, count=count),
                    bool(cfg.get("background")),
                    subtitle=(f"{count:,} members remain" if count
                              else None))
                if png:
                    files.append((f"farewell-{uid}.png", png))
            await adapter.send(cfg["channel_id"], Response(
                content=render_template(
                    cfg.get("message") or DEFAULT_FAREWELL,
                    member=display or f"<@{uid}>", server=server_name,
                    count=count),
                files=tuple(files)))
        except Exception:
            logger.exception("farewell failed")

    async def _handle_honeypot(message, guild_id: str, raw) -> None:
        """Someone posted in the trap. Delete, warn, DM, then ban.

        The DM must come BEFORE the ban: afterwards there is no mutual
        guild and they would never learn why.
        """
        uid = message.author.id
        if not honeypot_guard.claim(guild_id, uid):
            return  # a burst of messages produces exactly one ban

        try:
            await adapter.delete_message(message.channel.id, message.id)
        except Exception:
            pass

        member = None
        try:
            member = await adapter.fetch_member(guild_id, uid)
        except Exception:
            pass

        if is_exempt(member, bot_owner_id=config.BOT_OWNER_ID):
            # Staff get a nudge. A moderator pinning the notice must not be
            # banned for it.
            try:
                await adapter.send_dm(uid, Response(content=(
                    "Heads up: that channel is a bot trap, so I deleted "
                    "your message. Posting there bans anyone without staff "
                    "permissions.")))
            except Exception:
                pass
            honeypot_guard.release(guild_id, uid)
            logger.info("honeypot nudge for exempt user %s", uid)
            return

        entry = None
        try:
            entry = warnings_store.add(
                guild_id, uid, moderator_id=None,
                reason="Posted in the #do-not-type honeypot", action="ban")
            await notify_target(
                adapter, uid, action="banned",
                server_name=(await adapter.guild_info(guild_id)).name
                if await adapter.guild_info(guild_id) else "the server",
                reason="Posted in the honeypot channel, which is an "
                       "automatic ban for compromised or scam accounts.")
            await adapter.ban(guild_id, uid,
                              reason="Honeypot: posted in #do-not-type",
                              delete_message_seconds=BAN_DELETE_SECONDS)
            logger.warning("honeypot banned %s in %s", uid, guild_id)
            await _audit(guild_id, "member_leave", "Honeypot ban",
                         f"<@{uid}> posted in the honeypot and was banned.")
        except Exception:
            logger.exception("honeypot ban failed for %s", uid)
            # Roll the warning back so a failed ban does not leave a
            # phantom record with an appeal id nobody can act on.
            if entry is not None:
                warnings_store.remove(guild_id, entry.id)
        finally:
            honeypot_guard.release(guild_id, uid)

    async def _announce_level_up(guild_id, fallback_channel, *,
                                 author=None, name: str = "") -> None:
        """Post a level-up, if one just happened and a channel is set."""
        event = xp.pop_level_up()
        if not event:
            return
        (event_guild, user_id), before, after = event
        if event_guild != guild_id:
            return
        target = settings.ranking_channel(guild_id) or fallback_channel
        if not target:
            return
        entry = ranking.get(guild_id, user_id)

        earned_new_title = (
            entry.role_name
            and (role_for_level(before) or ("",))[0] != entry.role_name)
        if entry.role_name:
            # Apply on every level-up, not only on a new title: a member
            # who levelled before the roles existed still needs theirs.
            err = await adapter.apply_rank_role(
                guild_id, user_id, entry.role_name,
                [n for n, _c in ROLE_TIERS.values()])
            if err:
                logger.warning("rank role for %s failed: %s", user_id, err)
        from corbin.commands.ranking_cmds import (average_colour,
                                                  level_up_embed)

        # The level-up belongs to whoever sent the message that tipped it.
        # Guard anyway: if it ever doesn't, use a mention rather than
        # putting the wrong person's name and face on the card.
        mine = author is not None and str(getattr(author, "id", "")) == \
            str(user_id)
        avatar_url = None
        if mine:
            avatar_url = (getattr(author, "avatar_url", None)
                          or getattr(author, "default_avatar_url", None))
        colour = None
        if avatar_url:
            colour = await asyncio.to_thread(
                average_colour, await _fetch_bytes(str(avatar_url)))
        embed = level_up_embed(
            name if (mine and name) else "Someone", entry,
            avatar_url=str(avatar_url) if avatar_url else None,
            colour=colour, new_title=bool(earned_new_title))
        try:
            # The mention goes in content: embeds never notify anyone, and
            # the ping is the part a member actually sees.
            await adapter.send(target, Response(
                content=f"<@{user_id}> reached **level {after}**!",
                embed=embed))
        except Exception:
            # WARNING: a level-up that silently never posts looks exactly
            # like ranking being broken.
            logger.warning("level-up announcement failed", exc_info=True)

    def _remember_own_message(message_id, channel_id: str,
                              content: str) -> None:
        """Cache a message Corbin sent.

        The cache only ever held *incoming* messages, so a reply to
        Corbin found nothing in it and read as a reply to a stranger --
        which is how he started ignoring people replying to him.
        """
        if not message_id:
            return
        me = str(getattr(getattr(bot, "user", None), "id", "") or "")
        msg_cache.put(CachedMessage(
            id=str(message_id), channel_id=str(channel_id),
            author_id=me, author_name=conversation.bot_name,
            content=content, attachments=()))

    async def _reply_is_to_bot(message, channel_id: str,
                               me_id: str) -> bool:
        """True when this message replies to one of Corbin's.

        Cache first, then ask the API. The cache is small and bounded,
        so a reply to anything more than a few messages back would
        otherwise be misread as a reply to someone else.
        """
        ref = getattr(message, "reference_id", None)
        if not ref or not me_id:
            return False
        cached = msg_cache.get(channel_id, str(ref))
        if cached is not None:
            return str(cached.author_id) == str(me_id)
        author = await adapter.message_author(channel_id, str(ref))
        # Unknown even after asking: treat as not his. Answering a
        # stranger's reply to a stranger is the worse mistake.
        return author is not None and str(author) == str(me_id)

    async def _aimed_at_someone_else(message, raw, channel_id: str,
                                     me_id: str) -> bool:
        """True only when the message is *positively* for someone else.

        Uncertainty answers. This channel exists for asking Corbin
        things, so ignoring a question is a worse failure than
        answering one that was not quite for him -- and an unverifiable
        reference used to mean silence with no log line at all.
        """
        ref = getattr(message, "reference_id", None)
        if ref:
            author = None
            cached = msg_cache.get(channel_id, str(ref))
            if cached is not None:
                author = str(cached.author_id)
            else:
                author = await adapter.message_author(channel_id, str(ref))
            if author is None:
                return False          # cannot tell; answer anyway
            return author != str(me_id)
        for user in (getattr(raw, "mentions", None) or []):
            if str(getattr(user, "id", "")) != str(me_id):
                return True
        return False

    async def _collect_images(message) -> list:
        """Image blocks for the attachments on a message.

        Only when the model has vision (LLM_VISION=1), and only on this
        path -- a direct mention, reply or AI channel -- never for presence
        chime-ins. Without vision the transcript still names the picture,
        so he can say he can't see it instead of guessing.
        """
        if not llm.vision:
            return []
        blocks: list = []
        for item in (getattr(message, "attachments", None) or ())[
                :config.MAX_VISION_IMAGES]:
            url = str(getattr(item, "url", "") or "")
            name = str(getattr(item, "filename", "") or "")
            if not url or not _looks_like_image(name, item):
                continue
            raw = await vision.fetch(url)
            if raw is None:
                continue
            # Blocking: Pillow work off the event loop, always.
            ready = await asyncio.to_thread(vision.prepare, raw)
            if ready is None:
                logger.info("dropped an unreadable image: %s", name)
                continue
            b64, media_type, animated = ready
            blocks.append(vision.image_block(b64, media_type))
            if animated:
                blocks.append({"type": "text",
                               "text": vision.ANIMATION_GRID_HINT})
        if blocks:
            logger.info("vision: sending %d image(s) with this reply",
                        sum(1 for b in blocks if b.get("type") == "image"))
        return blocks

    def _looks_like_image(name: str, item) -> bool:
        content_type = str(getattr(item, "content_type", "") or "").lower()
        if content_type.startswith("image/"):
            return True
        return name.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"))

    async def _converse(content, member, speaker, channel_id, guild, message):
        """Answer conversationally.

        A blunt "what can you do?" is answered from the command registry
        straight away (``is_command_question``, a free regex). Otherwise
        the model gets one call with the ``lookup_commands`` tool: it
        either answers, or asks for the command list and is called again
        with it.
        """
        images = await _collect_images(message)
        files = await documents.collect(message, vision.fetch)

        reply_kwargs = dict(
            user_id=member.id, speaker=speaker, user_content=content,
            channel_id=channel_id,
            guild_name=guild.name if guild else None,
            channel_name=message.channel.name,
            guild_id=guild.id if guild else None,
            images=images, files=files,
        )

        reference = None
        in_guild = guild is not None
        guild_id = str(guild.id) if guild else None
        if is_command_question(content):
            reference = command_help.lookup(content, member,
                                            in_guild=in_guild,
                                            guild_id=guild_id)
            logger.info("command help: answering from the registry for %r",
                        content[:80])
        elif config.LLM_TOOLS:
            # One call that either answers or asks for a tool.
            async with adapter.typing(channel_id):
                first = await conversation.reply(
                    **reply_kwargs, tools=conversation_mod.REPLY_TOOLS)
            if not isinstance(first, conversation_mod.ToolRequest):
                return first
            topic = str(first.arguments.get("topic") or "").strip()
            reference = command_help.lookup(topic, member,
                                            in_guild=in_guild,
                                            guild_id=guild_id)
            logger.info("tool route: lookup_commands (%r)", topic)

        async with adapter.typing(channel_id):
            return await conversation.reply(**reply_kwargs,
                                            reference=reference)

    logger.info("connecting to Fluxer…")
    try:
        bot.run(config.FLUXER_TOKEN)
    finally:
        if voice is not None and len(voice):
            try:
                asyncio.run(voice.disconnect_all())
            except Exception:
                logger.debug("voice teardown failed", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
