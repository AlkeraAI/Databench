"""Every way the backend asks "is this person an active member of this org" and
"is this person the org's admin" gives the same answer, in every state.

The questions are asked from billing, the org routes, the auth dependencies and
the Files predicate. Each state below is built once and every answerer is asked
about it, so a change to one answerer that the others do not share fails here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from alkera_core.files.membership import active_member_of
from alkera_core.models import MembershipStatus, OrgMembership, TeamMembership, TeamRole, User
from backend.auth.dependencies import user_is_org_admin
from backend.services.org import memberships as membership_service
from backend.services.org import org_memberships
from backend.services.org import teams as team_service
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, make_member

pytestmark = [pytest.mark.asyncio]


async def _other_org(session: AsyncSession) -> UUID:
    org, _admin = await team_service.create_org_with_admin(
        session,
        org_name=f"other-{uuid4().hex[:8]}",
        admin_email=f"other-{uuid4().hex[:8]}@example.com",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await session.commit()
    return org.id


async def _set_status(session: AsyncSession, user: User, org_id: UUID, status: str) -> None:
    await session.execute(
        update(OrgMembership)
        .where(OrgMembership.user_id == user.id, OrgMembership.org_team_id == org_id)
        .values(status=MembershipStatus(status))
    )
    await session.commit()


async def _set_root_role(session: AsyncSession, user: User, org_id: UUID, role: TeamRole) -> None:
    await session.execute(
        update(TeamMembership)
        .where(TeamMembership.user_id == user.id, TeamMembership.team_id == org_id)
        .values(role=role)
    )
    await session.commit()


Build = Callable[[AsyncSession, UUID], Awaitable[User]]


async def _member(session: AsyncSession, org_id: UUID) -> User:
    user, _ = await make_member(session, org_id=org_id)
    await session.commit()
    return user


def _with_status(status: str) -> Build:
    async def build(session: AsyncSession, org_id: UUID) -> User:
        user = await _member(session, org_id)
        await _set_status(session, user, org_id, status)
        return user

    return build


async def _member_elsewhere(session: AsyncSession, org_id: UUID) -> User:
    return await _member(session, await _other_org(session))


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        pytest.param(_member, True, id="active"),
        pytest.param(_with_status("deactivated"), False, id="deactivated"),
        pytest.param(_with_status("pending"), False, id="pending"),
        pytest.param(_member_elsewhere, False, id="member-of-another-org-only"),
    ],
)
async def test_every_active_member_answer_agrees(
    real_session: AsyncSession, org_admin: OrgWithAdmin, build: Build, expected: bool
) -> None:
    org_id = org_admin.org_id
    user = await build(real_session, org_id)

    answers = {
        "is_active_member": await org_memberships.is_active_member(
            real_session, user_id=user.id, org_id=org_id
        ),
        "active": await org_memberships.active(real_session, user_id=user.id, org_team_id=org_id)
        is not None,
        "files": (
            await real_session.execute(
                select(User.id).where(User.id == user.id, active_member_of(org_id))
            )
        ).first()
        is not None,
    }
    assert answers == dict.fromkeys(answers, expected)


async def _root_admin(session: AsyncSession, org_id: UUID) -> User:
    user = await _member(session, org_id)
    await _set_root_role(session, user, org_id, TeamRole.ADMIN)
    return user


async def _subteam_admin(session: AsyncSession, org_id: UUID) -> User:
    user = await _member(session, org_id)
    team = await team_service.create_subteam(
        session, org_team_id=org_id, name=f"sub-{uuid4().hex[:6]}", parent_team_id=org_id
    )
    await membership_service.add_member(
        session, team_id=team.id, user_id=user.id, role=TeamRole.ADMIN
    )
    await session.commit()
    return user


async def _deactivated_root_admin(session: AsyncSession, org_id: UUID) -> User:
    user = await _root_admin(session, org_id)
    await _set_status(session, user, org_id, "deactivated")
    return user


async def _admin_elsewhere(session: AsyncSession, org_id: UUID) -> User:
    other = await _other_org(session)
    return await _root_admin(session, other)


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        pytest.param(_root_admin, True, id="admin-of-the-root"),
        pytest.param(_member, False, id="member-of-the-root"),
        pytest.param(_subteam_admin, False, id="admin-of-a-sub-team-only"),
        # Admin rows are read as they stand: a deactivated membership is refused
        # at every credential door before any of these is asked.
        pytest.param(_deactivated_root_admin, True, id="deactivated-admin-row"),
        pytest.param(_admin_elsewhere, False, id="admin-of-another-org"),
    ],
)
async def test_every_org_admin_answer_agrees(
    real_session: AsyncSession, org_admin: OrgWithAdmin, build: Build, expected: bool
) -> None:
    org_id = org_admin.org_id
    user = await build(real_session, org_id)
    membership = OrgMembership(user_id=user.id, org_team_id=org_id)

    answers = {
        "org_admin_ids": user.id
        in await org_memberships.org_admin_ids(real_session, org_id=org_id),
        "is_org_admin": await org_memberships.is_org_admin(real_session, membership),
        "user_is_org_admin": await user_is_org_admin(real_session, user, org_team_id=org_id),
    }
    assert answers == dict.fromkeys(answers, expected)
