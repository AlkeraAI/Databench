"""The policy decision.

``decide`` reduces ``(ActionDescriptor, mode, ruleset)`` → ``AutoDecision`` ∈
{allow, prompt, reject}. Each layer can only TIGHTEN (reject > prompt > allow),
never relax, so the floor and an explicit deny win. ``bypass`` waives the
challenge and the floor by design, as a labeled OUTCOME of ``evaluate_action``
rather than an early return: it is decided and audited like everything else.

The sanctioned relaxations — an ``allow`` RULE ("Always allow") and an effect
reclassification — apply against the ``default``/``auto`` mode base only. Neither
applies to ``read_only``/``plan``, whose contract is no mutation at all: there the
mode base (taken on the REAL classification) clamps both, so a blanket rule persisted
in some earlier session can't quietly re-open writes. A RECORDED standing allow is
clamped once more, by :func:`grant_reaches`: it is the answer a person gave to the
question ONE stance asked, so a stricter stance asks its own question instead.

The **floor** (destroy + the named operations) forces at least ``prompt``, except
in ``auto`` for EGRESS, which is routed to the grounded safety judge instead of the
human. DESTROY + floor-ops ALWAYS bind the human floor. ``confidence="unknown"``
(unparseable) is treated as >= write. :func:`gate_outcome` is the layer above: it
reads the measured impact and the declared intent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any

from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.plugins.plugin_base.permissions.consent_scope import standing_allow_scope

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.permissions.config import PermissionsConfig

# Operations that are ALWAYS on the floor regardless of effect inference
# (privilege escalation + mass mutation), in addition to every destroy/egress.
_FLOOR_OPERATIONS: frozenset[str] = frozenset(
    {"grant", "revoke", "truncate", "drop_database", "drop_schema"}
)

#: Modes whose advertised contract is "no side effects at all". An ``allow`` rule may
#: silence a prompt in ``default``/``auto`` (that's what "Always allow" is for), but it
#: must NOT re-open a mutation these modes refuse — an operator picks ``read_only`` /
#: ``plan`` precisely to point the agent at untrusted material, and a blanket rule
#: persisted weeks earlier (or the read_only clamp the harness forces on an explore
#: subagent) would otherwise silently waive the whole contract.
_NO_MUTATION_MODES: frozenset[str] = frozenset({"read_only", "plan"})

#: How much each stance asks of a person, strictest first. A standing ALLOW is the
#: record of ONE card answered under ONE stance, and it outlives the ask, the turn
#: and the chat — so the stance has to travel with it. A reader who moves a chat to
#: a stance that asks more has said "ask me", and a rule minted where less was asked
#: must not answer for them.
_STANCE_ASKS: dict[str, int] = {"read_only": 0, "plan": 0, "default": 1, "auto": 2, "bypass": 3}


def grant_reaches(*, granted_in: str | None, mode: str) -> bool:
    """Whether a standing ALLOW granted under ``granted_in`` may decide under ``mode``.

    ``granted_in is None`` is a rule with no stance on it — one a person WROTE into
    ``permissions.yml``, which is a statement about the project rather than the record
    of one answered card, and binds wherever a rule binds at all. A recorded stance
    binds its own stance and every LOOSER one; a stricter stance asks again.

    An unrecognised stance on either side reaches nothing: a grant must never widen on
    a word that cannot be ranked."""
    if granted_in is None:
        return True
    granted = _STANCE_ASKS.get(granted_in)
    in_force = _STANCE_ASKS.get(mode)
    if granted is None or in_force is None:
        return False
    return in_force >= granted


#: What the model is told about ``Effect.MEMORY``, spelled once: every mode's steering,
#: the data-source rules, the root prompt and the knowledge tools' own descriptions all
#: quote it. A model in read-only mode once read "don't persist anything" as covering
#: the knowledge base and refused to save what the user asked it to remember.
KNOWLEDGE_IS_MEMORY = (
    "Saving knowledge is always allowed and expected, in every mode — read-only "
    "and plan included: `context_note` / `context_edit` are your memory, not a change to the "
    "workspace, the user's files, or any data system, so a request to save, note, or "
    "remember something is fulfilled with the knowledge tool, never refused. Only sharing a "
    "note with the team follows the mode; in read-only and plan a note is kept private."
)

#: What the model is told, per permission mode, about a statement that would CHANGE
#: connected data — an INSERT / UPDATE / DELETE / MERGE, DDL, a load, or any connector
#: operation that mutates. Spelled once and quoted by every surface that steers the
#: model (the per-turn mode reminder, the mode-switch notice, the workspace's source
#: brief), so no surface can say "the data is read-only, always" while the mode says
#: the reader is asked. Whether a WRITE runs is the mode's decision, exactly as it is
#: for a file write: ``_mode_default`` below is the gate these sentences describe.
DATA_WRITE_RULES: dict[str, str] = {
    "read_only": (
        "A statement that would change connected data — insert, update, delete, DDL, a "
        "load — is refused in this mode without asking anyone: say so in one line, then "
        "do the read-only part of the request."
    ),
    "plan": (
        "A statement that would change connected data — insert, update, delete, DDL, a "
        "load — is refused in this mode without asking anyone: say so in one line, then "
        "do the read-only part of the request."
    ),
    "default": (
        "A statement that would change connected data — insert, update, delete, DDL, a "
        "load — is NOT refused in this mode: propose it and call the SQL tool, and the "
        "user is asked to approve it on a permission card before it runs. Never refuse "
        "a data write on your own here, and never say the data is read-only."
    ),
    "auto": (
        "A statement that changes connected data runs without asking when it is "
        "recoverable; a destructive one (an unguarded update or delete, a drop, a "
        "truncate) pauses for the user."
    ),
    "bypass": (
        "A statement that changes connected data runs immediately, with no approval "
        "asked — the same as every other action in this mode."
    ),
}

#: How a connection that CANNOT be written — by its own nature, whatever the mode — is
#: described to the model, so the brief states a fact about that one connection and
#: never a blanket rule about the data.
READ_ONLY_CONNECTION_RULE = (
    "is read-only by its nature, in every mode: a write to it is refused with that reason, "
    "and no mode changes it."
)


class AutoDecision(IntEnum):
    """Ordered so ``max`` tightens — a later layer can raise but never lower."""

    ALLOW = 0
    PROMPT = 1
    REJECT = 2


def needs_auto_grounding(*, mode: str, effect: Effect, decided_by: str) -> bool:
    """Whether a decided action is the auto-mode middle the safety judge rules on.
    True only when the deterministic policy resolved a WRITE or an EGRESS by the
    MODE base (a known write / a data egress) OR by UNKNOWN CONFIDENCE (an ambiguous
    write the policy would otherwise prompt) — NOT a floor (``decided_by="floor"``)
    and NOT an explicit rule allow/deny (``"rule"``), which keep their decision.
    The SINGLE predicate behind grounding at EVERY chokepoint (the harness loop +
    the in-tool SQL gate), so auto mode is judged uniformly.

    HARD INVARIANT (do not relax): DESTROY is NEVER in this set — in auto mode an
    irreversible destroy (and the privilege-escalation / mass-mutation floor-ops)
    hits the human floor and is never judged: a person decides it, on a card or
    by an earlier "Always allow" of that exact command line. EGRESS IS judged (the
    judge blocks true off-machine exfiltration), but DESTROY stays human-gated."""
    return (
        mode == "auto"
        and effect in (Effect.WRITE, Effect.EGRESS)
        and decided_by in ("mode", "confidence")
    )


def is_floor(descriptor: ActionDescriptor) -> bool:
    """The destructive/exfiltration/exec floor, which forces at least a prompt.
    ``bypass`` waives DESTROY/EGRESS in ``evaluate_action`` — but NOT EXEC, which
    it refuses without an explicit rule (PERMISSIONS section 6.4)."""
    if descriptor.effect in (Effect.DESTROY, Effect.EGRESS, Effect.EXEC):
        return True
    # An unguarded UPDATE/DELETE is already classified DESTROY by the sqlglot
    # escalation, so the descriptor.effect check above catches it.
    return descriptor.operation in _FLOOR_OPERATIONS


def is_exec(descriptor: ActionDescriptor) -> bool:
    """A statement that runs a program or reaches the filesystem on the DATA
    server — the one tier ``bypass`` does not waive. Read off the REAL effect so a
    reclassify-down cannot strip it."""
    return descriptor.effect == Effect.EXEC


def _binds_floor(descriptor: ActionDescriptor, mode: str) -> bool:
    """Whether this action binds the HUMAN floor (forces ≥ prompt) in ``mode``.

    Identical to :func:`is_floor` for every mode EXCEPT ``auto``, where a pure
    EGRESS is routed to the grounded judge instead of the human (the judge blocks
    true off-machine exfiltration, allows legitimate task egress) — so in auto it
    does NOT bind the floor. DESTROY and the named floor-ops (privilege escalation /
    mass mutation) ALWAYS bind, and a covering intent waives the impact challenge
    above, never this."""
    if mode == "auto":
        return (
            descriptor.effect in (Effect.DESTROY, Effect.EXEC)
            or descriptor.operation in _FLOOR_OPERATIONS
        )
    return is_floor(descriptor)


def _mode_default(effect: Effect, mode: str) -> AutoDecision:
    """The base decision from the active mode + the action's effect (before the
    floor/rule tightening). Mirrors harness ``mode_auto_decision`` for SQL."""
    if effect == Effect.READ:
        return AutoDecision.ALLOW
    if effect == Effect.MEMORY:
        # The knowledge base is the agent's memory: recording what it learned changes
        # neither the workspace nor a data system, so every mode admits it, the
        # no-mutation modes included. A deny rule still tightens it, as always.
        return AutoDecision.ALLOW
    if mode == "read_only":
        return AutoDecision.REJECT  # analyst mode: no mutation at all
    if mode == "auto":
        # Base/fallback for auto: allow the recoverable WRITE middle AND egress (the
        # grounded judge gates BOTH above this in the runtime loop — blocking true
        # off-machine exfiltration); an irreversible DESTROY and an EXEC (a program
        # on the data host) are forced to the human floor here, never the judge.
        # Floor-ops are tightened separately in ``decide``.
        return (
            AutoDecision.PROMPT if effect in (Effect.DESTROY, Effect.EXEC) else AutoDecision.ALLOW
        )
    if mode == "plan":
        return AutoDecision.REJECT  # explore→propose→stop: no side effects
    # default
    return AutoDecision.PROMPT


def decide(
    descriptor: ActionDescriptor,
    *,
    mode: str,
    rule_decision: AutoDecision | None = None,
    floor_descriptor: ActionDescriptor | None = None,
) -> AutoDecision:
    """Net decision: the strictest of (rule-or-mode base) and the floor.

    Tighten-only, in every mode. ``bypass`` is settled in :func:`evaluate_action`,
    where it is labeled and audited.

    ``rule_decision`` is the matching ``.alkera/permissions.yml`` rule's decision
    (None → fall back to the mode default for ``descriptor.effect``; a standing allow
    the stance in force is stricter than is already filtered out there). A rule may
    RELAX the mode base in ``default``/``auto`` — that is what "Always allow"
    persists — but never in ``read_only``/``plan``, whose contract is no mutation at
    all; there the mode base clamps the rule. The floor then tightens: a floor
    action is forced to at least ``prompt`` (it can be raised to reject by an
    explicit deny rule, never lowered to allow).

    ``floor_descriptor``: when an effect reclassification has been applied,
    ``descriptor`` carries the RECLASSIFIED effect (used for the mode base) while
    ``floor_descriptor`` is the ORIGINAL — the floor fires if EITHER is a floor
    action, and in ``read_only``/``plan`` the mode base is taken on EITHER too, so
    reclassification can tighten freely but never pull a real write below the analyst
    modes' refusal nor a destroy/egress/floor-op below its prompt."""
    mode_base = _mode_default(descriptor.effect, mode)
    base = rule_decision if rule_decision is not None else mode_base
    if mode in _NO_MUTATION_MODES:
        # An analyst mode is a ceiling, not a default: neither a persisted rule nor an
        # effect reclassification may re-open a mutation it refuses. The ceiling is taken
        # on the REAL classification too, so `reclassify: insert → read` tightens
        # elsewhere without silently waiving the mode's whole contract here.
        if floor_descriptor is not None:
            mode_base = max(mode_base, _mode_default(floor_descriptor.effect, mode))
        base = max(base, mode_base)
    # Unknown-confidence is treated as at least a write that prompts.
    if descriptor.confidence == "unknown" and base == AutoDecision.ALLOW:
        base = AutoDecision.PROMPT
    # The HUMAN floor (mode-aware: in auto, EGRESS is judged, not floored — see
    # ``_binds_floor``). DESTROY + floor-ops still force ≥ prompt in every mode.
    if _binds_floor(descriptor, mode) or (
        floor_descriptor is not None and _binds_floor(floor_descriptor, mode)
    ):
        base = max(base, AutoDecision.PROMPT)
    return base


