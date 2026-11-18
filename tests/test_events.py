"""Offline suite for server events: the calendar, adding several at once,
the 🔔 and its DM question, and the reminders (owner, 2026-11-18)."""
from __future__ import annotations
import asyncio, json, logging, tempfile, time
from pathlib import Path
from types import SimpleNamespace
logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, run

from corbin.commands import event_cmds as C
from corbin.core import events as E
from corbin.core.context import (Channel, CommandContext, Guild, Member,
                                 Platform, Surface, User)
from corbin.core.permissions import Perm
from corbin.core.settings import ChatSettings

DAY = 86400
NOW = 1_800_000_000  # 2027-01-15


def suite_offsets(r: Results) -> None:
    r.check("one", E.parse_offsets("1 hour"), ([3600], None))
    r.check("several, words and shorthand",
            E.parse_offsets("a day and 30 mins, 2h"),
            ([86400, 7200, 1800], None))
    r.check("a week", E.parse_offsets("one week before"), ([604800], None))
    r.check("half an hour", E.parse_offsets("half an hour"), ([1800], None))
    r.check("none", E.parse_offsets("none please"), ([], None))
    r.check("chat isn't an answer", E.parse_offsets("thanks, see you"),
            (None, None))
    r.ok("too far ahead is refused", E.parse_offsets("60 days")[1])
    r.ok("too many is refused", E.parse_offsets(
        "1m, 2m, 3m, 4m, 5m, 6m")[1])
    r.check("described", E.describe_all([86400, 3600, 1800]),
            "1 day, 1 hour and 30 minutes")


def suite_extracted(r: Results) -> None:
    def check(items, server_tz="America/Los_Angeles", mod_tz=None):
        return E.check_extracted(json.dumps(items), now=NOW,
                                 server_tz=server_tz, mod_tz=mod_tz)

    found, problems, ok, need = check([
        {"title": "Movie night", "date": "2027-01-22", "time": "20:00",
         "timezone": None, "description": "Bring snacks"},
        {"title": "Game night", "date": "2027-01-23", "time": "19:00",
         "timezone": "EST", "description": None},
        {"title": "Karaoke", "date": "2027-01-24", "time": None},
        {"title": "Old", "date": "2026-01-01", "time": "10:00"},
        {"title": "Bad zone", "date": "2027-01-24", "time": "10:00",
         "timezone": "Moonbase"},
    ])
    r.ok("readable", ok)
    r.check("two good events", [e["title"] for e in found],
            ["Movie night", "Game night"])
    r.check("server zone used when none is written", found[0]["tz"],
            "America/Los_Angeles")
    r.check("...8pm Pacific is 04:00 UTC next day",
            found[0]["start"], 1800676800)
    r.check("a written zone wins", found[1]["tz"], "America/New_York")
    r.ok("no time: refused, not guessed",
         any("Karaoke" in p and "needs a time" in p for p in problems))
    r.ok("in the past: refused", any("Old" in p and "past" in p
                                      for p in problems))
    r.ok("unknown zone: refused", any("Moonbase" in p for p in problems))

    found, problems, _, need = check([{"title": "X", "date": "2027-01-22",
                                       "time": "20:00"}], server_tz=None)
    r.ok("no zone anywhere: held for the question, never UTC",
         not found and not problems and need[0]["title"] == "X")
    found, _, _, _ = check([{"title": "X", "date": "2027-01-22",
                             "time": "20:00"}], server_tz="Asia/Tokyo",
                           mod_tz="Europe/London")
    r.check("the mod's own zone comes before the server's (owner, "
            "2026-11-18)", found[0]["tz"], "Europe/London")
    found, _, _ = E.check_items(need, now=NOW,
                                fallback=E.resolve_tz("PT"))
    r.check("...and the held one is added once a zone is given",
            found[0]["tz"], "America/Los_Angeles")
    r.check("prose around the JSON is tolerated", E.check_extracted(
        'Here:\n```json\n[{"title":"A","date":"2027-02-01","time":"10:00"}]'
        '\n```', now=NOW, server_tz="UTC", mod_tz=None)[0][0]["title"], "A")
    r.check("not JSON at all is unreadable", E.check_extracted(
        "sorry", now=NOW, server_tz="UTC", mod_tz=None)[2], False)


