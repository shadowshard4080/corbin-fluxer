# Corbin for Fluxer

A chat-companion bot for [Fluxer](https://fluxer.app), running entirely on a
**local language model** of your choice. No cloud AI and no API keys for the
model: point it at Ollama, LM Studio, llama.cpp's server or anything else
that speaks the OpenAI chat API.

Out of the box he is **Corbin**, a dry-witted raven. His name, personality,
emoji, owner and the rest are yours to change (see [Make him yours](#make-him-yours)).

## What he does

- **Talks.** Answers when mentioned, replied to, in DMs, or in a channel set
  to answer everything. In "global" mode he also joins conversations on his
  own now and then, and brings things up unprompted (musings) in a general
  channel you choose.
- **Remembers people.** Pulls durable facts out of conversations ("has a dog
  called Ziggy") and uses them later. `my_memory` / `forget_me` to view or
  erase.
- **Runs commands from plain language.** Fluxer has no slash commands, so you
  just ask: "Corbin, ban @spammer for scam links", "play some lofi in Music",
  "remind me in 2 hours to stretch". Risky actions ask for a yes first, and
  some can be undone.
- **Moderation:** ban (timed or permanent), kick, timeout, warnings with
  appeals, role assignment, message purge, a `#do-not-type` honeypot that bans
  scam bots, and event logging.
- **Server features:** XP and levels with rank roles, welcome and farewell
  cards (or AI-written welcomes), music from YouTube/Spotify links, internet
  radio, reminders, weather and daily weather alerts, moon phases and full-moon
  announcements, hourly animal pictures, a daily quote, bible verse lookup,
  image search, and a daily usage digest.

Ask him "what can you do?" for the full list in your server.

## Setup

You need Python 3.10+, a model server, and a Fluxer bot account. For voice
(music and radio) you also need `ffmpeg` on your PATH.

### 1. Install

```bash
git clone <this repo> corbin && cd corbin
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. A model

With [Ollama](https://ollama.com), the simplest route is to use any chat model
and let the bot send the persona:

```bash
ollama pull qwen3:14b
```

then in `.env`: `LLM_MODEL=qwen3:14b`, `LLM_PERSONA_IN_MODEL=0`.

Better on small models: bake the persona into the model with the bundled
Modelfile, so it isn't re-sent with every message.

```bash
ollama create corbin -f ollama/Modelfile
```

then `LLM_MODEL=corbin`, `LLM_PERSONA_IN_MODEL=1`.

**Set a context window of at least 8K tokens** (the Modelfile uses 16K).
Ollama's default of 4096 is too small: the bot's prompts run 2.5-4K tokens,
and the model runs out of room mid-reply. The bot warns at startup if it can
read a smaller value.

Bigger models follow the persona and command rules better. Around 14B is a
reasonable floor; reasoning models work but are slower. If replies feel slow,
set `LLM_FAST_MODEL` to something small for command detection and memory.

Using LM Studio, llama.cpp or vLLM instead? Set `LLM_URL` to its `/v1`
endpoint and `LLM_MODEL` to the model name it serves.

### 3. A Fluxer bot

Create a bot application in Fluxer's developer settings, copy its token, and
invite it to your server with permission to read and send messages (plus
Manage Roles, Kick, Ban, Manage Messages and so on for the moderation
features you want).

### 4. Configure

```bash
cp .env.example .env
```

Fill in `FLUXER_TOKEN` and your model settings. Every other setting is
documented in `.env.example`; anything left empty is off or defaulted.

### 5. Run

```bash
python bot.py
```

The startup log prints a redacted summary of what was loaded. Then, in your
server: "@Corbin hello".

To have him talk freely, an admin says "Corbin, set your mode to global". He
asks which channel is the general chat, which is where his unprompted thoughts
go. "Corbin, always reply in here" makes one channel answer every message.

## Make him yours

| What | Where |
|---|---|
| Name | `BOT_NAME` in `.env`, **and** the persona files below |
| Personality, voice, opinions | `persona/SOUL.md` (sent to the model as its system prompt) |
| Interests and specific tastes | `persona/SOUL_ADDITIONS.md` (sent every time) |
| Baked-in persona for Ollama | the `SYSTEM` block in `ollama/Modelfile` |
| Owner (owner-only commands) | `BOT_OWNER_ID` |
| His own custom emoji | `BOT_EMOJI_NAME` + `BOT_EMOJI_ID`, and mention it in the persona |
| Rank role names and colours | `RANK_ROLES_FILE` (JSON, see `.env.example`) |
| Level-up image | `LEVEL_UP_IMAGE` |
| Radio station | `RADIO_NAME`, `RADIO_STREAM_URL`, `RADIO_NOW_PLAYING_URL` |
| Where state is kept | `DATA_ROOT` (default `data/`) |

Writing a persona: specific beats generic. A small model given "likes music"
invents a backstory; given "likes post-rock, especially long builds that pay
off" it has something real to say. Rules work best with an example of the
exact sentence you don't want. `persona/SOUL.md` has a "Voice Anti-Patterns"
section that does this for common AI tells; keep it, whoever he becomes.

After changing a persona file, restart the bot. After changing the
Modelfile, run `ollama create` again.

## Optional extras

- **Weather and weather alerts:** `WEATHER_API_KEY` from openweathermap.org.
- **Image search:** `GOOGLE_API_KEY` and `CSE_ID` (Google Programmable Search).
- **Spotify links in the music player:** `SPOTIPY_CLIENT_ID` / `SECRET`.
- **Images:** `LLM_VISION=1` with a vision model (llava, qwen2.5-vl, gemma3).
  Without it he knows a picture was posted and says he can't see it.
- **Moon phases:** download
  [de421.bsp](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de421.bsp)
  into `data/`.
- **Bible commentary:** `python tools/build_gill_db.py` (needs
  `beautifulsoup4`) builds John Gill's Exposition locally. Without it, verse
  lookups use the NET translators' notes.
- **Moving from Discord:** `copy_emojis` copies a Discord server's emojis and
  stickers in, given `DISCORD_TOKEN` and `DISCORD_GUILD_ID`.
- **YouTube bot checks:** if music playback fails with "confirm you're not a
  bot", run `python tools/check_youtube.py`. Usually it needs exported cookies
  (`YTDL_COOKIEFILE`) and a [PO token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).

## How a command runs

```
message
  │
  ├─ 1. direct-invocation gate ....... free.  @mention / AI channel / reply only
  ├─ 2. request-shape gate .......... free.  regex; most chat exits here
  ├─ 3. classifier ................... one model call
  │        ├─ confidence : did I parse the right command + args?
  │        └─ directive  : was this an order, or banter?
  ├─ 4. hard gates ................... deterministic, never model-decided
  │        permission · hierarchy · self/bot/owner · blast radius
  ├─ 5. risk score ................... tier base, moved by context
  └─ 6. execute | confirm | refuse | converse
```

The classifier reports **parse confidence** and **directive force**
separately. "lol someone should ban Steve" is a correct parse of `ban(Steve)`
that was never an order, so it routes to conversation, not to a confirmation
prompt somebody might say "yes" to while still in the joke.

Risk tiers set a base bar (`SAFE` 0.60, `WRITE` 0.80, `DESTRUCTIVE` 0.90) and
context moves it: a stated reason or a brand-new account lowers it, an
established member raises it. Permission and role hierarchy are hard gates
that confidence never outvotes, and they are enforced for the person asking,
not just for the bot. Destructive actions that can be reversed get a 60-second
"undo".

## Layout

```
bot.py               wiring: events, scheduled jobs, the reply path
config.py            every setting, read from .env
persona/             SOUL.md and SOUL_ADDITIONS.md
ollama/Modelfile     optional baked-persona model
corbin/identity.py   the bot's name and emoji shortcode
corbin/ai/           model layer (llm.py, ollama.py), conversation, classifier,
                     presence, musings, memory extraction, command help
corbin/core/         platform-neutral logic; imports no SDK
corbin/commands/     command handlers, one module per area
corbin/platforms/    the Fluxer adapter
tests/               offline test suite
tools/               diagnostics and the commentary builder
```

## Tests

```bash
python tests/run_tests.py
```

About 1,700 checks. No network, no model, no Fluxer account needed; only
Pillow has to be installed.
