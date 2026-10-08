"""Atomic move-member-between-teams (service level)."""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.models import TeamRole
from backend.services.identity import users as user_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from backend.services.org.memberships import MembershipError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email


async def _sub(s: AsyncSession, *, parent_id: UUID, name: str) -> UUID:
    team = await team_service.create_subteam(
        s, org_team_id=parent_id, name=name, parent_team_id=parent_id
    )
    await s.commit()
    return team.id


async def _user(s: AsyncSession, org_id: UUID) -> UUID:
    u = await user_service.create_user(
        s,
        org_team_id=org_id,
        email=_unique_email("move"),
        first_name="Mover",
        last_name="User",
        password="x" * 12,
    )
    await s.commit()
    return u.id


async def _in(s: AsyncSession, *, team_id: UUID, user_id: UUID) -> bool:
    return await membership_service.get(s, team_id=team_id, user_id=user_id) is not None


@pytest.mark.asyncio
async def test_move_preserves_role(real_session: AsyncSession, org_admin: OrgWithAdmin):
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    b = await _sub(real_session, parent_id=root, name="B")
    u = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a, user_id=u, role=TeamRole.ADMIN)
    await real_session.commit()

    moved = await membership_service.move_member(
        real_session, user_id=u, from_team_id=a, to_team_id=b
    )
    await real_session.commit()

    assert moved.role is TeamRole.ADMIN  # role carried across
    assert not await _in(real_session, team_id=a, user_id=u)
    assert await _in(real_session, team_id=b, user_id=u)
    assert await _in(real_session, team_id=root, user_id=u)  # shared ancestor kept


@pytest.mark.asyncio
async def test_move_leaves_sibling_membership_intact(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    a1 = await _sub(real_session, parent_id=a, name="A1")
    a2 = await _sub(real_session, parent_id=a, name="A2")
    b = await _sub(real_session, parent_id=root, name="B")
    u = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a1, user_id=u)
    await membership_service.add_member(real_session, team_id=a2, user_id=u)
    await real_session.commit()

    await membership_service.move_member(real_session, user_id=u, from_team_id=a1, to_team_id=b)
    await real_session.commit()

    assert not await _in(real_session, team_id=a1, user_id=u)
    assert await _in(real_session, team_id=b, user_id=u)
    assert await _in(real_session, team_id=a2, user_id=u)  # sibling untouched
    assert await _in(real_session, team_id=a, user_id=u)  # still under A via A2


@pytest.mark.asyncio
async def test_move_from_missing_membership_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    b = await _sub(real_session, parent_id=root, name="B")
    u = await _user(real_session, root)  # never added to A
    with pytest.raises(MembershipError):
        await membership_service.move_member(real_session, user_id=u, from_team_id=a, to_team_id=b)


@pytest.mark.asyncio
async def test_move_last_org_admin_refused(real_session: AsyncSession, org_admin: OrgWithAdmin):
    root = org_admin.org_id
    b = await _sub(real_session, parent_id=root, name="B")
    with pytest.raises(MembershipError):
        await membership_service.move_member(
            real_session, user_id=org_admin.admin_id, from_team_id=root, to_team_id=b
        )
