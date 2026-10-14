"""Offline suite for the platform-neutral core."""
from __future__ import annotations
import asyncio, logging, time

logging.getLogger("corbin").setLevel(logging.CRITICAL)

from _harness import Results, member, run

from corbin.ai.classifier import parse_intent, build_vocabulary
from corbin.ai.json_utils import coerce_bool, coerce_float, extract_json_object
from corbin.core.confirm import (PendingStore, is_affirmative, is_negative, is_undo)
from corbin.core.context import Response, Surface
from corbin.core.dispatch import (Dispatcher, Intent, _registry_keywords,
                                  has_non_directive_markers, looks_like_request)
from corbin.ai.classifier import ScriptedClassifier
from corbin.core.permissions import Perm, describe, has, missing, resolve, verify_against
from corbin.core.registry import BASE_THRESHOLD, Param, ParamError, Registry, Risk
from corbin.core.risk import Decision, TargetProfile, assess


def build_registry() -> Registry:
    r = Registry()

    @r.command(name="ban", description="Ban a member", risk=Risk.DESTRUCTIVE,
               permission=Perm.BAN_MEMBERS, aliases=("yeet",),
               params=(Param("user", "who", kind="user"),
                       Param("reason", "why", required=False, max_length=400)),
               examples=("ban @spammer for raiding",))
    async def ban(ctx, user, reason=None): return "banned"

    @r.command(name="clear", description="Delete messages", risk=Risk.DESTRUCTIVE,
               permission=Perm.MANAGE_MESSAGES,
               params=(Param("count", "n", kind="integer", min_value=1, max_value=500),),
               blast_radius=lambda a: a.get("count", 1))
    async def clear(ctx, count): return "cleared"

    @r.command(name="assignrole", description="Give a role", risk=Risk.WRITE,
               permission=Perm.MANAGE_ROLES,
               params=(Param("user", "who", kind="user"), Param("role", "which", kind="role")))
    async def assignrole(ctx, user, role): return "assigned"

    @r.command(name="weather", description="Show the weather", risk=Risk.SAFE,
               params=(Param("location", "where"),))
    async def weather(ctx, location): return "sunny"

    return r


NEWBIE = TargetProfile("t-new", "Newbie", tenure_days=2, account_age_days=3,
                       role_count=0, message_count=3)
VETERAN = TargetProfile("t-vet", "Veteran", tenure_days=400, account_age_days=1200,
                        role_count=2, message_count=4000, top_role_position=3)
WARNED = TargetProfile("t-warn", "Warned", tenure_days=200, account_age_days=800,
                       role_count=1, message_count=60, warning_count=2,
                       top_role_position=2)
STAFF = TargetProfile("t-staff", "Admin", tenure_days=300, account_age_days=900,
                      role_count=3, message_count=900,
                      permissions=Perm.MANAGE_GUILD, top_role_position=5)


# ---------------------------------------------------------------- permissions
def suite_permissions(r: Results) -> None:
    e = resolve([Perm.SEND_MESSAGES, Perm.BAN_MEMBERS])
    r.ok("OR-folds role bits", has(e, Perm.BAN_MEMBERS | Perm.SEND_MESSAGES))
    r.ok("lacks unrelated bit", not has(e, Perm.KICK_MEMBERS))
    r.check("missing computes lack", missing(e, Perm.KICK_MEMBERS), Perm.KICK_MEMBERS)
    r.ok("ADMINISTRATOR implies all", has(resolve([Perm.ADMINISTRATOR]), Perm.MANAGE_ROLES))
    r.ok("guild owner implies all", has(resolve([], is_guild_owner=True), Perm.BAN_MEMBERS))
    r.ok("Perm.NONE always satisfied", has(Perm.NONE, Perm.NONE))
    r.check("describe is readable", describe(Perm.BAN_MEMBERS), "Ban Members")
    r.check("vendor match -> no problems",
            list(verify_against({"BAN_MEMBERS": int(Perm.BAN_MEMBERS)})), [])
    r.check("vendor drift is reported",
            len(verify_against({"BAN_MEMBERS": 999})), 1)
    r.check("unknown vendor flag ignored", list(verify_against({"WARP_DRIVE": 1})), [])


