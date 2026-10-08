"""Re-parent membership recalculation (service level).

These pin the delicate part of `team_service.reparent_team`: when a subtree
moves to a new parent, every user in it must end up a member of the new
ancestors, lose the old ancestors they no longer sit under, keep an ancestor
they still reach through a *sibling* branch, and never have an ADMIN row
auto-stripped. The last test pins the one irreducibly-ambiguous case.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.models import TeamRole
from backend.services.identity import users as user_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
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
        email=_unique_email("reparent"),
        first_name="Re",
        last_name="Parent",
        password="x" * 12,
    )
    await s.commit()
    return u.id


async def _in(s: AsyncSession, *, team_id: UUID, user_id: UUID) -> bool:
    return await membership_service.get(s, team_id=team_id, user_id=user_id) is not None


@pytest.mark.asyncio
async def test_reparent_adds_new_ancestor_and_drops_old(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    b = await _sub(real_session, parent_id=root, name="B")
    a1 = await _sub(real_session, parent_id=a, name="A1")
    user = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a1, user_id=user)
    await real_session.commit()
    assert await _in(real_session, team_id=a, user_id=user)

    team = await team_service.get_by_id(real_session, a1)
    assert team is not None
    await team_service.reparent_team(real_session, team=team, new_parent_id=b)
    await real_session.commit()

    assert await _in(real_session, team_id=a1, user_id=user)  # still in moved team
    assert await _in(real_session, team_id=b, user_id=user)  # new ancestor materialized
    assert await _in(real_session, team_id=root, user_id=user)  # shared root kept
    assert not await _in(real_session, team_id=a, user_id=user)  # shed ancestor dropped


@pytest.mark.asyncio
async def test_reparent_keeps_ancestor_reachable_via_sibling_branch(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    b = await _sub(real_session, parent_id=root, name="B")
    a1 = await _sub(real_session, parent_id=a, name="A1")
    a2 = await _sub(real_session, parent_id=a, name="A2")
    w = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a1, user_id=w)
    await membership_service.add_member(real_session, team_id=a2, user_id=w)
    await real_session.commit()

    team = await team_service.get_by_id(real_session, a1)
    assert team is not None
    await team_service.reparent_team(real_session, team=team, new_parent_id=b)
    await real_session.commit()

    # W still reaches A through A2 (the sibling branch), so the A row survives.
    assert await _in(real_session, team_id=a, user_id=w)
    assert await _in(real_session, team_id=b, user_id=w)


@pytest.mark.asyncio
async def test_reparent_drops_all_shed_ancestors_multilevel(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """Two shed ancestors (P and A) must BOTH be dropped, deterministically.
    The REMOVE pass runs leaf-first so A is deleted before P is evaluated —
    otherwise P's justification check could see A's about-to-be-deleted row and
    keep P (an order-dependent bug)."""
    root = org_admin.org_id
    p = await _sub(real_session, parent_id=root, name="P")
    a = await _sub(real_session, parent_id=p, name="A")
    a1 = await _sub(real_session, parent_id=a, name="A1")
    b = await _sub(real_session, parent_id=root, name="B")
    u = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a1, user_id=u)
    await real_session.commit()
    assert await _in(real_session, team_id=a, user_id=u)
    assert await _in(real_session, team_id=p, user_id=u)

    team = await team_service.get_by_id(real_session, a1)
    assert team is not None
    await team_service.reparent_team(real_session, team=team, new_parent_id=b)
    await real_session.commit()

    assert await _in(real_session, team_id=a1, user_id=u)
    assert await _in(real_session, team_id=b, user_id=u)
    assert await _in(real_session, team_id=root, user_id=u)
    assert not await _in(real_session, team_id=a, user_id=u)
    assert not await _in(real_session, team_id=p, user_id=u)


@pytest.mark.asyncio
async def test_reparent_never_strips_admin_row(real_session: AsyncSession, org_admin: OrgWithAdmin):
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    b = await _sub(real_session, parent_id=root, name="B")
    a1 = await _sub(real_session, parent_id=a, name="A1")
    x = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a, user_id=x, role=TeamRole.ADMIN)
    await membership_service.add_member(real_session, team_id=a1, user_id=x)
    await real_session.commit()

    team = await team_service.get_by_id(real_session, a1)
    assert team is not None
    await team_service.reparent_team(real_session, team=team, new_parent_id=b)
    await real_session.commit()

    membership = await membership_service.get(real_session, team_id=a, user_id=x)
    assert membership is not None and membership.role is TeamRole.ADMIN


@pytest.mark.asyncio
async def test_reparent_drops_direct_member_of_shed_ancestor_documented(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """CONTRACT (documented loss): a *direct* MEMBER row on an intermediate
    ancestor is indistinguishable from an inherited one, so when the user's
    only sub-branch is moved away the row is dropped. Pinned deliberately —
    the precise fix would be an `is_direct` flag on TeamMembership."""
    root = org_admin.org_id
    a = await _sub(real_session, parent_id=root, name="A")
    b = await _sub(real_session, parent_id=root, name="B")
    a1 = await _sub(real_session, parent_id=a, name="A1")
    y = await _user(real_session, root)
    await membership_service.add_member(real_session, team_id=a, user_id=y)  # "direct" member of A
    await membership_service.add_member(real_session, team_id=a1, user_id=y)
    await real_session.commit()

    team = await team_service.get_by_id(real_session, a1)
    assert team is not None
    await team_service.reparent_team(real_session, team=team, new_parent_id=b)
    await real_session.commit()

    assert not await _in(real_session, team_id=a, user_id=y)  # documented loss
    assert await _in(real_session, team_id=b, user_id=y)
    assert await _in(real_session, team_id=root, user_id=y)
