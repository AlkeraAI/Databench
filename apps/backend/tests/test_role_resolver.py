"""The role resolver against a real org tree: memberships materialised by the
membership service, owner rows written by org creation, assignments inserted
directly, and the real ancestor-chain loader — one chain fetch per team.

Tree: org root → Eng → {Data, Platform}. The founding admin owns the org.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alkera_core.authz import (
    ActingContext,
    CredentialKind,
    PrincipalKind,
    Role,
    RoleResolver,
    ScopeKind,
)
from alkera_core.models import OrgMembership, RoleAssignment, Team, TeamMembership, TeamRole, User
from backend.services.identity import users as user_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.asyncio

HUMAN_LADDER = frozenset({Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER})
ADMIN_DOWN = frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER})
MEMBER_DOWN = frozenset({Role.MEMBER, Role.VIEWER})


@dataclass
class Tree:
    session: AsyncSession
    org: UUID
    eng: UUID
    data: UUID
    platform: UUID
    founder: User
    data_member: User
    eng_admin: User
    outsider_org: UUID


async def _user(session: AsyncSession, org_id: UUID, tag: str) -> User:
    user = await user_service.create_user(
        session,
        org_team_id=org_id,
        email=f"{tag}-{secrets.token_hex(6)}@alkera.dev",
        first_name=tag,
        last_name="User",
        password="pw-1234567890",
    )
    await session.commit()
    return user


async def _join(session: AsyncSession, user: User, team_id: UUID, role: TeamRole) -> None:
    await membership_service.add_member(session, team_id=team_id, user_id=user.id, role=role)
    await session.commit()


@pytest_asyncio.fixture
async def tree(real_session: AsyncSession, org_admin: OrgWithAdmin) -> Tree:
    org_id = org_admin.org_id
    eng = await team_service.create_subteam(
        real_session, org_team_id=org_id, name="Eng", parent_team_id=org_id
    )
    await real_session.commit()
    data = await team_service.create_subteam(
        real_session, org_team_id=org_id, name="Data", parent_team_id=eng.id
    )
    platform = await team_service.create_subteam(
        real_session, org_team_id=org_id, name="Platform", parent_team_id=eng.id
    )
    await real_session.commit()
    founder = (
        await real_session.execute(select(User).where(User.id == org_admin.admin_id))
    ).scalar_one()
    data_member = await _user(real_session, org_id, "data")
    await _join(real_session, data_member, data.id, TeamRole.MEMBER)
    eng_admin = await _user(real_session, org_id, "engadm")
    await _join(real_session, eng_admin, eng.id, TeamRole.ADMIN)
    other_org, _other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=f"other-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="pw-1234567890",
    )
    await real_session.commit()
    return Tree(
        session=real_session,
        org=org_id,
        eng=eng.id,
        data=data.id,
        platform=platform.id,
        founder=founder,
        data_member=data_member,
        eng_admin=eng_admin,
        outsider_org=other_org.id,
    )


def _ctx(user: User) -> ActingContext:
    return ActingContext.for_user(user_id=user.id, org_id=user.home_org_team_id, email=user.email)


def _resolver(tree: Tree, ctx: ActingContext) -> RoleResolver:
    return RoleResolver(tree.session, ctx, ancestor_chain=team_service.ancestor_chain)


async def _grant(
    tree: Tree,
    *,
    principal_kind: PrincipalKind,
    principal_id: UUID,
    scope_id: UUID,
    role: Role,
    scope_kind: ScopeKind = ScopeKind.TEAM,
    revoked: bool = False,
) -> RoleAssignment:
    row = RoleAssignment(
        org_team_id=tree.org,
        principal_kind=principal_kind,
        principal_id=principal_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        role=role,
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    tree.session.add(row)
    await tree.session.commit()
    return row


# --------------------------------------------------------------------------- #
# users: memberships, owner rows, descent
# --------------------------------------------------------------------------- #


async def test_the_founder_owns_the_org_and_that_descends_to_every_team(tree: Tree) -> None:
    resolver = _resolver(tree, _ctx(tree.founder))
    assert (Role.OWNER in await resolver.org_roles()) is True
    assert await resolver.is_org_admin() is True
    for team in (tree.org, tree.eng, tree.data, tree.platform):
        answer = await resolver.for_team(team)
        assert answer.in_org is True
        assert answer.roles == HUMAN_LADDER
        assert answer.highest is Role.OWNER


async def test_the_founders_chain_is_the_real_ancestor_chain(tree: Tree) -> None:
    answer = await _resolver(tree, _ctx(tree.founder)).for_team(tree.data)
    assert answer.team_id == tree.data
    assert answer.chain_ids == (tree.data, tree.eng, tree.org)


async def test_a_data_member_is_a_member_of_data_and_of_every_team_above_it(tree: Tree) -> None:
    """Membership is materialised up the chain by the membership service, so
    the member holds a row on Eng and the root too — and is a member there."""
    resolver = _resolver(tree, _ctx(tree.data_member))
    assert (await resolver.for_team(tree.data)).roles == MEMBER_DOWN
    assert (await resolver.for_team(tree.eng)).roles == MEMBER_DOWN
    assert (await resolver.for_team(tree.org)).roles == MEMBER_DOWN
    assert (await resolver.for_team(tree.platform)).roles == frozenset()
    assert await resolver.is_org_admin() is False
    assert (Role.OWNER in await resolver.org_roles()) is False


async def test_an_eng_admin_administers_data_and_platform_by_descent(tree: Tree) -> None:
    resolver = _resolver(tree, _ctx(tree.eng_admin))
    for team in (tree.eng, tree.data, tree.platform):
        answer = await resolver.for_team(team)
        assert answer.is_admin is True
        assert answer.roles == ADMIN_DOWN
        assert answer.highest is Role.ADMIN


async def test_a_sub_team_admin_is_not_an_org_admin(tree: Tree) -> None:
    """Descent goes downward only: the Eng admin holds a MEMBER row at the root."""
    resolver = _resolver(tree, _ctx(tree.eng_admin))
    assert (await resolver.for_team(tree.org)).roles == MEMBER_DOWN
    assert await resolver.is_org_admin() is False
    assert (Role.OWNER in await resolver.org_roles()) is False


async def test_a_user_with_no_membership_at_all_holds_nothing_in_their_own_org(
    tree: Tree,
) -> None:
    loner = await _user(tree.session, tree.org, "loner")
    resolver = _resolver(tree, _ctx(loner))
    for team in (tree.org, tree.eng, tree.data):
        answer = await resolver.for_team(team)
        assert answer.in_org is True
        assert answer.roles == frozenset()
        assert answer.highest is None


# --------------------------------------------------------------------------- #
# tenancy: foreign and unknown teams
# --------------------------------------------------------------------------- #


async def test_a_foreign_team_is_not_in_org_and_holds_nothing_even_with_a_stray_admin_row(
    tree: Tree,
) -> None:
    """The shape a moved account leaves behind: a cross-org ADMIN row written
    directly (the membership service cannot produce one). The resolver must
    refuse on tenancy before it reads any role."""
    # The seat needs the org membership the schema requires behind it.
    tree.session.add(OrgMembership(user_id=tree.founder.id, org_team_id=tree.outsider_org))
    tree.session.add(
        TeamMembership(user_id=tree.founder.id, team_id=tree.outsider_org, role=TeamRole.ADMIN)
    )
    await tree.session.commit()
    answer = await _resolver(tree, _ctx(tree.founder)).for_team(tree.outsider_org)
    assert answer.in_org is False
    assert answer.roles == frozenset()
    assert answer.is_admin is False
    assert answer.highest is None
    assert answer.chain_ids == (tree.outsider_org,)


async def test_a_foreign_team_holds_nothing_even_with_a_stray_owner_assignment(
    tree: Tree,
) -> None:
    other_root = (
        await tree.session.execute(select(Team).where(Team.id == tree.outsider_org))
    ).scalar_one()
    tree.session.add(
        RoleAssignment(
            org_team_id=other_root.id,
            principal_kind=PrincipalKind.USER,
            principal_id=tree.founder.id,
            scope_kind=ScopeKind.ORG,
            scope_id=other_root.id,
            role=Role.OWNER,
        )
    )
    await tree.session.commit()
    answer = await _resolver(tree, _ctx(tree.founder)).for_team(tree.outsider_org)
    assert (answer.in_org, answer.roles) == (False, frozenset())


async def test_an_unknown_team_is_not_in_org_and_holds_nothing(tree: Tree) -> None:
    answer = await _resolver(tree, _ctx(tree.founder)).for_team(uuid4())
    assert answer.in_org is False
    assert answer.roles == frozenset()
    assert answer.chain_ids == ()


async def test_a_foreign_team_strips_even_the_agent_marker(tree: Tree) -> None:
    ctx = ActingContext.for_agent(
        user_id=tree.founder.id, org_id=tree.org, email=tree.founder.email, session_id="sess-1"
    )
    answer = await _resolver(tree, ctx).for_team(tree.outsider_org)
    assert answer.roles == frozenset()


# --------------------------------------------------------------------------- #
# assignments
# --------------------------------------------------------------------------- #


async def test_a_revoked_owner_row_is_ignored_but_the_admin_membership_remains(
    tree: Tree,
) -> None:
    owner_row = (
        await tree.session.execute(
            select(RoleAssignment).where(
                RoleAssignment.org_team_id == tree.org, RoleAssignment.role == Role.OWNER
            )
        )
    ).scalar_one()
    owner_row.revoked_at = datetime.now(UTC)
    await tree.session.commit()
    resolver = _resolver(tree, _ctx(tree.founder))
    assert (Role.OWNER in await resolver.org_roles()) is False
    assert await resolver.is_org_admin() is True
    assert (await resolver.for_team(tree.data)).roles == ADMIN_DOWN


async def test_a_viewer_grant_applies_on_its_team_and_does_not_descend(tree: Tree) -> None:
    viewer = await _user(tree.session, tree.org, "viewer")
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=viewer.id,
        scope_id=tree.eng,
        role=Role.VIEWER,
    )
    resolver = _resolver(tree, _ctx(viewer))
    eng = await resolver.for_team(tree.eng)
    assert eng.roles == frozenset({Role.VIEWER})
    assert eng.highest is Role.VIEWER
    assert (eng.is_member, eng.is_admin) == (False, False)
    assert (await resolver.for_team(tree.data)).roles == frozenset()
    assert (await resolver.for_team(tree.org)).roles == frozenset()


async def test_a_member_grant_applies_on_its_team_and_does_not_descend(tree: Tree) -> None:
    granted = await _user(tree.session, tree.org, "granted")
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=granted.id,
        scope_id=tree.eng,
        role=Role.MEMBER,
    )
    resolver = _resolver(tree, _ctx(granted))
    assert (await resolver.for_team(tree.eng)).roles == MEMBER_DOWN
    assert (await resolver.for_team(tree.data)).roles == frozenset()


async def test_an_admin_grant_on_an_ancestor_descends(tree: Tree) -> None:
    granted = await _user(tree.session, tree.org, "grantedadm")
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=granted.id,
        scope_id=tree.eng,
        role=Role.ADMIN,
    )
    resolver = _resolver(tree, _ctx(granted))
    assert (await resolver.for_team(tree.data)).roles == ADMIN_DOWN
    assert (await resolver.for_team(tree.platform)).roles == ADMIN_DOWN
    assert (await resolver.for_team(tree.org)).roles == frozenset()
    assert await resolver.is_org_admin() is False


async def test_an_owner_grant_at_org_scope_makes_an_org_admin_and_owner(tree: Tree) -> None:
    granted = await _user(tree.session, tree.org, "coowner")
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=granted.id,
        scope_id=tree.org,
        role=Role.OWNER,
        scope_kind=ScopeKind.ORG,
    )
    resolver = _resolver(tree, _ctx(granted))
    assert (Role.OWNER in await resolver.org_roles()) is True
    assert await resolver.is_org_admin() is True
    assert (await resolver.for_team(tree.platform)).roles == HUMAN_LADDER


async def test_a_grant_to_another_principal_kind_with_the_same_id_does_not_count(
    tree: Tree,
) -> None:
    """Assignments are keyed on (kind, id): a service grant that happens to
    carry a user's uuid is not that user's grant."""
    granted = await _user(tree.session, tree.org, "mismatch")
    await _grant(
        tree,
        principal_kind=PrincipalKind.SERVICE,
        principal_id=granted.id,
        scope_id=tree.org,
        role=Role.OWNER,
        scope_kind=ScopeKind.ORG,
    )
    assert (await _resolver(tree, _ctx(granted)).for_team(tree.data)).roles == frozenset()


async def test_a_revoked_grant_is_ignored(tree: Tree) -> None:
    granted = await _user(tree.session, tree.org, "revoked")
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=granted.id,
        scope_id=tree.data,
        role=Role.ADMIN,
        revoked=True,
    )
    assert (await _resolver(tree, _ctx(granted)).for_team(tree.data)).roles == frozenset()


# --------------------------------------------------------------------------- #
# the other credential shapes
# --------------------------------------------------------------------------- #


async def test_an_agent_carries_the_users_roles_plus_the_agent_marker(tree: Tree) -> None:
    ctx = ActingContext.for_agent(
        user_id=tree.data_member.id,
        org_id=tree.org,
        email=tree.data_member.email,
        session_id="sess-42",
    )
    resolver = _resolver(tree, ctx)
    assert (await resolver.for_team(tree.data)).roles == MEMBER_DOWN | {Role.AGENT}
    assert (await resolver.for_team(tree.platform)).roles == frozenset({Role.AGENT})
    assert await resolver.is_org_admin() is False


async def test_an_agent_for_the_founder_is_an_owner_and_an_agent(tree: Tree) -> None:
    ctx = ActingContext.for_agent(
        user_id=tree.founder.id, org_id=tree.org, email=tree.founder.email, session_id="sess-9"
    )
    resolver = _resolver(tree, ctx)
    assert (await resolver.for_team(tree.data)).roles == HUMAN_LADDER | {Role.AGENT}
    assert (Role.OWNER in await resolver.org_roles()) is True


async def test_a_personal_access_token_resolves_its_owners_roles(tree: Tree) -> None:
    ctx = ActingContext.for_pat(
        token_id=uuid4(),
        org_id=tree.org,
        label="laptop",
        user_id=tree.eng_admin.id,
        email=tree.eng_admin.email,
    )
    resolver = _resolver(tree, ctx)
    assert (await resolver.for_team(tree.data)).roles == ADMIN_DOWN
    assert Role.AGENT not in (await resolver.for_team(tree.data)).roles
    assert await resolver.is_org_admin() is False


async def test_a_ci_token_is_a_service_in_its_own_org_and_nothing_elsewhere(tree: Tree) -> None:
    ctx = ActingContext.for_service(
        token_id=uuid4(), org_id=tree.org, label="ci", credential=CredentialKind.CI_TOKEN
    )
    resolver = _resolver(tree, ctx)
    own = await resolver.for_team(tree.data)
    assert (own.in_org, own.roles, own.highest) == (True, frozenset({Role.SERVICE}), None)
    assert (await resolver.for_team(tree.org)).roles == frozenset({Role.SERVICE})
    assert await resolver.is_org_admin() is False
    foreign = await resolver.for_team(tree.outsider_org)
    assert (foreign.in_org, foreign.roles) == (False, frozenset())


async def test_a_service_grant_of_admin_on_a_team_makes_the_token_its_admin(tree: Tree) -> None:
    token_id = uuid4()
    await _grant(
        tree,
        principal_kind=PrincipalKind.SERVICE,
        principal_id=token_id,
        scope_id=tree.data,
        role=Role.ADMIN,
    )
    ctx = ActingContext.for_service(
        token_id=token_id, org_id=tree.org, label="proxy", credential=CredentialKind.PROXY_TOKEN
    )
    resolver = _resolver(tree, ctx)
    data = await resolver.for_team(tree.data)
    assert data.roles == frozenset({Role.SERVICE}) | ADMIN_DOWN
    assert data.highest is Role.ADMIN
    assert (await resolver.for_team(tree.platform)).roles == frozenset({Role.SERVICE})
    assert (await resolver.for_team(tree.eng)).roles == frozenset({Role.SERVICE})


# --------------------------------------------------------------------------- #
# one chain walk per team
# --------------------------------------------------------------------------- #


class _CountingLoader:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def __call__(self, db: AsyncSession, team_id: UUID) -> Sequence[Team]:
        self.calls.append(team_id)
        return await team_service.ancestor_chain(db, team_id)


async def test_the_same_team_twice_and_the_org_admin_question_cost_one_chain_fetch(
    tree: Tree,
) -> None:
    loader = _CountingLoader()
    resolver = RoleResolver(tree.session, _ctx(tree.eng_admin), ancestor_chain=loader)
    first = await resolver.for_team(tree.data)
    second = await resolver.for_team(tree.data)
    assert first == second
    assert await resolver.is_org_admin() is False
    assert (Role.OWNER in await resolver.org_roles()) is False
    assert loader.calls == [tree.data]


async def test_an_ancestor_of_a_walked_chain_is_answered_without_a_fetch(tree: Tree) -> None:
    loader = _CountingLoader()
    resolver = RoleResolver(tree.session, _ctx(tree.data_member), ancestor_chain=loader)
    await resolver.for_team(tree.data)
    eng = await resolver.for_team(tree.eng)
    root = await resolver.for_team(tree.org)
    assert loader.calls == [tree.data]
    assert eng.chain_ids == (tree.eng, tree.org)
    assert root.chain_ids == (tree.org,)
    assert eng.roles == MEMBER_DOWN
    assert root.roles == MEMBER_DOWN


async def test_answers_from_the_cache_match_answers_from_a_fresh_walk(tree: Tree) -> None:
    """An ancestor answered off a descendant's rows must equal the answer a
    fresh resolver gives when asked about that ancestor first."""
    granted = await _user(tree.session, tree.org, "cachecheck")
    await _join(tree.session, granted, tree.data, TeamRole.MEMBER)
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=granted.id,
        scope_id=tree.eng,
        role=Role.VIEWER,
    )
    warm = _resolver(tree, _ctx(granted))
    await warm.for_team(tree.data)
    cold = _resolver(tree, _ctx(granted))
    for team in (tree.eng, tree.org):
        assert await warm.for_team(team) == await cold.for_team(team)


async def test_a_sibling_team_needs_its_own_walk(tree: Tree) -> None:
    loader = _CountingLoader()
    resolver = RoleResolver(tree.session, _ctx(tree.data_member), ancestor_chain=loader)
    await resolver.for_team(tree.data)
    await resolver.for_team(tree.platform)
    assert loader.calls == [tree.data, tree.platform]
    # Eng and the root were on both chains; still no third fetch.
    await resolver.for_team(tree.eng)
    await resolver.for_team(tree.org)
    assert loader.calls == [tree.data, tree.platform]


async def test_org_roles_is_the_root_answer(tree: Tree) -> None:
    resolver = _resolver(tree, _ctx(tree.founder))
    assert await resolver.org_roles() == (await resolver.for_team(tree.org)).roles
    assert resolver.ctx.org_id == tree.org


# --------------------------------------------------------------------------- #
# a demotion or removal ends what the owner row granted
# --------------------------------------------------------------------------- #


async def _second_root_admin(tree: Tree) -> User:
    second = await _user(tree.session, tree.org, "second")
    await _join(tree.session, second, tree.org, TeamRole.ADMIN)
    return second


async def _founder_root_row(tree: Tree) -> TeamMembership:
    row = await membership_service.get(tree.session, team_id=tree.org, user_id=tree.founder.id)
    assert row is not None
    return row


async def test_a_demoted_founder_is_neither_owner_nor_admin_anywhere(tree: Tree) -> None:
    """The founder's owner row was justified by the admin membership the org was
    created with. Demoting that membership revokes the row in the same
    transaction, so the resolver stops merging owner standing back in."""
    await _second_root_admin(tree)
    await membership_service.change_role(
        tree.session, await _founder_root_row(tree), TeamRole.MEMBER
    )
    await tree.session.commit()

    resolver = _resolver(tree, _ctx(tree.founder))
    assert (Role.OWNER in await resolver.org_roles()) is False
    assert await resolver.is_org_admin() is False
    root = await resolver.for_team(tree.org)
    assert (root.in_org, root.roles, root.highest) == (True, MEMBER_DOWN, Role.MEMBER)
    for team in (tree.eng, tree.data, tree.platform):
        answer = await resolver.for_team(team)
        assert (answer.in_org, answer.roles, answer.highest) == (True, frozenset(), None)


async def test_re_promoting_the_founder_restores_admin_by_membership_but_not_owner(
    tree: Tree,
) -> None:
    """Owner standing returns only through an explicit assignment; a promotion
    writes a membership and nothing else."""
    await _second_root_admin(tree)
    row = await _founder_root_row(tree)
    await membership_service.change_role(tree.session, row, TeamRole.MEMBER)
    await tree.session.commit()
    await membership_service.change_role(tree.session, row, TeamRole.ADMIN)
    await tree.session.commit()

    resolver = _resolver(tree, _ctx(tree.founder))
    assert await resolver.is_org_admin() is True
    assert (Role.OWNER in await resolver.org_roles()) is False
    assert (await resolver.for_team(tree.data)).roles == ADMIN_DOWN


async def test_a_removed_founder_holds_nothing_in_the_org(tree: Tree) -> None:
    await _second_root_admin(tree)
    await membership_service.remove_member(tree.session, await _founder_root_row(tree))
    await tree.session.commit()

    resolver = _resolver(tree, _ctx(tree.founder))
    assert (Role.OWNER in await resolver.org_roles()) is False
    assert await resolver.is_org_admin() is False
    for team in (tree.org, tree.eng, tree.data):
        answer = await resolver.for_team(team)
        assert (answer.in_org, answer.roles) == (True, frozenset())


async def test_a_demotion_on_a_sub_team_ends_the_admin_grant_below_it_only(tree: Tree) -> None:
    """The eng admin also holds an explicit admin grant on Data and, oddly, an
    owner grant on the org root. Demoting them on Eng ends Eng and Data; the
    root grant was never Eng's to justify."""
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=tree.eng_admin.id,
        scope_id=tree.data,
        role=Role.ADMIN,
    )
    await _grant(
        tree,
        principal_kind=PrincipalKind.USER,
        principal_id=tree.eng_admin.id,
        scope_id=tree.org,
        role=Role.OWNER,
        scope_kind=ScopeKind.ORG,
    )
    eng_row = await membership_service.get(
        tree.session, team_id=tree.eng, user_id=tree.eng_admin.id
    )
    assert eng_row is not None
    await membership_service.change_role(tree.session, eng_row, TeamRole.MEMBER)
    await tree.session.commit()

    resolver = _resolver(tree, _ctx(tree.eng_admin))
    # The root owner grant still descends everywhere: that is the grant that stood.
    assert (Role.OWNER in await resolver.org_roles()) is True
    assert (await resolver.for_team(tree.data)).roles == HUMAN_LADDER
    live = (
        await tree.session.execute(
            select(RoleAssignment.scope_id, RoleAssignment.revoked_at.is_(None)).where(
                RoleAssignment.principal_id == tree.eng_admin.id
            )
        )
    ).all()
    assert {scope: live for scope, live in live} == {tree.data: False, tree.org: True}
