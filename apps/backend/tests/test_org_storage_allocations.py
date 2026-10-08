"""A member's storage allowance sums over their leaf teams, like their budget:
their own limit in each team, else that team's storage allocation, else no limit."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.allocations import TeamAllocation
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.storage_limits import UserStorageLimit
from backend.services.org import storage_limits as storage_limit_service
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


async def _team_under(org_id: UUID, name: str, parent: UUID | None = None) -> UUID:
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        team = await team_service.create_subteam(
            s, org_team_id=org_id, name=name, parent_team_id=parent or org_id
        )
        await s.commit()
        return team.id


async def _join(user_id: UUID, team_id: UUID) -> None:
    from backend.services.org import memberships as membership_service

    async with AsyncSessionLocal() as s:
        await membership_service.add_member(s, team_id=team_id, user_id=user_id)
        await s.commit()


async def _own_limit(org_id: UUID, team_id: UUID, user_id: UUID, limit: int) -> None:
    async with AsyncSessionLocal() as s:
        s.add(
            UserStorageLimit(
                org_team_id=org_id, team_id=team_id, user_id=user_id, limit_bytes=limit
            )
        )
        await s.commit()


async def _team_allocation(org_id: UUID, team_id: UUID, limit: int) -> None:
    async with AsyncSessionLocal() as s:
        s.add(
            TeamAllocation(
                org_team_id=org_id,
                team_id=team_id,
                resource="storage",
                limit_bytes=limit,
                window="none",
            )
        )
        await s.commit()


async def _drive_wide_limits(org_id: UUID, user_id: UUID) -> list[int]:
    """The drive-wide ceilings ``ceilings_for`` hands the quota check (a drive
    with no folders yet, so only drive-wide ceilings can bind)."""
    drive = FileDrive(quota_bytes=10**15, root_node_id=None)
    async with AsyncSessionLocal() as s:
        ceilings = await storage_limit_service.ceilings_for(
            s, org_id=org_id, user_id=user_id, drive=drive
        )
    return sorted(c.limit_bytes for c in ceilings.users if c.scope_path is None)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        pytest.param(("own", 100), ("own", 50), [150], id="two-own-limits-sum"),
        pytest.param(("own", 100), ("team", 70), [170], id="team-allocation-stands-in"),
        pytest.param(("own", 100), (None, 0), [], id="an-unlimited-team-leaves-no-ceiling"),
        pytest.param(("both", 100), ("team", 70), [170], id="own-limit-wins-over-allocation"),
    ],
)
async def test_the_drive_wide_ceiling_is_the_sum_over_two_teams(
    org_admin: OrgWithAdmin,
    real_session: Any,
    a: tuple[str | None, int],
    b: tuple[str | None, int],
    expected: list[int],
) -> None:
    eng = await _team_under(org_admin.org_id, "Eng")
    ops = await _team_under(org_admin.org_id, "Ops")
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _join(member.id, eng)
    await _join(member.id, ops)
    for team, (kind, limit) in ((eng, a), (ops, b)):
        if kind in ("own", "both"):
            await _own_limit(org_admin.org_id, team, member.id, limit)
        if kind in ("team", "both"):
            await _team_allocation(
                org_admin.org_id, team, limit * 1000 if kind == "both" else limit
            )

    assert await _drive_wide_limits(org_admin.org_id, member.id) == expected


async def test_the_materialized_parent_adds_nothing_to_the_sum(
    org_admin: OrgWithAdmin, real_session: Any
) -> None:
    eng = await _team_under(org_admin.org_id, "Eng")
    web = await _team_under(org_admin.org_id, "Web", parent=eng)
    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _join(member.id, web)  # materializes Eng and the root
    await _team_allocation(org_admin.org_id, eng, 10_000)
    await _team_allocation(org_admin.org_id, org_admin.org_id, 99_999)
    await _own_limit(org_admin.org_id, web, member.id, 300)

    assert await _drive_wide_limits(org_admin.org_id, member.id) == [300]


async def test_my_storage_shows_the_summed_allowance(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    eng = await _team_under(org_admin.org_id, "Eng")
    ops = await _team_under(org_admin.org_id, "Ops")
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await _join(member.id, eng)
    await _join(member.id, ops)
    await _team_allocation(org_admin.org_id, eng, 2_000)
    await _team_allocation(org_admin.org_id, ops, 3_000)

    await login(client, member.email, password or "")
    mine = (await client.get("/api/v1/me/storage")).json()
    assert (mine["limit_bytes"], mine["limit_source"]) == (5_000, "team")