@dataclass(frozen=True)
class PolicyResult:
    """The outcome of :func:`evaluate_action` — the decision plus *why* it landed
    there (surfaced in the prompt + the audit log)."""

    decision: AutoDecision
    decided_by: str
    """``read`` | ``rule`` | ``mode`` | ``floor`` | ``confidence`` | ``bypass``."""
    reasons: list[str] = field(default_factory=list)
    effective_effect: Effect = Effect.READ


class GateOutcome(StrEnum):
    """What the measured impact says about a write, above the effect tiers."""

    PROCEED = "proceed"
    """The affected set is empty, so the effect tiers decide alone."""
    COVERED = "covered"
    """Real impact, named by a declared intent. Wanted breakage, recorded."""
    CHALLENGE = "challenge"
    """Real impact and no covering intent."""
    ESCALATE = "escalate"
    """The impact read cannot rule on its own, so a human does."""
    DEGRADED = "degraded"
    """No usable graph. Today's effect-tier decision stands, audited as degraded."""


def gate_outcome(impact: Any, *, covered: bool) -> GateOutcome:
    """Read the impact assessment. Impact is checked BEFORE degradation, so a
    partial read that still found breakage challenges on what it found rather
    than falling back to the effect tiers that see nothing."""
    if impact is None:
        return GateOutcome.PROCEED
    if impact.unsure:
        return GateOutcome.ESCALATE
    if impact.has_impact:
        return GateOutcome.COVERED if covered else GateOutcome.CHALLENGE
    return GateOutcome.DEGRADED if impact.status == "degraded" else GateOutcome.PROCEED


