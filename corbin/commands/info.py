"""Information lookups: weather, time, server stats.

These hit real external APIs on purpose. Corbin *could* answer "what's the
weather in Oslo" conversationally, but he would be guessing -- that is the
line drawn in PORTING_SCOPE.md between commands worth keeping and the ones
(/joke, /riddle) that conversation does better.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..core.context import CommandContext, Embed, Response
from ..core.permissions import Perm
from ..core.registry import Param, Risk, command

logger = logging.getLogger("corbin.commands.info")

_OWM = "https://api.openweathermap.org/data/2.5/weather"
_OWM_FORECAST = "https://api.openweathermap.org/data/2.5/forecast"
_TIMEOUT = 10.0

#: US state codes. OpenWeather's `q=` needs "City,STATE,US" -- given
#: "Denver, CO" it looks for a *country* called CO and returns 404.
_US_STATES = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO
MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
DC PR VI GU AS MP
""".split())

_CONDITION_ICON = {
    "Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️", "Drizzle": "🌦️",
    "Thunderstorm": "⛈️", "Snow": "❄️", "Mist": "🌫️", "Fog": "🌫️",
    "Haze": "🌫️", "Smoke": "🌫️", "Dust": "🌫️", "Sand": "🌫️",
    "Ash": "🌋", "Squall": "💨", "Tornado": "🌪️",
}


def _location_params(location: str) -> dict:
    """Turn a human location into OpenWeather query params.

    Handles the two shapes that silently misbehave:
      * "Denver, CO" -> q="Denver,CO,US" (bare state 404s)
      * "80202"          -> zip="80202,US" (a bare postcode in `q=` matches
                            some unrelated place name -- 80202 returned
                            the wrong town instead of Denver)
    """
    text = (location or "").strip()
    if text.replace("-", "").replace(" ", "").isdigit() and 5 <= len(text) <= 10:
        return {"zip": f"{text},US"}

    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) == 2 and parts[1].upper() in _US_STATES:
        return {"q": f"{parts[0]},{parts[1].upper()},US"}
    if len(parts) == 3 and parts[1].upper() in _US_STATES:
        return {"q": f"{parts[0]},{parts[1].upper()},{parts[2].upper()}"}
    return {"q": text}


