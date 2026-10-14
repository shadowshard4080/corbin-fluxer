"""Offline suite for XP, levelling, and the ranking store."""
from __future__ import annotations
import json, logging, tempfile
from pathlib import Path
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.core.ranking import (ROLE_TIERS, RankingStore, UserRank,
                                 XPAccumulator, calculate_level, next_role,
                                 role_for_level, sanitize_id, xp_for_level,
                                 xp_for_message)


def suite_formulas(r: Results) -> None:
    # Must match cogs/ranking_cog.py exactly or levels shift between bots.
    r.check("below 100 xp is level 0", calculate_level(0), 0)
    r.check("99 xp is still level 0", calculate_level(99), 0)
    r.check("100 xp is level 1", calculate_level(100), 1)
    r.check("level scales linearly", calculate_level(100 + 999_6), 11)

    mismatches = [l for l in range(0, 600)
                  if calculate_level(xp_for_level(l)) != l]
    r.check("xp_for_level roundtrips for every level 0-599", mismatches, [])
    boundary = [l for l in range(1, 600)
                if calculate_level(xp_for_level(l) - 1) != l - 1]
    r.check("one XP below a threshold is the previous level", boundary, [])

    # Word-count breakpoints, interpolated.
    r.check("one word", xp_for_message("hi"), 3)
    r.check("empty message still floors at 3", xp_for_message(""), 3)
    r.check("10 words", xp_for_message("w " * 10), 6)
    r.check("20 words", xp_for_message("w " * 20), 12)
    r.check("40 words caps", xp_for_message("w " * 40), 20)
    r.check("200 words still caps", xp_for_message("w " * 200), 20)
    r.ok("interpolates between points", 6 < xp_for_message("w " * 15) < 12)

    r.check("level 0 has no title", role_for_level(0), None)
    r.check("level 1 title", role_for_level(1)[0], "One of No Renown")
    r.check("level 30 title", role_for_level(30)[0], "Moongazer")
    r.check("level 9999 caps at the top tier",
            role_for_level(9999)[0], "Lord of Eternal Night")
    r.check("next title from level 1", next_role(1), (25, "Moongazer"))
    r.check("no next title at the top", next_role(9999), None)

    r.check("ids are path-safe", sanitize_id("../../etc/passwd"), "etcpasswd")
    r.check("normal id survives", sanitize_id("12345"), "12345")


def suite_progress(r: Results) -> None:
    entry = UserRank("u", chat_xp=100)
    r.check("level 1 at 100 xp", entry.level, 1)
    into, span = entry.progress
    r.check("no progress into a fresh level", into, 0)
    r.ok("span is the level width", span > 900)
    r.ok("empty bar at 0 progress", entry.bar().startswith("░"))

    full = UserRank("u", chat_xp=xp_for_level(2) - 1)
    r.ok("nearly-full bar before levelling", full.bar().count("█") >= 14)
    r.check("title reflects level", UserRank("u", chat_xp=30_000).role_name,
            "Moongazer")


def suite_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RankingStore(tmp)
        r.check("unknown user is zeroed", store.get("g", "u").chat_xp, 0)

        entry = store.get("g", "u")
        entry.chat_xp, entry.total_messages = 500, 12
        store.save("g", entry)
        r.check("persists xp", store.get("g", "u").chat_xp, 500)
        r.check("persists messages", store.get("g", "u").total_messages, 12)

        # The Discord bot writes extra keys we must not clobber.
        path = Path(tmp) / "user_data" / "g" / "u.json"
        data = json.loads(path.read_text())
        data["vc_sessions"] = {"keep": "me"}
        path.write_text(json.dumps(data))
        entry = store.get("g", "u")
        entry.chat_xp = 800
        store.save("g", entry)
        r.check("foreign keys survive a write",
                json.loads(path.read_text()).get("vc_sessions"), {"keep": "me"})

        r.check("no temp files left",
                [f.name for f in path.parent.glob("*.tmp")], [])

        for uid, xp in (("a", 5000), ("b", 300), ("c", 50_000)):
            e = store.get("g", uid); e.chat_xp = xp; store.save("g", e)
        top, total = store.leaderboard("g", limit=2)
        r.check("sorted by xp desc", [t.user_id for t in top], ["c", "a"])
        r.check("total counts everyone", total, 4)
        page2, _ = store.leaderboard("g", limit=2, offset=2)
        r.check("pagination offsets", len(page2), 2)
        r.check("position is 1-based", store.position("g", "c"), 1)
        r.check("unknown user has no position", store.position("g", "zz"), None)
        r.check("unknown guild is empty", store.leaderboard("zz"), ([], 0))

        corrupt = Path(tmp) / "user_data" / "g" / "bad.json"
        corrupt.write_text("{not json")
        r.check("corrupt file reads as zero", store.get("g", "bad").chat_xp, 0)