def decide_gate(base: PolicyResult, outcome: GateOutcome, *, mode: str) -> tuple[AutoDecision, str]:
    """Fold the impact outcome into the effect-tier decision, tighten-only.

    Auto mode is the target. A write it would have run unattended is refused once
    when the graph says it breaks something and no intent covers it, and the
    refusal carries the impact path so the agent can answer it. An unsure read
    reaches the human through the same prompt every other mode uses."""
    if outcome is GateOutcome.CHALLENGE and mode == "auto":
        return (AutoDecision.REJECT, "impact")
    if outcome is GateOutcome.CHALLENGE:
        return (max(base.decision, AutoDecision.PROMPT), "impact")
    if outcome is GateOutcome.ESCALATE:
        return (max(base.decision, AutoDecision.PROMPT), "unsure")
    if outcome is GateOutcome.COVERED:
        return (base.decision, "intent")
    return (base.decision, base.decided_by)


def evaluate_action(
    descriptor: ActionDescriptor,
    *,
    mode: str,
    permissions: PermissionsConfig | None = None,
    shared_host: bool = False,
    owner: str = "",
) -> PolicyResult:
    """The full Layer-A policy decision for one action: reclassify → rules →
    floor → ``decide``, with provenance. PURE (no broker, no I/O).

    ``bypass`` resolves here, labeled ``bypass`` so the caller records what it
    waived instead of returning before anything is measured.

    The effect-reclassification (``permissions.reclassify``) changes the tier the
    POLICY reasons over while the floor + the rule precedence still bind the REAL
    classification — so a reclassify can tighten freely but can never pull a real
    destroy/egress/floor op below its prompt. An EXEC is never lowered at all: its
    effective tier is the higher of the classified and the reclassified one, which
    is EXEC. Shared by ``gate_sql_action`` (the SQL tool body) and the harness
    request resolver (bash/fs at the broker seam).

    ``shared_host`` says the session runs on a box that serves more than one person
    (a cloud box, which carries a fence). There an EXEC is granted only by the box
    owner's policy (:meth:`PermissionRule.grants_exec`), and of the answers recorded
    in the local overlay only those ``owner`` gave decide anything: ``owner`` is the
    chat owner's user id, empty for a chat the box was not told the owner of
    (:meth:`PermissionsConfig.for_host`).
    """
    if permissions is not None:
        permissions = permissions.for_host(shared_host=shared_host, owner=owner)
    override = permissions.reclassified_effect(descriptor) if permissions is not None else None
    if override is not None and is_exec(descriptor):
        # EXEC tops the tier order, so max(classified, reclassified) is EXEC: a rule
        # may never lower it.
        override = None
    effective = (
        descriptor if override is None else descriptor.model_copy(update={"effect": override})
    )
    rule = _matched_rule(descriptor, effective, permissions, mode=mode)
    if mode == "bypass":
        # ``bypass`` waives the asking, never the boundary. It waives DESTROY/EGRESS
        # (its whole point), but NOT EXEC: a statement that runs a program or reaches
        # the filesystem on the data host is refused even here, unless a rule that
        # names ``effect: exec`` grants it and nothing stricter matches.
        if is_exec(descriptor):
            if (
                rule == AutoDecision.ALLOW
                and permissions is not None
                and permissions.grants_exec(descriptor, mode=mode, shared_host=shared_host)
            ):
                return PolicyResult(
                    AutoDecision.ALLOW, "rule", list(descriptor.reasons), descriptor.effect
                )
            return PolicyResult(
                AutoDecision.REJECT,
                "exec_floor",
                [
                    "server-side program execution / filesystem access is refused even in "
                    "bypass unless a rule naming effect: exec allows it",
                    *descriptor.reasons,
                ],
                descriptor.effect,
            )
        return PolicyResult(
            AutoDecision.ALLOW, "bypass", list(descriptor.reasons), effective.effect
        )

    # A genuine read (original AND effective both read) is allowed outright — we
    # require BOTH so a destroy reclassified DOWN to read can't skip the floor,
    # AND no tightening rule: an explicit deny/ask must bind reads too (deny
    # network fetches, force review of prod reads). The fast path swallowing a
    # matched rule would silently drop the strongest control a user can write.
    if (
        descriptor.effect == Effect.READ
        and effective.effect == Effect.READ
        and (rule is None or rule == AutoDecision.ALLOW)
    ):
        return PolicyResult(AutoDecision.ALLOW, "read", list(descriptor.reasons), Effect.READ)

    if (
        rule == AutoDecision.ALLOW
        and mode not in _NO_MUTATION_MODES
        and permissions is not None
        and descriptor.effect == Effect.DESTROY
        and standing_allow_scope(descriptor) == "exact"
        and permissions.exact_allow(descriptor, mode=mode)
    ):
        # The destroy floor asks a person. A person already answered for this
        # exact command line, in this stance or a stricter one, and nothing
        # stricter matches: that answer is the one the floor would ask for. A
        # different line — other arguments, other paths — asks again.
        return PolicyResult(AutoDecision.ALLOW, "rule", list(descriptor.reasons), descriptor.effect)

    decision = decide(effective, mode=mode, rule_decision=rule, floor_descriptor=descriptor)
    decided_by = _provenance(descriptor, effective, mode=mode, rule=rule, decision=decision)
    return PolicyResult(decision, decided_by, list(descriptor.reasons), effective.effect)