# ------------------------------------------------------------------ registry
def suite_registry(r: Results) -> None:
    reg = build_registry()
    r.check("registered count", len(reg), 4)
    r.check("alias resolves", reg.get("yeet").name, "ban")
    r.check("slash prefix tolerated", reg.get("/ban").name, "ban")
    r.check("case-insensitive", reg.get("BAN").name, "ban")
    r.check("unknown returns None", reg.get("nuke"), None)
    r.check("usage string", reg.get("ban").usage(), "!ban <user> [reason]")

    ban = reg.get("ban")
    r.check("mention coerced to id",
            ban.coerce_args({"user": "<@!12345>"})["user"], "12345")
    r.check("role mention coerced",
            reg.get("assignrole").coerce_args({"user": "1", "role": "<@&99>"})["role"], "99")
    r.raises("missing required rejected", ParamError, ban.coerce_args, {})
    r.raises("unknown arg rejected", ParamError, ban.coerce_args, {"user": "1", "x": 2})
    r.raises("over max rejected", ParamError, reg.get("clear").coerce_args, {"count": 9999})
    r.raises("non-numeric rejected", ParamError, reg.get("clear").coerce_args, {"count": "lots"})
    r.check("optional defaults to None", ban.coerce_args({"user": "1"})["reason"], None)
    r.check("long reason truncated",
            len(ban.coerce_args({"user": "1", "reason": "x" * 900})["reason"]), 400)
    r.check("blast radius read from args",
            reg.get("clear").estimate_blast_radius({"count": 250}), 250)
    r.check("blast radius default", ban.estimate_blast_radius({"user": "1"}), 1)

    schema = ban.json_schema()
    r.check("schema required list", schema["parameters"]["required"], ["user"])
    r.ok("vocabulary names commands", "ban(" in build_vocabulary(reg))
    r.ok("vocabulary marks risk", "[destructive]" in build_vocabulary(reg))

    dup = Registry()
    dup.register(ban)
    r.raises("duplicate name rejected", ValueError, dup.register, ban)
    r.raises("non-async handler rejected", TypeError,
             lambda: dup.command(name="sync", description="d")(lambda ctx: None))
    r.raises("empty response rejected", ValueError, Response)


