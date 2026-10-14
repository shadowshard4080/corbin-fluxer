"""Per-guild persisted settings.

Same JSON-file-per-concern shape as the Discord build's ``cogs/utils.py``, so
the two stay legible side by side, with two changes:

* **Atomic writes.** The Discord build does ``json.dump(settings, open(f,"w"))``
  -- the file is truncated the instant it opens, so a crash mid-write leaves
  an empty or half-written settings file. Here we write a temp file and
  ``os.replace`` it, which is atomic on POSIX.
* **Write-through cache.** Reads hit memory; every write persists immediately,
  so a restart cannot lose a setting somebody just changed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("corbin.settings")


def _read(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        logger.warning("unreadable settings file %s; starting empty", path)
        return {}


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class SettingsStore:
    """One JSON file, keyed by guild id."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._data: dict = _read(self.path)

    # -- guild-scoped access ---------------------------------------------

    def guild(self, guild_id: str | int) -> dict:
        return self._data.get(str(guild_id), {})

    def get(self, guild_id: str | int, key: str, default: Any = None) -> Any:
        return self.guild(guild_id).get(key, default)

    def set(self, guild_id: str | int, key: str, value: Any) -> None:
        self._data.setdefault(str(guild_id), {})[key] = value
        self.flush()

    def update(self, guild_id: str | int, **values: Any) -> None:
        self._data.setdefault(str(guild_id), {}).update(values)
        self.flush()

    def unset(self, guild_id: str | int, key: str) -> None:
        bucket = self._data.get(str(guild_id))
        if bucket and key in bucket:
            del bucket[key]
            self.flush()

    def clear_guild(self, guild_id: str | int) -> None:
        if str(guild_id) in self._data:
            del self._data[str(guild_id)]
            self.flush()

    def guilds(self) -> Iterator[str]:
        return iter(tuple(self._data))

    def flush(self) -> None:
        try:
            _write_atomic(self.path, self._data)
        except OSError:
            logger.exception("could not persist settings to %s", self.path)

    def __len__(self) -> int:
        return len(self._data)