def _matched_rule(
    descriptor: ActionDescriptor,
    effective: ActionDescriptor,
    permissions: PermissionsConfig | None,
    *,
    mode: str,
) -> AutoDecision | None:
    """The strictest matching rule. Rules are tighten-only, so a deny or ask on the
    REAL effect must still win after a reclassify-down. ``mode`` is the stance in
    force, which a standing allow has to reach (:func:`grant_reaches`)."""
    if permissions is None:
        return None
    matched = [
        r
        for r in (
            permissions.rule_decision(descriptor, mode=mode),
            permissions.rule_decision(effective, mode=mode),
        )
        if r is not None
    ]
    return max(matched) if matched else None


def _provenance(
    descriptor: ActionDescriptor,
    effective: ActionDescriptor,
    *,
    mode: str,
    rule: AutoDecision | None,
    decision: AutoDecision,
) -> str:
    """Which layer is responsible for the final decision. The FLOOR owns the label
    whenever a floor action lands on PROMPT, even when the mode base already
    prompted, because the audit must say WHY a destroy paused. An explicit deny
    rule that tightens it to REJECT takes the label instead."""
    if (_binds_floor(descriptor, mode) or _binds_floor(effective, mode)) and (
        decision == AutoDecision.PROMPT
    ):
        return "floor"
    if rule is not None and decision == rule:
        return "rule"
    if effective.confidence == "unknown" and rule is None and decision == AutoDecision.PROMPT:
        return "confidence"
    return "mode"


__all__ = [
    "DATA_WRITE_RULES",
    "KNOWLEDGE_IS_MEMORY",
    "READ_ONLY_CONNECTION_RULE",
    "AutoDecision",
    "GateOutcome",
    "PolicyResult",
    "decide",
    "decide_gate",
    "evaluate_action",
    "gate_outcome",
    "grant_reaches",
    "is_exec",
    "is_floor",
    "needs_auto_grounding",
]