def suite_store(r: Results) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        st = E.EventStore(ChatSettings(Path(tmp) / "c.json"))
        st.put("g", {"channel_id": "c", "message_id": "m"})
        a, b = st.add("g", [
            {"title": "Movie night", "start": NOW + 2 * DAY, "tz": "UTC"},
            {"title": "Game night", "start": NOW + DAY, "tz": "UTC"}],
            by="mod")
        r.check("numbered in order", (a["id"], b["id"]), (1, 2))
        r.check("listed soonest first",
                [e["id"] for e in st.upcoming("g", NOW)], [2, 1])
        r.check("found by its card", st.find_calendar("m"), "g")

        # Subscribed long before; reminders at 1 day and 1 hour.
        cfg = st.get("g")
        for ev in cfg["events"].values():
            ev["created"] = NOW - DAY * 3
        st.put("g", cfg)
        st.subscribe("g", "u1", [DAY, 3600])
        cfg = st.get("g")
        cfg["subscribers"]["u1"]["since"] = NOW - DAY * 3
        st.put("g", cfg)
        r.check("nothing due early", st.due("g", NOW - 10), [])
        due = st.due("g", NOW + 1)
        r.check("game night's day-before is due", [(e["id"], u, o) for
                                                   e, u, o in due],
                [(2, "u1", DAY)])
        r.check("...once", st.due("g", NOW + 2), [])
        r.check("the hour-before comes later", [(e["id"], o) for e, _u, o
                                                in st.due("g", NOW + DAY -
                                                          3600)],
                [(2, 3600)])
        r.check("once game night starts, only movie night's are left",
                [(e["id"], o) for e, _u, o in st.due("g", NOW + DAY + 5)],
                [(1, DAY)])

        # A subscriber who joins two hours before an event: no "1 day
        # before" sent late, but the hour-before still comes.
        st.subscribe("g", "late", [DAY, 3600])
        cfg = st.get("g")
        cfg["subscribers"]["late"]["since"] = NOW + 2 * DAY - 7200
        st.put("g", cfg)
        due = st.due("g", NOW + 2 * DAY - 7000)
        r.check("a time already past at subscribing never fires",
                [(u, o) for _e, u, o in due if u == "late"], [])
        due = st.due("g", NOW + 2 * DAY - 3600)
        r.check("...the rest does", [(u, o) for _e, u, o in due
                                     if u == "late"], [("late", 3600)])

        r.check("removed by name", [e["id"] for e in
                                    st.remove("g", "movie")], [1])
        r.check("by number", [e["id"] for e in st.remove("g", "#2")], [2])
        r.check("nothing left", st.remove("g", "3"), [])
        st.add("g", [{"title": "Old", "start": NOW, "tz": "UTC"}], by="m")
        r.check("kept a while after it starts", st.prune(
            "g", NOW + 3600), 0)
        r.check("then pruned", st.prune("g", NOW + E.KEEP_AFTER_START), 1)
        r.ok("unsubscribe", st.unsubscribe("g", "u1")
             and st.offsets("g", "u1") is None)


class _Adapter:
    def __init__(self, channels=()):
        self.sent, self.edits, self.reactions, self.dms = [], [], [], []
        self.edit_ok = True
        self.http = SimpleNamespace(
            channels=list(channels), made=[], perms=[])
        adapter = self

        class _Http:
            async def get_guild_channels(self, gid):
                return adapter.http.channels

            async def create_guild_channel(self, gid, **kw):
                adapter.http.made.append(kw)
                return {"id": "ev"}

            async def edit_channel_permissions(self, cid, target, **kw):
                adapter.http.perms.append((cid, target, kw))
        self._http = _Http()
        self.client = SimpleNamespace(user=SimpleNamespace(id="bot"))

    def _rest(self):
        return self._http

    async def fetch_member(self, gid, uid):
        return SimpleNamespace(roles=[SimpleNamespace(id="botrole")])

    async def send(self, channel_id, response):
        self.sent.append((channel_id, response))
        return f"msg{len(self.sent)}"

    async def edit_message(self, channel_id, message_id, response):
        self.edits.append((channel_id, message_id, response))
        return self.edit_ok

    async def add_reaction(self, channel_id, message_id, emoji):
        self.reactions.append((message_id, emoji))
        return True

    async def send_dm_ref(self, user_id, response):
        self.dms.append((user_id, response.content))
        return ("dm-" + user_id, "d1")


