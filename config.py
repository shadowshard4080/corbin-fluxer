"""Configuration, read from the environment and a ``.env`` file beside this one.

Copy ``.env.example`` to ``.env`` and fill it in. Only ``FLUXER_TOKEN`` is
required; everything else has a working default or switches its feature
off when unset. Nothing here has a default that could work by accident: a
missing token is reported by :func:`validate`, not silently substituted,
because a bot that boots without one looks healthy and answers nothing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

# Imported after .env is loaded: it reads BOT_NAME from the environment.
from corbin import identity  # noqa: E402


def _get(*names: str, default: str = "") -> str:
    """First name with a value wins."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return default


def _flag(name: str, default: bool) -> bool:
    value = _get(name, default="1" if default else "0").lower()
    return value not in ("0", "false", "no", "off")


def _float(name: str) -> Optional[float]:
    value = _get(name)
    return float(value) if value else None


def _path(name: str, default: Path) -> Path:
    value = _get(name)
    path = Path(value) if value else default
    return path if path.is_absolute() else (HERE / path).resolve()


# --- Platform -------------------------------------------------------------
FLUXER_TOKEN = _get("FLUXER_TOKEN")
#: Only for a self-hosted Fluxer instance.
FLUXER_API_URL = _get("FLUXER_API_URL") or None
#: Used to build the "add me to your server" link.
FLUXER_WEB_BASE = _get("FLUXER_WEB_BASE", default="https://web.fluxer.app")

# --- Identity -------------------------------------------------------------
BOT_NAME = identity.NAME
#: Your Fluxer user id. Unlocks owner-only commands (echo, leave, verify)
#: and bypasses permission checks. Leave empty and nobody is the owner.
BOT_OWNER_ID = _get("BOT_OWNER_ID")
#: The bot's own custom emoji: its id, and the name it has in the server
#: (BOT_EMOJI_NAME, read in corbin/identity.py). The persona writes
#: ``:name:``; this is what that expands to. Unset: no expansion.
BOT_EMOJI_ID = _get("BOT_EMOJI_ID")
#: A channel for boot reports and the daily usage digest. Optional.
LOG_CHANNEL_ID = _get("LOG_CHANNEL_ID")

# --- The model ------------------------------------------------------------
#: Any OpenAI-compatible endpoint: Ollama, LM Studio, llama.cpp, vLLM.
LLM_URL = _get("LLM_URL", default="http://localhost:11434/v1")
#: The model that talks. With the bundled Modelfile this is the name you
#: gave it in `ollama create`.
LLM_MODEL = _get("LLM_MODEL", default="corbin")
#: A smaller model for the intent classifier and memory extraction, which
#: run on most messages and only need short structured answers. Defaults
#: to LLM_MODEL.
LLM_FAST_MODEL = _get("LLM_FAST_MODEL") or LLM_MODEL
#: 1 when the persona is baked into the model (the Modelfile's SYSTEM
#: block), so PERSONA_PATH is NOT sent again; a short rule block goes
#: instead. 0 sends the full persona file as the system prompt, which
#: works with any model but costs ~4K tokens a call.
LLM_PERSONA_IN_MODEL = _flag("LLM_PERSONA_IN_MODEL", False)
#: Sampling overrides. Unset uses the model's own defaults.
LLM_TEMPERATURE = _float("LLM_TEMPERATURE")
#: Qwen-family models loop without one; 1.5 is Qwen's recommendation.
LLM_PRESENCE_PENALTY = _float("LLM_PRESENCE_PENALTY")
LLM_PROBE_TIMEOUT = float(_get("LLM_PROBE_TIMEOUT", default="5.0"))
#: Offer the model a tool to look up the bot's own commands. Turn off if
#: your server rejects requests that carry tools.
LLM_TOOLS = _flag("LLM_TOOLS", True)
#: 1 only if the model can see images (llava, qwen2.5-vl, gemma3, ...).
LLM_VISION = _flag("LLM_VISION", False)
#: Images sent per reply when vision is on.
MAX_VISION_IMAGES = int(_get("MAX_VISION_IMAGES", default="2"))

# --- Persona --------------------------------------------------------------
PERSONA_PATH = _path("PERSONA_PATH", HERE / "persona" / "SOUL.md")
#: Extra tastes and quirks, sent to the model whichever persona mode is on.
PERSONA_ADDITIONS_PATH = _path("PERSONA_ADDITIONS_PATH",
                               HERE / "persona" / "SOUL_ADDITIONS.md")

# --- Behaviour ------------------------------------------------------------
#: Unprompted musings, only in servers set to global mode.
MUSINGS_ENABLED = _flag("MUSINGS", True)
#: Seed channels where the bot answers everything (comma-separated ids).
#: Normally set from chat instead ("<name>, always reply in here").
AI_CHANNELS = {c.strip() for c in _get("AI_CHANNELS").split(",")
               if c.strip()}