def suite_accumulator(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RankingStore(tmp)
        acc = XPAccumulator(store, window=0.0)  # flush on the next message

        acc.record("g", "u", "hello there friend")
        r.check("first message is held, not written",
                store.get("g", "u").chat_xp, 0)
        acc.record("g", "u", "another one")
        r.ok("window close flushes", store.get("g", "u").chat_xp > 0)

        acc2 = XPAccumulator(store, window=9999.0)
        acc2.record("g", "v", "hi")
        r.check("open window holds", store.get("g", "v").chat_xp, 0)
        acc2.flush_all()
        r.check("flush_all writes", store.get("g", "v").chat_xp, 3)
        r.check("messages counted", store.get("g", "v").total_messages, 1)

        acc3 = XPAccumulator(store, window=9999.0)
        acc3.record("g", "w", "a"); acc3.record("g", "w", "b")
        acc3.flush_all()
        r.check("batched into one write",
                store.get("g", "w").total_messages, 2)
        r.check("xp summed", store.get("g", "w").chat_xp, 6)

        acc4 = XPAccumulator(store, window=9999.0)
        big = store.get("g", "lvl"); big.chat_xp = 99; store.save("g", big)
        acc4.record("g", "lvl", "word " * 10)
        acc4.flush_all()
        r.ok("level-up detected", acc4.pop_level_up() is not None)
        r.check("level-up is one-shot", acc4.pop_level_up(), None)


def suite_confirm_phrasing(r: Results) -> None:
    import corbin.commands.moderation  # noqa: F401  (for the fallback case)
    import corbin.commands.ranking_cmds  # noqa: F401
    from corbin.core.registry import registry

    spec = registry.get("setup_ranking")
    r.ok("has a natural confirm phrase", spec.confirm_phrase is not None)
    r.check("on, with a channel",
            spec.describe_call({"enabled": True, "channel": "99"}),
            "turn ranking on and announce level-ups in <#99>")
    r.check("on, no channel",
            spec.describe_call({"enabled": True}),
            "turn ranking on in this server")
    r.check("off", spec.describe_call({"enabled": False}),
            "turn ranking off in this server")
    r.ok("channel is an optional param",
         not spec.param("channel").required)

    ban = registry.get("ban")
    r.check("commands without a phrase fall back", ban.describe_call({}), None)

    # Corbin's own words must obey SOUL.md too.
    from corbin.core.dispatch import _summarise
    r.ok("no em-dash in the fallback summary",
         "\u2014" not in _summarise(ban, {"user": "u"}, None))


def suite_channel_resolution(r: Results) -> None:
    from corbin.commands.ranking_cmds import _resolve_channel
    from corbin.core.context import (Channel, CommandContext, Guild, Member,
                                     Platform, Surface, User)

    class _R:
        async def send(self, x): return "m"
        async def fetch_member(self, g, u): return None

    def ctx(index=None):
        return CommandContext(
            platform=Platform.FLUXER, surface=Surface.AI,
            invoker=Member(user=User(id="u", name="U"), guild_id="g"),
            channel=Channel(id="here", name="general", guild_id="g"),
            guild=Guild(id="g", name="G"), responder=_R(),
            extras={"channel_index": index or {}})

    idx = {"ranking": "555", "general": "111"}
    r.check("numeric id passes through", _resolve_channel(ctx(), "555"), "555")
    r.check("name resolves", _resolve_channel(ctx(idx), "ranking"), "555")
    r.check("hash prefix stripped", _resolve_channel(ctx(idx), "#ranking"), "555")
    r.check("case-insensitive", _resolve_channel(ctx(idx), "Ranking"), "555")
    r.check("unknown name is None", _resolve_channel(ctx(idx), "nope"), None)
    r.check("empty is None", _resolve_channel(ctx(idx), ""), None)
    r.check("None is None", _resolve_channel(ctx(idx), None), None)
    # The bug: a placeholder must not resolve to anything.
    r.check("placeholder does not resolve",
            _resolve_channel(ctx(idx), "a channel"), None)


def suite_level_up_card(r: Results) -> None:
    """The rank-up post matches the Discord build, not a bare ping."""
    import io

    from corbin.commands.ranking_cmds import (LEVEL_UP_COLOUR, LEVEL_UP_GIF,
                                              average_colour, level_up_embed)

    entry = UserRank("1", chat_xp=250_000, total_messages=4321,
                     vc_seconds=3 * 3600 + 25 * 60)
    e = level_up_embed("Sam", entry, avatar_url="https://a/x.png")
    names = [f.name for f in e.fields]
    r.check("titled like the Discord card", e.title,
            "🏆 Sam has ranked up.")
    r.check("carries the configured image", e.image_url, LEVEL_UP_GIF)
    r.check("and the avatar", e.thumbnail_url, "https://a/x.png")
    r.check("the Discord fields, in order", names,
            ["Level:", "Current role:", "Total XP:", "Total messages sent:",
             "Total VC time:"])
    r.check("voice time as hours and minutes", e.fields[-1].value, "3h 25m")
    r.check("level matches the entry", e.fields[0].value, str(entry.level))

    quiet = level_up_embed("Sam", UserRank("1", chat_xp=500),
                           new_title=True)
    r.ok("no voice time, no voice field",
         "Total VC time:" not in [f.name for f in quiet.fields])
    r.ok("a new title is called out", quiet.fields[1].name == "New role:")
    r.check("no avatar colour falls back to gold", quiet.colour,
            LEVEL_UP_COLOUR)

    try:
        from PIL import Image
    except ImportError:
        return
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buf, "PNG")
    r.check("the card takes the avatar's colour",
            average_colour(buf.getvalue()), 0xC82828)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (5, 5, 5)).save(buf, "PNG")
    r.ok("near-black is rejected", average_colour(buf.getvalue()) is None)
    r.ok("junk bytes are rejected", average_colour(b"nope") is None)


SUITES = [
    ("ranking formulas", suite_formulas),
    ("channel resolution", suite_channel_resolution),
    ("confirm phrasing", suite_confirm_phrasing),
    ("rank progress", suite_progress),
    ("ranking store", suite_store),
    ("xp accumulator", suite_accumulator),
    ("level-up card", suite_level_up_card),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