# ---------------------------------------------------------------------- risk
def suite_risk(r: Results) -> None:
    reg = build_registry()
    ban, clear = reg.get("ban"), reg.get("clear")
    mod = member("mod", perms=Perm.BAN_MEMBERS | Perm.MANAGE_MESSAGES, position=10)
    admin = member("admin", perms=Perm.ADMINISTRATOR, position=50)

    def verdict(spec, args, *, invoker=mod, conf=0.95, directive=True,
                target=None, surface=Surface.AI, owner=False):
        return assess(spec, surface=surface, invoker=invoker, args=args,
                      confidence=conf, directive=directive, target=target,
                      is_bot_owner=owner)

    # --- the three specified scenarios -----------------------------------
    # The owner's rule: a ban runs unasked only on a non-staff member AND
    # with an explicit reason. Everything else asks.
    r.check("SPEC 1: ban a new member, with a reason -> execute",
            verdict(ban, {"user": "t-new", "reason": "spam links"},
                    target=NEWBIE).decision, Decision.EXECUTE)
    r.check("...with NO reason -> confirm",
            verdict(ban, {"user": "t-new"}, target=NEWBIE).decision,
            Decision.CONFIRM)
    r.check("a staff member, even WITH a reason -> confirm",
            verdict(ban, {"user": "t-staff", "reason": "testing"},
                    invoker=admin, target=STAFF).decision, Decision.CONFIRM)
    r.check("a target that couldn't be identified -> confirm, never execute",
            verdict(ban, {"user": "Somebody", "reason": "raiding"},
                    target=None).decision, Decision.CONFIRM)
    r.check("SPEC 2: ban a veteran, no reason -> confirm",
            verdict(ban, {"user": "t-vet"}, target=VETERAN).decision, Decision.CONFIRM)
    r.check("SPEC 3: ban a veteran WITH reason -> execute",
            verdict(ban, {"user": "t-vet", "reason": "raiding"}, target=VETERAN).decision,
            Decision.EXECUTE)

    # --- the two axes are independent ------------------------------------
    r.check("high confidence + joke -> converse, NOT confirm",
            verdict(ban, {"user": "t-vet"}, target=VETERAN, directive=False).decision,
            Decision.NOT_A_COMMAND)
    r.check("joke about a newbie also declines",
            verdict(ban, {"user": "t-new"}, target=NEWBIE, directive=False).decision,
            Decision.NOT_A_COMMAND)

    # --- threshold arithmetic --------------------------------------------
    base = BASE_THRESHOLD[Risk.DESTRUCTIVE]
    r.ok("newbie lowers the bar",
         verdict(ban, {"user": "t-new"}, target=NEWBIE).threshold < base)
    r.ok("veteran raises the bar",
         verdict(ban, {"user": "t-vet"}, target=VETERAN).threshold > base)
    r.ok("prior warnings lower the bar",
         verdict(ban, {"user": "t-warn"}, target=WARNED).threshold < base)
    r.ok("reason lowers the bar further",
         verdict(ban, {"user": "t-vet", "reason": "raiding"}, target=VETERAN).threshold
         < verdict(ban, {"user": "t-vet"}, target=VETERAN).threshold)
    r.ok("threshold never exceeds cap",
         verdict(ban, {"user": "t-vet"}, target=VETERAN).threshold <= 0.98)
    r.ok("threshold never below floor",
         verdict(ban, {"user": "t-new"}, target=NEWBIE).threshold >= 0.55)
    r.ok("adjustments are recorded",
         len(verdict(ban, {"user": "t-new"}, target=NEWBIE).adjustments) >= 2)
    r.ok("explain() mentions the command",
         "cmd=ban" in verdict(ban, {"user": "t-new"}, target=NEWBIE).explain())

    # --- tier separation --------------------------------------------------
    r.check("SAFE tier runs on low confidence",
            verdict(reg.get("weather"), {"location": "Oslo"}, conf=0.65).decision,
            Decision.EXECUTE)
    r.check("DESTRUCTIVE tier will not",
            verdict(ban, {"user": "t-vet"}, conf=0.65, target=VETERAN).decision,
            Decision.CONFIRM)

    # --- hard gates -------------------------------------------------------
    peon = member("peon", perms=Perm.NONE, position=1)
    r.check("no permission -> denied",
            verdict(ban, {"user": "t-new"}, invoker=peon, conf=1.0, target=NEWBIE).decision,
            Decision.DENIED)
    r.ok("denial names the missing permission",
         verdict(ban, {"user": "t-new"}, invoker=peon, conf=1.0,
                 target=NEWBIE).missing_permission == Perm.BAN_MEMBERS)
    r.check("self-target refused",
            verdict(ban, {"user": "mod"}, conf=1.0,
                    target=TargetProfile("mod", "Mod")).decision, Decision.REFUSED)
    r.check("bot target refused",
            verdict(ban, {"user": "b"}, conf=1.0,
                    target=TargetProfile("b", "Bot", is_bot=True)).decision,
            Decision.REFUSED)
    r.check("guild owner target refused",
            verdict(ban, {"user": "o"}, conf=1.0,
                    target=TargetProfile("o", "Owner", is_guild_owner=True)).decision,
            Decision.REFUSED)
    r.check("higher-ranked target refused",
            verdict(ban, {"user": "hi"}, conf=1.0,
                    target=TargetProfile("hi", "Boss", top_role_position=99)).decision,
            Decision.REFUSED)
    r.check("equal-rank target refused",
            verdict(ban, {"user": "eq"}, conf=1.0,
                    target=TargetProfile("eq", "Peer", top_role_position=10)).decision,
            Decision.REFUSED)
    r.check("staff target always confirms",
            verdict(ban, {"user": "t-staff", "reason": "abuse"}, conf=1.0,
                    invoker=admin, target=STAFF).decision, Decision.CONFIRM)
    owner_reg = Registry()

    @owner_reg.command(name="shutdown", description="Stop the bot",
                       risk=Risk.DESTRUCTIVE, owner_only=True)
    async def shutdown(ctx): return None

    shutdown_spec = owner_reg.get("shutdown")
    r.check("owner-only denies a mere admin",
            assess(shutdown_spec, surface=Surface.AI, invoker=admin, args={},
                   confidence=1.0).decision, Decision.DENIED)
    r.check("owner-only allows the bot owner",
            assess(shutdown_spec, surface=Surface.AI, invoker=admin, args={},
                   confidence=1.0, is_bot_owner=True).decision, Decision.EXECUTE)
    r.check("owner-only denies even on an explicit surface",
            assess(shutdown_spec, surface=Surface.SLASH, invoker=admin, args={},
                   confidence=1.0).decision, Decision.DENIED)

    # --- blast radius -----------------------------------------------------
    r.check("small clear executes",
            verdict(clear, {"count": 5}, invoker=admin, conf=0.99).decision,
            Decision.EXECUTE)
    r.check("large clear confirms",
            verdict(clear, {"count": 250}, invoker=admin, conf=0.99).decision,
            Decision.CONFIRM)

    # --- explicit surfaces skip scoring -----------------------------------
    for surface in (Surface.SLASH, Surface.PREFIX):
        r.check(f"{surface.value} ignores confidence and tone",
                verdict(ban, {"user": "t-vet"}, conf=0.0, directive=False,
                        target=VETERAN, surface=surface).decision, Decision.EXECUTE)
    r.check("explicit surface still enforces permission",
            verdict(ban, {"user": "t-new"}, invoker=peon, conf=1.0,
                    target=NEWBIE, surface=Surface.SLASH).decision, Decision.DENIED)
    r.check("explicit surface still enforces hierarchy",
            verdict(ban, {"user": "hi"}, conf=1.0, surface=Surface.PREFIX,
                    target=TargetProfile("hi", "Boss", top_role_position=99)).decision,
            Decision.REFUSED)

    # --- bot owner bypasses permissions and hierarchy, but not policy -----
    # Mirrors the Discord build's `administrator or user.id == OWNER`.
    r.check("bot owner bypasses a missing permission",
            verdict(ban, {"user": "t-new", "reason": "spam"}, invoker=peon,
                    conf=1.0, target=NEWBIE, owner=True).decision,
            Decision.EXECUTE)
    r.check("non-owner without permission is still denied",
            verdict(ban, {"user": "t-new"}, invoker=peon, conf=1.0,
                    target=NEWBIE).decision, Decision.DENIED)
    r.check("bot owner bypass applies on explicit surfaces too",
            verdict(ban, {"user": "t-new"}, invoker=peon, conf=1.0,
                    target=NEWBIE, owner=True,
                    surface=Surface.SLASH).decision, Decision.EXECUTE)

    # --- bot owner bypasses hierarchy but not policy ----------------------
    r.check("bot owner bypasses hierarchy",
            verdict(ban, {"user": "hi", "reason": "raiding"}, conf=1.0,
                    owner=True,
                    target=TargetProfile("hi", "Boss", top_role_position=99)).decision,
            Decision.EXECUTE)
    r.check("bot owner still cannot ban a bot",
            verdict(ban, {"user": "b"}, conf=1.0, owner=True,
                    target=TargetProfile("b", "Bot", is_bot=True)).decision,
            Decision.REFUSED)


