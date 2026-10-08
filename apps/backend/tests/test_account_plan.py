"""The deletion plan's blockers and fates, each driven by the row that causes it.

Every case seeds one condition on an otherwise clean account and asserts the
plan names exactly that blocker, and that the neighbouring condition which
must NOT block (a cancelled plan, a box on the person's own hardware, a machine
already released) leaves the plan clear.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.account.plan import compute_plan
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.files import FileHold
from sqlalchemy import text
from tests._compute_helpers import make_machine_type
from tests._org_machine_rows import make_org_machine
from tests.conftest import OrgWithAdmin, make_member

pytestmark = pytest.mark.asyncio


async def _codes(user_id: uuid.UUID) -> list[tuple[str, uuid.UUID | None]]:
    async with AsyncSessionLocal() as session:
        plan = await compute_plan(session, user_id)
        await session.rollback()
    return [(b.code, b.org_id) for b in plan.blockers]


async def test_a_clean_solo_account_has_no_blockers(org_admin: OrgWithAdmin) -> None:
    assert await _codes(org_admin.admin_id) == []


async def test_platform_staff_must_drop_the_role_first(platform_admin: OrgWithAdmin) -> None:
    assert await _codes(platform_admin.admin_id) == [("platform_staff", None)]


async def test_a_legal_hold_in_an_org_that_would_close_blocks(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            FileHold(
                org_team_id=org_admin.org_id,
                scope="org",
                matter_ref="matter-1",
                placed_by=org_admin.admin_id,
            )
        )
        await session.commit()
    assert await _codes(org_admin.admin_id) == [("legal_hold", org_admin.org_id)]


async def test_machines_in_an_org_that_would_close_block(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        await make_org_machine(
            session,
            org_id=org_admin.org_id,
            user_id=org_admin.admin_id,
            machine_type=machine_type,
            with_allocation=False,
        )
    assert await _codes(org_admin.admin_id) == [("org_machines", org_admin.org_id)]


async def test_an_org_machine_in_an_org_that_keeps_going_stays_with_the_org(
    org_admin: OrgWithAdmin,
) -> None:
    async with AsyncSessionLocal() as session:
        member, _ = await make_member(session, org_id=org_admin.org_id, verified=True)
        machine_type = await make_machine_type(session)
        await make_org_machine(
            session, org_id=org_admin.org_id, user_id=member.id, machine_type=machine_type
        )
    assert await _codes(member.id) == []


@pytest.mark.parametrize(
    ("tenancy", "state", "blocks"),
    [
        pytest.param("org", "ready", True, id="pooled-live"),
        pytest.param("dedicated", "asleep", True, id="dedicated-asleep"),
        pytest.param("org", "released", False, id="released"),
        pytest.param("personal", "ready", False, id="own-hardware"),
    ],
)
async def test_live_compute_of_ones_own_blocks(
    org_admin: OrgWithAdmin, tenancy: str, state: str, blocks: bool
) -> None:
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        values: dict[str, Any] = {
            "user_id": org_admin.admin_id,
            "org_team_id": org_admin.org_id,
            "machine_type_id": machine_type.id,
            "tenancy": tenancy,
            "state": state,
            "name": f"box-{uuid.uuid4().hex[:6]}",
            "price_per_minute_nanos": 0,
            "true_cost_per_minute_nanos": 0,
        }
        if tenancy == "dedicated":
            values["tenant_org_id"] = org_admin.org_id
        session.add(ComputeAllocation(**values))
        await session.commit()
    codes = await _codes(org_admin.admin_id)
    assert codes == ([("live_compute", org_admin.org_id)] if blocks else [])


async def test_the_last_admin_blocks_and_a_plain_member_does_not(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as session:
        member, _ = await make_member(session, org_id=org_admin.org_id, verified=True)
    assert await _codes(org_admin.admin_id) == [("last_admin", org_admin.org_id)]
    assert await _codes(member.id) == []


async def test_a_member_in_an_org_with_no_active_admin_hands_to_the_oldest_member(
    org_admin: OrgWithAdmin,
) -> None:
    """An org whose only admin was deactivated: a plain member leaving is not
    blocked, and their shared items go to the longest-standing active member."""
    async with AsyncSessionLocal() as session:
        first, _ = await make_member(session, org_id=org_admin.org_id, verified=True)
        second, _ = await make_member(session, org_id=org_admin.org_id, verified=True)
        await session.execute(
            text(
                "UPDATE org_memberships SET status = 'deactivated' "
                "WHERE user_id = :u AND org_team_id = :o"
            ),
            {"u": org_admin.admin_id, "o": org_admin.org_id},
        )
        await session.commit()
    async with AsyncSessionLocal() as session:
        plan = await compute_plan(session, second.id)
        await session.rollback()
    (org,) = plan.orgs
    assert (org.fate, org.transfer_to_user_id) == ("leave", first.id)
