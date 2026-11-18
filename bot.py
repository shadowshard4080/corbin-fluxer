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
import contextlib
import dataclasses
import logging
import os
from time import monotonic as _monotonic
import re
import sys
from pathlib import Path
from typing import Optional

import config


def _log_handlers() -> list:
    """stderr, plus a file rotated daily and deleted after LOG_DAYS days.
    LOG_FILE empty: stderr only."""
    from logging.handlers import TimedRotatingFileHandler

    handlers: list = [logging.StreamHandler()]
    if config.LOG_FILE:
        path = Path(config.LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(TimedRotatingFileHandler(
            path, when="midnight", utc=True,
            backupCount=max(1, config.LOG_DAYS - 1), encoding="utf-8"))
    return handlers


logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    handlers=_log_handlers(),
)
logger = logging.getLogger("corbin")


class _HttpxFailuresOnly(logging.Filter):
    """httpx logs every request at INFO, success or not -- the radio's
    now-playing poll alone was a line every 40 s. Keep only the ones that
    didn't come back 2xx/3xx. Not a level change: httpx logs a 500 at
    INFO too, and that one should still show."""

    _OK = re.compile(r'"HTTP/[\d.]+ [23]\d\d\b')

    def filter(self, record: logging.LogRecord) -> bool:
        return not self._OK.search(record.getMessage())


logging.getLogger("httpx").addFilter(_HttpxFailuresOnly())

from corbin.core.logtext import said                          # noqa: E402

#: What people call him out loud. Voice chat listens for it (allowing
#: for speech recognition's spelling) to tell what's meant for him.
VOICE_NAME = config.BOT_NAME

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
from corbin.core.context import Channel, Response               # noqa: E402
from corbin.core.reactions import (Panel, ReactionPanels,       # noqa: E402
                                   normalise, reaction_emoji)
from corbin.core import vision                                 # noqa: E402
from corbin.core import documents                              # noqa: E402
from corbin.core import doc_output                             # noqa: E402
from corbin.core import events as events_mod                   # noqa: E402
from corbin.core import link_previews                          # noqa: E402
from corbin.core.media import (media_labels,                    # noqa: E402
                               strip_emoji_ids)
from corbin.ai.command_help import (CommandHelp,                # noqa: E402
                                    is_command_question)
from corbin.core.dispatch import Dispatcher                     # noqa: E402
from corbin.core.registry import registry                       # noqa: E402
from corbin.core.audit import (CATEGORIES, COLOURS,             # noqa: E402
                               CachedMessage, MessageCache,
                               truncate)
from corbin.core.memory import MemoryOptOut, MemoryStore        # noqa: E402
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
from corbin.core.health import (FIRST_BEAT_WAIT,               # noqa: E402
                                GatewayPing, api_latency,
                                human_duration, human_latency,
                                local_model_state)
from corbin.core.bans import BanStore, ChatbanStore, humanise   # noqa: E402
from corbin.core.honeypot import (BAN_DELETE_SECONDS,           # noqa: E402
                                  HoneypotGuard, is_exempt)
from corbin.core.player import PlayerManager                    # noqa: E402
from corbin.core.reminders import ReminderStore                 # noqa: E402
from corbin.core.voice import EmptyChannelWatch, VoiceManager   # noqa: E402
from corbin.core.video import ConnectionIds, StreamManager      # noqa: E402
from corbin.core.features import FeatureAllowlist               # noqa: E402
from corbin.core.voicechat import (StudioConfig, VoiceChat,     # noqa: E402
                                   VoiceOptOut, VoiceStudio,
                                   strip_name)
from corbin.core.scheduled import (Scheduler, animal_post,  # noqa: E402
                                   fetch_quote, is_due,
                                   pick_animal)
from corbin.core.warnings import WarningStore                   # noqa: E402
from corbin.core.ranking import (RankingStore, RankRoles,       # noqa: E402
                                 XPAccumulator)
from corbin.core.settings import ChatSettings                   # noqa: E402
from corbin.core.risk import Decision                           # noqa: E402
from corbin.platforms.base import FLUXER_CAPABILITIES           # noqa: E402
from corbin.platforms.fluxer.adapter import (FluxerAdapter, is_missing_permissions,  # noqa: E402
                                             probe, to_member, to_message)
from corbin.commands._notify import notify_target               # noqa: E402
from corbin.commands.config_cmds import match_general_answer     # noqa: E402
from corbin.commands import reaction_role_cmds                 # noqa: E402
from corbin.commands import ranking_cmds                       # noqa: E402
from corbin.commands import event_cmds                         # noqa: E402
from corbin.core.reaction_roles import (ReactionRoleStore,      # noqa: E402
                                        event_key as rr_event_key)
from corbin.commands.config_cmds import (                        # noqa: E402
    QUIET_ESCAPE, apply_silence, match_silence_answer, match_unsilence,
    works_while_quiet)
from corbin.runtime import Executor                             # noqa: E402

import corbin.commands.audit_cmds   # noqa: E402,F401 (registers commands)
import corbin.commands.banner_cmds  # noqa: E402,F401
import corbin.commands.config_cmds  # noqa: E402,F401
import corbin.commands.digest_cmds  # noqa: E402,F401
import corbin.commands.expression_cmds  # noqa: E402,F401
import corbin.commands.general     # noqa: E402,F401
import corbin.commands.info        # noqa: E402,F401
import corbin.commands.memory_cmds  # noqa: E402,F401
import corbin.commands.moon_cmds    # noqa: E402,F401
import corbin.commands.music_cmds   # noqa: E402,F401
import corbin.commands.radio_cmds    # noqa: E402,F401
import corbin.commands.reaction_role_cmds  # noqa: E402,F401
import corbin.commands.event_cmds  # noqa: E402,F401
import corbin.commands.reminder_cmds # noqa: E402,F401
import corbin.commands.ranking_cmds  # noqa: E402,F401
import corbin.commands.scheduled_cmds  # noqa: E402,F401
import corbin.commands.spamblock_cmds  # noqa: E402,F401
import corbin.commands.stream_cmds   # noqa: E402,F401
import corbin.commands.tv_cmds       # noqa: E402,F401
import corbin.commands.video_cmds    # noqa: E402,F401
import corbin.commands.util_cmds    # noqa: E402,F401
import corbin.commands.verse_cmds    # noqa: E402,F401
import corbin.commands.voicechat_cmds  # noqa: E402,F401
import corbin.commands.allowlist_cmds  # noqa: E402,F401
import corbin.commands.warn_cmds     # noqa: E402,F401
import corbin.commands.welcome_cmds  # noqa: E402,F401
import corbin.commands.moderation  # noqa: E402,F401


def _data_key_problems() -> list[str]:
    """Set the at-rest key and prove it opens the data already on disk.

    Refusing to start beats finding out on the first memory lookup: the
    bot would be up, unable to remember anyone, and saying nothing. With
    no key set this only logs "plain text".
    """
    from corbin.core import crypto

    key = config.DATA_KEY
    if config.DATA_KEY_FILE:
        try:
            key = Path(config.DATA_KEY_FILE).read_text(
                encoding="ascii").strip()
        except OSError as exc:
            return [f"DATA_KEY_FILE {config.DATA_KEY_FILE} can't be "
                    f"read: {exc}"]
    try:
        crypto.configure(key)
    except crypto.DataKeyError as exc:
        return [str(exc)]
    root = Path(config.DATA_ROOT)
    files = [root / name for name in ("spamblock_warnings.json",
                                      "fluxer_reminders.json")]
    why = (crypto.check_dir(root / "user_memories")
           or crypto.check_dir(root / "server_memories")
           or crypto.check_files(files))
    if why:
        return [why + ". Put the right DATA_KEY back in .env; starting "
                "without it would lose that data"]
    logger.info("data at rest: %s", "encrypted" if crypto.enabled()
                else "plain text (set DATA_KEY_FILE to encrypt)")
    return []


