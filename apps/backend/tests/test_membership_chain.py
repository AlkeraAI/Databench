"""Tests for materialized membership chain + cascade-on-remove."""

from __future__ import annotations

from uuid import UUID

import pytest
from alkera_core.authz import PrincipalKind, Role, ScopeKind
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import RoleAssignment, TeamMembership, TeamRole
from backend.services.identity import users as user_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from backend.services.org.memberships import MembershipError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, make_member


async def _create_subteam(s: AsyncSession, *, parent_id: UUID, name: str) -> UUID:
    team = await team_service.create_subteam(
        s, org_team_id=parent_id, name=name, parent_team_id=parent_id
    )
    await s.commit()
    return team.id


async def _list_membership_team_ids(s: AsyncSession, user_id: UUID) -> set[UUID]:
    rows = await s.execute(select(TeamMembership.team_id).where(TeamMembership.user_id == user_id))
    return set(rows.scalars().all())


@pytest.mark.asyncio
async def test_add_to_subteam_materializes_ancestors(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    # Create a fresh user in the org (no memberships yet beyond the admin).
    new_user = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=_unique_email("chain"),
        first_name="Chain",
        last_name="User",
        password="x" * 12,
    )
    await real_session.commit()

    sub_team_id = await _create_subteam(real_session, parent_id=org_admin.org_id, name="Sub")

    await membership_service.add_member(
        real_session,
        team_id=sub_team_id,
        user_id=new_user.id,
        role=TeamRole.MEMBER,
    )
    await real_session.commit()

    teams = await _list_membership_team_ids(real_session, new_user.id)
    assert sub_team_id in teams
    assert org_admin.org_id in teams  # root materialized


@pytest.mark.asyncio
async def test_remove_from_parent_cascades_to_subteams(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    new_user = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=_unique_email("cascade"),
        first_name="Cascade",
        last_name="User",
        password="x" * 12,
    )
    await real_session.commit()

    sub_team_id = await _create_subteam(real_session, parent_id=org_admin.org_id, name="SubB")
    await membership_service.add_member(
        real_session,
        team_id=sub_team_id,
        user_id=new_user.id,
        role=TeamRole.MEMBER,
    )
    await real_session.commit()
    assert org_admin.org_id in await _list_membership_team_ids(real_session, new_user.id)
    assert sub_team_id in await _list_membership_team_ids(real_session, new_user.id)

    # Remove from parent (org root) — descendants must vanish too.
    root_membership = await membership_service.get(
        real_session, team_id=org_admin.org_id, user_id=new_user.id
    )
    assert root_membership is not None
    # We have to use a non-admin role; root membership was MEMBER per the chain.
    assert root_membership.role is TeamRole.MEMBER
    await membership_service.remove_member(real_session, root_membership)
    await real_session.commit()

    remaining = await _list_membership_team_ids(real_session, new_user.id)
    assert sub_team_id not in remaining
    assert org_admin.org_id not in remaining