async def _get_json(url: str, params: dict) -> Optional[dict]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.info("%s returned %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception:
        logger.exception("request to %s failed", url)
        return None


@command(
    name="weather",
    description="Current weather for a place",
    risk=Risk.SAFE,
    guild_only=False,
    params=(
        Param("location", "city name, e.g. Oslo or Portland OR"),
        Param("units", "metric or imperial", required=False,
              default="imperial", choices=("metric", "imperial")),
    ),
    examples=("what's the weather in Oslo",
              "weather for Portland in celsius"),
)
async def weather(ctx: CommandContext, location: str,
                  units: str = "imperial") -> Any:
    return await weather_embed(location, units)


async def weather_embed(location: str, units: str = "imperial") -> Any:
    """The current-weather card, or a str explaining why there isn't one.

    Shared by the ``weather`` command and the daily alert job, so an alert
    posts exactly what asking would have.
    """
    import config

    if not config.WEATHER_API_KEY:
        return "No weather API key is configured, so I can't look that up."

    params = {**_location_params(location), "units": units,
              "appid": config.WEATHER_API_KEY}
    data = await _get_json(_OWM, params)
    if not data or data.get("cod") != 200:
        return (f"Couldn't find weather for “{location}”. Try adding a "
                "country, like “Denver, CO, US”.")

    main = data.get("main", {})
    weather_block = (data.get("weather") or [{}])[0]
    condition = weather_block.get("main", "")
    degree = "°C" if units == "metric" else "°F"
    speed = "m/s" if units == "metric" else "mph"

    name = data.get("name") or location
    country = (data.get("sys") or {}).get("country", "")
    icon = _CONDITION_ICON.get(condition, "🌡️")

    embed = Embed(
        title=f"{icon}  {name}{', ' + country if country else ''}",
        description=(weather_block.get("description") or "").capitalize(),
    )
    if main.get("temp") is not None:
        embed.add_field("Temperature", f"{main['temp']:.0f}{degree}", inline=True)
    if main.get("feels_like") is not None:
        embed.add_field("Feels like", f"{main['feels_like']:.0f}{degree}",
                        inline=True)
    if main.get("humidity") is not None:
        embed.add_field("Humidity", f"{main['humidity']}%", inline=True)
    wind = (data.get("wind") or {}).get("speed")
    if wind is not None:
        embed.add_field("Wind", f"{wind:.0f} {speed}", inline=True)
    return embed


@command(
    name="forecast",
    description="Multi-day forecast for a place",
    risk=Risk.SAFE,
    guild_only=False,
    params=(
        Param("location", "city name or postcode"),
        Param("units", "metric or imperial", required=False,
              default="imperial", choices=("metric", "imperial")),
    ),
    examples=("what's the forecast for Denver",
              "forecast for Oslo in celsius"),
)
async def forecast(ctx: CommandContext, location: str,
                   units: str = "imperial") -> Any:
    import config

    if not config.WEATHER_API_KEY:
        return "No weather API key is configured."
    params = {**_location_params(location), "units": units,
              "appid": config.WEATHER_API_KEY}
    data = await _get_json(_OWM_FORECAST, params)
    if not data or str(data.get("cod")) != "200":
        return f"Couldn't find a forecast for “{location}”."

    degree = "°C" if units == "metric" else "°F"
    city = (data.get("city") or {}).get("name") or location

    # OpenWeather's free forecast is 3-hourly; collapse to per-day highs
    # and lows so the embed is readable.
    days: dict[str, dict] = {}
    for slot in data.get("list") or []:
        stamp = str(slot.get("dt_txt") or "")[:10]
        if not stamp:
            continue
        main = slot.get("main") or {}
        entry = days.setdefault(stamp, {"hi": None, "lo": None, "what": {}})
        for key, value in (("hi", main.get("temp_max")),
                           ("lo", main.get("temp_min"))):
            if value is None:
                continue
            current = entry[key]
            if current is None:
                entry[key] = value
            else:
                entry[key] = max(current, value) if key == "hi" \
                    else min(current, value)
        cond = ((slot.get("weather") or [{}])[0].get("main") or "")
        if cond:
            entry["what"][cond] = entry["what"].get(cond, 0) + 1

    if not days:
        return f"No forecast data came back for “{location}”."

    lines = []
    for stamp in sorted(days)[:5]:
        entry = days[stamp]
        cond = max(entry["what"], key=entry["what"].get) if entry["what"] \
            else ""
        icon = _CONDITION_ICON.get(cond, "")
        when = datetime.strptime(stamp, "%Y-%m-%d")
        hi = f"{entry['hi']:.0f}{degree}" if entry["hi"] is not None else "?"
        lo = f"{entry['lo']:.0f}{degree}" if entry["lo"] is not None else "?"
        lines.append(f"`{when:%a %d %b}`  {icon} {cond}  **{hi}** / {lo}")

    embed = Embed(title=f"Forecast for {city}", colour=0x5BC0DE)
    embed.add_list_field("Next days", lines)
    return embed


def _resolve_channel(ctx: CommandContext,
                     value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip().lstrip("#<>")
    if text.isdigit():
        return text
    return (ctx.extras.get("channel_index") or {}).get(text.lower())


def _learned_tz(ctx: CommandContext) -> Optional[str]:
    """The asker's timezone, if the memory extractor has learned it."""
    memory = ctx.extras.get("memory")
    if memory is None:
        return None
    try:
        return memory.get_user(ctx.invoker.id).timezone or None
    except Exception:
        return None


@command(
    name="weatheralerts",
    description="Post the weather for a place every day at a set time",
    risk=Risk.WRITE,
    permission=Perm.MANAGE_GUILD,
    aliases=("weather_alerts", "daily_weather"),
    params=(
        Param("action", "add, list or remove",
              choices=("add", "list", "remove")),
        Param("location", "the place; for remove, the place or alert id, "
              "or \"all\"", required=False),
        Param("time", "time of day, e.g. 7am or 18:30", required=False),
        Param("timezone", "e.g. America/Los_Angeles, PST, Europe/London; "
              "defaults to the asker's own, then UTC", required=False),
        Param("channel", "where to post; defaults to this channel",
              kind="channel", required=False),
        Param("units", "metric or imperial", required=False,
              default="imperial", choices=("metric", "imperial")),
    ),
    confirm_phrase=lambda a: (
        f"{a.get('action')} a daily weather alert"
        + (f" for {a.get('location')}" if a.get("location") else "")),
    examples=("post the weather for Denver every day at 7am PST",
              "add a daily weather alert for Oslo at 18:00 in #weather",
              "list the weather alerts", "remove the Oslo weather alert"),
)
async def weatheralerts(ctx: CommandContext, action: str,
                        location: Optional[str] = None,
                        time: Optional[str] = None,
                        timezone: Optional[str] = None,
                        channel: Optional[str] = None,
                        units: str = "imperial") -> Any:
    from datetime import datetime, timezone as _tz

    from ..core import weather_alerts as wa

    settings = ctx.extras["settings"]
    alerts = [wa.normalise(a) for a in settings.weather_alerts(ctx.guild_id)]

    if action == "list":
        if not alerts:
            return "No weather alerts set here."
        return Embed(title="Daily weather alerts", colour=0x5BC0DE,
                     description="\n".join(f"• {wa.describe(a)}"
                                           for a in alerts))

    if action == "remove":
        if not location:
            # This used to wipe every alert. Removing everything has to be
            # asked for in so many words.
            return ("Which one? Give me the place or the alert id, or say "
                    "“remove all weather alerts”.")
        if location.strip().lower() == "all":
            settings.set_weather_alerts(ctx.guild_id, [])
            return f"Removed all {len(alerts)} weather alert(s) here."
        kept = [a for a in alerts if not wa.matches(a, location)]
        if len(kept) == len(alerts):
            return f"No alert matches “{location}”. Say “list the weather " \
                   f"alerts” to see them."
        settings.set_weather_alerts(ctx.guild_id, kept)
        return f"Removed the alert for {location}."

    # -- add ---------------------------------------------------------------
    if not location:
        return "Which place should I report on?"
    clock = wa.parse_clock(time) if time else (7, 0)
    if clock is None:
        return f"I couldn't read “{time}” as a time. Try 7am or 18:30."
    zone_text = timezone or _learned_tz(ctx) or "UTC"
    zone = wa.resolve_tz(zone_text)
    if zone is None:
        return (f"“{zone_text}” isn't a timezone I know. Use a name like "
                "America/Los_Angeles or Europe/London, or PST/EST.")
    target = _resolve_channel(ctx, channel) if channel else ctx.channel.id
    if not target:
        return f"I can't find a channel called “{channel}”."

    # Check the place resolves NOW, not at 7am tomorrow when nobody is
    # watching and the alert just never posts.
    preview = await weather_embed(location, units)
    if isinstance(preview, str):
        return preview

    others = [a for a in alerts if a["location"].lower() != location.lower()]
    if len(others) >= wa.MAX_PER_GUILD:
        return f"This server already has {wa.MAX_PER_GUILD} weather alerts."
    alert = wa.normalise({
        "location": location, "hour": clock[0], "minute": clock[1],
        "tz": wa.tz_name(zone), "channel_id": str(target), "units": units,
    })
    settings.set_weather_alerts(ctx.guild_id, others + [alert])
    when = wa.next_fire(alert, datetime.now(_tz.utc))
    return Response(
        content=(f"Daily weather for **{location}** set (id `{alert['id']}`)."
                 f" First post {when:%a %d %b at %H:%M} {alert['tz']} in "
                 f"<#{target}>. Here's what it'll look like:"),
        embed=preview)


@command(
    name="time",
    description="Current time, optionally for a specific place",
    risk=Risk.SAFE,
    guild_only=False,
    params=(Param("location", "city or timezone", required=False),),
    examples=("what time is it", "what time is it in Tokyo"),
)
async def time_cmd(ctx: CommandContext, location: Optional[str] = None) -> str:
    now_utc = datetime.now(timezone.utc)
    if not location:
        return f"It's {now_utc:%H:%M} UTC ({now_utc:%A, %d %B %Y})."

    # Reuse the weather API's coordinates + tz offset rather than adding a
    # second provider; OpenWeather returns `timezone` as seconds from UTC.
    import config

    if not config.WEATHER_API_KEY:
        return f"It's {now_utc:%H:%M} UTC — I can't resolve local times without a key."

    data = await _get_json(_OWM, {**_location_params(location),
                                  "appid": config.WEATHER_API_KEY})
    if not data or data.get("cod") != 200:
        return f"I couldn't place “{location}”."

    offset = data.get("timezone", 0)
    local = now_utc + timedelta(seconds=offset)
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(abs(offset) // 60, 60)
    name = data.get("name") or location
    return (f"It's {local:%H:%M} in {name} "
            f"({local:%A, %d %B}, UTC{sign}{hours:02d}:{minutes:02d}).")


@command(
    name="serverstats",
    description="Basic statistics for this server",
    risk=Risk.SAFE,
    aliases=("server_info", "serverinfo"),
    examples=("show server stats", "how many people are in this server"),
)
async def serverstats(ctx: CommandContext) -> Any:
    if ctx.guild is None:
        return "That only works inside a server."

    adapter = ctx.extras["adapter"]
    raw = await adapter._guild(ctx.guild.id)
    if raw is None:
        return "I couldn't read this server's details."

    embed = Embed(title=ctx.guild.name)
    member_count = getattr(raw, "member_count", None)
    if member_count is not None:
        embed.add_field("Members", str(member_count), inline=True)
    if ctx.guild.owner_id:
        embed.add_field("Owner", f"<@{ctx.guild.owner_id}>", inline=True)
    created = getattr(raw, "created_at", None)
    if created is not None:
        embed.add_field("Created", f"{created:%d %B %Y}", inline=True)
    embed.add_field("Server ID", ctx.guild.id, inline=True)
    return embed