# ------------------------------------------------------------------- confirm
def suite_grants(r: Results) -> None:
    """Naming one person as an approver, without giving them Manage Guild.

    The alternative to this is handing out a guild permission, which
    grants far more than the job needs. A grant must therefore stay
    pinned to the commands listed and nothing else.
    """
    reg = build_registry()
    ban, clear = reg.get("ban"), reg.get("clear")
    plain = member("helper", perms=Perm.SEND_MESSAGES, position=1)
    GRANTS = frozenset({"ban"})

    def verdict(spec, *, grants=frozenset(), invoker=plain):
        return assess(spec, surface=Surface.AI, invoker=invoker,
                      args={"user": "t-new", "reason": "spam"},
                      confidence=0.99,
                      directive=True, target=NEWBIE, is_bot_owner=False,
                      grants=grants)

    r.check("without a grant, a plain member is denied",
            verdict(ban).decision, Decision.DENIED)
    r.check("the granted command is allowed",
            verdict(ban, grants=GRANTS).decision, Decision.EXECUTE)
    r.check("a command NOT granted is still denied",
            verdict(clear, grants=GRANTS).decision, Decision.DENIED)
    r.check("an empty grant set changes nothing",
            verdict(ban, grants=frozenset()).decision, Decision.DENIED)

    # Specs register kebab-cased; everything written by hand uses
    # underscores. Both spellings have to work or the grant silently
    # never matches -- which is exactly what happened first time.
    kebab = reg.get("assignrole")
    for spelling in (kebab.name, kebab.name.replace("-", "_"),
                     kebab.name.replace("_", "-")):
        r.check(f"grant matches spelling {spelling!r}",
                assess(kebab, surface=Surface.AI, invoker=plain,
                       args={"user": "u", "role": "r"}, confidence=0.99,
                       directive=True, is_bot_owner=False,
                       grants=frozenset({spelling})).decision,
                Decision.EXECUTE)

    # A grant is permission only. Every other gate still runs.
    owner_target = TargetProfile(user_id="t-owner", is_guild_owner=True,
                                 top_role_position=99)
    r.check("a grant does not let you touch the server owner",
            assess(ban, surface=Surface.AI, invoker=plain,
                   args={"user": "t-owner"}, confidence=0.99,
                   directive=True, target=owner_target,
                   is_bot_owner=False, grants=frozenset({"ban"})).decision,
            Decision.REFUSED)
    r.check("a grant does not turn a joke into a command",
            assess(ban, surface=Surface.AI, invoker=plain,
                   args={"user": "t-new"}, confidence=0.99,
                   directive=False, target=NEWBIE, is_bot_owner=False,
                   grants=frozenset({"ban"})).decision,
            Decision.NOT_A_COMMAND)