def main() -> int:
    # SIGCHLD inherited as "ignore" (the launcher's choice; cron/tmux can
    # pass it down) makes the kernel reap every child itself, so asyncio
    # finds none: "Unknown child process pid N, will report returncode
    # 255" for every ffmpeg/ffprobe, and exit codes that mean nothing.
    import signal
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        logger.warning("SIGCHLD was inherited as ignored; reset to the "
                       "default so subprocess exit codes are real")
    boot_started = _monotonic()
    problems = config.validate()
    problems += _data_key_problems()
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
    # The "don't remember me" check is attached once settings exist below.
    memory = MemoryStore(config.DATA_ROOT)
    conversation = Conversation(llm, soul_path=config.PERSONA_PATH,
                                additions_path=config.PERSONA_ADDITIONS_PATH,
                                memory=memory,
                                extractor=MemoryExtractor(llm))
    owner_id = config.BOT_OWNER_ID or None
    dispatcher = Dispatcher(
        registry, ModelIntentClassifier(llm.classifier_call()),
        resolver=adapter, bot_owner_id=owner_id,
        # Late-bound: the allowlist lives in settings, built below.
        features=lambda gid: feature_allowlist.enabled(gid),
    )
    executor = Executor(adapter, dispatcher)
    # Same owner id as the dispatcher, so the help never offers a command
    # the risk gates would then refuse.
    command_help = CommandHelp(registry, bot_owner_id=owner_id,
                               features=lambda gid:
                                   feature_allowlist.enabled(gid))
    greeter = Greeter(llm)

    settings = ChatSettings(config.SETTINGS_PATH)
    #: Each server's milestone roles (core/ranking.py, RankRoles).
    rank_roles = RankRoles(settings)
    memory_optout = MemoryOptOut(settings)
    memory.opted_out = memory_optout
    #: The owner's per-server allowlist for voice chat and streaming.
    feature_allowlist = FeatureAllowlist(settings)
    # He mentions streaming only where it's allowed (STREAMING_LINE).
    conversation.features = feature_allowlist.enabled
    seeded = feature_allowlist.seed(config.VOICE_CHAT_GUILDS, "voice")
    if seeded:
        logger.info("allowlist: seeded voice for %d server(s) from "
                    "VOICE_CHAT_GUILDS; DM me to change it from now on",
                    seeded)
    voice_optout = VoiceOptOut(settings)
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
        voice.on_voice_time = digest_store.record_voice
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
            from corbin.commands.music_cmds import track_embed, video_ok

            card = (id(track), str(where))
            if player is not None and player.last_card == card:
                # The play command already posted this track's card here.
                logger.info("track card for %r already in %s; not "
                            "reposting", track.title, where)
            else:
                try:
                    await adapter.send(where, Response(embed=track_embed(
                        track, guild_id=guild_id, player=player,
                        video=video_ok(feature_allowlist, guild_id, track),
                        status=(music_video.status_line(guild_id)
                                if music_video is not None else ""))))
                    if player is not None:
                        player.last_card = card
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

    empty_watch = None
    if voice is not None:
        def _occupants(channel_id: str) -> int:
            """People in a voice channel, not counting him."""
            me = str(getattr(getattr(bot, "user", None), "id", "") or "")
            return sum(1 for uid, cid in voice_states.items()
                       if cid == str(channel_id) and uid != me)

        async def _leave_empty(guild_id: str) -> None:
            # The music stop clears the queue too; with no player it's a
            # plain disconnect. Either closes voice chat with the session.
            if player_mgr is not None:
                await player_mgr.stop(guild_id)
            else:
                await voice.disconnect(guild_id)

        empty_watch = EmptyChannelWatch(voice, occupants=_occupants,
                                        leave=_leave_empty)

    voicechat = None
    if voice is not None and config.VOICE_CHAT:
        #: Logged once: a speaker we can't tie to a member is not answered.
        _unknown_speakers: set[str] = set()

        async def _voice_reply(guild_id: str, channel_id: str,
                               user_id: str, text: str, as_text: bool):
            """One heard utterance, handled like a message addressed to him.

            The dispatcher first, flagged as spoken, so everyday commands
            work by voice and a "yes" answers a pending confirmation,
            while staff, owner-only and destructive commands are refused
            (risk.assess). Anything else is conversation through the same
            ``_converse`` as text.

            ``as_text``: they asked for it in the chat, so it's written
            and posted like any reply. Otherwise it's recorded and
            returned to be spoken.
            """
            if not feature_allowlist.allowed(guild_id, "voice"):
                return None
            if settings.is_silenced(guild_id, channel_id) or \
                    settings.mode(guild_id) == "off":
                logger.info("voice: not answering in %s (silenced or off)",
                            channel_id)
                return None
            member = None
            if user_id.isdigit():
                try:
                    member = await adapter.fetch_member(guild_id, user_id)
                except Exception:
                    logger.debug("voice member fetch failed", exc_info=True)
            if member is None:
                # No guessing who spoke: speaker attribution is the main
                # lesson of the text bot, and permissions hang off it.
                if user_id not in _unknown_speakers:
                    _unknown_speakers.add(user_id)
                    logger.warning(
                        "voice: can't match speaker %r to a member of %s; "
                        "not answering them (check the participant "
                        "identity format logged at join)", user_id, guild_id)
                return None
            if member.is_bot:
                return None
            speaker = member.display_name or member.id
            guild = await adapter.guild_info(guild_id)
            session = voice.get(guild_id)
            channel_name = session.channel_name if session else ""
            conversation.record(channel_id, speaker, text, is_bot=False,
                                user_id=member.id)
            presence.mark_dirty(guild_id, channel_id)

            executor.extras["channel_index"] = \
                await adapter.guild_channel_index(guild_id)

            # "<name>, yes." must still confirm: the yes/no matcher wants
            # the bare word, and in voice he's addressed by name.
            result = await dispatcher.handle(
                strip_name(text, VOICE_NAME, loose=True), invoker=member,
                channel_id=channel_id, guild_id=guild_id,
                directly_invoked=True, spoken=True)
            if result.converse:
                if member.id in chatbans:
                    return None
                reply = await _converse(
                    text, member, speaker, channel_id, guild, None,
                    spoken=not as_text, channel_name=channel_name,
                    set_aside=_set_aside(result))
            elif result.decision is Decision.EXECUTE or \
                    result.undo is not None:
                async with adapter.typing(channel_id):
                    reply = await executor.run(
                        result, invoker=member,
                        channel=Channel(id=channel_id, name=channel_name,
                                        guild_id=guild_id),
                        guild=guild, raw_text=text)
            else:
                reply = result.message

            if not reply:
                return None
            if as_text:
                await _post_reply(channel_id, guild_id, reply,
                                  user_id=member.id, speaker=speaker,
                                  guild_name=guild.name if guild else "")
            else:
                conversation.record(channel_id, conversation.bot_name,
                                    reply, is_bot=True)
                presence.mark_spoke(guild_id, channel_id)
            # A command that ended voice chat ("leave") returns its reply
            # after the listener closed, so there's no one to say it.
            how = ("posted" if as_text
                   else "spoken" if voicechat.listener(guild_id) is not None
                   else "not spoken: voice chat ended")
            logger.info("voice: answered %s in %s (%s, %d chars)", speaker,
                        channel_id, how, len(reply))
            return reply

        async def _voice_greeting(channel_name: str) -> Optional[str]:
            """One fresh spoken hello for joining a voice channel.

            Started *before* the join, so the model call overlaps it. None
            when it doesn't produce one; the caller has stock lines.
            """
            c = llm.constants
            prompt = (
                f"You've just joined the voice channel \"{channel_name}\" "
                "and are saying hello out loud. One short, casual spoken "
                "line in your own voice, under twelve words. Don't explain "
                "how to talk to you or mention listening, privacy or "
                "commands: that's already in the chat. No emoji.")
            try:
                result = await llm.call(
                    model=c.MODEL_MAIN,
                    system=conversation.standalone_system(),
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1200)
            except Exception:
                logger.debug("voice greeting failed", exc_info=True)
                return None
            line = (result.text or "").strip().strip('"').strip()
            return line if 0 < len(line) <= 160 else None

        async def _voice_post(channel_id: str, text: str) -> None:
            await adapter.send(channel_id, Response(content=text))

        voicechat = VoiceChat(
            voice,
            VoiceStudio(StudioConfig(
                url=config.VOICESTUDIO_URL,
                api_key=config.VOICESTUDIO_API_KEY,
                voice=config.VOICESTUDIO_VOICE,
                tts_model=config.VOICESTUDIO_TTS_MODEL,
                language=config.VOICESTUDIO_LANGUAGE,
                steps=config.VOICESTUDIO_STEPS,
                vocabulary=f"{VOICE_NAME}.",
                stream_stt=config.VOICE_STREAM_STT,
                stt_model=config.VOICESTUDIO_STT_MODEL)),
            bot_name=VOICE_NAME, reply=_voice_reply, post=_voice_post,
            allowed=lambda gid: feature_allowlist.allowed(gid, "voice"),
            opted_out=voice_optout, fx=config.VOICE_FX,
            caws=config.CAW_SOUNDS, caw_chance=config.CAW_CHANCE,
            barge_in=config.VOICE_BARGE_IN)

    # GUILD_CREATE carries a voice_states snapshot that fluxer.py drops on
    # the floor, and there is no REST endpoint for it. Without seeding from
    # it, anyone already sitting in a channel before the bot started is
    # invisible and he asks "which voice channel?" pointlessly.
    _inner_dispatch = bot._dispatch
    #: Each guild's voice connection id, which fluxer.py's models drop and
    #: streaming needs (corbin/core/video.py).
    connection_ids = ConnectionIds(
        lambda: str(getattr(getattr(bot, "user", None), "id", "") or ""))

    async def _dispatch_with_voice(event_name, data):
        if event_name in ("VOICE_SERVER_UPDATE", "VOICE_STATE_UPDATE"):
            try:
                connection_ids.observe(event_name, data)
            except Exception:
                logger.debug("connection id capture failed", exc_info=True)
        # The bot's own status does not render on Fluxer even though the
        # gateway accepts the payload. fluxer.py models no presence at
        # all, so there is no reference for the shape Fluxer actually
        # uses -- log the first few inbound ones and read it off a real
        # user who has a status set.
        if event_name == "PRESENCE_UPDATE" and _presence_samples[0] < 3:
            _presence_samples[0] += 1
            logger.info("PRESENCE_UPDATE sample %d: %r",
                        _presence_samples[0], data)

        if event_name == "GUILD_MEMBER_UPDATE" and isinstance(data, dict):
            asyncio.create_task(_guard_dm_roles(data))

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

    from corbin.core import video as _video_mod

    _video_mod.TIMER = config.STREAM_TIMER
    streams = None
    if voice is not None:
        # One stream across every server, and no voice chat while it runs.
        # See corbin/core/video.py.
        streams = StreamManager(
            voice, connection_ids, send_voice_state=adapter.send_voice_state,
            voicechat=voicechat,
            allowed=lambda gid: feature_allowlist.allowed(gid, "streaming"))
        if voicechat is not None:
            voicechat.streaming_in = streams.streaming_in

        async def _stream_ended(stream, reason: str) -> None:
            # Ended on its own (the video ran out, the feed broke): say so
            # in the voice channel's chat, not silently drop the tile.
            await adapter.send(stream.channel_id, Response(
                content=f"📺 The stream ended: {reason}."))

        streams.on_ended = _stream_ended

    # Live TV catalogue: iptv-org, refreshed daily, every
    # stream health-checked in the background. Paused while a stream is
    # on air; idle while no server has streaming allowed.
    iptv = None
    if config.IPTV_ENABLED and streams is not None:
        from corbin.core.iptv import IptvCatalogue

        iptv = IptvCatalogue(
            config.DATA_ROOT,
            busy=lambda: streams.streaming_in() is not None,
            wanted=lambda: any("streaming" in f for f in
                               feature_allowlist.entries().values()))

    # Each server's own TV rules and stream quality; the owner's
    # allowlist above still decides whether a server streams at all.
    from corbin.core.tv_rules import TVRules

    tv_rules = TVRules(settings, allow_1080p=config.ALLOW_1080P,
                       default_max_minutes=config.TV_MAX_MINUTES,
                       default_quality=config.STREAM_QUALITY)
    conversation.tv_enabled = tv_rules.enabled
    tv_guide = None
    if iptv is not None:
        from corbin.core.epg import Guide

        tv_guide = Guide()

    music_video = None
    if streams is not None and player_mgr is not None:
        from corbin.core.music_video import MusicVideo

        async def _video_notice(channel_id: str, text: str) -> None:
            await adapter.send(channel_id, Response(content=text))

        # The video follows the queue: each new track's video is swapped
        # in, and it ends with the music.
        music_video = MusicVideo(streams, player_mgr, voice, extractor,
                                 notify=_video_notice,
                                 quality=tv_rules.quality)
        player_mgr.on_track_started.append(music_video.track_started)
        player_mgr.on_queue_end.append(music_video.queue_ended)

    # Watching live TV: one ffmpeg per channel, sound through
    # the player path, picture through the stream manager.
    tv = None
    if iptv is not None:
        from corbin.core.tv import TV

        async def _tv_notice(channel_id: str, text: str) -> None:
            await adapter.send(channel_id, Response(content=text))

        tv = TV(iptv, streams, voice, player=player_mgr,
                music_video=music_video, notify=_tv_notice, rules=tv_rules)

    # The local video library (corbin/core/video_player.py): episodes from
    # VIDEO_ROOT, through the same stream manager as TV.
    videos = None
    if streams is not None:
        from corbin.core.video_player import VideoPlayer

        async def _video_notice(channel_id: str, text: str) -> None:
            await adapter.send(channel_id, Response(content=text))

        videos = VideoPlayer(config.VIDEO_ROOT, streams, voice,
                             settings=settings, rules=tv_rules,
                             player=player_mgr, tv=tv,
                             notify=_video_notice)
        logger.info("video library: %s (%d show(s))", config.VIDEO_ROOT,
                    len(videos.shows()))
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
    #: (channel, user) -> (guild, target channel, deadline): "presence or
    #: silence?" waiting on an answer. See _answer_silence_question.
    silence_questions: dict = {}
    #: (channel, user) -> {guild, target, message, deadline}: "which roles
    #: and emoji?" waiting on an answer. See _answer_reaction_roles.
    reaction_role_questions: dict = {}
    #: (channel, user) -> {guild, stage, channel, make, here, deadline}:
    #: "enable ranking" asking where level-ups go and which roles.
    #: See _answer_ranking_setup.
    ranking_questions: dict = {}
    #: (dm channel, user) -> {guild, guild_name, deadline}: "how long
    #: before each event?" after a 🔔 on a calendar. See
    #: _answer_event_question.
    event_questions: dict = {}
    event_store = events_mod.EventStore(settings)
    #: (channel, user) -> {guild, items, deadline}: "what timezone is
    #: that in?" from events_add. See _answer_event_timezone.
    event_tz_questions: dict = {}
    reaction_roles = ReactionRoleStore(settings.store)
    def _count_command(spec, ctx) -> None:
        """The digest's "Commands run" (Executor.on_command)."""
        guild = getattr(ctx, "guild", None)
        digest_store.record(
            "command", user_id=str(ctx.invoker.id),
            user_name=ctx.invoker.display_name or "",
            guild_id=str(ctx.guild_id) if ctx.guild_id else None,
            guild_name=getattr(guild, "name", "") or "")

    executor.on_command = _count_command
    executor.extras = {"settings": settings, "ranking": ranking,
                       "llm": llm,
                       "memory": memory, "warnings": warnings_store,
                       "bans": bans, "chatbans": chatbans,
                       "moon": moon_calc, "reminders": reminders_store,
                       "gill": gill, "digest": digest_store,
                       "voice": voice, "voice_states": voice_states,
                       "voicechat": voicechat,
                       "streams": streams,
                       "music_video": music_video,
                       "iptv": iptv, "tv": tv, "videos": videos,
                       "tv_rules": tv_rules, "tv_guide": tv_guide,
                       "voice_greeter": (_voice_greeting if voicechat
                                         is not None else None),
                       "features": feature_allowlist,
                       "voice_optout": voice_optout,
                       "memory_optout": memory_optout,
                       "conversation": conversation,
                       "player": player_mgr, "extractor": extractor,
                       "log_channel": config.LOG_CHANNEL_ID,
                       "general_questions": general_questions,
                       "silence_questions": silence_questions,
                       "reaction_roles": reaction_roles,
                       "reaction_role_questions": reaction_role_questions,
                       "ranking_questions": ranking_questions,
                       "events": event_store,
                       "event_tz_questions": event_tz_questions,
                       "rank_roles": rank_roles}

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

        for gid in settings.guilds_with("hourly_animals"):
            if settings.scheduled_marker(gid, "hourly_animals") == marker:
                continue
            cfg = settings.hourly_animals(gid)
            key, label = pick_animal(cfg.get("animal"))
            # Uploads the bytes rather than linking: see fetch_animal.
            post = await animal_post(key, f"Here is your Hourly {label}.")
            if post is None:
                # Not marked sent, so the next tick tries again rather
                # than skipping the hour on one bad fetch.
                logger.info("tinyfox gave nothing for %s; will retry", key)
                continue
            try:
                await adapter.send(cfg["channel_id"], post)
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
        # Credit voice sessions still running, so today gets its share.
        if voice is not None:
            digest_store.record_voice(voice.collect_voice_time())
        posted = digest_store.snapshot_and_reset(marker)
        try:
            await adapter.send(config.LOG_CHANNEL_ID, Response(
                embed=render(posted, "overview",
                             title=f"{config.BOT_NAME} — daily digest for {marker}")))
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
    if empty_watch is not None:
        async def _empty_voice_job() -> None:
            empty_watch.sweep()

        scheduler.add("empty_voice", _empty_voice_job)
    scheduler.add("digest", _digest_job)
    scheduler.add("daily_quote", _daily_quote_job)
    scheduler.add("hourly_animals", _hourly_animal_job)
    scheduler.add("weather_alerts", _weather_alerts_job)

    async def _events_job() -> None:
        """Event reminders by DM, and past events off the calendars."""
        for gid in event_store.guilds():
            try:
                due = event_store.due(gid)
                pruned = event_store.prune(gid)
                name = ""
                if due or pruned:
                    info = await adapter.guild_info(gid)
                    name = getattr(info, "name", "") or ""
                for ev, uid, offset in due:
                    text = events_mod.reminder_text(ev, offset,
                                                    name or "a server")
                    if await adapter.send_dm(uid, Response(content=text)):
                        logger.info("events: reminded %s of #%s in %s",
                                    uid, ev["id"], gid)
                    else:
                        logger.warning("events: reminder DM to %s for #%s "
                                       "in %s failed", uid, ev["id"], gid)
                if pruned:
                    await event_cmds.refresh_calendar(adapter, event_store,
                                                      gid, name)
            except Exception:
                logger.exception("events job failed for %s", gid)

    scheduler.add("events", _events_job)

    # -- banner rotation (corbin/core/banner.py) ---------------------------
    from corbin.core import banner as banner_mod

    banner_store = banner_mod.BannerSettings(settings)
    #: Guilds mid-rotation: "change the banner now" racing the job must
    #: not download, repost and delete the same image twice.
    banner_busy: set = set()

    async def _banner_download(url: str) -> Optional[bytes]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0,
                                         follow_redirects=True) as client:
                resp = await client.get(url)
        except Exception as exc:
            logger.info("banner download failed: %s", type(exc).__name__)
            return None
        if resp.status_code != 200 or \
                len(resp.content) > 4 * banner_mod.MAX_BYTES:
            return None
        return resp.content

    async def _banner_post(channel_id: str, content: str,
                           files: list) -> Optional[str]:
        return await adapter.send(channel_id, Response(
            content=content, files=tuple(files)))

    async def _banner_rotate(guild_id: str) -> "banner_mod.Result":
        gid = str(guild_id)
        if gid in banner_busy:
            return banner_mod.Result(False, "I'm already changing the "
                                            "banner; give it a moment.")
        banner_busy.add(gid)
        try:
            me = getattr(bot, "user", None)
            return await banner_mod.rotate(banner_store, gid, banner_mod.Hooks(
                history=adapter.channel_history, download=_banner_download,
                post=_banner_post, delete=adapter.delete_message_strict,
                set_banner=adapter.set_guild_banner,
                bot_id=str(getattr(me, "id", "") or "")))
        except Exception as exc:
            logger.warning("banner rotation failed in %s", gid,
                           exc_info=True)
            return banner_mod.Result(False, f"That didn't work: {exc}")
        finally:
            banner_busy.discard(gid)

    executor.extras["banner_rotate"] = _banner_rotate

    async def _banner_job() -> None:
        import time as _t

        now = _t.time()
        for gid in banner_store.guilds():
            if banner_store.due(gid, now):
                await _banner_rotate(gid)

    scheduler.add("banner_rotation", _banner_job)

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
        """Organic chime-in is opt-in: it needs mode=global for the guild.
        Musings go through here too."""
        if settings.is_silenced(guild_id, channel_id) or \
                settings.is_presence_off(guild_id, channel_id):
            return False
        return settings.mode(guild_id) == "global"

    async def _presence_send(channel_id: str, text: str,
                             target: Optional[dict]) -> None:
        conversation.record(channel_id, conversation.bot_name, text,
                            is_bot=True)
        for chunk in split_message(text):
            sent_id = await adapter.send(channel_id, Response(content=chunk))
            # Presence lines are his too -- a reply to one is a
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

    # -- reaction controls ---------------------------------------------------
    # Fluxer has no buttons yet, so embeds that need them carry emoji
    # reactions instead (core/reactions.py). adapter.send attaches them;
    # the handlers below act on a press.
    panels = ReactionPanels(adapter)
    adapter.panels = panels
    executor.extras["panels"] = panels

    def _me() -> str:
        return str(getattr(getattr(bot, "user", None), "id", "") or "")

    async def _display_name(gid, user_id: str) -> str:
        try:
            member = await adapter.fetch_member(gid, user_id) if gid else None
        except Exception:
            member = None
        return (getattr(member, "display_name", None) or "someone")

    async def _verse_press(panel, message_id: str, emoji: str,
                           user_id: str) -> None:
        from corbin.commands.verse_cmds import (BUTTON_TRANSLATION,
                                                build_verse_embed)

        want = BUTTON_TRANSLATION.get(emoji)
        if not want or want == panel.state.get("translation"):
            return
        embed = await build_verse_embed(panel.state["ref"],
                                        translation=want, gill=gill)
        embed.controls = None   # already a panel; don't re-add reactions
        if await adapter.edit_message(panel.channel_id, message_id,
                                      Response(embed=embed)):
            panel.state["translation"] = want
            logger.info("verse panel %s switched to %s", message_id, want)

    async def _verse_resolver(channel_id: str, message_id: str, emoji: str):
        """Rebuild a verse panel the registry lost (a restart)."""
        from corbin.commands.verse_cmds import (BUTTON_TRANSLATION,
                                                VERSE_BUTTONS, ref_from_title)

        if normalise(emoji) not in {normalise(e) for e in BUTTON_TRANSLATION}:
            return None
        data = await adapter.fetch_message_raw(channel_id, message_id)
        if not data or str((data.get("author") or {}).get("id")) != _me():
            return None
        embed = (data.get("embeds") or [{}])[0] or {}
        ref = ref_from_title(embed.get("title") or "")
        if ref is None:
            return None
        footer = str((embed.get("footer") or {}).get("text") or "")
        current = ("commentary" if "commentary" in (embed.get("title") or "")
                   else "nasb" if "New American Standard" in footer
                   else "kjv")
        return Panel(kind="verse", channel_id=str(channel_id), guild_id=None,
                     emojis=tuple(e for e, _ in VERSE_BUTTONS),
                     state={"ref": ref, "translation": current})

    async def _music_press(panel, message_id: str, emoji: str,
                           user_id: str) -> None:
        from corbin.commands.music_cmds import (LOOP_CYCLE, loop_mode,
                                                track_embed, video_ok)

        gid = panel.guild_id
        session = voice.get(gid) if voice is not None else None
        player = player_mgr.get(gid) if player_mgr is not None else None
        if session is None or player is None or player.current is None:
            return
        # Only people listening get to drive it.
        if voice_states.get(str(user_id)) != str(session.channel_id):
            logger.info("music panel: %s pressed %s but isn't in the voice "
                        "channel; ignored", user_id, emoji)
            return
        name = await _display_name(gid, user_id)
        key = normalise(emoji)
        note = ""

        async def _close(title: str, footer: str) -> None:
            """Retire this card: its track is done, and so are its buttons."""
            done = track_embed(player.current, title=title)
            panels.drop(message_id)
            done.footer = footer
            await adapter.edit_message(panel.channel_id, message_id,
                                       Response(embed=done))

        if key == normalise("⏯️"):
            paused = player_mgr.toggle_pause(gid)
            if paused is None:
                return
            note = f"{'Paused' if paused else 'Resumed'} by {name}"
        elif key == normalise("⏮️"):
            await _refresh_card(gid, activity="⏮️ Starting from the "
                                               "beginning…")
            track = await player_mgr.restart(gid)
            if track is None:
                await _refresh_card(gid, note="Couldn't restart that one")
                return
            note = f"Restarted by {name}"
            logger.info("music panel: %s restarted %r", name, track.title)
        elif key == normalise("⏭️"):
            current = player.current
            await _close("Skipped", f"Skipped by {name}")
            await player_mgr.skip(gid)
            logger.info("music panel: %s skipped %r", name, current.title)
            return
        elif key == normalise("⏹️"):
            await _close("Stopped", f"Stopped by {name}")
            await player_mgr.stop(gid)
            logger.info("music panel: %s stopped playback", name)
            return
        elif key == normalise("🔁"):
            nxt = LOOP_CYCLE[(LOOP_CYCLE.index(loop_mode(player)) + 1)
                             % len(LOOP_CYCLE)]
            player.loop_track = nxt == "track"
            player.loop_queue = nxt == "queue"
            note = f"Loop {nxt} ({name})"
        elif key == normalise("🔀"):
            if len(player.queue) < 2:
                note = "Not enough queued to shuffle"
            else:
                player.shuffle()
                note = f"Shuffled {len(player.queue)} tracks ({name})"
        elif key == normalise("📺") and music_video is not None:
            note = await _toggle_video(gid, name)
        else:
            return
        await _refresh_card(gid, note=note)

    #: card message id -> its last "who did what", kept across the
    #: status-only refreshes so they don't wipe it. Per card, so the next
    #: song's card doesn't inherit "Paused by ..." from this one.
    card_notes: dict[str, str] = {}

    async def _refresh_card(gid: str, *, note: Optional[str] = None,
                            activity: str = "") -> None:
        """Re-render the guild's live Now Playing card in place: playing
        or paused, the video's state, anything in progress."""
        from corbin.commands.music_cmds import track_embed, video_ok

        found = panels.latest("music", gid)
        player = player_mgr.get(gid) if player_mgr is not None else None
        if found is None or player is None or player.current is None:
            return
        message_id, panel = found
        if note is not None:
            card_notes[str(message_id)] = note
            while len(card_notes) > 200:
                card_notes.pop(next(iter(card_notes)))
        embed = track_embed(
            player.current, guild_id=gid, player=player,
            note=card_notes.get(str(message_id), ""), activity=activity,
            video=video_ok(feature_allowlist, gid, player.current),
            status=(music_video.status_line(gid)
                    if music_video is not None else ""))
        embed.controls = None   # already a panel; don't re-add reactions
        try:
            await adapter.edit_message(panel.channel_id, message_id,
                                       Response(embed=embed))
        except Exception:
            logger.debug("card refresh failed", exc_info=True)

    if music_video is not None:
        music_video.on_status = lambda gid: _refresh_card(gid)

    async def _toggle_video(gid: str, name: str) -> str:
        """📺 on the Now Playing card: video on or off. The footer note."""
        from corbin.core.music_video import NoVideo
        from corbin.core.video import StreamBusy

        if music_video.active(gid):
            await music_video.stop(gid, f"turned off by {name}")
            logger.info("music panel: %s turned the video off", name)
            return f"Video turned off by {name}"
        try:
            await music_video.start(gid)
        except (NoVideo, StreamBusy, PermissionError, RuntimeError) as exc:
            logger.info("music panel: video for %s refused: %s", name, exc)
            return f"No video: {exc}"
        except Exception as exc:
            logger.warning("music panel: video failed", exc_info=True)
            return f"No video: {exc}"
        logger.info("music panel: %s turned the video on", name)
        # Just who: the card's status line already says "Video on: click
        # Corbin's stream tile to watch", and repeating it doubled the
        # line on the card.
        return f"Video turned on by {name}"

    async def _tv_press(panel, message_id: str, emoji: str,
                        user_id: str) -> None:
        """⬅️ / ➡️ on a live TV list: the page before or after, edited in
        place, re-rendered from the cached catalogue."""
        from corbin.commands.tv_cmds import PAGE_STEP, permit, render

        step = PAGE_STEP.get(emoji)
        if not step or iptv is None:
            return
        state = dict(panel.state)
        before = int(state.get("page") or 1)
        state["page"] = before + step
        # The server's category limits apply to a page turn too.
        embed = render(iptv, state,      # clamps state["page"]
                       permit(tv_rules, state.get("guild_id")),
                       tv_guide)
        if state["page"] == before:
            return                      # already on the first/last page
        embed.controls = None   # already a panel; don't re-add reactions
        if await adapter.edit_message(panel.channel_id, message_id,
                                      Response(embed=embed)):
            panel.state["page"] = state["page"]

    panels.on("verse", _verse_press)
    panels.resolver(_verse_resolver)
    panels.on("music", _music_press)
    panels.on("tv_list", _tv_press)

    async def _video_list_press(panel, message_id: str, emoji: str,
                                user_id: str) -> None:
        """⬅️ / ➡️ on an episode list, edited in place."""
        from corbin.commands.video_cmds import render as render_videos
        from corbin.commands.tv_cmds import PAGE_STEP

        step = PAGE_STEP.get(emoji)
        if not step or videos is None:
            return
        state = dict(panel.state)
        before = int(state.get("page") or 1)
        state["page"] = before + step
        embed = render_videos(videos, state)
        if state["page"] == before:
            return
        embed.controls = None
        if await adapter.edit_message(panel.channel_id, message_id,
                                      Response(embed=embed)):
            panel.state["page"] = state["page"]

    async def _video_card_press(panel, message_id: str, emoji: str,
                                user_id: str) -> None:
        """⏯️ ⏭️ ⏹️ on the episode card, for people in the channel."""
        from corbin.commands.video_cmds import now_playing as video_card

        gid = panel.guild_id or panel.state.get("guild_id")
        p = videos.playing(gid) if videos is not None else None
        if p is None:
            return
        if voice_states.get(str(user_id)) != str(p.voice_channel_id):
            logger.info("video card: %s pressed %s but isn't in the voice "
                        "channel; ignored", user_id, emoji)
            return
        name = await _display_name(gid, user_id)
        key = normalise(emoji)
        session = voice.get(gid) if voice is not None else None
        where = getattr(session, "channel_name", "") or "the voice channel"
        if key == normalise("⏯️"):
            paused = videos.pause(gid)
            if paused is None:
                return
            embed = video_card(p, where)
            embed.footer = f"{'Paused' if paused else 'Resumed'} by {name}"
        elif key == normalise("⏭️"):
            nxt = await videos.step(gid, 1)
            if nxt is None:
                return
            embed = video_card(nxt, where)
            embed.footer = f"Skipped by {name}"
        elif key == normalise("⏹️"):
            await videos.stop(gid, f"stopped by {name}")
            panels.drop(message_id)
            embed = video_card(p, where)
            embed.controls = None
            embed.footer = f"Stopped by {name}"
            await adapter.edit_message(panel.channel_id, message_id,
                                       Response(embed=embed))
            return
        else:
            return
        embed.controls = None       # already a panel
        await adapter.edit_message(panel.channel_id, message_id,
                                   Response(embed=embed))

    panels.on("video_list", _video_list_press)
    panels.on("video", _video_card_press)

    reaction_events_seen: list = []

    async def _on_reaction(raw, added: bool) -> None:
        try:
            if not reaction_events_seen:
                # Reaction delivery was never verified live. One line
                # proves the gateway sends them at all.
                reaction_events_seen.append(True)
                logger.info("first reaction event received (%r on %s)",  # log-ok: an emoji
                            getattr(getattr(raw, "emoji", None), "unicode",
                                    None), getattr(raw, "message_id", "?"))
            # Before the emoji filter below: that drops custom emoji,
            # which are the usual choice for reaction roles. Any reaction
            # on a reaction-roles message stops there.
            if await _reaction_role_event(raw, added):
                return
            emoji = reaction_emoji(getattr(raw, "emoji", None))
            if not emoji:
                return
            channel_id = str(raw.channel_id)
            message_id = str(raw.message_id)
            user_id = str(raw.user_id)
            if user_id == _me():
                return
            # A calendar card: 🔔 subscribes, anything else is ignored.
            event_guild = event_store.find_calendar(
                message_id, str(raw.guild_id) if raw.guild_id else None)
            if event_guild is not None:
                if normalise(emoji) == normalise(events_mod.BELL):
                    info = await adapter.guild_info(event_guild)
                    await event_cmds.bell(
                        adapter, event_store, event_questions,
                        guild_id=event_guild,
                        guild_name=getattr(info, "name", "") or "",
                        user_id=user_id, added=added)
                return
            await panels.press(message_id=message_id, channel_id=channel_id,
                               user_id=user_id, emoji=emoji, added=added,
                               bot_user_id=_me())
        except Exception:
            logger.exception("reaction handling failed")

    @bot.event
    async def on_raw_reaction_add(raw) -> None:  # type: ignore[misc]
        await _on_reaction(raw, True)

    @bot.event
    async def on_raw_reaction_remove(raw) -> None:  # type: ignore[misc]
        await _on_reaction(raw, False)

    # The persona plus the "you are on Fluxer" block: without the block
    # he could welcome someone to "the Discord server".
    greeter.soul = conversation.standalone_system()

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
        # Before any stream can start: a broken drawtext must turn the
        # timer off, not stop every video from starting.
        from corbin.core.video import check_timer
        from corbin.core.loopwatch import WATCH

        if streams is not None:
            await check_timer()
        else:
            logger.info("streaming: off (needs the voice extra: "
                        "pip install 'fluxer.py[voice]', and ffmpeg)")
        if player_mgr is not None:
            # YouTube streams through byte ranges (corbin/core/relay.py):
            # one long download is throttled to about playback speed.
            from corbin.core.relay import RELAY

            await RELAY.start()
        # Logs what's blocking the event loop when it stalls (see
        # corbin/core/loopwatch.py): the audio of a stream needs it.
        WATCH.start()
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
        if voicechat is not None:
            # VoiceStudio is a separate app, often not running. Say so at
            # boot, not at the first "talk with me"; it's re-checked
            # whenever voice chat starts, so starting it later needs no
            # restart.
            if await voicechat.studio.probe():
                logger.info("voice chat: ready (VoiceStudio at %s)",
                            config.VOICESTUDIO_URL)
            else:
                logger.warning("voice chat: unavailable until VoiceStudio "
                               "is reachable (%s: %s)",
                               config.VOICESTUDIO_URL,
                               voicechat.studio.last_error)
            servers = feature_allowlist.entries()
            logger.info("allowlist: voice on in %d server(s)",
                        sum("voice" in f for f in servers.values()))
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
        if iptv is not None:
            iptv.start()

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
        # Usually no heartbeat yet at boot: say so, and fill it in below.
        embed.add_field("Bot latency",
                        human_latency(gateway_ping.latency)
                        if gateway_ping.latency is not None
                        else "measuring…",
                        inline=True)
        embed.add_field("API latency", human_latency(api), inline=True)
        embed.add_field(
            "Model",
            f"{model_name} — {'up' if model_up else 'NOT RESPONDING'}",
            inline=True)
        if voicechat is not None:
            embed.add_field(
                "Voice",
                ("VoiceStudio not reachable"
                 if not voicechat.studio.available
                 else f"Voice profile '{config.VOICESTUDIO_VOICE}' not found"
                 if voicechat.studio.voice_found is False else "Ready"),
                inline=True)

        try:
            sent = await adapter.send(config.LOG_CHANNEL_ID,
                                      Response(embed=embed))
        except Exception:
            logger.warning("could not post the startup notice to %s",
                           config.LOG_CHANNEL_ID, exc_info=True)
            return
        if sent and gateway_ping.latency is None:
            task = asyncio.create_task(_fill_bot_latency(embed, sent))
            _background.add(task)
            task.add_done_callback(_background.discard)

    #: Keeps fire-and-forget tasks referenced until they finish.
    _background: set = set()

    async def _fill_bot_latency(embed, message_id: str) -> None:
        """Edit the boot notice once the first heartbeat is timed."""
        latency = await gateway_ping.wait(FIRST_BEAT_WAIT)
        for item in embed.fields:
            if item.name == "Bot latency":
                item.value = human_latency(latency)
        try:
            await adapter.edit_message(config.LOG_CHANNEL_ID, message_id,
                                       Response(embed=embed))
        except Exception:
            logger.warning("could not update the startup notice's latency",
                           exc_info=True)

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
        # Any human traffic counts as the bot being awake; the fallback
        # status shows idle once the server has been quiet for a while.
        status.last_active = _monotonic()
        message = to_message(raw)
        me_id = str(getattr(getattr(bot, "user", None), "id", "") or "")

        if message.author.id == me_id:
            # Cache his own echo before ignoring it. Progress notes and
            # post-and-delete replies never went through
            # _remember_own_message, so deleting one logged "Message
            # deleted ... predates my cache" on every search.
            _remember_own_message(message.id, channel_id=message.channel.id,
                                  content=message.content or "")
            return
        if message.author.is_bot:
            return
        if message.guild_id is None and not message.channel.is_dm \
                and await adapter.channel_is_dm(message.channel.id):
            # An uncached DM channel arrives looking like a guild channel
            # that didn't mention him, and was dropped without a word.
            # Fixed here, once, so every later is_dm check sees it.
            message = dataclasses.replace(
                message, channel=dataclasses.replace(message.channel,
                                                     is_dm=True))

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
        # The server nickname wins, as in Fluxer's own UI; the message
        # only carries the account name.
        speaker = await adapter.member_nick(hp_guild, message.author.id) \
            or speaker
        conversation.record(channel_id, speaker, content,
                            is_bot=False, message_id=message.id,
                            user_id=message.author.id,
                            media=media_labels(message),
                            previews=link_previews.preview_texts(
                                link_previews.embeds_of(raw)))

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

        # Silenced (or mode off) still lets the way back through: a
        # message that could be unsilence/chat_mode goes on to the
        # dispatcher, and anything it routes elsewhere is dropped below.
        # Without this, unsilencing a channel from inside it was impossible.
        quiet_here = (settings.is_silenced(gid, channel_id)
                      or settings.mode(gid) == "off")
        # By name counts as addressing him here: "<name>, unsilence" with
        # no @mention was dropped as not-for-him, which in a channel he
        # ignores is indistinguishable from the command being broken.
        named = bool(re.search(rf"\b{re.escape(VOICE_NAME)}\b", content or "",
                               re.IGNORECASE))
        if quiet_here:
            if await _unsilence_directly(message, raw, gid, channel_id,
                                         content, chan_map, me_id, named):
                return
            pending = dispatcher.store.peek(channel_id, message.author.id)
            if not (QUIET_ESCAPE.search(content or "")
                    or (pending is not None
                        and works_while_quiet(pending.command))):
                if named or (me_id and me_id in message.content):
                    logger.info("quiet in %s: ignoring a message to me "
                                "(no unsilence/mode wording)", channel_id)
                return

        if await _answer_general_question(gid, channel_id, message.author.id,
                                          content, chan_map):
            return
        if await _answer_silence_question(gid, channel_id, message.author.id,
                                          content):
            return
        if await _answer_reaction_roles(gid, channel_id, message, raw):
            return
        if await _answer_ranking_setup(gid, channel_id, message, raw,
                                       content, chan_map):
            return
        if gid is None and await _answer_event_question(channel_id, message,
                                                         content):
            return
        if gid is not None and await _answer_event_timezone(
                gid, channel_id, message, content):
            return

        addressed = bool(me_id and me_id in message.content)
        # A reply only counts when it is a reply to HIM. Treating
        # every reply as an invocation meant two people replying to each
        # other pulled him into the middle of it.
        replying_to_corbin = await _reply_is_to_bot(message, channel_id,
                                                    me_id)
        ai_channel = settings.is_ai_channel(gid, channel_id)

        directly_invoked = (addressed or replying_to_corbin
                            or message.channel.is_dm or ai_channel
                            or (quiet_here and named))
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

        # Start typing now, before the command classifier. It is a model
        # call, and typing used to begin only after it, so he looked idle for several seconds after every
        # message. It ends before the reply is posted (a refresh after the
        # send would leave him "typing" for nothing). Skipped where a reply
        # is unlikely: a quiet channel only lets commands through, and a
        # chatbanned author only gets command replies, which type below.
        if quiet_here or message.author.id in chatbans:
            early_typing = contextlib.AsyncExitStack()
        else:
            early_typing = adapter.typing(channel_id)
        async with early_typing:
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

            if (gid and events_mod.match_add(content)
                    and event_store.get(str(gid)).get("channel_id")):
                # "Add these events to the calendar:" and a list: a
                # classifier can score a long list under the floor and
                # send it to chat. Permissions still apply.
                logger.info("events: add request matched directly in %s",
                            channel_id)
                result = dispatcher.direct("events-add", {}, invoker=member,
                                           guild_id=gid)
            else:
                result = await dispatcher.handle(
                    content, invoker=member, channel_id=channel_id,
                    guild_id=gid, directly_invoked=True,
                    mention_ids=mention_ids,
                )
            if quiet_here and not (result.spec is not None
                                   and works_while_quiet(result.spec.name)):
                if result.decision is Decision.CONFIRM:
                    dispatcher.store.drop(channel_id, member.id)
                logger.info("quiet in %s: ignoring %s (%s)", channel_id,
                            result.spec.name if result.spec else "chat",
                            result.message)
                return

            # Chatbanned people keep full use of commands; he just stops
            # chatting with them.
            if result.converse and message.author.id in chatbans:
                return

            if result.converse:
                reply = await _converse(content, member, speaker, channel_id,
                                        guild, message,
                                        set_aside=_set_aside(result))
                # The classifier guessed at a command but wasn't sure:
                # answer the message, then name the command it might be.
                if reply and result.suggest is not None:
                    hint = command_help.did_you_mean(
                        result.suggest, member, in_guild=guild is not None,
                        guild_id=str(guild.id) if guild else None)
                    if hint:
                        reply = f"{reply}\n\n{hint}"
                        logger.info("suggested %s after the reply",
                                    result.suggest.name)
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
        await _post_reply(channel_id, gid, reply, user_id=member.id,
                          speaker=speaker,
                          guild_name=guild.name if guild else "")

    #: (channel, user) -> {"at", "files", "originals"}: documents someone
    #: attached, kept DOCUMENT_MEMORY seconds for follow-up edits.
    recent_documents: dict = {}
    DOCUMENT_MEMORY = 3600.0

    def _remember_documents(key, files, originals) -> None:
        now = _monotonic()
        for k in [k for k, v in recent_documents.items()
                  if now - v["at"] > DOCUMENT_MEMORY]:
            recent_documents.pop(k, None)
        recent_documents[key] = {"at": now, "files": list(files),
                                 "originals": dict(originals)}
        while len(recent_documents) > 200:
            recent_documents.pop(next(iter(recent_documents)))

    def _recent_documents(key):
        entry = recent_documents.get(key)
        if entry is None or _monotonic() - entry["at"] > DOCUMENT_MEMORY:
            return None
        return entry

    async def _post_reply(channel_id: str, gid, reply: str, *,
                          user_id: str, speaker: str,
                          guild_name: str = "",
                          reply_to: Optional[str] = None) -> None:
        """Send a conversational reply and do the bookkeeping around it.

        ``reply_to`` threads the first chunk onto that message.
        """
        conversation.record(channel_id, conversation.bot_name, reply,
                            is_bot=True)
        # Both paths share the cooldown, so the loop will not also answer
        # a channel he just replied in.
        presence.mark_spoke(gid, channel_id)
        digest_store.record("message", user_id=user_id, user_name=speaker,
                            guild_id=gid, guild_name=guild_name)
        # Long code goes up as a file. History above keeps the inline
        # version, so he still knows what he sent.
        # Documents first: a ```file:``` block is not code (doc_output).
        body, code_files = reply, []
        if adapter.capabilities().file_upload:
            if doc_output.has_blocks(body):
                earlier = _recent_documents((str(channel_id), str(user_id)))
                body, doc_files = await asyncio.to_thread(
                    doc_output.extract, body,
                    earlier["originals"] if earlier else {})
                code_files.extend(doc_files)
            body, more = extract_code_files(body)
            code_files.extend(more)
        chunks = split_message(body)
        sent_ids = []
        for i, chunk in enumerate(chunks):
            files = tuple(code_files) if i == len(chunks) - 1 else ()
            try:
                sent_id = await adapter.send(channel_id, Response(
                    content=chunk, files=files,
                    reply_to=reply_to if i == 0 else None))
            except Exception as exc:
                if not (isinstance(exc, PermissionError)
                        or is_missing_permissions(exc)):
                    raise
                # A channel it can read but not post in. One warning
                # naming it, not an on_message traceback.
                logger.warning("can't answer %s in %s: %s (give my role "
                               "Send Messages there)", speaker, channel_id,
                               exc)
                return
            _remember_own_message(sent_id, channel_id, chunk)
            sent_ids.append(sent_id)
        # The success path used to log nothing, so "generated a reply but
        # it never showed up" was indistinguishable from "never replied".
        if chunks and all(sent_ids):
            logger.info("replied to %s in %s (%d chars, %d message(s): %s) "
                        "| sent=%s", speaker, channel_id, len(reply),
                        len(chunks), ",".join(map(str, sent_ids)),
                        said(reply, 120))
        else:
            logger.warning("reply to %s in %s did not post: %d chunk(s), "
                           "ids=%s | text=%s", speaker, channel_id,
                           len(chunks), sent_ids, said(reply, 200))
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

    async def _answer_reaction_roles(gid, channel_id: str, message,
                                     raw) -> bool:
        """Take a reply to "which roles and emoji?" from ``reaction_roles``.

        Same rules as the general-chat question. Parsed from the RAW
        content: the readable copy has custom emoji flattened to
        ``:name:``, and their ids are what the reactions need.
        """
        if (str(channel_id), str(message.author.id)) not in \
                reaction_role_questions:
            return False
        member, _guild = await _member_for(gid, raw, message.author.id)
        reply = await reaction_role_cmds.answer(
            reaction_role_questions, reaction_roles, adapter, guild_id=gid,
            channel_id=channel_id, member=member,
            raw_content=message.content or "")
        if reply is None:
            return False
        conversation.record(channel_id, conversation.bot_name, reply,
                            is_bot=True)
        presence.mark_spoke(gid, channel_id)
        sent_id = await adapter.send(channel_id, Response(content=reply))
        _remember_own_message(sent_id, channel_id, reply)
        return True

    async def _answer_event_question(channel_id: str, message,
                                     content: str) -> bool:
        """Take a DM answer to "how long before each event?" (asked
        after a 🔔 on a calendar)."""
        reply = event_cmds.answer_reminder_question(
            event_questions, event_store, channel_id=channel_id,
            user_id=str(message.author.id), content=content)
        if reply is None:
            return False
        sent_id = await adapter.send(channel_id, Response(content=reply))
        _remember_own_message(sent_id, channel_id, reply)
        return True

    async def _answer_event_timezone(gid, channel_id: str, message,
                                     content: str) -> bool:
        """Take the mod's answer to "what timezone is that in?"."""
        if (str(channel_id), str(message.author.id)) not in \
                event_tz_questions:
            return False
        info = await adapter.guild_info(gid)
        reply = await event_cmds.answer_timezone_question(
            event_tz_questions, {"adapter": adapter, **executor.extras},
            guild_id=str(gid), guild_name=getattr(info, "name", "") or "",
            channel_id=channel_id, user_id=str(message.author.id),
            content=content)
        if reply is None:
            return False
        sent_id = await adapter.send(channel_id, Response(content=reply))
        _remember_own_message(sent_id, channel_id, reply)
        return True

    async def _answer_ranking_setup(gid, channel_id: str, message, raw,
                                    content: str, chan_map) -> bool:
        """Take a reply to "enable ranking"'s questions (which channel,
        which roles). Same rules as the general-chat question: the
        person asked, in that channel, within 10 minutes, and only a
        clear answer; anything else is handled as normal."""
        if (str(channel_id), str(message.author.id)) not in \
                ranking_questions:
            return False
        member, _guild = await _member_for(gid, raw, message.author.id)
        index = await adapter.guild_channel_index(gid) if gid else {}
        reply = await ranking_cmds.answer_setup(
            # executor.extras has no adapter: runtime adds it per
            # command, so a handler outside a command passes it itself.
            ranking_questions, {"adapter": adapter, **executor.extras},
            guild_id=gid, channel_id=channel_id, member=member,
            content=content, mentioned=chan_map, index=index)
        if reply is None:
            return False
        sent_id = await adapter.send(channel_id, Response(content=reply))
        _remember_own_message(sent_id, channel_id, reply)
        return True

    async def _reaction_role_event(raw, added: bool) -> bool:
        """Give or take a role for a reaction on a reaction-roles message.

        True when the message is one of ours, handled or not, so a
        reaction there never reaches the panels.
        """
        message_id = str(raw.message_id)
        found = reaction_roles.find(
            message_id, str(raw.guild_id) if raw.guild_id else None)
        if found is None:
            return False
        gid, entry = found
        user_id = str(raw.user_id)
        if user_id == _me():
            return True
        key = rr_event_key(getattr(raw, "emoji", None))
        role_id = reaction_roles.role_for(gid, message_id, key)
        if role_id is None:
            return True
        # An opt-in DM role: their own reaction is the consent, so it is
        # recorded BEFORE the role goes on, or the member-update guard
        # (_guard_dm_roles) would see the role without it and take it off.
        if reaction_roles.is_dm_role(gid, role_id):
            reaction_roles.set_consent(gid, role_id, user_id, added)
        try:
            if added:
                await adapter.add_role(gid, user_id, role_id)
            else:
                await adapter.remove_role(gid, user_id, role_id)
        except Exception as exc:
            # Usually hierarchy: the role was moved above his after setup.
            logger.warning("reaction role: couldn't %s role %s for %s in "
                           "%s: %s", "give" if added else "take", role_id,
                           user_id, gid, exc)
            return True
        logger.info("reaction role: %s%s for %s in %s",
                    "+" if added else "-", role_id, user_id, gid)
        return True

    async def _guard_dm_roles(data) -> None:
        """Take an opt-in DM role back off anyone who didn't react for it.

        A moderator (or another bot, or ``assignrole``) giving it would
        put that member on the messager's DM list without asking them.
        GUILD_MEMBER_UPDATE carries the member's full role list; Fluxer
        has no audit log, so who gave it is unknown and nobody is told.
        """
        try:
            gid = str(data.get("guild_id") or "")
            uid = str((data.get("user") or {}).get("id") or "")
            if not gid or not uid or _is_me(uid):
                return
            for role_id in reaction_roles.unconsented(
                    gid, uid, data.get("roles") or []):
                try:
                    await adapter.remove_role(gid, uid, role_id)
                except Exception as exc:
                    logger.warning("dm opt-in: couldn't take role %s off "
                                   "%s in %s (given without their "
                                   "reaction): %s", role_id, uid, gid, exc)
                    continue
                logger.info("dm opt-in: took role %s off %s in %s; it was "
                            "given without their reaction", role_id, uid,
                            gid)
        except Exception:
            logger.exception("dm opt-in guard failed")

    async def _member_for(gid, raw, author_id: str):
        """The author as a Member with roles, for permission checks."""
        guild = await adapter.guild_info(gid) if gid else None
        member = None
        if gid:
            try:
                member = await adapter.fetch_member(gid, author_id)
            except Exception:
                logger.debug("member fetch failed for %s", author_id,
                             exc_info=True)
        if member is None:
            member = to_member(
                getattr(raw, "member", None) or raw.author,
                guild_id=gid or "",
                guild_owner_id=guild.owner_id if guild else None,
            )
        return member, guild

    async def _unsilence_directly(message, raw, gid, channel_id: str,
                                  content: str, chan_map, me_id: str,
                                  named: bool) -> bool:
        """"Unsilence" in a quiet channel, without the classifier.

        Any classifier miss (a typo, a misroute to silence) used
        to drop the message silently, and in a silenced channel that left
        no way out. Permissions are still checked (``Dispatcher.direct``).
        """
        if not (named or (me_id and me_id in message.content)
                or await _reply_is_to_bot(message, channel_id, me_id)):
            return False
        args = match_unsilence(content, chan_map.keys())
        if args is None:
            return False
        member, guild = await _member_for(gid, raw, message.author.id)
        result = dispatcher.direct("unsilence", args, invoker=member,
                                   guild_id=gid)
        if result.decision is Decision.EXECUTE:
            reply = await executor.run(result, invoker=member,
                                       channel=message.channel, guild=guild,
                                       raw_text=content)
        else:
            reply = result.message
        logger.info("unsilence in %s by %s (direct): %s", channel_id,  # log-ok: his own reply
                    message.author.id, (reply or "")[:80])
        if reply:
            await _post_reply(channel_id, gid, reply, user_id=member.id,
                              speaker=getattr(member, "display_name", "")
                              or "someone",
                              guild_name=guild.name if guild else "")
        return True

    async def _answer_silence_question(gid, channel_id: str, user_id: str,
                                       content: str) -> bool:
        """Take a reply to "presence or silence?" from ``silence``.

        Same rules as the general-chat question: only the person asked,
        here, within the TTL, and only a clear answer.
        """
        key = (str(channel_id), str(user_id))
        asked = silence_questions.get(key)
        if asked is None:
            return False
        asked_gid, target, deadline = asked
        if _monotonic() > deadline or str(asked_gid) != str(gid):
            silence_questions.pop(key, None)
            return False
        scope = match_silence_answer(content)
        if scope is None:
            return False
        silence_questions.pop(key, None)
        reply = apply_silence(settings, gid, target, scope)
        logger.info("channel %s in guild %s: %s (answered)", target, gid,
                    "presence off" if scope == "presence" else "silenced")
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
            # A link preview loading arrives as an update. Keep its text
            # on the transcript entry for when he replies.
            conversation.set_previews(
                msg.channel.id, msg.id,
                link_previews.preview_texts(link_previews.embeds_of(raw)))
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
            if reaction_roles.drop_message(mid, gid or None):
                logger.info("reaction roles: message %s deleted; "
                            "forgotten", mid)
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
            if empty_watch is not None:
                empty_watch.check(gid)
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

        if is_exempt(member):
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
        """Post every pending level-up that has a channel to go to."""
        while True:
            event = xp.pop_level_up()
            if not event:
                return
            try:
                await _announce_one_level_up(event, guild_id,
                                             fallback_channel,
                                             author=author, name=name)
            except Exception:
                # WARNING: a level-up that silently never posts looks
                # exactly like ranking being broken.
                logger.warning("level-up announcement failed",
                               exc_info=True)

    async def _announce_one_level_up(event, guild_id, fallback_channel, *,
                                     author=None, name: str = "") -> None:
        (event_guild, user_id), before, after = event
        # flush_due closes every guild's windows, so this can be a level-up
        # from another server. The message's channel is only a fallback
        # for its own server.
        target = settings.ranking_channel(event_guild) or (
            fallback_channel if str(event_guild) == str(guild_id) else None)
        if not target:
            return
        entry = ranking.get(event_guild, user_id)

        # The server's own tiers (RankRoles), not the defaults.
        title = rank_roles.role_for(event_guild, entry.level)
        earned_new_title = (
            title and rank_roles.role_for(event_guild, before) != title)
        if title:
            # Apply on every level-up, not only on a new title: a member
            # who levelled before the roles existed still needs theirs.
            err = await adapter.apply_rank_role(
                event_guild, user_id, title,
                rank_roles.all_names(event_guild))
            if err:
                logger.warning("rank role for %s failed: %s", user_id, err)
        from corbin.commands.ranking_cmds import (average_colour,
                                                  level_up_embed)

        # XP is batched, so the message that flushes a window is often
        # someone else's: the next message anyone sends after the
        # window closes. Use the author only when it is the member who
        # levelled; otherwise look them up. Using the author regardless
        # put the wrong face on the card; skipping the lookup is what
        # posted "Someone has ranked up." with no avatar.
        mine = author is not None and str(getattr(author, "id", "")) == \
            str(user_id)
        if mine:
            display = name
            avatar_url = (getattr(author, "avatar_url", None)
                          or getattr(author, "default_avatar_url", None))
        else:
            display = await adapter.member_nick(event_guild, user_id) \
                or await _display_name(event_guild, user_id)
            if display == "someone":
                display = ""
            avatar_url = await adapter.user_avatar_url(user_id)
        if not display or not avatar_url:
            logger.info("level-up card for %s: name=%r avatar=%r", user_id,
                        display, avatar_url)
        colour = None
        if avatar_url:
            colour = await asyncio.to_thread(
                average_colour, await _fetch_bytes(str(avatar_url)))
        embed = level_up_embed(
            display or "Someone", entry,
            avatar_url=str(avatar_url) if avatar_url else None,
            colour=colour, new_title=bool(earned_new_title), title=title)
        # The mention goes in content: embeds never notify anyone, and
        # the ping is the part a member actually sees.
        await adapter.send(target, Response(
            content=f"<@{user_id}> reached **level {after}**!",
            embed=embed))

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

    def _set_aside(result):
        """The command a message matched that did NOT run, if any: one
        read as mentioned rather than requested, or a guess under the
        confidence floor (``suggest``). Both reach the reply as
        [Tool:CommandNotRun], or the model may say it did the thing."""
        if result.decision is Decision.NOT_A_COMMAND:
            return result.spec
        return result.suggest

    async def _converse(content, member, speaker, channel_id, guild, message,
                        *, spoken: bool = False,
                        channel_name: Optional[str] = None,
                        set_aside=None):
        """Answer conversationally.

        A blunt "what can you do?" is answered from the command registry
        straight away (``is_command_question``, a free regex). Otherwise,
        with ``LLM_TOOLS`` on, the model gets one call with the
        ``lookup_commands`` tool: it either answers, or asks for the
        command list and is called again with it.

        Voice chat calls this with ``message=None`` (no attachments, the
        voice channel's name passed in) and ``spoken=True`` for an answer
        that will be read aloud.

        ``set_aside`` is the command the dispatcher matched but read as
        not a request (``Decision.NOT_A_COMMAND``). The model is told it
        exists, so it can't answer "you should join vc" with "I'm
        text-only".
        """
        if message is not None:
            # Images only reach a model that can see them.
            images = await _collect_images(message)
            originals: dict = {}
            files = await documents.collect(message, vision.fetch,
                                            originals=originals)
            channel_name = message.channel.name
        else:
            images, files, originals = [], [], {}
        # Documents: an attached file is kept for an hour so "now fix the
        # second paragraph" can edit it without it being sent again; a
        # request for a file gets the how-to note.
        doc_key = (str(channel_id), str(member.id))
        if files:
            _remember_documents(doc_key, files, originals)
        elif not spoken and doc_output.wants_document(content):
            earlier = _recent_documents(doc_key)
            if earlier is not None:
                files = list(earlier["files"])
                logger.info("files: re-reading %d document(s) %s sent "
                            "earlier", len(files), speaker)
        documents_note = bool(
            not spoken and adapter.capabilities().file_upload
            and (files or doc_output.wants_document(content)))

        reply_kwargs = dict(
            user_id=member.id, speaker=speaker, user_content=content,
            channel_id=channel_id,
            guild_name=guild.name if guild else None,
            channel_name=channel_name,
            guild_id=guild.id if guild else None,
            images=images, files=files, spoken=spoken,
            documents_note=documents_note,
        )
        if set_aside is not None:
            reply_kwargs["set_aside"] = command_help.set_aside(
                set_aside, member, in_guild=guild is not None,
                guild_id=str(guild.id) if guild else None)

        # Read now: the preview usually loads a second or so after the
        # message.
        found = await _link_previews_for(message, channel_id)
        if found:
            reply_kwargs["link_previews"] = found

        reference = None
        in_guild = guild is not None
        guild_id = str(guild.id) if guild else None
        if is_command_question(content):
            reference = command_help.lookup(content, member,
                                            in_guild=in_guild,
                                            guild_id=guild_id)
            logger.info("command help: answering from the registry for %s",
                        said(content))
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
            logger.info("tool route: lookup_commands (%s)", said(topic))

        async with adapter.typing(channel_id):
            return await conversation.reply(**reply_kwargs,
                                            reference=reference)

    async def _link_previews_for(message, channel_id: str) -> list[str]:
        """What the links in the message being answered show.

        From the transcript entry when the preview update already came
        in; otherwise the message is fetched, once more after a short
        wait, since Fluxer unfurls a second or so after posting.
        """
        if message is None or not link_previews.has_link(message.content):
            return []
        for delay in (0.0, 1.5):
            if delay:
                await asyncio.sleep(delay)
            entry = conversation.find(channel_id, message.id)
            if entry and entry.get("previews"):
                return list(entry["previews"])
            data = await adapter.fetch_message_raw(channel_id, message.id)
            found = link_previews.preview_texts(link_previews.embeds_of(data))
            if found:
                conversation.set_previews(channel_id, message.id, found)
                logger.info("link preview: %d for the message from %s",
                            len(found), message.author.display_name)
                return found
        logger.info("link preview: none loaded for a link from %s",
                    message.author.display_name)
        return []

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
