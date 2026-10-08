"""Every registered state machine ends from every state, within its bounds.

The table-driven gate: for each registered machine, every state that is not
a rest state has a bound or a reason it needs none, following the bounds from
it reaches rest, a bound that waits on another deadline is longer than it, and
every failure code has one outcome. The gaps (states nothing ends yet) are
listed here and may only shrink. Decoys show each check refuses what it must.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from alkera_core.config import settings
from alkera_core.lifecycle import (
    Bound,
    Exempt,
    Gap,
    LifecycleError,
    Outcome,
    StateMachine,
    gaps,
    longest_path_to_rest,
    recheck,
    register,
    registered,
)

PRODUCT = {name: machine for name, machine in registered().items() if not name.startswith("test_")}

#: States nothing on the server ends yet. Closing one removes it here.
GAPS_ALLOWED = {
    "compute_allocation.asleep",
    "compute_allocation.draining",
    "compute_allocation.releasing",
    "file_op.queued",
    "machine_credential.unclaimed",
    "notebook_kernel.busy",
    "notebook_kernel.idle",
    "notebook_kernel.restarting",
    "notebook_kernel.starting",
    "upload_session.committing",
}


@pytest.mark.parametrize("name", sorted(PRODUCT))
def test_every_non_rest_state_reaches_rest_within_its_ceiling(name: str) -> None:
    machine = PRODUCT[name]
    for state in sorted(machine.states - machine.rest):
        rule = machine.bounds[state]
        assert isinstance(rule, Bound | Exempt | Gap), f"{name}.{state}"
        assert longest_path_to_rest(machine, state) <= machine.ceiling, f"{name}.{state}"
        if isinstance(rule, Bound):
            assert rule.ender, f"{name}.{state} names who ends it"


def test_the_gaps_only_shrink() -> None:
    assert set(gaps()) == GAPS_ALLOWED


def test_the_move_reaches_rest_in_bounded_time_from_every_state() -> None:
    move = PRODUCT["workspace_move"]
    # requested 10 min, draining 120+30+60+60 s, switching 2 min, waking
    # 300 s, each with the recovery sweep's minute of slack.
    assert longest_path_to_rest(move, "waking") == timedelta(seconds=300 + 60)
    assert longest_path_to_rest(move, "requested") == timedelta(seconds=600 + 60)
    assert longest_path_to_rest(move, "draining") == timedelta(seconds=270 + 60)


def test_every_move_failure_code_has_one_outcome_that_puts_the_pin_back() -> None:
    from alkera_core.compute.workspace_move import MOVE_ERROR_CODES

    move = PRODUCT["workspace_move"]
    assert set(MOVE_ERROR_CODES) <= set(move.outcomes)
    for code, outcome in move.outcomes.items():
        assert outcome.lands_in == "failed", code
        assert "restore_pin" in outcome.effects, code
        assert outcome.words.endswith("."), code


def test_a_wake_timeout_configured_at_the_lease_ttl_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The move that hung for ten minutes: its wake deadline equalled the
    deadline it waited on. Configured that way, the registry refuses it."""
    recheck("workspace_move")
    monkeypatch.setattr(
        settings, "move_wake_timeout_seconds", 60 + settings.files_lease_grant_delay_seconds
    )
    with pytest.raises(
        LifecycleError, match=r"workspace_move\.waking .* must exceed move\.hand_back"
    ):
        recheck("workspace_move")


def test_a_turn_abandoned_before_it_reads_stalled_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "chat_turn_abandon_seconds", settings.chat_turn_silence_seconds)
    with pytest.raises(LifecycleError, match=r"must exceed chat\.turn_silence"):
        recheck("chat_turn")


# ---- decoys -----------------------------------------------------------------


def _machine(name: str, **over: object) -> StateMachine:
    fields: dict[str, object] = {
        "name": f"test_{name}",
        "states": frozenset({"a", "b", "done"}),
        "rest": frozenset({"done"}),
        "edges": {"a": frozenset({"b"}), "b": frozenset({"done"})},
        "bounds": {
            "a": Bound(timedelta(seconds=10), "b", ender="x"),
            "b": Bound(timedelta(seconds=10), "done", ender="x"),
        },
    }
    fields.update(over)
    return StateMachine(**fields)  # type: ignore[arg-type]


def test_a_well_formed_machine_registers() -> None:
    machine = register(_machine("fine"))
    assert longest_path_to_rest(machine, "a") == timedelta(seconds=20) + 2 * timedelta(seconds=60)


@pytest.mark.parametrize(
    ("over", "why"),
    [
        pytest.param(
            {"bounds": {"a": Bound(timedelta(seconds=10), "b", ender="x")}},
            "test_unbounded.b has no bound and no exemption",
            id="unbounded",
        ),
        pytest.param(
            {
                "edges": {"a": frozenset({"b"}), "b": frozenset({"a", "done"})},
                "bounds": {
                    "a": Bound(timedelta(seconds=10), "b", ender="x"),
                    "b": Bound(timedelta(seconds=10), "a", ender="x"),
                },
            },
            "loop",
            id="cycle",
        ),
        pytest.param(
            {
                "bounds": {
                    "a": Bound(timedelta(seconds=10), "done", ender="x"),
                    "b": Bound(timedelta(seconds=10), "done", ender="x"),
                }
            },
            "not an edge",
            id="lands-off-its-edges",
        ),
        pytest.param(
            {
                "bounds": {
                    "a": Bound(timedelta(seconds=60), "b", ender="x"),
                    "b": Bound(
                        timedelta(seconds=60), "done", must_exceed=("test_equal.a",), ender="x"
                    ),
                }
            },
            "must exceed",
            id="equal-dependent-deadline",
        ),
        pytest.param(
            {
                "bounds": {
                    "a": Bound(timedelta(seconds=10), "b", "boom", ender="x"),
                    "b": Bound(timedelta(seconds=10), "done", ender="x"),
                }
            },
            "has no outcome",
            id="code-with-no-outcome",
        ),
        pytest.param(
            {
                "bounds": {
                    "a": Bound(timedelta(seconds=10), "b", ender="x"),
                    "b": Exempt("", "a person"),
                }
            },
            "names its reason",
            id="exemption-with-no-reason",
        ),
        pytest.param(
            {"ceiling": timedelta(seconds=30)},
            "past",
            id="past-its-ceiling",
        ),
        pytest.param(
            {"outcomes": {"x": Outcome("nowhere")}},
            "undeclared state",
            id="outcome-lands-nowhere",
        ),
    ],
)
def test_a_machine_that_may_not_end_is_refused(
    over: dict[str, object], why: str, request: pytest.FixtureRequest
) -> None:
    name = {"equal-dependent-deadline": "equal", "unbounded": "unbounded"}.get(
        request.node.callspec.id, request.node.callspec.id.replace("-", "_")
    )
    with pytest.raises(LifecycleError, match=why):
        register(_machine(name, **over))
    assert f"test_{name}" not in registered()