class ChatSettings:
    """Corbin's per-guild conversational configuration.

    Replaces the Discord build's split across ``ai_chatbot_settings.json`` and
    ``global_chatbot_settings.json`` with one file, since on Fluxer the two
    were always read together.

    * ``ai_channels``  -- channels where every message is treated as directed
    * ``mode``         -- "mention" (default) or "global"
    * ``silenced``     -- channels where Corbin stays quiet entirely
    * ``general_channel`` -- where unprompted musings go
    """

    MODES = ("mention", "global", "off")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.store = SettingsStore(path)

    # -- AI channels ------------------------------------------------------

    def ai_channels(self, guild_id: str | int) -> set[str]:
        return {str(c) for c in self.store.get(guild_id, "ai_channels", [])}

    def is_ai_channel(self, guild_id: Optional[str], channel_id: str) -> bool:
        if not guild_id:
            return False
        return str(channel_id) in self.ai_channels(guild_id)

    def add_ai_channel(self, guild_id: str | int, channel_id: str) -> bool:
        """Returns True if it was added, False if it was already set."""
        channels = self.ai_channels(guild_id)
        if str(channel_id) in channels:
            return False
        channels.add(str(channel_id))
        self.store.set(guild_id, "ai_channels", sorted(channels))
        return True

    def remove_ai_channel(self, guild_id: str | int, channel_id: str) -> bool:
        channels = self.ai_channels(guild_id)
        if str(channel_id) not in channels:
            return False
        channels.discard(str(channel_id))
        self.store.set(guild_id, "ai_channels", sorted(channels))
        return True

    def toggle_ai_channel(self, guild_id: str | int, channel_id: str) -> bool:
        """Returns the new state: True = now an AI channel."""
        if self.add_ai_channel(guild_id, channel_id):
            return True
        self.remove_ai_channel(guild_id, channel_id)
        return False

    # -- mode -------------------------------------------------------------

    def mode(self, guild_id: Optional[str]) -> str:
        if not guild_id:
            return "mention"
        value = self.store.get(guild_id, "mode", "mention")
        return value if value in self.MODES else "mention"

    def set_mode(self, guild_id: str | int, mode: str) -> None:
        if mode not in self.MODES:
            raise ValueError(f"mode must be one of {', '.join(self.MODES)}")
        self.store.set(guild_id, "mode", mode)

    # -- general channel (where musings go) --------------------------------

    def general_channel(self, guild_id: Optional[str]) -> Optional[str]:
        if not guild_id:
            return None
        value = self.store.get(guild_id, "general_channel")
        return str(value) if value else None

    def set_general_channel(self, guild_id: str | int,
                            channel_id: Optional[str]) -> None:
        if channel_id:
            self.store.set(guild_id, "general_channel", str(channel_id))
        else:
            self.store.unset(guild_id, "general_channel")

    # -- ranking ----------------------------------------------------------

    def ranking_enabled(self, guild_id: Optional[str]) -> bool:
        """Off by default, matching the Discord build's opt-in behaviour."""
        if not guild_id:
            return False
        return bool(self.store.get(guild_id, "ranking", False))

    def set_ranking(self, guild_id: str | int, enabled: bool) -> None:
        self.store.set(guild_id, "ranking", bool(enabled))

    def ranking_channel(self, guild_id: Optional[str]) -> Optional[str]:
        if not guild_id:
            return None
        value = self.store.get(guild_id, "ranking_channel")
        return str(value) if value else None

    def set_ranking_channel(self, guild_id: str | int,
                            channel_id: Optional[str]) -> None:
        if channel_id:
            self.store.set(guild_id, "ranking_channel", str(channel_id))
        else:
            self.store.unset(guild_id, "ranking_channel")

    # -- welcome / farewell / greeting -------------------------------------
    # All three are per-guild and off by default, matching the Discord build.

    def _feature(self, guild_id: Optional[str], key: str) -> dict:
        if not guild_id:
            return {}
        value = self.store.get(guild_id, key, {})
        return value if isinstance(value, dict) else {}

    def welcome(self, guild_id: Optional[str]) -> dict:
        return self._feature(guild_id, "welcome")

    def set_welcome(self, guild_id: str | int, *,
                    enabled: Optional[bool] = None,
                    channel_id: Optional[str] = None,
                    message: Optional[str] = None,
                    card: Optional[bool] = None,
                    card_text: Optional[str] = None,
                    background: Optional[bool] = None) -> dict:
        """Update only what was given. ``None`` means "leave it as it is",
        so changing the card text doesn't also require turning it on."""
        return self._set_card_feature("welcome", guild_id, enabled=enabled,
                                      channel_id=channel_id, message=message,
                                      card=card, card_text=card_text,
                                      background=background)

    def _set_card_feature(self, key: str, guild_id: str | int,
                          **values: Any) -> dict:
        cfg = dict(self._feature(str(guild_id), key))
        for name, value in values.items():
            if value is None:
                continue
            if name in ("enabled", "card", "background"):
                value = bool(value)
            elif name == "channel_id":
                value = str(value)
            cfg[name] = value
        self.store.set(guild_id, key, cfg)
        return cfg

    def farewell(self, guild_id: Optional[str]) -> dict:
        return self._feature(guild_id, "farewell")

    def set_farewell(self, guild_id: str | int, *,
                     enabled: Optional[bool] = None,
                     channel_id: Optional[str] = None,
                     message: Optional[str] = None,
                     card: Optional[bool] = None,
                     card_text: Optional[str] = None,
                     background: Optional[bool] = None) -> dict:
        return self._set_card_feature("farewell", guild_id, enabled=enabled,
                                      channel_id=channel_id, message=message,
                                      card=card, card_text=card_text,
                                      background=background)

    def greeting(self, guild_id: Optional[str]) -> dict:
        return self._feature(guild_id, "greeting")

    def set_greeting(self, guild_id: str | int, *, enabled: bool,
                     custom_info: Optional[str] = None) -> dict:
        cfg = dict(self.greeting(str(guild_id)))
        cfg["enabled"] = bool(enabled)
        if custom_info is not None:
            cfg["custom_info"] = custom_info
        self.store.set(guild_id, "greeting", cfg)
        return cfg

    # -- audit logging -----------------------------------------------------

    def audit(self, guild_id: Optional[str]) -> dict:
        return self._feature(guild_id, "audit")

    def audit_enabled(self, guild_id: Optional[str], category: str) -> bool:
        cfg = self.audit(guild_id)
        if not cfg.get("enabled") or not cfg.get("channel_id"):
            return False
        from .audit import normalise_categories

        return normalise_categories(cfg.get("categories")).get(category, False)

    def audit_channel(self, guild_id: Optional[str]) -> Optional[str]:
        value = self.audit(guild_id).get("channel_id")
        return str(value) if value else None

    def set_audit(self, guild_id: str | int, *, enabled: bool,
                  channel_id: Optional[str] = None,
                  categories: Optional[dict] = None) -> dict:
        from .audit import normalise_categories

        cfg = dict(self.audit(str(guild_id)))
        cfg["enabled"] = bool(enabled)
        if channel_id is not None:
            cfg["channel_id"] = str(channel_id)
        cfg["categories"] = normalise_categories(
            categories if categories is not None else cfg.get("categories"))
        self.store.set(guild_id, "audit", cfg)
        return cfg

    def set_audit_category(self, guild_id: str | int, category: str,
                           on: bool) -> dict:
        from .audit import CATEGORIES, normalise_categories

        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        cfg = dict(self.audit(str(guild_id)))
        cats = normalise_categories(cfg.get("categories"))
        cats[category] = bool(on)
        cfg["categories"] = cats
        self.store.set(guild_id, "audit", cfg)
        return cfg

    # -- scheduled content -------------------------------------------------

    def daily_quote(self, guild_id: Optional[str]) -> dict:
        return self._feature(guild_id, "daily_quote")

    def set_daily_quote(self, guild_id: str | int, *, enabled: bool,
                        channel_id: Optional[str] = None,
                        hour: Optional[int] = None,
                        minute: Optional[int] = None) -> dict:
        cfg = dict(self.daily_quote(str(guild_id)))
        cfg["enabled"] = bool(enabled)
        if channel_id is not None:
            cfg["channel_id"] = str(channel_id)
        if hour is not None:
            cfg["hour"] = max(0, min(23, int(hour)))
        if minute is not None:
            cfg["minute"] = max(0, min(59, int(minute)))
        cfg.setdefault("hour", 8)
        cfg.setdefault("minute", 0)
        self.store.set(guild_id, "daily_quote", cfg)
        return cfg

    def hourly_animals(self, guild_id: Optional[str]) -> dict:
        return self._feature(guild_id, "hourly_animals")

    def set_hourly_animals(self, guild_id: str | int, *, enabled: bool,
                           channel_id: Optional[str] = None,
                           animal: Optional[str] = None) -> dict:
        cfg = dict(self.hourly_animals(str(guild_id)))
        cfg["enabled"] = bool(enabled)
        if channel_id is not None:
            cfg["channel_id"] = str(channel_id)
        if animal is not None:
            cfg["animal"] = animal or None
        self.store.set(guild_id, "hourly_animals", cfg)
        return cfg

    def mark_scheduled_sent(self, guild_id: str | int, key: str,
                            marker: str) -> None:
        """Record that a job ran, so a restart does not repeat it."""
        cfg = dict(self._feature(str(guild_id), key))
        cfg["last_sent"] = marker
        self.store.set(guild_id, key, cfg)

    def scheduled_marker(self, guild_id: Optional[str],
                         key: str) -> Optional[str]:
        return self._feature(guild_id, key).get("last_sent")

    def guilds_with(self, key: str) -> list[str]:
        """Guild ids that have ``key`` enabled."""
        out = []
        for gid in self.store.guilds():
            cfg = self._feature(gid, key)
            if cfg.get("enabled") and cfg.get("channel_id"):
                out.append(gid)
        return out

    # -- versefinder -------------------------------------------------------

    def versefinder_enabled(self, guild_id: Optional[str]) -> bool:
        """Off by default: auto-detection in every channel is intrusive."""
        if not guild_id:
            return False
        return bool(self.store.get(guild_id, "versefinder", False))

    def set_versefinder(self, guild_id: str | int, enabled: bool) -> None:
        self.store.set(guild_id, "versefinder", bool(enabled))

    # -- honeypot ----------------------------------------------------------

    def honeypot(self, guild_id: Optional[str]) -> Optional[str]:
        if not guild_id:
            return None
        value = self.store.get(guild_id, "honeypot_channel")
        return str(value) if value else None

    def set_honeypot(self, guild_id: str | int,
                     channel_id: Optional[str]) -> None:
        if channel_id:
            self.store.set(guild_id, "honeypot_channel", str(channel_id))
        else:
            self.store.unset(guild_id, "honeypot_channel")

    def is_honeypot(self, guild_id: Optional[str], channel_id: str) -> bool:
        target = self.honeypot(guild_id)
        return bool(target) and str(channel_id) == target

    # -- full moon announcements -------------------------------------------

    def fullmoon(self, guild_id: Optional[str]) -> Optional[str]:
        if not guild_id:
            return None
        value = self.store.get(guild_id, "fullmoon_channel")
        return str(value) if value else None

    def set_fullmoon(self, guild_id: str | int,
                     channel_id: Optional[str]) -> None:
        if channel_id:
            self.store.set(guild_id, "fullmoon_channel", str(channel_id))
        else:
            self.store.unset(guild_id, "fullmoon_channel")

    def guilds_with_fullmoon(self) -> list[str]:
        return [g for g in self.store.guilds() if self.fullmoon(g)]

    # -- weather alerts ----------------------------------------------------

    def weather_alerts(self, guild_id: Optional[str]) -> list[dict]:
        if not guild_id:
            return []
        value = self.store.get(guild_id, "weather_alerts", [])
        return [a for a in value if isinstance(a, dict)] \
            if isinstance(value, list) else []

    def set_weather_alerts(self, guild_id: str | int,
                           alerts: list[dict]) -> None:
        self.store.set(guild_id, "weather_alerts", alerts)

    def guilds_with_weather(self) -> list[str]:
        return [g for g in self.store.guilds() if self.weather_alerts(g)]

    # -- verification ------------------------------------------------------

    def is_verified(self, key: Optional[str]) -> bool:
        if not key:
            return False
        return bool(self.store.get("_verified", str(key), False))

    def set_verified(self, key: str, enabled: bool) -> None:
        self.store.set("_verified", str(key), bool(enabled))

    # -- silencing --------------------------------------------------------

    def silenced(self, guild_id: str | int) -> set[str]:
        return {str(c) for c in self.store.get(guild_id, "silenced", [])}

    def is_silenced(self, guild_id: Optional[str], channel_id: str) -> bool:
        if not guild_id:
            return False
        return str(channel_id) in self.silenced(guild_id)

    def toggle_silence(self, guild_id: str | int, channel_id: str) -> bool:
        """Returns the new state: True = now silenced."""
        channels = self.silenced(guild_id)
        now_silenced = str(channel_id) not in channels
        if now_silenced:
            channels.add(str(channel_id))
        else:
            channels.discard(str(channel_id))
        self.store.set(guild_id, "silenced", sorted(channels))
        return now_silenced
