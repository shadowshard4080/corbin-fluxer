#!/usr/bin/env python3
"""Offline test runner. No dependencies, no network, no API keys.

    python tests/run_tests.py
"""
from __future__ import annotations
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import test_core  # noqa: E402
import test_runtime  # noqa: E402
import test_conversation  # noqa: E402
import test_audit  # noqa: E402
import test_bans  # noqa: E402
import test_digest  # noqa: E402
import test_honeypot  # noqa: E402
import test_expressions  # noqa: E402
import test_health  # noqa: E402
import test_media  # noqa: E402
import test_documents  # noqa: E402
import test_tool_calls  # noqa: E402
import test_command_help  # noqa: E402
import test_weather_alerts  # noqa: E402
import test_memory  # noqa: E402
import test_no_disclosure  # noqa: E402
import test_purge  # noqa: E402
import test_reply_targeting  # noqa: E402
import test_roles  # noqa: E402
import test_spotify  # noqa: E402
import test_status  # noqa: E402
import test_voice_audio  # noqa: E402
import test_utils_cmds  # noqa: E402
import test_verses  # noqa: E402
import test_warnings  # noqa: E402
import test_welcome  # noqa: E402
import test_moon  # noqa: E402
import test_notify  # noqa: E402
import test_player  # noqa: E402
import test_presence  # noqa: E402
import test_musings  # noqa: E402
import test_ranking  # noqa: E402
import test_scheduled  # noqa: E402
import test_settings  # noqa: E402
from _harness import run  # noqa: E402

if __name__ == "__main__":
    print("\nCorbin for Fluxer — offline suite\n")
    sys.exit(run(test_core.SUITES + test_runtime.SUITES + test_conversation.SUITES
                   + test_settings.SUITES
                   + test_ranking.SUITES
                   + test_expressions.SUITES
                   + test_health.SUITES
                   + test_media.SUITES
                   + test_documents.SUITES
                   + test_tool_calls.SUITES
                   + test_command_help.SUITES
                   + test_weather_alerts.SUITES
                   + test_memory.SUITES
                   + test_no_disclosure.SUITES
                   + test_purge.SUITES
                   + test_reply_targeting.SUITES
                   + test_roles.SUITES
                   + test_spotify.SUITES
                   + test_status.SUITES
                   + test_voice_audio.SUITES
                   + test_welcome.SUITES
                   + test_presence.SUITES
                   + test_musings.SUITES
                   + test_audit.SUITES
                   + test_warnings.SUITES
                   + test_notify.SUITES
                   + test_scheduled.SUITES
                   + test_verses.SUITES
                   + test_digest.SUITES
                   + test_player.SUITES
                   + test_bans.SUITES
                   + test_honeypot.SUITES
                   + test_moon.SUITES
                   + test_utils_cmds.SUITES))