#: Request GUILD_PRESENCES. Off by default: a gateway that rejects an
#: unauthorised intent refuses IDENTIFY and the bot fails to start.
PRESENCE_INTENT = _flag("PRESENCE_INTENT", False)
LOG_LEVEL = _get("LOG_LEVEL", "CORBIN_LOG_LEVEL", default="INFO").upper()
#: The log file, rotated at midnight (UTC) and kept LOG_DAYS days. Logs
#: can hold message text and user ids, so keep the retention short.
#: Set LOG_FILE= (empty) for stderr only.
LOG_FILE = (str(_path("LOG_FILE", HERE / "logs" / "bot.log"))
            if os.environ.get("LOG_FILE", None) != "" else "")
LOG_DAYS = int(_get("LOG_DAYS", default="14") or 14)

# --- State ----------------------------------------------------------------
#: Memories, XP, reminders, bans, settings. Created on first write.
DATA_ROOT = str(_path("DATA_ROOT", HERE / "data"))
SETTINGS_PATH = _path("SETTINGS_PATH", Path(DATA_ROOT) / "chat_settings.json")

#: Encryption at rest for memories, warnings and reminders
#: (corbin/core/crypto.py). Needs `pip install cryptography`. Make a key
#: with `python tools/encrypt_data.py --new-key` and keep a copy somewhere
#: safe: without it that data is unreadable. Better than DATA_KEY itself:
#: DATA_KEY_FILE, a file OUTSIDE this folder (chmod 600), so a copy or
#: backup of the bot's folder doesn't carry the key with the data. Both
#: empty: plain JSON, as before.
DATA_KEY = _get("DATA_KEY", "CORBIN_DATA_KEY")
DATA_KEY_FILE = _get("DATA_KEY_FILE", "CORBIN_DATA_KEY_FILE")

# --- Optional integrations (each feature is off when its key is unset) ----
WEATHER_API_KEY = _get("WEATHER_API_KEY")          # openweathermap.org
GOOGLE_API_KEY = _get("GOOGLE_API_KEY")            # image search
CSE_ID = _get("CSE_ID")                            # image search engine id
SPOTIPY_CLIENT_ID = _get("SPOTIPY_CLIENT_ID")      # Spotify links in music
SPOTIPY_CLIENT_SECRET = _get("SPOTIPY_CLIENT_SECRET")
#: For copy_emojis: a Discord bot token and the Discord server to copy from.
DISCORD_BOT_TOKEN = _get("DISCORD_TOKEN")
DISCORD_GUILD_ID = _get("DISCORD_GUILD_ID")
#: JPL ephemeris (de421.bsp) for moon phases. Looked for in DATA_ROOT and
#: beside bot.py when unset.
EPHEMERIS_PATH = _get("EPHEMERIS_PATH")
#: John Gill's Exposition for bible-verse commentary, built by
#: tools/build_gill_db.py. Missing: NET translators' notes are used instead.
GILL_DB_PATH = _path("GILL_DB_PATH", Path(DATA_ROOT) / "gill_commentary.sqlite")


# --- Streaming video (screen share) -----------------------------------------
# All of it needs the voice extra and ffmpeg, and a server the owner has
# allowlisted for "streaming" (DM the bot: "add <server id> to the
# allowlist for streaming"). Nothing streams anywhere until then.
#: Live TV catalogue from iptv-org: a daily download plus a background
#: health check of every stream. Idle while no server allows streaming.
#: 0 turns it off.
IPTV_ENABLED = _flag("IPTV", True)
#: The small "12:34 / 24:02" in the corner of music videos and local
#: videos. 0 turns it off. It also turns itself off, with a warning, if
#: this ffmpeg can't draw text.
STREAM_TIMER = _flag("STREAM_TIMER", True)
#: The local video library: one folder per show, seasons in subfolders, a
#: video straight in the folder is a film. Read on every request; missing
#: means no videos.
VIDEO_ROOT = _path("VIDEO_ROOT", Path(DATA_ROOT) / "video")
#: Default live TV watch limit in minutes for servers that haven't set
#: their own. 0 = none.
TV_MAX_MINUTES = int(_get("TV_MAX_MINUTES", default="0") or 0)
#: Let servers stream at 1080p30 (6 Mbit/s). A 1080p encode can take a
#: whole CPU core: leave this off on a small host. Asking for 1080p while
#: it's off is refused with that reason, never silently downgraded.
ALLOW_1080P = _flag("ALLOW_1080P", False)
#: The stream size for servers that haven't picked one: 720p or 1080p.
#: 1080p needs ALLOW_1080P; without it, it reads as 720p.
STREAM_QUALITY = _get("STREAM_QUALITY", default="720p")


# --- Voice chat (VoiceStudio) ----------------------------------------------
#: Spoken conversation in voice channels needs VoiceStudio running on this
#: host (or reachable from it). It is probed at boot and again each time
#: voice chat is started; when it is down, voice chat says so and stays off.
VOICE_CHAT = _flag("VOICE_CHAT", True)
#: First-boot seed for the owner's voice allowlist (comma-separated
#: server ids). Read only while the allowlist has never been written;
#: after that, DM the bot to change it ("add <id> to the allowlist for
#: voice"). Opt-in: empty means nowhere, so adding the bot to a new server
#: never hands it an open microphone by accident.
VOICE_CHAT_GUILDS = {g.strip() for g in _get("VOICE_CHAT_GUILDS").split(",")
                     if g.strip()}
