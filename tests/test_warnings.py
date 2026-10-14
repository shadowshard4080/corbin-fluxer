"""Offline suite for warnings and appeals."""
from __future__ import annotations
import json, logging, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.warnings import (APPEAL_COOLDOWN_DAYS, MAX_TIMEOUT_MINUTES,
                                  WarningStore, clamp_timeout,
                                  describe_duration, parse_time)


def suite_helpers(r: Results) -> None:
    r.check("zero clamps up", clamp_timeout(0), 1)
    r.check("negative clamps up", clamp_timeout(-5), 1)
    r.check("normal passes", clamp_timeout(10), 10)
    # Above 28 days Discord 400s, and Fluxer mirrors its limits elsewhere.
    r.check("caps at 28 days", clamp_timeout(999_999), MAX_TIMEOUT_MINUTES)
    r.check("exactly 28 days allowed", clamp_timeout(MAX_TIMEOUT_MINUTES),
            MAX_TIMEOUT_MINUTES)
    r.check("garbage falls back", clamp_timeout("abc"), 10)
    r.check("None falls back", clamp_timeout(None), 10)

    r.check("minutes", describe_duration(5), "5 minutes")
    r.check("singular minute", describe_duration(1), "1 minute")
    r.check("hours", describe_duration(120), "2 hours")
    r.check("days", describe_duration(2880), "2 days")

    r.ok("iso parsed", parse_time("2026-01-01T00:00:00+00:00") is not None)
    r.ok("naive gets utc",
         parse_time("2026-01-01T00:00:00").tzinfo is not None)
    r.check("garbage is None", parse_time("not a date"), None)
    r.check("empty is None", parse_time(""), None)


def suite_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = WarningStore(tmp)
        r.check("empty to start", s.count("g", "u"), 0)
        r.check("no guild counts zero", s.count(None, "u"), 0)

        w = s.add("g", "u", moderator_id="m", reason="spam", action="timeout")
        r.check("id is short enough to type", len(w.id), 8)
        r.check("counted", s.count("g", "u"), 1)
        r.check("reason stored", w.reason, "spam")
        r.check("action stored", w.action, "timeout")
        r.ok("timestamped", w.timestamp is not None)

        found, owner = s.find("g", w.id)
        r.check("found by id", found.reason, "spam")
        r.check("owner returned", owner, "u")
        r.check("unknown id", s.find("g", "zzzz"), (None, None))

        # File shape must match the Discord build, since core.stores reads
        # it for the risk model's "prior warnings" signal.
        raw = json.loads((Path(tmp) / "spamblock_warnings.json").read_text())
        r.check("guild -> user -> list shape", list(raw), ["g"])
        r.check("keyed by user", list(raw["g"]), ["u"])
        r.check("entry fields", sorted(raw["g"]["u"][0]),
                ["action", "id", "moderator_id", "reason", "timestamp"])

        from corbin.core.stores import WarningStore as RiskView
        r.check("risk model sees the same count",
                RiskView(tmp).count("g", "u"), 1)

        r.ok("edit works", s.edit("g", w.id, "updated"))
        r.check("edit applied", s.find("g", w.id)[0].reason, "updated")
        r.ok("edit unknown id is False", not s.edit("g", "zzzz", "x"))

        s.add("g", "u", moderator_id="m", reason="second")
        r.check("two warnings", s.count("g", "u"), 2)
        r.check("guild total", s.guild_total("g"), 2)

        r.ok("remove works", s.remove("g", w.id))
        r.check("count drops", s.count("g", "u"), 1)
        r.ok("remove twice is False", not s.remove("g", w.id))

        r.ok("persists across reload", WarningStore(tmp).count("g", "u") == 1)


def suite_appeal(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = WarningStore(tmp)
        w = s.add("g", "u", moderator_id="m", reason="spam")

        ok, nxt = w.can_appeal()
        r.ok("a fresh warning can be appealed", ok)
        r.check("no next time yet", nxt, None)

        s.stamp_appeal("g", w.id)
        again = s.find("g", w.id)[0]
        ok2, nxt2 = again.can_appeal()
        r.ok("cannot appeal twice straight away", not ok2)
        r.ok("next time is in the future", nxt2 > datetime.now(timezone.utc))
        delta = nxt2 - again.last_appeal_at
        r.check("cooldown is the documented window", delta.days,
                APPEAL_COOLDOWN_DAYS)

        # An old appeal must free up again.
        path = Path(tmp) / "spamblock_warnings.json"
        data = json.loads(path.read_text())
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        data["g"]["u"][0]["last_appeal_at"] = old
        path.write_text(json.dumps(data))
        stale = WarningStore(tmp).find("g", w.id)[0]
        r.ok("an expired cooldown allows appealing again",
             stale.can_appeal()[0])

        r.ok("stamping an unknown id is False", not s.stamp_appeal("g", "zz"))


SUITES = [
    ("warning helpers", suite_helpers),
    ("warning store", suite_store),
    ("appeal cooldown", suite_appeal),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