class _LLM:
    class constants:
        MODEL_FAST = "fast"
        EFFORT_FAST = "none"

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def call(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=self.reply)


class _Resp:
    async def send(self, *a, **k):
        return None


def _ctx(cs, adapter, *, llm=None, raw=""):
    return CommandContext(
        platform=Platform.FLUXER, surface=Surface.AI,
        invoker=Member(user=User(id="mod", name="Mod"), guild_id="g",
                       is_guild_owner=True),
        channel=Channel(id="here", name="chat", guild_id="g"),
        guild=Guild(id="g", name="Test Server"), responder=_Resp(),
        extras={"settings": cs, "adapter": adapter, "llm": llm,
                "raw_text": raw, "channel_index": {"news": "n1"}})


def suite_commands(r: Results) -> None:
    run_ = asyncio.run
    with tempfile.TemporaryDirectory() as tmp:
        cs = ChatSettings(Path(tmp) / "c.json")
        ad = _Adapter([{"id": "here", "name": "chat", "type": 0,
                        "parent_id": "cat"}])
        out = run_(C.events_add(_ctx(cs, ad, raw="add movie night")))
        r.ok("no calendar yet: says how to set one up",
             "set up an events calendar" in out)

        out = run_(C.events_setup(_ctx(cs, ad), timezone="PT"))
        r.check("#events made in the asker's category",
                (ad.http.made[0]["name"], ad.http.made[0]["parent_id"]),
                ("events", "cat"))
        everyone = [kw for _c, t, kw in ad.http.perms if t == "g"][0]
        r.ok("everyone may read and react, not post",
             int(everyone["allow"]) & int(Perm.ADD_REACTIONS)
             and int(everyone["deny"]) & int(Perm.SEND_MESSAGES))
        r.ok("he can post there, through his role too", any(
            t == "botrole" for _c, t, _k in ad.http.perms))
        r.ok("the calendar is posted with its bell",
             ad.sent and ad.sent[0][0] == "ev"
             and ad.reactions == [("msg1", E.BELL)])
        st = E.EventStore(cs)
        r.check("default timezone stored", st.get("g")["tz"],
                "America/Los_Angeles")
        r.ok("setup reply", "calendar is up" in out)

        # Several at once, one without a time.
        future = time.time() + 10 * DAY
        day = time.strftime("%Y-%m-%d", time.gmtime(future))
        day2 = time.strftime("%Y-%m-%d", time.gmtime(future + DAY))
        llm = _LLM(json.dumps([
            {"title": "Movie night", "date": day, "time": "20:00"},
            {"title": "Game night", "date": day2, "time": "19:00",
             "timezone": "EST", "description": "Bring a controller"},
            {"title": "Karaoke", "date": day2, "time": None}]))
        raw = ("add these events: movie night (day) 8pm, game night "
               "(day2) 7pm EST bring a controller, karaoke (day2)")
        out = run_(C.events_add(_ctx(cs, ad, llm=llm, raw=raw)))
        r.ok("the whole message goes to the model, not the classifier's "
             "argument", raw in llm.calls[0]["messages"][0]["content"])
        r.ok("as a structured task at temperature 0",
             llm.calls[0]["structured"] and
             llm.calls[0]["temperature"] == 0.0)
        r.ok("both added, each date spelled out",
             "Added 2 events" in out and "#1 · Movie night" in out
             and "#2 · Game night" in out and "<t:" in out)
        r.ok("the one with no time is listed as not added",
             "Not added" in out and "Karaoke" in out)
        r.ok("the card was edited in place", ad.edits and
             ad.edits[-1][1] == "msg1")
        card = ad.edits[-1][2].embed
        r.check("card lists them", [f.name for f in card.fields],
                ["#1 · Movie night", "#2 · Game night"])

        out = run_(C.events_add(_ctx(cs, ad, llm=_LLM("no idea"),
                                     raw="add stuff")))
        r.ok("unreadable model output: says so", "couldn't make out" in out)

        ad.edit_ok = False
        out = run_(C.events_remove(_ctx(cs, ad), "movie"))
        r.ok("removed by name", "Removed #1 Movie night" in out)
        r.ok("a deleted card is reposted with a fresh bell",
             st.get("g")["message_id"] == "msg2"
             and ("msg2", E.BELL) in ad.reactions)
        r.ok("list is for everyone", "Game night" in str(
            run_(C.events_list(_ctx(cs, ad))).fields))
        out = run_(C.events_timezone(_ctx(cs, ad), "Moonbase"))
        r.ok("a bad timezone is refused", "isn't a timezone" in out)