#: VoiceStudio's service root, without /v1.
VOICESTUDIO_URL = _get("VOICESTUDIO_URL", default="http://127.0.0.1:3900")
#: Only for a remote or locked-down backend (its OMNIVOICE_API_KEY).
VOICESTUDIO_API_KEY = _get("VOICESTUDIO_API_KEY", "OMNIVOICE_API_KEY")
#: OmniVoice steps per line: 32 is its quality preset, 16 is faster.
VOICESTUDIO_STEPS = int(_get("VOICESTUDIO_STEPS", default="32") or 32)
#: Transcribe while people talk, over VoiceStudio's streaming socket,
#: instead of uploading each utterance after it ends. Off until
#: tools/check_voicestudio.py shows it's faster and as accurate on your
#: engine. Any failure falls back to the upload.
VOICE_STREAM_STT = _flag("VOICE_STREAM_STT", False)
#: The socket's ?model= (a streaming model id). Empty: VoiceStudio's
#: default.
VOICESTUDIO_STT_MODEL = _get("VOICESTUDIO_STT_MODEL")
#: Barge-in: the person he's answering can cut him off by talking over
#: him. 0 turns it off (he finishes every line).
VOICE_BARGE_IN = _flag("VOICE_BARGE_IN", True)
#: Raven calls spliced in where a spoken reply says *caw*: one audio file,
#: or a folder of them (a different one each time). Missing or empty: he
#: says the word instead.
CAW_SOUNDS = str(_path("CAW_SOUNDS", Path(DATA_ROOT) / "caws"))
#: Chance (0-1) of a caw after he finishes speaking, on lines that didn't
#: already have one. Only with recordings. 0 turns it off.
CAW_CHANCE = float(_get("CAW_CHANCE", default="0.2") or 0)
#: A voice-profile id or name from VoiceStudio, or "default".
VOICESTUDIO_VOICE = _get("VOICESTUDIO_VOICE", default="default")
#: An installed engine id, or "tts-1" for whichever engine is active.
VOICESTUDIO_TTS_MODEL = _get("VOICESTUDIO_TTS_MODEL", default="tts-1")
#: Spoken language hint for transcription ("en"). Empty: auto-detect.
VOICESTUDIO_LANGUAGE = _get("VOICESTUDIO_LANGUAGE")
#: Effect on his voice: "raven" (the default), "off", or a raw ffmpeg
#: audio filter chain. See FX_PRESETS in corbin/core/voicechat.py.
VOICE_FX = _get("VOICE_FX", default="raven")


def validate() -> list[str]:
    """Return a list of human-readable problems. Empty means good to go."""
    problems: list[str] = []
    if not FLUXER_TOKEN:
        problems.append(
            "FLUXER_TOKEN is not set. Create a bot in Fluxer's developer "
            "settings and put its token in .env (see .env.example).")
    if not PERSONA_PATH.exists():
        problems.append(f"persona file not found at {PERSONA_PATH}")
    return problems


def summary() -> str:
    """Redacted view of what got loaded, for the boot log."""
    def mask(value: str) -> str:
        if not value:
            return "MISSING"
        return f"set ({len(value)} chars, …{value[-4:]})"

    return "\n".join([
        f"  name          : {BOT_NAME}",
        f"  owner id      : {BOT_OWNER_ID or '(none)'}",
        f"  FLUXER_TOKEN  : {mask(FLUXER_TOKEN)}",
        f"  model         : {LLM_MODEL} @ {LLM_URL}"
        + (f" (fast: {LLM_FAST_MODEL})" if LLM_FAST_MODEL != LLM_MODEL
           else ""),
        f"  persona       : {PERSONA_PATH} "
        f"{'ok' if PERSONA_PATH.exists() else 'MISSING'}"
        f" ({'baked into model' if LLM_PERSONA_IN_MODEL else 'sent as system prompt'})",
        f"  additions     : {PERSONA_ADDITIONS_PATH} "
        f"{'ok' if PERSONA_ADDITIONS_PATH.exists() else '(none)'}",
        f"  vision        : {'on' if LLM_VISION else 'off'}",
        f"  data root     : {DATA_ROOT}",
        f"  log channel   : {LOG_CHANNEL_ID or '(none)'}",
        f"  AI channels   : {', '.join(sorted(AI_CHANNELS)) or '(none seeded)'}",
        f"  log file      : {LOG_FILE or '(stderr only)'}",
        f"  voice chat    : "
        f"{('VoiceStudio @ ' + VOICESTUDIO_URL + ', voice ' + VOICESTUDIO_VOICE + ', fx ' + (VOICE_FX or 'off')) if VOICE_CHAT else 'disabled'}",
        f"  streaming     : live TV {'on' if IPTV_ENABLED else 'off'}, "
        f"videos {VIDEO_ROOT}"
        f"{'' if VIDEO_ROOT.is_dir() else ' (missing)'}, "
        f"default {STREAM_QUALITY if ALLOW_1080P else '720p'}"
        f"{'' if ALLOW_1080P else ' (1080p not allowed)'}",
    ])