def suite_confirm(r: Results) -> None:
    s = PendingStore()
    s.put(guild_id="g", channel_id="c", user_id="u", command="ban",
          args={"user": "t"}, summary="ban @T")
    r.ok("owner can see it", s.peek("c", "u") is not None)
    r.ok("bystander cannot", s.peek("c", "other") is None)
    r.ok("other channel cannot", s.peek("other", "u") is None)
    r.ok("take consumes", s.take("c", "u") is not None)
    r.ok("take is one-shot", s.take("c", "u") is None)

    s.put(guild_id="g", channel_id="c", user_id="u", command="ban",
          args={}, summary="x", ttl=0.01)
    time.sleep(0.03)
    r.ok("expired confirmation is dead", s.take("c", "u") is None)

    async def _undo(): pass
    s.put_undo(guild_id="g", channel_id="c", user_id="u", command="ban",
               summary="ban @T", undo=_undo)
    r.ok("undo scoped to invoker", s.take_undo("c", "other") is None)
    r.ok("undo retrievable", s.take_undo("c", "u") is not None)
    r.ok("undo is one-shot", s.take_undo("c", "u") is None)

    for text in ("yes", "y", "yep!", "sure", "do it", "go ahead", "OK"):
        r.ok(f"affirmative: {text!r}", is_affirmative(text))
    for text in ("no", "nope", "cancel", "nevermind", "wait"):
        r.ok(f"negative: {text!r}", is_negative(text))
    r.ok("undo recognised", is_undo("undo"))
    r.ok("qualified yes is not a bare yes", not is_affirmative("yes but ban everyone"))
    r.ok("sentence is not a yes", not is_affirmative("yes I was talking to Sam"))

    s2 = PendingStore()
    s2.put(guild_id="g", channel_id="c", user_id="u", command="x", args={},
           summary="s", ttl=0.01)
    time.sleep(0.03)
    r.check("sweep reclaims", s2.sweep(), 1)


# ----------------------------------------------------------------- json/intent
def suite_parsing(r: Results) -> None:
    reg = build_registry()
    r.check("clean object", extract_json_object('{"a":1}'), {"a": 1})
    r.check("fenced object", extract_json_object('```json\n{"a":1}\n```'), {"a": 1})
    r.check("prose-wrapped", extract_json_object('Sure! {"a":1} ok'), {"a": 1})
    r.check("nested braces", extract_json_object('{"a":{"b":2}}'), {"a": {"b": 2}})
    r.check("brace inside string", extract_json_object('{"a":"}"}'), {"a": "}"})
    r.check("truncated mid-string salvages",
            extract_json_object('{"command":"ban","args":{"reason":"spamm'),
            {"command": "ban"})
    r.check("truncated after comma salvages",
            extract_json_object('{"command":"ban","confidence":0.9,'),
            {"command": "ban", "confidence": 0.9})
    r.check("no json at all", extract_json_object("nope"), None)
    r.check("empty string", extract_json_object(""), None)

    r.check("float passthrough", coerce_float(0.9), 0.9)
    r.check("string float", coerce_float("0.9"), 0.9)
    r.check("percentage", coerce_float("90%"), 0.9)
    r.check("word high", coerce_float("high"), 0.95)
    r.check("None -> default", coerce_float(None, 0.0), 0.0)
    r.check("bool is not a float", coerce_float(True, 0.0), 0.0)
    r.check("bool passthrough", coerce_bool(False), False)
    r.check("string no", coerce_bool("no"), False)
    r.check("garbage -> default", coerce_bool("maybe", True), True)

    i = parse_intent('{"command":"ban","args":{"user":"1","reason":"spam"},'
                     '"confidence":0.95,"directive":true}', reg)
    r.check("parses command", i.command, "ban")
    r.check("parses args", dict(i.args), {"user": "1", "reason": "spam"})
    r.check("parses confidence", i.confidence, 0.95)
    r.ok("parses directive", i.directive)

    j = parse_intent('{"command":"ban","args":{"user":"1"},"confidence":0.93,'
                     '"directive":false,"why_not":"jest"}', reg)
    r.ok("non-directive preserved with high confidence",
         j.directive is False and j.confidence == 0.93)

    h = parse_intent('{"command":"nuke","confidence":0.99}', reg)
    r.check("hallucinated command rejected", h.command, None)
    r.check("hallucinated confidence zeroed", h.confidence, 0.0)

    x = parse_intent('{"command":"ban","args":{"user":"1","severity":"max"},'
                     '"confidence":0.9}', reg)
    r.check("undeclared arg stripped", dict(x.args), {"user": "1"})

    r.check("alias normalised to canonical name",
            parse_intent('{"command":"yeet","args":{"user":"1"}}', reg).command, "ban")
    r.check("null command", parse_intent('{"command":null}', reg).command, None)
    r.check("unparseable -> no command", parse_intent("sorry!", reg).command, None)
    r.ok("unparseable is non-directive", not parse_intent("sorry!", reg).directive)
    r.check("confidence clamped",
            parse_intent('{"command":"ban","args":{"user":"1"},"confidence":5}', reg).confidence,
            1.0)


