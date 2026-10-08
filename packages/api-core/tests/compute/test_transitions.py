"""The machine lifecycle is a table, and every cell of it is exercised.

Every (from, to) pair over the ten states is one case: a pair the table names
moves the row and records exactly that edge; every other pair raises and leaves
the row and the session untouched.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime

import pytest
from alkera_core.compute.transitions import (
    LEGAL_EDGES,
    IllegalTransitionError,
    revive,
    transition,
)
from alkera_core.models.compute import (
    COMPUTE_ALLOCATION_STATES,
    ComputeAllocation,
    ComputeAllocationEvent,
)
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)

#: The lifecycle as the product describes it, written out independently of the
#: module's table so an edge added or dropped there is a red case here.
EXPECTED_EDGES = {
    ("pending", "provisioning"),
    ("pending", "failed"),
    ("provisioning", "bootstrapping"),
    ("provisioning", "ready"),
    ("provisioning", "failed"),
    ("provisioning", "releasing"),
    ("bootstrapping", "ready"),
    ("bootstrapping", "failed"),
    ("bootstrapping", "releasing"),
    ("ready", "draining"),
    ("ready", "asleep"),
    ("ready", "releasing"),
    ("ready", "lost"),
    ("ready", "failed"),
    ("draining", "ready"),
    ("draining", "asleep"),
    ("draining", "releasing"),
    ("draining", "lost"),
    ("draining", "failed"),
    ("asleep", "ready"),
    ("asleep", "releasing"),
    ("asleep", "lost"),
    ("asleep", "failed"),
    ("releasing", "released"),
    ("releasing", "failed"),
    ("lost", "released"),
    ("failed", "released"),
}

ALL_PAIRS = list(itertools.product(COMPUTE_ALLOCATION_STATES, repeat=2))


def _alloc(state: str) -> ComputeAllocation:
    return ComputeAllocation(id=uuid.uuid4(), state=state, ready_at=None, released_at=None)


def test_the_table_names_every_state_and_only_real_ones() -> None:
    assert set(LEGAL_EDGES) == set(COMPUTE_ALLOCATION_STATES)
    assert len(COMPUTE_ALLOCATION_STATES) == 10
    for targets in LEGAL_EDGES.values():
        assert targets <= set(COMPUTE_ALLOCATION_STATES)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [pytest.param(a, b, id=f"{a}->{b}") for a, b in ALL_PAIRS if (a, b) in EXPECTED_EDGES],
)
def test_a_legal_edge_moves_the_row_and_records_itself(from_state: str, to_state: str) -> None:
    session = AsyncSession()
    alloc = _alloc(from_state)
    actor = {"kind": "user", "id": "u-1"}
    event = transition(session, alloc, to_state, reason="why", actor=actor, now=NOW)
    assert alloc.state == to_state
    assert alloc.state_changed_at == NOW
    assert isinstance(event, ComputeAllocationEvent)
    assert (event.allocation_id, event.from_state, event.to_state) == (
        alloc.id,
        from_state,
        to_state,
    )
    assert (event.reason, event.actor, event.at) == ("why", actor, NOW)
    assert event in session.new
    if to_state == "ready":
        assert alloc.ready_at == NOW
    if to_state in ("released", "failed"):
        assert alloc.released_at == NOW


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [pytest.param(a, b, id=f"{a}->{b}") for a, b in ALL_PAIRS if (a, b) not in EXPECTED_EDGES],
)
def test_an_illegal_edge_raises_and_changes_nothing(from_state: str, to_state: str) -> None:
    session = AsyncSession()
    alloc = _alloc(from_state)
    with pytest.raises(IllegalTransitionError) as caught:
        transition(session, alloc, to_state, reason="why", now=NOW)
    assert (caught.value.from_state, caught.value.to_state) == (from_state, to_state)
    assert alloc.state == from_state
    assert alloc.state_changed_at is None
    assert not session.new


@pytest.mark.parametrize("bogus", ["", "READY", "stopped", "running"])
def test_a_state_outside_the_vocabulary_is_refused(bogus: str) -> None:
    alloc = _alloc("ready")
    with pytest.raises(IllegalTransitionError):
        transition(AsyncSession(), alloc, bogus, now=NOW)
    assert alloc.state == "ready"


def test_ready_at_keeps_the_first_time_a_machine_came_up() -> None:
    session = AsyncSession()
    alloc = _alloc("draining")
    first = datetime(2026, 9, 1, tzinfo=UTC)
    alloc.ready_at = first
    transition(session, alloc, "ready", now=NOW)
    assert alloc.ready_at == first


#: The one move out of a finished state: a REGISTERED box registering again.
EXPECTED_REVIVABLE = {"releasing", "released", "failed"}


@pytest.mark.parametrize(
    ("origin", "from_state"),
    [
        pytest.param(origin, state, id=f"{origin}-{state}")
        for origin in ("registered", "provisioned")
        for state in COMPUTE_ALLOCATION_STATES
    ],
)
def test_only_a_finished_registered_box_is_revived(origin: str, from_state: str) -> None:
    session = AsyncSession()
    alloc = _alloc(from_state)
    alloc.origin = origin
    alloc.released_at = NOW
    if origin == "registered" and from_state in EXPECTED_REVIVABLE:
        revive(session, alloc, reason="back", actor={"kind": "box"}, now=NOW)
        assert (alloc.state, alloc.ready_at, alloc.released_at) == ("ready", NOW, None)
        (event,) = session.new
        assert isinstance(event, ComputeAllocationEvent)
        assert (event.from_state, event.to_state, event.reason) == (from_state, "ready", "back")
        return
    with pytest.raises(IllegalTransitionError):
        revive(session, alloc, reason="back", now=NOW)
    assert alloc.state == from_state
    assert alloc.released_at == NOW
    assert not session.new
