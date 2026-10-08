"""The lifecycle's storage: the CHECK names ten states, the history persists.

The pure edge table is exhausted in ``packages/api-core/tests/compute``; this
module proves what only Postgres can — a state outside the vocabulary cannot be
committed however it is written, and an edge committed through ``transition``
is readable back, in order, from ``compute_allocation_events``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.compute.transitions import transition
from alkera_core.models.compute import ComputeAllocation, ComputeAllocationEvent
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


async def _alloc(session: AsyncSession, org: OrgWithAdmin, state: str) -> ComputeAllocation:
    mt = await make_machine_type(session, provider="ec2")
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        tenancy="pool",
        state=state,
    )
    session.add(alloc)
    await session.commit()
    return alloc


@pytest.mark.parametrize("state", ["bootstrapping", "lost", "asleep"])
async def test_the_extra_states_are_storable(
    real_session: AsyncSession, org_admin: OrgWithAdmin, state: str
) -> None:
    alloc = await _alloc(real_session, org_admin, state)
    assert (await real_session.get(ComputeAllocation, alloc.id)) is not None


@pytest.mark.parametrize("bogus", ["running", "stopped", "READY"])
async def test_a_state_outside_the_vocabulary_is_refused_by_the_database(
    real_session: AsyncSession, org_admin: OrgWithAdmin, bogus: str
) -> None:
    alloc = await _alloc(real_session, org_admin, "ready")
    with pytest.raises(IntegrityError, match="ck_compute_allocations_state"):
        await real_session.execute(
            text("UPDATE compute_allocations SET state = :s WHERE id = :i"),
            {"s": bogus, "i": alloc.id},
        )
    await real_session.rollback()


async def test_committed_edges_read_back_in_order(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    alloc = await _alloc(real_session, org_admin, "provisioning")
    t0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    actor = {"kind": "system", "id": "reconcile"}
    transition(real_session, alloc, "bootstrapping", reason="running", actor=actor, now=t0)
    transition(
        real_session, alloc, "ready", reason="claimed", actor=actor, now=t0 + timedelta(minutes=3)
    )
    await real_session.commit()
    rows = (
        (
            await real_session.execute(
                select(ComputeAllocationEvent)
                .where(ComputeAllocationEvent.allocation_id == alloc.id)
                .order_by(ComputeAllocationEvent.at)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.from_state, r.to_state, r.reason, r.actor) for r in rows] == [
        ("provisioning", "bootstrapping", "running", actor),
        ("bootstrapping", "ready", "claimed", actor),
    ]
    await real_session.refresh(alloc)
    assert alloc.state == "ready"
    assert alloc.state_changed_at == t0 + timedelta(minutes=3)