# ------------------------------------------------------------------- gates
def suite_gates(r: Results) -> None:
    reg = build_registry()
    for text in ("ban @steve", "what's the weather in Oslo",
                 "clear 50 messages", "who is @sam", "give @sam a role"):
        r.ok(f"request shape: {text!r}", looks_like_request(text, reg))
    for text in ("hey corbin", "lol", "", "  ", "mmm", "brb"):
        r.ok(f"no request shape: {text!r}", not looks_like_request(text, reg))
    r.ok("essay rejected", not looks_like_request("ban " + "x " * 900, reg))
    r.ok("no registry -> permissive", looks_like_request("anything at all"))

    # The gate is derived from the registry, so a command that is NOT
    # registered must not keep burning a classifier call. /joke and friends
    # are deliberately unported -- conversation handles them in persona.
    vocab = _registry_keywords(reg)
    for dropped in ("joke", "riddle", "insult"):
        r.ok(f"dropped command absent from gate: {dropped}", dropped not in vocab)
    for kept in ("ban", "clear", "weather"):
        r.ok(f"registered command in gate: {kept}", kept in vocab)
    for text in ("lol ban him", "someone should ban steve", "what if you banned me",
                 "can you even ban people?", "jk", "/s"):
        r.ok(f"non-directive marker: {text!r}", has_non_directive_markers(text))
    r.ok("plain order has no marker", not has_non_directive_markers("ban @steve for spam"))


# ---------------------------------------------------------------- dispatcher
class _Resolver:
    def __init__(self, profiles): self.profiles = profiles
    async def profile(self, guild_id, user_ref):
        return self.profiles.get(user_ref)


