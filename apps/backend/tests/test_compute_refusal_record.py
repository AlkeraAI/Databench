"""A refused start is on record ONCE per window, not once per retry.

A refusal is an answer only a human changes: no grant, no credit, the ceiling in
use. The box that was refused keeps asking (its mirror re-registers on a timer,
a browser retries), and every ask used to write a ``refused`` frame — which
re-invalidates every open browser in the org — and an org audit row. A box
refused all day produced thousands of identical rows, which buries the trail the
audit exists to make legible.

So the FIRST refusal of each (org, machine type, reason) inside
``compute_refusal_record_window_seconds`` is recorded in full and the repeats are
not re-filed. What the caller gets back never changes: the same typed error, the
same status, the same code.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgAuditEvent, User
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from backend.services.compute.grants import (
    REFUSED_AUDIT_ACTION,
    ComputeLimitReachedError,
    NoComputeGrantError,
    admit,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import make_grant, make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.compute_rows


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _user(session: AsyncSession, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    assert user is not None
    return user


async def _frames(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "compute_machine.changed",
                EventOutbox.entity_id == str(org_id),
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _audit(org_id: UUID) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(OrgAuditEvent)
            .where(
                OrgAuditEvent.org_team_id == org_id,
                OrgAuditEvent.action == REFUSED_AUDIT_ACTION,
            )
            .order_by(OrgAuditEvent.created_at)
        )
        return list(rows.scalars().all())


async def _refuse_no_grant(
    session: AsyncSession, org: OrgWithAdmin, mt: ComputeMachineType, *also: ComputeMachineType
) -> str:
    """One start the plane refuses for want of a grant, rolled back the way the
    route's failed request is. Returns the wire code. Every row the caller still
    reads is reloaded — a rollback expires them all."""
    user = await _user(session, org.admin_id)
    with pytest.raises(NoComputeGrantError) as info:
        await admit(session, ctx=_ctx(org), user=user, org_team_id=org.org_id, machine_type=mt)
    await session.rollback()
    for row in (mt, *also):
        await session.refresh(row)
    return info.value.code


async def test_an_identical_refusal_inside_the_window_is_not_filed_again(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Ten identical refusals: one frame, one audit row — and ten refusals."""
    mt = await make_machine_type(real_session)
    codes = [await _refuse_no_grant(real_session, org_admin, mt) for _ in range(10)]

    assert codes == ["no_compute_grant"] * 10, "every caller is still refused, with the code"
    frames = await _frames(org_admin.org_id)
    assert [f.payload["reason"] for f in frames] == ["no_compute_grant"]
    rows = await _audit(org_admin.org_id)
    assert len(rows) == 1
    assert rows[0].detail["reason"] == "no_compute_grant"
    assert rows[0].target == mt.provider_type_id


async def test_a_different_reason_is_recorded_even_inside_the_window(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The dedupe is per REASON: the day the refusal changes from 'no grant' to
    'the ceiling is in use', the trail says so immediately."""
    mt = await make_machine_type(real_session)
    await _refuse_no_grant(real_session, org_admin, mt)

    grant = await make_grant(
        real_session, org_team_id=org_admin.org_id, machine_type_id=mt.id, ceiling=1
    )
    real_session.add(
        ComputeAllocation(
            user_id=org_admin.admin_id,
            org_team_id=org_admin.org_id,
            machine_type_id=mt.id,
            grant_id=grant.id,
            state="ready",
        )
    )
    await real_session.commit()
    user = await _user(real_session, org_admin.admin_id)
    with pytest.raises(ComputeLimitReachedError):
        await admit(
            real_session,
            ctx=_ctx(org_admin),
            user=user,
            org_team_id=org_admin.org_id,
            machine_type=mt,
        )
    await real_session.rollback()

    assert [r.detail["reason"] for r in await _audit(org_admin.org_id)] == [
        "no_compute_grant",
        "compute_limit_reached",
    ]
    assert [f.payload["reason"] for f in await _frames(org_admin.org_id)] == [
        "no_compute_grant",
        "compute_limit_reached",
    ]


async def test_a_different_machine_type_is_recorded_even_inside_the_window(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The dedupe is per MACHINE TYPE: a refusal for the GPU box is not hidden
    by one already on record for the CPU box."""
    cpu = await make_machine_type(real_session, compute_class="cpu")
    gpu = await make_machine_type(real_session, compute_class="gpu")
    await _refuse_no_grant(real_session, org_admin, cpu, gpu)
    await _refuse_no_grant(real_session, org_admin, gpu, cpu)
    await _refuse_no_grant(real_session, org_admin, cpu, gpu)

    assert sorted(r.target or "" for r in await _audit(org_admin.org_id)) == sorted(
        [cpu.provider_type_id, gpu.provider_type_id]
    )


async def test_the_same_refusal_is_recorded_again_once_the_window_passes(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trail keeps a heartbeat: a box refused for hours leaves one row per
    window, so 'this was still refused at 4pm' is answerable."""
    # The boundary is moved off the wall clock and onto the row. Asserting that
    # two service calls and their commits BOTH landed inside one second was
    # asserting how busy the one Postgres this run shares was: a checkpoint or
    # a neighbour's session checkout between them wrote a second row and failed
    # a claim that is not about seconds. An hour-wide window cannot be crossed
    # by any load, and the "window passed" half backdates the row the window is
    # measured from — the row's own ``created_at`` is the DATABASE clock, so
    # moving it there keeps both sides of the comparison on one clock, which
    # freezing the Python side would not.
    monkeypatch.setattr(settings, "compute_refusal_record_window_seconds", 3600)
    mt = await make_machine_type(real_session)
    await _refuse_no_grant(real_session, org_admin, mt)
    await _refuse_no_grant(real_session, org_admin, mt)
    inside = await _audit(org_admin.org_id)
    assert len(inside) == 1, "still inside the window"

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(OrgAuditEvent)
            .where(OrgAuditEvent.id == inside[0].id)
            .values(created_at=OrgAuditEvent.created_at - timedelta(seconds=2))
        )
        await session.commit()
    monkeypatch.setattr(settings, "compute_refusal_record_window_seconds", 1)
    await _refuse_no_grant(real_session, org_admin, mt)

    assert len(await _audit(org_admin.org_id)) == 2
    assert len(await _frames(org_admin.org_id)) == 2


async def test_the_dedupe_can_be_switched_off(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that wants every refusal on record sets the window to 0."""
    monkeypatch.setattr(settings, "compute_refusal_record_window_seconds", 0)
    mt = await make_machine_type(real_session)
    await _refuse_no_grant(real_session, org_admin, mt)
    await _refuse_no_grant(real_session, org_admin, mt)

    assert len(await _audit(org_admin.org_id)) == 2
    assert len(await _frames(org_admin.org_id)) == 2
