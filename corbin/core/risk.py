"""Risk scoring for natural-language command invocation.

The policy, in one sentence: *a natural-language command executes when the
classifier is more confident than the situation is dangerous.*

Tier sets a base bar (``registry.BASE_THRESHOLD``).  Context then moves it.
Banning a two-day-old account with no roles is overwhelmingly likely to be
spam cleanup, so the bar drops.  Banning a two-year veteran with roles is
unusual, so it rises.  Naming a reason is strong evidence of deliberation --
nobody types "for raiding" by accident -- so it drops the bar either way.

Every adjustment is recorded on the :class:`Assessment` so a moderation
decision can be explained after the fact, which matters more here than
anywhere else in the bot.

Hard gates are *not* part of the score.  Permission, role hierarchy, and the
joke screen can never be outvoted by confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import AbstractSet, Any, Mapping, Optional, Sequence

from .context import Member, Surface
from .permissions import Perm, STAFF_PERMS, has
from .registry import BASE_THRESHOLD, CommandSpec, Risk

#: Confidence can never be required above/below these, so a pile of modifiers
#: can't make a command unreachable or a destructive one a pushover.
MIN_THRESHOLD = 0.55
MAX_THRESHOLD = 0.98

#: A member younger than this in the guild reads as "new".
NEW_MEMBER_DAYS = 7
#: An account younger than this reads as "throwaway".
NEW_ACCOUNT_DAYS = 30
#: Tenure beyond this, with roles and real activity, reads as "established".
ESTABLISHED_DAYS = 90
ESTABLISHED_MESSAGES = 100
#: Affecting more entities than this always confirms, whatever the confidence.
BLAST_RADIUS_CONFIRM = 10


class Decision(Enum):
    EXECUTE = "execute"
    CONFIRM = "confirm"
    DENIED = "denied"                # invoker lacks permission
    REFUSED = "refused"              # policy forbids it outright
    NOT_A_COMMAND = "not_a_command"  # joke/hypothetical -> converse instead


@dataclass
class TargetProfile:
    """What we know about the member a command is aimed at.

    Assembled by the dispatcher from the platform (tenure, roles, account age)
    plus Corbin's own stores -- ``spamblock_warnings.json`` and the per-guild
    XP/message counts -- both of which already exist.
    """

    user_id: str
    display_name: str = ""
    tenure_days: Optional[float] = None
    account_age_days: Optional[float] = None
    role_count: int = 0
    message_count: int = 0
    warning_count: int = 0
    is_bot: bool = False
    is_guild_owner: bool = False
    permissions: Perm = Perm.NONE
    top_role_position: int = 0

    @property
    def is_staff(self) -> bool:
        return self.is_guild_owner or bool(self.permissions & STAFF_PERMS)

    @property
    def is_new_member(self) -> bool:
        return self.tenure_days is not None and self.tenure_days < NEW_MEMBER_DAYS

    @property
    def is_new_account(self) -> bool:
        return (
            self.account_age_days is not None
            and self.account_age_days < NEW_ACCOUNT_DAYS
        )

    @property
    def is_unestablished(self) -> bool:
        """No roles and effectively no activity -- a drive-by presence."""
        return self.role_count == 0 and self.message_count < 10

    @property
    def is_established(self) -> bool:
        return (
            self.tenure_days is not None
            and self.tenure_days >= ESTABLISHED_DAYS
            and self.role_count > 0
            and self.message_count >= ESTABLISHED_MESSAGES
        )


@dataclass
class Adjustment:
    name: str
    delta: float
    reason: str


@dataclass
class Assessment:
    """The full, inspectable outcome of a risk evaluation."""

    decision: Decision
    command: str
    confidence: float
    threshold: float
    base_threshold: float
    adjustments: Sequence[Adjustment] = field(default_factory=tuple)
    reason: str = ""
    missing_permission: Perm = Perm.NONE

    @property
    def executed_immediately(self) -> bool:
        return self.decision is Decision.EXECUTE

    def explain(self) -> str:
        """One-line audit string for the log."""
        moves = " ".join(
            f"{a.name}{a.delta:+.2f}" for a in self.adjustments
        ) or "none"
        return (
            f"cmd={self.command} decision={self.decision.value} "
            f"conf={self.confidence:.2f} threshold={self.threshold:.2f} "
            f"(base={self.base_threshold:.2f} {moves}) reason={self.reason}"
        )


def _clamp(value: float) -> float:
    return max(MIN_THRESHOLD, min(MAX_THRESHOLD, value))


def targets_member(spec: CommandSpec, args: Mapping[str, Any]) -> bool:
    """Does this call act on a specific member (a filled user param)?"""
    return any(p.kind == "user" and args.get(p.name) for p in spec.params)


def has_reason(args: Mapping[str, Any]) -> bool:
    reason = args.get("reason")
    return isinstance(reason, str) and len(reason.strip()) >= 3


def compute_threshold(
    spec: CommandSpec,
    args: Mapping[str, Any],
    target: Optional[TargetProfile],
) -> tuple[float, float, list[Adjustment]]:
    """Return (threshold, base, adjustments)."""
    base = BASE_THRESHOLD[spec.risk]
    adjustments: list[Adjustment] = []

    reason_text = args.get("reason")
    if isinstance(reason_text, str) and len(reason_text.strip()) >= 3:
        adjustments.append(
            Adjustment("reason_given", -0.15, "a reason was stated explicitly")
        )

    if target is not None:
        if target.is_new_member:
            adjustments.append(
                Adjustment("target_new_member", -0.15,
                           f"joined {target.tenure_days:.0f}d ago")
            )
        if target.is_new_account:
            adjustments.append(
                Adjustment("target_new_account", -0.10,
                           f"account {target.account_age_days:.0f}d old")
            )
        if target.is_unestablished:
            adjustments.append(
                Adjustment("target_unestablished", -0.10,
                           "no roles and almost no messages")
            )
        if target.warning_count > 0:
            adjustments.append(
                Adjustment("target_has_warnings", -0.10,
                           f"{target.warning_count} prior warning(s)")
            )
        if target.is_established:
            adjustments.append(
                Adjustment("target_established", +0.15,
                           "long-standing member with roles and activity")
            )

    threshold = _clamp(base + sum(a.delta for a in adjustments))
    return threshold, base, adjustments


def assess(
    spec: CommandSpec,
    *,
    surface: Surface,
    invoker: Member,
    args: Mapping[str, Any],
    confidence: float = 1.0,
    directive: bool = True,
    target: Optional[TargetProfile] = None,
    is_bot_owner: bool = False,
    grants: "AbstractSet[str]" = frozenset(),
) -> Assessment:
    """Decide whether to execute, confirm, refuse, or converse.

    Hard gates run first and are never overridden by confidence.
    """
    def verdict(decision: Decision, reason: str, *,
                threshold: float = 0.0, base: float = 0.0,
                adjustments: Sequence[Adjustment] = (),
                missing: Perm = Perm.NONE) -> Assessment:
        return Assessment(
            decision=decision, command=spec.name, confidence=confidence,
            threshold=threshold, base_threshold=base, adjustments=adjustments,
            reason=reason, missing_permission=missing,
        )

    # --- Hard gate 1: was this even an instruction? -----------------------
    # A joke must fall through to conversation, never to a confirmation
    # prompt -- otherwise the bot offers to execute every joke, and somebody
    # says "yes" while still in the bit.
    if surface is Surface.AI and not directive:
        return verdict(Decision.NOT_A_COMMAND, "not phrased as an instruction")

    # --- Hard gate 2: authorization (never model-decided) -----------------
    if spec.owner_only and not is_bot_owner:
        return verdict(Decision.DENIED, "owner-only command")

    # The bot owner bypasses guild permissions, matching the Discord build's
    # `guild_permissions.administrator or user.id == OWNER` pattern.
    #
    # ``grants`` names commands this particular invoker may run without
    # the guild permission -- a deliberate, per-person, per-command
    # exception configured by the owner. It is narrower than handing out
    # Manage Guild, which is the alternative and grants far more. It
    # never widens anything below: the target gates, hierarchy checks
    # and confidence thresholds all still apply.
    effective = invoker.permissions
    # Names register kebab-cased ("approve-claim") while everything
    # written by hand says approve_claim. Compare both rather than make
    # the caller remember which spelling the registry chose.
    granted = bool(grants) and (
        spec.name in grants
        or spec.name.replace("-", "_") in grants
        or spec.name.replace("_", "-") in grants)
    if not is_bot_owner and not granted and not has(effective,
                                                    spec.permission):
        from .permissions import missing as missing_perms

        return verdict(
            Decision.DENIED,
            "invoker lacks the required permission",
            missing=missing_perms(effective, spec.permission),
        )

    # --- Hard gate 3: policy refusals -------------------------------------
    if target is not None:
        if target.user_id == invoker.id:
            return verdict(Decision.REFUSED, "cannot target yourself")
        if target.is_bot:
            return verdict(Decision.REFUSED, "cannot target a bot")
        if target.is_guild_owner:
            return verdict(Decision.REFUSED, "cannot target the server owner")
        # Discord/Fluxer only enforce hierarchy for the *bot*, not the
        # invoker, so Corbin enforces it himself.
        if (
            not is_bot_owner
            and not invoker.is_guild_owner
            and target.top_role_position >= invoker.top_role_position
        ):
            return verdict(
                Decision.REFUSED,
                "target ranks at or above you in the role hierarchy",
            )

    threshold, base, adjustments = compute_threshold(spec, args, target)

    # --- Explicit surfaces skip scoring entirely --------------------------
    # A slash command or `!ban @x` is already unambiguous; there is no parse
    # to be unsure about.
    if surface is not Surface.AI:
        return verdict(
            Decision.EXECUTE, f"explicit {surface.value} invocation",
            threshold=threshold, base=base, adjustments=adjustments,
        )

    # --- Blast radius always confirms -------------------------------------
    radius = spec.estimate_blast_radius(args)
    if radius > BLAST_RADIUS_CONFIRM:
        return verdict(
            Decision.CONFIRM, f"affects {radius} items",
            threshold=threshold, base=base, adjustments=adjustments,
        )

    # --- Moving against staff always confirms -----------------------------
    if spec.risk is Risk.DESTRUCTIVE and target is not None and target.is_staff:
        return verdict(
            Decision.CONFIRM, "target holds staff permissions",
            threshold=threshold, base=base, adjustments=adjustments,
        )

    # --- Acting on a member without asking needs both conditions -----------
    # The owner's rule: ban (and anything else destructive aimed at a
    # person) runs unasked ONLY on a non-staff member AND with an explicit
    # reason. An unidentified target counts as "can't tell", never as
    # "fine": that is how "ban @<a mod>" once ran with every target check
    # skipped, because the mention had not been resolved to an id yet.
    if spec.risk is Risk.DESTRUCTIVE and targets_member(spec, args):
        if target is None:
            return verdict(
                Decision.CONFIRM, "couldn't verify who the target is",
                threshold=threshold, base=base, adjustments=adjustments,
            )
        if not has_reason(args):
            return verdict(
                Decision.CONFIRM, "no reason was given",
                threshold=threshold, base=base, adjustments=adjustments,
            )

    if confidence >= threshold:
        return verdict(
            Decision.EXECUTE, "confidence clears the contextual bar",
            threshold=threshold, base=base, adjustments=adjustments,
        )
    return verdict(
        Decision.CONFIRM, "confidence below the contextual bar",
        threshold=threshold, base=base, adjustments=adjustments,
    )
