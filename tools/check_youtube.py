#!/usr/bin/env python3
"""Why YouTube is refusing. Run this ON THE SERVER, from the bot's folder.

The "prove I'm not a bot" message has two possible causes and they need
different fixes, so guessing wastes a round trip:

* the PO token provider is not answering on :4416, or
* there is no usable cookiefile.

On this host the second is the harder requirement. Its IP gets
``playability status: LOGIN_REQUIRED`` from every player client without
cookies, and **no PO token satisfies that** -- so a green provider and a
missing cookie file still fails. Client rotation never fixes either.

    python3 tools/check_youtube.py
    python3 tools/check_youtube.py --url https://youtu.be/...
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

OK, BAD, MEH = "  ok  ", " FAIL ", " warn "
#: Cookies that prove a signed-in first-party session. Without these the
#: file is an anonymous export and buys nothing.
REQUIRED_COOKIES = ("LOGIN_INFO", "SID", "__Secure-1PSID")
DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def line(status: str, text: str) -> None:
    print(f"[{status}] {text}")


def check_provider() -> bool:
    from corbin.core import audio

    if audio.POT_DISABLE:
        line(MEH, "POT_DISABLE is set — the provider is switched off "
                  "deliberately")
        return False
    url = audio.POT_PROVIDER_URL
    try:
        import httpx

        resp = httpx.get(f"{url}/ping", timeout=3.0)
        if resp.status_code == 200:
            line(OK, f"PO token provider answering at {url}")
            return True
        line(BAD, f"PO token provider at {url} replied "
                  f"{resp.status_code}, expected 200")
    except Exception as exc:
        line(BAD, f"PO token provider at {url} unreachable: "
                  f"{type(exc).__name__}: {exc}")
    print("        docker run -d --init --restart unless-stopped \\\n"
          "          -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider")
    return False


def check_cookies() -> bool:
    from corbin.core.audio import find_cookiefile

    path = find_cookiefile()
    if not path:
        line(BAD, "no cookiefile found")
        print(f"        looked at: $YTDL_COOKIEFILE="
              f"{os.environ.get('YTDL_COOKIEFILE') or '(unset)'}, "
              f"./cookies.txt, ~/cookies.txt")
        print(f"        cwd is {Path.cwd()}, home is {Path.home()}")
        print("        this host REQUIRES cookies — see the notes at the "
              "end")
        return False

    line(OK, f"cookiefile found at {path}")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        line(BAD, f"...but it cannot be read: {exc}")
        return False

    names, youtube_scoped, expiries = set(), 0, []
    for raw in text.splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, expires, name, _value = parts[:7]
        if "youtube.com" in domain:
            youtube_scoped += 1
            names.add(name)
            try:
                if int(expires) > 0:
                    expiries.append(int(expires))
            except ValueError:
                pass

    if not youtube_scoped:
        line(BAD, "the file has no youtube.com cookies — it was exported "
                  "from the wrong site")
        return False
    line(OK, f"{youtube_scoped} youtube.com cookie(s)")

    missing = [c for c in REQUIRED_COOKIES if c not in names]
    if "LOGIN_INFO" in missing or ("SID" in missing and
                                   "__Secure-1PSID" in missing):
        line(BAD, f"not a signed-in export — missing {', '.join(missing)}")
        print("        export from an INCOGNITO window that visited "
              "youtube.com while signed in")
        return False
    if missing:
        line(MEH, f"missing {', '.join(missing)} (may still work)")
    else:
        line(OK, "signed-in session cookies present "
                 "(LOGIN_INFO + SID/__Secure-1PSID)")

    if expiries:
        soonest = min(expiries)
        days = (soonest - time.time()) / 86400
        if days < 0:
            line(BAD, f"cookies EXPIRED {abs(days):.0f} days ago — "
                      f"re-export them")
            return False
        if days < 7:
            line(MEH, f"cookies expire in {days:.0f} day(s) — re-export "
                      f"soon")
        else:
            line(OK, f"cookies valid for another {days:.0f} day(s)")
    return True


def check_extraction(url: str, pot_ready: bool) -> bool:
    try:
        import yt_dlp
    except ImportError:
        line(BAD, "yt-dlp is not installed in this environment")
        return False
    line(OK, f"yt-dlp {yt_dlp.version.__version__}")

    from corbin.core import audio

    opts = audio.base_opts()
    clients = (audio.POT_PLAYER_CLIENTS if pot_ready
               else audio.NO_POT_PLAYER_CLIENTS)
    opts["extractor_args"]["youtube"]["player_client"] = list(clients)
    opts["quiet"], opts["no_warnings"] = True, True
    print(f"\n  extracting {url}\n  clients: {', '.join(clients)}")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        message = str(exc)
        line(BAD, "extraction failed")
        print(f"        {message.strip()[:400]}")
        if audio.is_bot_check_error(message):
            print("\n        This is the bot check. Fix the failures "
                  "above — rotating clients will not help.")
        return False

    formats = [f for f in (info.get("formats") or [])
               if f.get("acodec") not in (None, "none")]
    if not formats:
        line(BAD, "extraction worked but returned no audio formats "
                  "(SABR-only) — playback would start and stop at 0s")
        return False
    line(OK, f"extracted \"{(info.get('title') or '?')[:60]}\" with "
             f"{len(formats)} audio format(s)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--skip-extract", action="store_true")
    args = ap.parse_args()

    print("PO token provider")
    pot = check_provider()
    print("\nCookies")
    cookies = check_cookies()

    extracted = None
    if not args.skip_extract:
        print("\nReal extraction")
        extracted = check_extraction(args.url, pot)

    print("\n" + "=" * 62)
    if extracted:
        print("YouTube extraction works. If playback still dies at 0s, "
              "that is FFmpeg\nagainst the stream URL, not extraction.")
        return 0
    if not cookies:
        print("FIX THIS FIRST: the cookiefile.\n"
              "  1. In a browser, open an INCOGNITO window and sign in to "
              "youtube.com.\n"
              "  2. Export cookies for youtube.com in Netscape format.\n"
              "  3. Copy it to this host and point YTDL_COOKIEFILE at it, "
              "or drop it\n     beside bot.py as cookies.txt.\n"
              "  4. Keep the pristine export somewhere else — yt-dlp "
              "rewrites the file\n     in place, and you will want to "
              "restore it.\n"
              "  Cookies rot in weeks. This is the recurring one.")
    elif not pot:
        print("FIX THIS FIRST: start the PO token provider container, then "
              "re-run.\n  The bot re-probes within 60s of a failure, so no "
              "restart is needed.")
    else:
        print("Provider and cookies both look right but extraction still "
              "failed.\nRead the yt-dlp error above — it is something "
              "else.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