def suite_dispatch(r: Results) -> None:
    reg = build_registry()
    mod = member("mod", perms=Perm.BAN_MEMBERS | Perm.MANAGE_MESSAGES, position=10)
    resolver = _Resolver({"t-new": NEWBIE, "t-vet": VETERAN})

    def dispatcher(script):
        return Dispatcher(reg, ScriptedClassifier(script), resolver=resolver)

    def go(d, text, *, invoker=mod, direct=True):
        return asyncio.run(d.handle(text, invoker=invoker, channel_id="c",
                                    guild_id="g", directly_invoked=direct))

    # organic messages never dispatch, even if perfectly phrased
    d = dispatcher({"ban @Newbie": Intent("ban", {"user": "t-new"}, 0.99, True)})
    res = go(d, "ban @Newbie", direct=False)
    r.ok("organic message does not dispatch", res.converse and not res.handled)
    r.check("organic never calls the model", len(d.classifier.calls), 0)

    # free gate short-circuits before the model
    d = dispatcher({})
    res = go(d, "good morning everyone")
    r.ok("chatter converses", res.converse)
    r.check("chatter never calls the model", len(d.classifier.calls), 0)

    # the three scenarios, end to end
    d = dispatcher({"ban @Newbie": Intent(
        "ban", {"user": "t-new", "reason": "spam links"}, 0.95, True)})
    r.check("E2E newbie with a reason executes",
            go(d, "ban @Newbie").decision, Decision.EXECUTE)

    # The bug: the classifier returns the mentioned NAME, and the target
    # used to be looked up by that name, found nobody, and skipped every
    # target gate -- so "ban @<a mod>" just ran.
    staff_resolver = _Resolver({"t-staff": STAFF})
    d = Dispatcher(reg, ScriptedClassifier({"ban @Admin": Intent(
        "ban", {"user": "Admin", "reason": "test"}, 0.99, True)}),
        resolver=staff_resolver)
    res = asyncio.run(d.handle("ban @Admin", invoker=mod, channel_id="c",
                               guild_id="g", directly_invoked=True,
                               mention_ids={"Admin": "t-staff"}))
    r.check("E2E 'ban @<mod>' by mention asks first", res.decision,
            Decision.CONFIRM)
    r.check("...having resolved the mention to the real id",
            res.args.get("user"), "t-staff")

    d = dispatcher({"ban @Veteran": Intent("ban", {"user": "t-vet"}, 0.95, True)})
    res = go(d, "ban @Veteran")
    r.check("E2E veteran confirms", res.decision, Decision.CONFIRM)
    r.ok("confirm message restates intent", "ban" in res.message and "?" in res.message)

    d = dispatcher({"ban @Veteran for raiding":
                    Intent("ban", {"user": "t-vet", "reason": "raiding"}, 0.95, True)})
    r.check("E2E veteran+reason executes",
            go(d, "ban @Veteran for raiding").decision, Decision.EXECUTE)

    # joke -> conversation, and nothing is left pending
    d = dispatcher({"lol someone should ban @Veteran":
                    Intent("ban", {"user": "t-vet"}, 0.95, False, "jest")})
    res = go(d, "lol someone should ban @Veteran")
    r.ok("joke converses", res.converse and not res.handled)
    r.check("joke leaves nothing pending", len(d.store), 0)

    # confirm -> yes runs it
    d = dispatcher({"ban @Veteran": Intent("ban", {"user": "t-vet"}, 0.95, True)})
    go(d, "ban @Veteran")
    r.check("pending stored", len(d.store), 1)
    res = go(d, "yes")
    r.check("yes executes", res.decision, Decision.EXECUTE)
    r.check("pending consumed", len(d.store), 0)

    # confirm -> no cancels
    d = dispatcher({"ban @Veteran": Intent("ban", {"user": "t-vet"}, 0.95, True)})
    go(d, "ban @Veteran")
    res = go(d, "no")
    r.ok("no cancels", res.handled and "ancel" in res.message)
    r.check("cancel clears pending", len(d.store), 0)

    # a bystander's "yes" must not confirm someone else's ban
    d = dispatcher({"ban @Veteran": Intent("ban", {"user": "t-vet"}, 0.95, True)})
    go(d, "ban @Veteran")
    other = member("other", perms=Perm.BAN_MEMBERS, position=9)
    res = go(d, "yes", invoker=other)
    r.ok("bystander yes does not execute", res.decision is not Decision.EXECUTE)
    r.check("pending survives bystander", len(d.store), 1)

    # permission loss between prompt and confirmation is re-checked
    d = dispatcher({"ban @Veteran": Intent("ban", {"user": "t-vet"}, 0.95, True)})
    go(d, "ban @Veteran")
    demoted = member("mod", perms=Perm.NONE, position=10)
    res = go(d, "yes", invoker=demoted)
    r.check("revoked permission blocks confirmation", res.decision, Decision.DENIED)

    # unparseable args ask rather than guess
    d = dispatcher({"ban someone": Intent("ban", {}, 0.95, True)})
    res = go(d, "ban someone")
    r.check("missing required asks", res.decision, Decision.CONFIRM)
    r.ok("asks for the missing field", "user" in res.message)

    # no permission -> conversational decline that does not restate the action
    d = dispatcher({"ban @Newbie": Intent("ban", {"user": "t-new"}, 0.99, True)})
    res = go(d, "ban @Newbie", invoker=member("peon", perms=Perm.NONE))
    r.check("unauthorised denied", res.decision, Decision.DENIED)
    r.ok("denial does not restate the target", "t-new" not in res.message)
    r.check("denial leaves nothing pending", len(d.store), 0)

    # classifier finding nothing -> conversation
    d = dispatcher({})
    r.ok("no match converses", go(d, "tell me a joke about ravens").converse)

    # the confirmation prompt must not echo defaulted arguments
    reg2 = Registry()

    @reg2.command(name="ban", description="Ban", risk=Risk.DESTRUCTIVE,
                  permission=Perm.BAN_MEMBERS,
                  params=(Param("user", "who", kind="user"),
                          Param("reason", "why", required=False),
                          Param("delete_days", "d", kind="integer",
                                required=False, default=0)))
    async def _ban(ctx, user, reason=None, delete_days=0): return None

    d = Dispatcher(reg2, ScriptedClassifier(
        {"ban @Veteran": Intent("ban", {"user": "t-vet"}, 0.95, True)}),
        resolver=resolver)
    res = asyncio.run(d.handle("ban @Veteran", invoker=mod, channel_id="c",
                               guild_id="g", directly_invoked=True))
    r.check("confirm asks about the veteran", res.decision, Decision.CONFIRM)
    r.ok("default arg not echoed", "delete_days" not in res.message)
    r.ok("target still named", "Veteran" in res.message)

    d = Dispatcher(reg2, ScriptedClassifier(
        {"ban @Veteran delete 3 days":
         Intent("ban", {"user": "t-vet", "delete_days": 3}, 0.95, True)}),
        resolver=resolver)
    res = asyncio.run(d.handle("ban @Veteran delete 3 days", invoker=mod,
                               channel_id="c", guild_id="g",
                               directly_invoked=True))
    r.ok("explicitly-set arg IS echoed", "delete_days=3" in res.message)

    # undo returns a real entry, never a sentinel string
    d = dispatcher({"ban @Newbie": Intent("ban", {"user": "t-new"}, 0.99, True)})

    async def _noop(): pass

    d.store.put_undo(guild_id="g", channel_id="c", user_id="mod",
                     command="ban", summary="ban @Newbie", undo=_noop)
    res = go(d, "undo")
    r.ok("undo carries the entry", res.undo is not None)
    r.ok("undo leaks no sentinel", "__undo__" not in res.message)
    r.ok("undo message is human", "Reversing" in res.message)