@pytest.mark.asyncio
async def test_re_add_after_existing_chain_is_no_op(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """Adding to leaf when ancestors are already there shouldn't error."""
    new_user = await user_service.create_user(
        real_session,
        org_team_id=org_admin.org_id,
        email=_unique_email("reuse"),
        first_name="Re-use",
        last_name="User",
        password="x" * 12,
    )
    await real_session.commit()
    sub = await _create_subteam(real_session, parent_id=org_admin.org_id, name="Reuse")

    await membership_service.add_member(
        real_session, team_id=sub, user_id=new_user.id, role=TeamRole.MEMBER
    )
    await real_session.commit()

    # Try adding to root directly — should fail (already a member).
    with pytest.raises(MembershipError):
        await membership_service.add_member(
            real_session,
            team_id=org_admin.org_id,
            user_id=new_user.id,
            role=TeamRole.MEMBER,
        )


# --------------------------------------------------------------------------- #
# removal sheds unjustified INHERITED ancestor rows (three-level trees)
# --------------------------------------------------------------------------- #


async def _user(s: AsyncSession, org_id: UUID, tag: str) -> UUID:
    user = await user_service.create_user(
        s,
        org_team_id=org_id,
        email=_unique_email(tag),
        first_name=tag,
        last_name="User",
        password="x" * 12,
    )
    await s.commit()
    return user.id


@pytest.mark.asyncio
async def test_remove_from_leaf_sheds_the_now_unjustified_intermediate(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """root > mid > leaf. `add_member(leaf)` materializes rows on mid + root, and
    entitlement (a team connection's shared credential, a KB scope, a billing
    pool) is granted purely by the presence of a row — so leaving the mid row
    behind kept a removed contractor inside mid."""
    root = org_admin.org_id
    mid = await _create_subteam(real_session, parent_id=root, name="Eng")
    leaf = await _create_subteam(real_session, parent_id=mid, name="Data")
    user_id = await _user(real_session, root, "shed")

    await membership_service.add_member(real_session, team_id=leaf, user_id=user_id)
    await real_session.commit()
    assert await _list_membership_team_ids(real_session, user_id) == {leaf, mid, root}

    leaf_row = await membership_service.get(real_session, team_id=leaf, user_id=user_id)
    assert leaf_row is not None
    await membership_service.remove_member(real_session, leaf_row)
    await real_session.commit()

    # mid is gone; the org root — the tenancy binding, not an access grant — stays.
    assert await _list_membership_team_ids(real_session, user_id) == {root}


@pytest.mark.asyncio
async def test_remove_from_leaf_keeps_an_intermediate_justified_by_a_sibling(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """The asymmetric case: an over-eager sweep would revoke access the user
    still legitimately holds through another branch."""
    root = org_admin.org_id
    mid = await _create_subteam(real_session, parent_id=root, name="Eng2")
    leaf_a = await _create_subteam(real_session, parent_id=mid, name="DataA")
    leaf_b = await _create_subteam(real_session, parent_id=mid, name="DataB")
    user_id = await _user(real_session, root, "sibling")

    await membership_service.add_member(real_session, team_id=leaf_a, user_id=user_id)
    await membership_service.add_member(real_session, team_id=leaf_b, user_id=user_id)
    await real_session.commit()

    row_a = await membership_service.get(real_session, team_id=leaf_a, user_id=user_id)
    assert row_a is not None
    await membership_service.remove_member(real_session, row_a)
    await real_session.commit()

    assert await _list_membership_team_ids(real_session, user_id) == {leaf_b, mid, root}


@pytest.mark.asyncio
async def test_remove_from_leaf_never_strips_an_admin_ancestor_row(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """An ADMIN row is an explicit grant, never inherited bookkeeping — the
    sweep must leave it alone even with nothing left in the subtree."""
    root = org_admin.org_id
    mid = await _create_subteam(real_session, parent_id=root, name="Eng3")
    leaf = await _create_subteam(real_session, parent_id=mid, name="Data3")
    user_id = await _user(real_session, root, "midadmin")

    await membership_service.add_member(
        real_session, team_id=mid, user_id=user_id, role=TeamRole.ADMIN
    )
    await membership_service.add_member(real_session, team_id=leaf, user_id=user_id)
    await real_session.commit()

    leaf_row = await membership_service.get(real_session, team_id=leaf, user_id=user_id)
    assert leaf_row is not None
    await membership_service.remove_member(real_session, leaf_row)
    await real_session.commit()

    assert await _list_membership_team_ids(real_session, user_id) == {mid, root}
    kept = await membership_service.get(real_session, team_id=mid, user_id=user_id)
    assert kept is not None and kept.role is TeamRole.ADMIN


@pytest.mark.asyncio
async def test_removal_revokes_entitlement_to_an_intermediate_teams_connection(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """The impact half, asserted through the surface that actually authorizes a
    shared credential fetch: a row on `mid` is all it takes."""
    from alkera_core.connections.models import TeamConnection
    from backend.services.connections import team_connections as team_connection_service

    root = org_admin.org_id
    mid = await _create_subteam(real_session, parent_id=root, name="Eng4")
    leaf = await _create_subteam(real_session, parent_id=mid, name="Data4")
    user_id = await _user(real_session, root, "entitled")

    connection = TeamConnection(
        team_id=mid,
        plugin="snowflake",
        handle=f"wh-{_unique_email('c').split('@')[0]}",
        auth_mode="shared",
    )
    real_session.add(connection)
    await membership_service.add_member(real_session, team_id=leaf, user_id=user_id)
    await real_session.commit()
    assert await team_connection_service.is_member_entitled(
        real_session, user_id=user_id, connection_id=connection.id, org_team_id=root
    )

    leaf_row = await membership_service.get(real_session, team_id=leaf, user_id=user_id)
    assert leaf_row is not None
    await membership_service.remove_member(real_session, leaf_row)
    await real_session.commit()

    assert not await team_connection_service.is_member_entitled(
        real_session, user_id=user_id, connection_id=connection.id, org_team_id=root
    )


# --------------------------------------------------------------------------- #
# the last-org-admin guard must ANSWER for any number of root admins
# --------------------------------------------------------------------------- #


async def _extra_root_admins(s: AsyncSession, *, org_id: UUID, count: int) -> list[UUID]:
    """`count` additional ADMIN members on the org root (the fixture admin is
    already there) — the ordinary shape produced by role=admin invitations or
    SSO group mapping."""
    ids: list[UUID] = []
    for _ in range(count):
        user, _pw = await make_member(s, org_id=org_id, role=TeamRole.ADMIN)
        ids.append(user.id)
    return ids


@pytest.mark.parametrize(
    ("root_admins", "refused"),
    [
        pytest.param(1, True, id="sole-admin-refused"),
        pytest.param(2, False, id="two-admins-allowed"),
        pytest.param(3, False, id="three-admins-allowed"),
        pytest.param(5, False, id="five-admins-allowed"),
    ],
)
@pytest.mark.asyncio
async def test_remove_root_admin_across_admin_counts(
    real_session: AsyncSession, org_admin: OrgWithAdmin, root_admins: int, refused: bool
):
    """The guard must refuse exactly when the org root would be left with no
    admin — and must ANSWER, not raise, in every other case.

    It used to ask for one row back from a query that legitimately returns many,
    so an org with three or more root admins blew up inside the service with
    `MultipleResultsFound` instead of removing anybody. That is not a
    `MembershipError`, so the route's handler missed it and offboarding 500'd."""
    extras = await _extra_root_admins(real_session, org_id=org_admin.org_id, count=root_admins - 1)
    target_id = extras[-1] if extras else org_admin.admin_id

    row = await membership_service.get(real_session, team_id=org_admin.org_id, user_id=target_id)
    assert row is not None and row.role is TeamRole.ADMIN

    if refused:
        with pytest.raises(MembershipError, match="last org admin"):
            await membership_service.remove_member(real_session, row)
    else:
        await membership_service.remove_member(real_session, row)
    await real_session.commit()

    survived = await membership_service.get(
        real_session, team_id=org_admin.org_id, user_id=target_id
    )
    assert (survived is not None) is refused
    remaining = await membership_service.org_admins(real_session, org_id=org_admin.org_id)
    assert len(remaining) == (root_admins if refused else root_admins - 1)


@pytest.mark.parametrize(
    ("root_admins", "refused"),
    [
        pytest.param(1, True, id="sole-admin-refused"),
        pytest.param(2, False, id="two-admins-allowed"),
        pytest.param(3, False, id="three-admins-allowed"),
    ],
)
@pytest.mark.asyncio
async def test_demote_root_admin_across_admin_counts(
    real_session: AsyncSession, org_admin: OrgWithAdmin, root_admins: int, refused: bool
):
    """`change_role` shares the same guard, so it shares the same failure mode:
    demoting one of three root admins must succeed, not raise."""
    extras = await _extra_root_admins(real_session, org_id=org_admin.org_id, count=root_admins - 1)
    target_id = extras[-1] if extras else org_admin.admin_id

    row = await membership_service.get(real_session, team_id=org_admin.org_id, user_id=target_id)
    assert row is not None

    if refused:
        with pytest.raises(MembershipError, match="last org admin"):
            await membership_service.change_role(real_session, row, TeamRole.MEMBER)
    else:
        await membership_service.change_role(real_session, row, TeamRole.MEMBER)
    await real_session.commit()

    still = await membership_service.get(real_session, team_id=org_admin.org_id, user_id=target_id)
    assert still is not None
    assert still.role is (TeamRole.ADMIN if refused else TeamRole.MEMBER)


@pytest.mark.asyncio
async def test_last_admin_guard_ignores_admins_of_other_teams(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """The asymmetric case an over-eager existence probe would get wrong: an
    ADMIN row on a SUBTEAM is not an org admin, so the sole root admin must
    still be refused."""
    sub = await _create_subteam(real_session, parent_id=org_admin.org_id, name="AdminSub")
    sub_admin, _pw = await make_member(real_session, org_id=org_admin.org_id)
    await membership_service.add_member(
        real_session, team_id=sub, user_id=sub_admin.id, role=TeamRole.ADMIN
    )
    await real_session.commit()

    row = await membership_service.get(
        real_session, team_id=org_admin.org_id, user_id=org_admin.admin_id
    )
    assert row is not None
    with pytest.raises(MembershipError, match="last org admin"):
        await membership_service.remove_member(real_session, row)


@pytest.mark.parametrize(
    "second_op",
    [
        pytest.param("remove", id="removal-vs-removal"),
        pytest.param("demote", id="removal-vs-demotion"),
    ],
)
@pytest.mark.asyncio
async def test_concurrent_drops_of_the_last_two_admins_cannot_empty_the_org(
    real_session: AsyncSession, org_admin: OrgWithAdmin, second_op: str
):
    """Sequentially the second drop is refused. Concurrently the guard used to run
    BEFORE the org tree lock (and `change_role` took no lock at all), so each
    transaction saw the other's still-present ADMIN row, both passed, and the org
    was left with ZERO root admins — unrecoverable through the API, because
    granting admin requires an existing org admin."""
    import asyncio

    from alkera_core.db.session import AsyncSessionLocal

    extra_id = (await _extra_root_admins(real_session, org_id=org_admin.org_id, count=1))[0]
    await real_session.commit()

    holding = asyncio.Event()  # first tx has taken the lock + deleted, not committed
    release = asyncio.Event()  # the test lets the first tx commit

    async def _first_removes_the_fixture_admin() -> None:
        async with AsyncSessionLocal() as s:
            row = await membership_service.get(
                s, team_id=org_admin.org_id, user_id=org_admin.admin_id
            )
            assert row is not None
            await membership_service.remove_member(s, row)
            holding.set()
            await release.wait()
            await s.commit()

    async def _second_drops_the_other_admin() -> str:
        async with AsyncSessionLocal() as s:
            row = await membership_service.get(s, team_id=org_admin.org_id, user_id=extra_id)
            assert row is not None and row.role is TeamRole.ADMIN
            try:
                if second_op == "remove":
                    await membership_service.remove_member(s, row)
                else:
                    await membership_service.change_role(s, row, TeamRole.MEMBER)
                await s.commit()
                return "dropped"
            except MembershipError:
                await s.rollback()
                return "refused"

    async with asyncio.timeout(60):
        first = asyncio.create_task(_first_removes_the_fixture_admin())
        await holding.wait()
        second = asyncio.create_task(_second_drops_the_other_admin())
        # It must PARK on the org's tree lock, not race past the guard on a read
        # taken before the first transaction's delete became visible.
        await asyncio.sleep(0.5)
        assert not second.done(), "the second drop did not serialize on the org tree lock"
        release.set()
        await first
        assert await second == "refused"

    async with AsyncSessionLocal() as s:
        survivors = await membership_service.org_admins(s, org_id=org_admin.org_id)
        still = await membership_service.get(s, team_id=org_admin.org_id, user_id=extra_id)
    assert [u.id for u in survivors] == [extra_id]
    assert still is not None and still.role is TeamRole.ADMIN


# --------------------------------------------------------------------------- #
# a demotion or removal revokes the owner/admin grants the membership carried
# --------------------------------------------------------------------------- #


async def _grant(
    s: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    scope_id: UUID,
    role: Role,
    scope_kind: ScopeKind = ScopeKind.TEAM,
) -> UUID:
    row = RoleAssignment(
        org_team_id=org_id,
        principal_kind=PrincipalKind.USER,
        principal_id=user_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        role=role,
    )
    s.add(row)
    await s.commit()
    return row.id


async def _live_by_id(s: AsyncSession, user_id: UUID) -> dict[UUID, bool]:
    """Every assignment row a user has, id -> whether it is still live. Read
    fresh from the database, never from the identity map."""
    rows = await s.execute(
        select(RoleAssignment)
        .where(
            RoleAssignment.principal_kind == PrincipalKind.USER,
            RoleAssignment.principal_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    return {row.id: row.revoked_at is None for row in rows.scalars().all()}


async def _founder_owner_row(s: AsyncSession, org_admin: OrgWithAdmin) -> RoleAssignment:
    return (
        await s.execute(
            select(RoleAssignment)
            .where(
                RoleAssignment.org_team_id == org_admin.org_id,
                RoleAssignment.principal_id == org_admin.admin_id,
                RoleAssignment.role == Role.OWNER,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _row(s: AsyncSession, team_id: UUID, user_id: UUID) -> TeamMembership:
    row = await membership_service.get(s, team_id=team_id, user_id=user_id)
    assert row is not None
    return row


@pytest.mark.parametrize(
    ("role", "scope_kind", "where", "revoked"),
    [
        pytest.param(Role.OWNER, ScopeKind.ORG, "root", True, id="owner-at-the-root"),
        pytest.param(Role.ADMIN, ScopeKind.TEAM, "root", True, id="admin-at-the-root"),
        pytest.param(Role.ADMIN, ScopeKind.TEAM, "sub", True, id="admin-below-the-root"),
        pytest.param(Role.MEMBER, ScopeKind.TEAM, "root", False, id="member-grant-untouched"),
        pytest.param(Role.VIEWER, ScopeKind.TEAM, "sub", False, id="viewer-grant-untouched"),
    ],
)
@pytest.mark.asyncio
async def test_demoting_a_root_admin_revokes_the_owner_and_admin_grants_beneath_the_root(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    role: Role,
    scope_kind: ScopeKind,
    where: str,
    revoked: bool,
):
    """An owner or admin grant descends through the role resolver exactly like an
    admin membership does, so a demotion that left one standing would be a
    demotion in name only. Member and viewer grants are explicit, non-descending
    grants that no membership justifies, so a demotion leaves them alone."""
    root = org_admin.org_id
    sub = await _create_subteam(real_session, parent_id=root, name="Grants")
    admin, _pw = await make_member(real_session, org_id=root, role=TeamRole.ADMIN)
    grant_id = await _grant(
        real_session,
        org_id=root,
        user_id=admin.id,
        scope_id=root if where == "root" else sub,
        role=role,
        scope_kind=scope_kind,
    )

    row = await _row(real_session, root, admin.id)
    await membership_service.change_role(real_session, row, TeamRole.MEMBER)
    await real_session.commit()

    assert (await _live_by_id(real_session, admin.id)) == {grant_id: not revoked}
    # Revoked, never deleted: the audit trail keeps the row.
    assert (await real_session.get(RoleAssignment, grant_id)) is not None
    # Another principal's grant is not this demotion's business.
    assert (await _founder_owner_row(real_session, org_admin)).revoked_at is None


@pytest.mark.asyncio
async def test_demoting_the_founder_revokes_the_owner_row_the_org_was_born_with(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    second, _pw = await make_member(real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN)
    founder_row = await _row(real_session, org_admin.org_id, org_admin.admin_id)

    await membership_service.change_role(real_session, founder_row, TeamRole.MEMBER)
    await real_session.commit()

    owner = await _founder_owner_row(real_session, org_admin)
    assert owner.revoked_at is not None
    # Nothing is transferred: the remaining admin is an admin by membership, not an owner.
    assert await _live_by_id(real_session, second.id) == {}


@pytest.mark.asyncio
async def test_a_demotion_on_a_sub_team_revokes_only_that_subtree(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """root > eng > data, root > ops. Demoting on eng ends admin standing over
    eng and everything below it; a grant on a sibling team or on the root was
    never justified by the eng membership and stands."""
    root = org_admin.org_id
    eng = await _create_subteam(real_session, parent_id=root, name="Eng")
    data = await _create_subteam(real_session, parent_id=eng, name="Data")
    ops = await _create_subteam(real_session, parent_id=root, name="Ops")
    user_id = await _user(real_session, root, "subtree")
    await membership_service.add_member(
        real_session, team_id=eng, user_id=user_id, role=TeamRole.ADMIN
    )
    await real_session.commit()
    on_eng = await _grant(real_session, org_id=root, user_id=user_id, scope_id=eng, role=Role.ADMIN)
    on_data = await _grant(
        real_session, org_id=root, user_id=user_id, scope_id=data, role=Role.ADMIN
    )
    on_ops = await _grant(real_session, org_id=root, user_id=user_id, scope_id=ops, role=Role.ADMIN)
    on_root = await _grant(
        real_session,
        org_id=root,
        user_id=user_id,
        scope_id=root,
        role=Role.OWNER,
        scope_kind=ScopeKind.ORG,
    )

    eng_row = await _row(real_session, eng, user_id)
    await membership_service.change_role(real_session, eng_row, TeamRole.MEMBER)
    await real_session.commit()

    assert await _live_by_id(real_session, user_id) == {
        on_eng: False,
        on_data: False,
        on_ops: True,
        on_root: True,
    }


@pytest.mark.asyncio
async def test_removing_a_member_revokes_the_grants_in_the_removed_subtree(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    eng = await _create_subteam(real_session, parent_id=root, name="Eng")
    data = await _create_subteam(real_session, parent_id=eng, name="Data")
    ops = await _create_subteam(real_session, parent_id=root, name="Ops")
    user_id = await _user(real_session, root, "removed")
    await membership_service.add_member(
        real_session, team_id=eng, user_id=user_id, role=TeamRole.ADMIN
    )
    await real_session.commit()
    on_eng = await _grant(real_session, org_id=root, user_id=user_id, scope_id=eng, role=Role.ADMIN)
    on_data = await _grant(
        real_session, org_id=root, user_id=user_id, scope_id=data, role=Role.OWNER
    )
    on_ops = await _grant(real_session, org_id=root, user_id=user_id, scope_id=ops, role=Role.ADMIN)
    on_root = await _grant(
        real_session, org_id=root, user_id=user_id, scope_id=root, role=Role.VIEWER
    )

    eng_row = await _row(real_session, eng, user_id)
    await membership_service.remove_member(real_session, eng_row)
    await real_session.commit()

    assert await _live_by_id(real_session, user_id) == {
        on_eng: False,
        on_data: False,
        on_ops: True,
        on_root: True,
    }
    assert await _list_membership_team_ids(real_session, user_id) == {root}


@pytest.mark.asyncio
async def test_removing_a_root_admin_revokes_every_owner_and_admin_grant_in_the_org(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    sub = await _create_subteam(real_session, parent_id=root, name="Sub")
    admin, _pw = await make_member(real_session, org_id=root, role=TeamRole.ADMIN)
    at_root = await _grant(
        real_session,
        org_id=root,
        user_id=admin.id,
        scope_id=root,
        role=Role.OWNER,
        scope_kind=ScopeKind.ORG,
    )
    below = await _grant(real_session, org_id=root, user_id=admin.id, scope_id=sub, role=Role.ADMIN)

    await membership_service.remove_member(real_session, await _row(real_session, root, admin.id))
    await real_session.commit()

    assert await _live_by_id(real_session, admin.id) == {at_root: False, below: False}
    assert (await _founder_owner_row(real_session, org_admin)).revoked_at is None


@pytest.mark.asyncio
async def test_re_promoting_a_demoted_founder_does_not_restore_the_owner_grant(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """A promotion writes a membership, never a grant: owner standing comes
    back only through an explicit assignment. The revoked row stays as the
    audit trail and no fresh owner row is minted."""
    await make_member(real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN)
    founder_row = await _row(real_session, org_admin.org_id, org_admin.admin_id)
    await membership_service.change_role(real_session, founder_row, TeamRole.MEMBER)
    await real_session.commit()
    owner_id = (await _founder_owner_row(real_session, org_admin)).id

    await membership_service.change_role(real_session, founder_row, TeamRole.ADMIN)
    await real_session.commit()

    assert founder_row.role is TeamRole.ADMIN
    assert await _live_by_id(real_session, org_admin.admin_id) == {owner_id: False}


@pytest.mark.asyncio
async def test_a_same_role_change_and_a_promotion_touch_no_grant(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    root = org_admin.org_id
    sub = await _create_subteam(real_session, parent_id=root, name="Sub")
    founder_row = await _row(real_session, root, org_admin.admin_id)
    await membership_service.change_role(real_session, founder_row, TeamRole.ADMIN)
    await real_session.commit()
    assert (await _founder_owner_row(real_session, org_admin)).revoked_at is None

    member, _pw = await make_member(real_session, org_id=root)
    viewer = await _grant(
        real_session, org_id=root, user_id=member.id, scope_id=sub, role=Role.VIEWER
    )
    await membership_service.change_role(
        real_session, await _row(real_session, root, member.id), TeamRole.ADMIN
    )
    await real_session.commit()
    assert await _live_by_id(real_session, member.id) == {viewer: True}


@pytest.mark.asyncio
async def test_the_revocation_rolls_back_with_the_demotion(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """One transaction: a demotion that does not commit revokes nothing."""
    await make_member(real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN)
    async with AsyncSessionLocal() as s:
        row = await _row(s, org_admin.org_id, org_admin.admin_id)
        await membership_service.change_role(s, row, TeamRole.MEMBER)
        await s.rollback()

    assert (await _founder_owner_row(real_session, org_admin)).revoked_at is None
    row = await _row(real_session, org_admin.org_id, org_admin.admin_id)
    await real_session.refresh(row)
    assert row.role is TeamRole.ADMIN


@pytest.mark.asyncio
async def test_a_demotion_never_reaches_a_grant_outside_the_org(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """The shape a moved account leaves behind: a stray owner row for this user
    under ANOTHER org's root. A demotion here is about this org's tree only."""
    other_org, _admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other {_unique_email('o').split('@')[0]}",
        admin_email=_unique_email("other-admin"),
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="x" * 12,
    )
    await real_session.commit()
    stray = await _grant(
        real_session,
        org_id=other_org.id,
        user_id=org_admin.admin_id,
        scope_id=other_org.id,
        role=Role.OWNER,
        scope_kind=ScopeKind.ORG,
    )
    await make_member(real_session, org_id=org_admin.org_id, role=TeamRole.ADMIN)

    founder_row = await _row(real_session, org_admin.org_id, org_admin.admin_id)
    await membership_service.change_role(real_session, founder_row, TeamRole.MEMBER)
    await real_session.commit()

    live = await _live_by_id(real_session, org_admin.admin_id)
    assert live[stray] is True
    assert live[(await _founder_owner_row(real_session, org_admin)).id] is False
