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
LOG_LEVEL = _get("LOG_LEVEL", default="INFO").upper()

# --- State ----------------------------------------------------------------
#: Memories, XP, reminders, bans, settings. Created on first write.
DATA_ROOT = str(_path("DATA_ROOT", HERE / "data"))
SETTINGS_PATH = _path("SETTINGS_PATH", Path(DATA_ROOT) / "chat_settings.json")

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
    ])