def suite_bell(r: Results) -> None:
    run_ = asyncio.run
    with tempfile.TemporaryDirectory() as tmp:
        st = E.EventStore(ChatSettings(Path(tmp) / "c.json"))
        st.put("g", {"channel_id": "c", "message_id": "m"})
        ad, questions = _Adapter(), {}
        run_(C.bell(ad, st, questions, guild_id="g", guild_name="Test Server",
                    user_id="u", added=True))
        r.check("subscribed with the default", st.offsets("g", "u"),
                list(E.DEFAULT_OFFSETS))
        r.ok("DMed the question", ad.dms and "How long before each event"
             in ad.dms[0][1] and "Test Server" in ad.dms[0][1])
        ans = lambda text, ch="dm-u": C.answer_reminder_question(  # noqa: E731
            questions, st, channel_id=ch, user_id="u", content=text)
        r.check("someone else's DM: not an answer",
                ans("2 hours", ch="other"), None)
        r.check("chat isn't an answer", ans("cool thanks"), None)
        r.ok("a bad one is explained", "30 days" in ans("90 days"))
        out = ans("1 day and 2 hours")
        r.ok("an answer sets them", "1 day and 2 hours" in out and
             st.offsets("g", "u") == [86400, 7200])
        r.check("...and closes the question", questions, {})

        run_(C.bell(ad, st, questions, guild_id="g", guild_name="Test Server",
                    user_id="u", added=True))
        r.ok("again: keeps their choice", st.offsets("g", "u") ==
             [86400, 7200])
        r.ok("none turns them off", "no event reminders" in ans("none")
             and st.offsets("g", "u") is None)
        run_(C.bell(ad, st, questions, guild_id="g", guild_name="Test Server",
                    user_id="v", added=True))
        run_(C.bell(ad, st, questions, guild_id="g", guild_name="Test Server",
                    user_id="v", added=False))
        r.ok("taking the bell off unsubscribes", st.offsets("g", "v") is None)

        text = E.reminder_text({"title": "Movie night", "start": NOW,
                                "tz": "UTC", "description": "Snacks"},
                               3600, "Test Server")
        r.ok("the reminder names it, when, where and how to stop",
             "Movie night" in text and f"<t:{NOW}:R>" in text
             and "Test Server" in text and "1 hour before" in text
             and E.BELL in text)


def suite_wiring(r: Results) -> None:
    from corbin.ai.command_help import CATEGORIES
    from corbin.core.registry import registry

    specs = {s.name: s for s in registry.ai_invocable()}
    setup = specs["events-setup"]
    r.check("the owner's phrasings lead (the classifier sees 3)",
            list(setup.examples[:2]), ["set up the calendar",
                                       "set up events"])
    r.ok("...and pass the topic gate", all(
        setup.on_topic(e) for e in ("set up the calendar", "set up events",
                                    "set up an events calendar")))
    r.ok("mods add and remove", specs["events-add"].permission
         == Perm.MANAGE_MESSAGES and specs["events-remove"].permission
         == Perm.MANAGE_MESSAGES)
    r.ok("setup needs Manage Server",
         setup.permission == Perm.MANAGE_GUILD)
    r.ok("listing is for everyone", specs["events-list"].permission
         == Perm.NONE)
    r.ok("help category", "event_cmds" in CATEGORIES)

    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    handle = src[src.index("    async def _handle(raw)"):]
    r.ok("the DM answer is taken before dispatch",
         handle.index("_answer_event_question(") <
         handle.index("addressed = bool("))
    react = src[src.index("    async def _on_reaction("):]
    react = react[:react.index("\n    @bot.event")]
    r.ok("a calendar's reactions never reach the panels",
         react.index("event_store.find_calendar(") <
         react.index("panels.press("))
    r.ok("the reminder job is scheduled",
         'scheduler.add("events", _events_job)' in src)

    # Live 2026-11-18: a 15-line list went to chat as an unsure match,
    # and the chat model said it had added them all.
    for text in ("Add these events to the calendar:\n\nLab 5, 11/23/2026 "
                 "4:30 PM", "Add Lab 6e, 12/10/2026 4:30 PM to the calendar",
                 "Bot, add an event: movie night friday 8pm",
                 "please put game night on the events calendar"):
        r.ok(f"skips the classifier: {text.splitlines()[0][:40]}",
             E.match_add(text))
    for text in ("add 🎨 Artist to the reaction roles",
                 "what events are coming up?", "remove event 3",
                 "I'll add it to my calendar later lol",
                 "schedule a reminder for tomorrow"):
        r.ok(f"...but not: {text[:40]}", not E.match_add(text))
    r.ok("bot.py runs it through Dispatcher.direct, only with a calendar",
         'dispatcher.direct("events-add"' in src
         and "events_mod.match_add(content)" in src
         and 'event_store.get(str(gid)).get("channel_id")' in src)
    aside = src[src.index("    def _set_aside(result):"):]
    aside = aside[:aside.index("\n    async def ")]
    r.ok("an unsure match reaches the reply as not run",
         "return result.suggest" in aside)
    from corbin.ai.command_help import set_aside_block
    r.ok("...and the reply is told never to claim it ran",
         "Never say or imply that you did it" in set_aside_block("x"))


