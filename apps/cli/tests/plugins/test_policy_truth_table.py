"""THE policy truth table — a reviewable, exhaustive (effect x confidence x mode)
matrix for ``decide()``.

This file exists to make the policy AUDITABLE: read the table top-to-bottom and a
wrong cell is visible at a glance. (A guarded ``DELETE`` once classified as a
recoverable ``write`` — a regression a per-case test pinned silently. A table you
can scan catches a wrong answer the way a scattered set of asserts can't.)

``bypass`` has no column. It is a total skip-all resolved in ``evaluate_action``,
where it can be labeled and audited, so it never reaches ``decide`` at all -- the
decided-and-recorded bypass is pinned in ``test_gate_impact.py``.

The table tests the PURE policy (``decide()`` with no rules/reclassify) — i.e.
"given an action of this effect+confidence, what does each mode do?". Whether a
given *command* classifies to a given effect is the classifier's job, covered by
``test_permissions_bash.py`` / ``test_sql_classifier.py``.
"""

from __future__ import annotations

import pytest
from alkera_cli.contracts.tool_types import ActionDescriptor, Confidence, Effect
from alkera_cli.plugins.plugin_base.permissions import AutoDecision, decide, evaluate_action

A = AutoDecision.ALLOW
P = AutoDecision.PROMPT
R = AutoDecision.REJECT

# The 4 human-decidable modes, in increasing leniency, are the columns.
_MODES = ("read_only", "plan", "default", "auto")

# Each row: (effect, confidence) -> the decision in each mode.
# READ-row note: a real read is always confidence="exact" (a parse failure fails
# CLOSED to write, never read), so READ+unknown isn't a reachable cell.
#
#                                     read_only  plan   default  auto
_TRUTH_TABLE: list[tuple[Effect, Confidence, tuple[AutoDecision, ...]]] = [
    # reads: explore freely everywhere (even read_only / plan allow reads).
    (Effect.READ, "exact", (A, A, A, A)),
    # recoverable writes: prompt by default; auto runs them; analyst/plan reject.
    (Effect.WRITE, "exact", (R, R, P, A)),
    # an UNKNOWN-confidence write NEVER silently runs under auto (downgrades to prompt).
    (Effect.WRITE, "unknown", (R, R, P, P)),
    (Effect.WRITE, "heuristic", (R, R, P, A)),  # heuristic is treated like exact
    # destroy is the floor: prompts under auto AND default.
    (Effect.DESTROY, "exact", (R, R, P, P)),
    (Effect.DESTROY, "unknown", (R, R, P, P)),
    # egress (data leaving) is the floor in every mode EXCEPT auto, where it's routed
    # to the grounded judge — so at the PURE policy level auto ALLOWs it (like a
    # write) and the judge gates true exfiltration above this; default still prompts.
    (Effect.EGRESS, "exact", (R, R, P, A)),
    # an UNKNOWN-confidence egress still downgrades to prompt in auto (unparseable).
    (Effect.EGRESS, "unknown", (R, R, P, P)),
]


def _desc(effect: Effect, confidence: Confidence, operation: str = "op") -> ActionDescriptor:
    return ActionDescriptor(
        capability="shell", effect=effect, operation=operation, confidence=confidence, raw="x"
    )


@pytest.mark.parametrize(
    ("effect", "confidence", "expected_per_mode"),
    [pytest.param(e, c, row, id=f"{e.value}-{c}") for e, c, row in _TRUTH_TABLE],
)
def test_policy_truth_table(
    effect: Effect, confidence: Confidence, expected_per_mode: tuple[AutoDecision, ...]
) -> None:
    desc = _desc(effect, confidence)
    for mode, expected in zip(_MODES, expected_per_mode, strict=True):
        got = decide(desc, mode=mode)
        assert got == expected, (
            f"{effect.value}/{confidence} in {mode}: expected {expected.name}, got {got.name}"
        )


# --- floor OPERATIONS: bind every mode ---------------------------------------
# grant/revoke/truncate/drop_database/drop_schema are floor even if the effect
# were (mis)inferred as a recoverable write — so they prompt under auto/default.
_FLOOR_OPS = ["grant", "revoke", "truncate", "drop_database", "drop_schema"]


@pytest.mark.parametrize("operation", _FLOOR_OPS)
@pytest.mark.parametrize("mode", ["auto", "default"])
def test_floor_operations_prompt_even_when_effect_looks_like_write(
    operation: str, mode: str
) -> None:
    # effect=WRITE on purpose: the floor must fire on the OPERATION, not just the effect.
    desc = _desc(Effect.WRITE, "exact", operation=operation)
    assert decide(desc, mode=mode) >= AutoDecision.PROMPT


@pytest.mark.parametrize("operation", _FLOOR_OPS)
def test_floor_operations_still_reject_in_read_only(operation: str) -> None:
    desc = _desc(Effect.WRITE, "exact", operation=operation)
    assert decide(desc, mode="read_only") == AutoDecision.REJECT


def test_bypass_settles_as_a_labeled_allow() -> None:
    """Bypass never reaches the table. It resolves one layer up, as an ALLOW that
    says bypass decided it, so the mode that waives every control is still the
    subject of a record. One case suffices: the short-circuit runs before any
    operation-sensitive logic."""
    out = evaluate_action(_desc(Effect.DESTROY, "unknown", operation="truncate"), mode="bypass")
    assert (out.decision, out.decided_by) == (AutoDecision.ALLOW, "bypass")


def test_an_explicit_deny_rule_tightens_every_cell() -> None:
    for effect in (Effect.READ, Effect.WRITE, Effect.DESTROY, Effect.EGRESS):
        desc = _desc(effect, "exact")
        for mode in _MODES:
            got = decide(desc, mode=mode, rule_decision=AutoDecision.REJECT)
            assert got == AutoDecision.REJECT, f"{effect.value} deny in {mode}: got {got.name}"


def test_an_allow_rule_cannot_lower_the_floor() -> None:
    # An allow rule relaxes a recoverable write, and (in auto) a data egress, but it
    # can NEVER pull the DESTROY floor below prompt. EGRESS is floored OUTSIDE auto,
    # so an allow rule can't lower it there either.
    assert decide(_desc(Effect.WRITE, "exact"), mode="default", rule_decision=A) == A
    assert decide(_desc(Effect.DESTROY, "exact"), mode="auto", rule_decision=A) == P
    assert decide(_desc(Effect.DESTROY, "exact"), mode="default", rule_decision=A) == P
    # egress: floored (→ prompt) in default even with an allow rule; in auto it is
    # judged, so the allow rule lets it through at the policy level (→ allow).
    assert decide(_desc(Effect.EGRESS, "exact"), mode="default", rule_decision=A) == P
    assert decide(_desc(Effect.EGRESS, "exact"), mode="auto", rule_decision=A) == A