def suite_choice_matching(r: Results) -> None:
    """A language model answers "voice activity", not ``voice``.

    Requiring the internal identifier made clear requests fail with a
    developer-facing error listing enum keys.
    """
    from corbin.core.registry import Param, ParamError, match_choice

    CATS = ("message_edit", "message_delete", "member_join", "member_leave",
            "channel_create", "channel_update", "channel_delete", "voice")

    r.check("exact key", match_choice("voice", CATS), "voice")
    r.check("natural phrase", match_choice("voice activity", CATS), "voice")
    r.check("plural", match_choice("message edits", CATS), "message_edit")
    r.check("spaces for underscores",
            match_choice("message edit", CATS), "message_edit")
    r.check("case-insensitive",
            match_choice("Message Deletes", CATS), "message_delete")
    r.check("partial but unique", match_choice("joins", CATS), "member_join")

    # Ambiguity must refuse, not guess: running the wrong category is worse
    # than asking again.
    r.check("ambiguous across channels", match_choice("channel", CATS), None)
    r.check("ambiguous across messages", match_choice("message", CATS), None)
    r.check("nonsense", match_choice("bananas", CATS), None)
    r.check("empty", match_choice("", CATS), None)

    p = Param("category", "which", choices=CATS)
    r.check("coerce resolves the phrase",
            p.coerce("voice activity"), "voice")
    try:
        p.coerce("bananas")
        r.ok("bad choice rejected", False)
    except ParamError as exc:
        r.ok("error names the value", "bananas" in str(exc))
        r.ok("error is not enum-dump shaped", "must be one of" not in str(exc))
        r.ok("error lists readable options", "message edit" in str(exc))


SUITES = [
    ("permissions", suite_permissions),
    ("choice matching", suite_choice_matching),
    ("registry", suite_registry),
    ("risk policy", suite_risk),
    ("per-user grants", suite_grants),
    ("confirmations", suite_confirm),
    ("json + intent parsing", suite_parsing),
    ("free gates", suite_gates),
    ("dispatcher end-to-end", suite_dispatch),
]

if __name__ == "__main__":
    raise SystemExit(run(SUITES))