def suite_ask_timezone(r: Results) -> None:
    """No zone written, none known for the mod, no server default: he
    asks, holds the events, and keeps the mod's answer (owner,
    2026-11-18)."""
    run_ = asyncio.run

    class _Mem:
        def __init__(self, tz=None):
            self.entry = SimpleNamespace(preferences={}, timezone=tz)
            self.saved = 0

        def get_user(self, uid):
            return self.entry

        def save_user(self, entry):
            self.saved += 1

    with tempfile.TemporaryDirectory() as tmp:
        cs = ChatSettings(Path(tmp) / "c.json")
        ad = _Adapter()
        st = E.EventStore(cs)
        st.put("g", {"channel_id": "ev", "message_id": "m"})
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * DAY))
        llm = _LLM(json.dumps([
            {"title": "Movie night", "date": day, "time": "20:00"},
            {"title": "Quiz", "date": day, "time": "18:00",
             "timezone": "EST"}]))
        questions, mem = {}, _Mem()
        ctx = _ctx(cs, ad, llm=llm, raw="add movie night 8pm, quiz 6pm EST")
        ctx.extras.update(event_tz_questions=questions, memory=mem)
        out = run_(C.events_add(ctx))
        r.ok("the one with a zone is added at once",
             "#1 · Quiz" in out and len(st.upcoming("g")) == 1)
        r.ok("the other is asked about", "What timezone is “Movie night” "
             "in?" in out and questions)
        extras = dict(ctx.extras)
        ask = lambda text, who="mod": run_(C.answer_timezone_question(  # noqa: E731
            questions, extras, guild_id="g", guild_name="Test Server",
            channel_id="here", user_id=who, content=text))
        r.check("someone else can't answer it", ask("PT", who="x"), None)
        r.check("chat isn't an answer", ask("lol hang on"), None)
        out = ask("pacific time")
        r.ok("an answer adds it in that zone", "#2 · Movie night" in out
             and st.upcoming("g")[-1]["tz"] == "America/Los_Angeles"
             and not questions)
        r.check("...and remembers the mod's zone",
                mem.entry.preferences.get("timezone"), "America/Los_Angeles")

        questions2, mem2 = {}, _Mem("Europe/London")
        ctx2 = _ctx(cs, ad, llm=_LLM(json.dumps([
            {"title": "Brunch", "date": day, "time": "11:00"}])),
            raw="add brunch at 11")
        ctx2.extras.update(event_tz_questions=questions2, memory=mem2)
        out = run_(C.events_add(ctx2))
        r.ok("a mod whose zone is known isn't asked",
             "#3 · Brunch" in out and not questions2
             and [e["tz"] for e in st.upcoming("g")
                  if e["title"] == "Brunch"] == ["Europe/London"])

    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    handle = src[src.index("    async def _handle(raw)"):]
    r.ok("bot.py takes the answer before dispatch",
         handle.index("_answer_event_timezone(") <
         handle.index("addressed = bool("))


SUITES = [
    ("events: reminder times", suite_offsets),
    ("events: reading a mod's list", suite_extracted),
    ("events: store and reminders", suite_store),
    ("events: commands", suite_commands),
    ("events: the bell", suite_bell),
    ("events: asking the timezone", suite_ask_timezone),
    ("events: wiring", suite_wiring),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
