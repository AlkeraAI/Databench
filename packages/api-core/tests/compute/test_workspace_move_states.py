"""The workspace move's state machine: every legal edge, every other edge
refused, and cancel only before the chats move."""

from __future__ import annotations

import itertools

import pytest
from alkera_core.compute.workspace_move import (
    CANCELABLE_STATES,
    ERROR_WORDS,
    MOVE_ERROR_CODES,
    TRANSITIONS,
    MoveTransitionError,
    check_transition,
    is_active,
    may_cancel,
    may_transition,
)
from alkera_core.models.org_machines import MOVE_FINISHED_STATES, MOVE_STATES

LEGAL = {
    ("requested", "draining"),
    ("requested", "failed"),
    ("requested", "canceled"),
    ("draining", "switching"),
    ("draining", "failed"),
    ("draining", "canceled"),
    ("switching", "waking"),
    ("switching", "failed"),
    ("waking", "done"),
    ("waking", "failed"),
}


@pytest.mark.parametrize(
    ("source", "target"),
    [pytest.param(s, t, id=f"{s}->{t}") for s, t in sorted(LEGAL)],
)
def test_every_legal_edge_is_taken(source: str, target: str) -> None:
    assert may_transition(source, target)
    check_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        pytest.param(s, t, id=f"{s}->{t}")
        for s, t in itertools.product(MOVE_STATES, MOVE_STATES)
        if (s, t) not in LEGAL
    ],
)
def test_every_other_edge_is_refused(source: str, target: str) -> None:
    assert not may_transition(source, target)
    with pytest.raises(MoveTransitionError):
        check_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        pytest.param("requested", "sleeping", id="unknown-target"),
        pytest.param("paused", "draining", id="unknown-source"),
    ],
)
def test_a_state_outside_the_vocabulary_is_refused(source: str, target: str) -> None:
    with pytest.raises(MoveTransitionError):
        check_transition(source, target)


def test_the_table_covers_every_state_and_nothing_else() -> None:
    assert set(TRANSITIONS) == set(MOVE_STATES)
    assert {(s, t) for s, targets in TRANSITIONS.items() for t in targets} == LEGAL


@pytest.mark.parametrize(
    ("state", "cancelable"),
    [
        pytest.param("requested", True, id="requested"),
        pytest.param("draining", True, id="draining"),
        pytest.param("switching", False, id="switching"),
        pytest.param("waking", False, id="waking"),
        pytest.param("done", False, id="done"),
        pytest.param("failed", False, id="failed"),
        pytest.param("canceled", False, id="canceled"),
    ],
)
def test_cancel_only_before_the_chats_move(state: str, cancelable: bool) -> None:
    assert may_cancel(state) is cancelable
    assert may_transition(state, "canceled") is cancelable
    assert (state in CANCELABLE_STATES) is cancelable


@pytest.mark.parametrize(
    ("state", "active"),
    [pytest.param(s, s not in MOVE_FINISHED_STATES, id=s) for s in MOVE_STATES]
    + [pytest.param("unknown", False, id="unknown")],
)
def test_active_is_every_state_that_has_not_ended(state: str, active: bool) -> None:
    assert is_active(state) is active


def test_failed_is_reachable_from_every_state_that_has_not_ended() -> None:
    for state in MOVE_STATES:
        assert may_transition(state, "failed") is (state not in MOVE_FINISHED_STATES)


def test_every_error_code_has_words_for_the_reader() -> None:
    assert set(ERROR_WORDS) == set(MOVE_ERROR_CODES)
    assert set(MOVE_ERROR_CODES) == {
        "target_capacity",
        "target_boot_failed",
        "drain_timeout",
        "not_allowed",
        "target_deleted",
        "wake_timeout",
        "not_started",
    }
    assert all(words and "—" not in words for words in ERROR_WORDS.values())
