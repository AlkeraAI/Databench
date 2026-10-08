"""The pure role merge: membership rows and assignment rows in, a closed role
set out. Every branch with plain data, no database."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    AssignmentRow,
    MembershipRow,
    PrincipalKind,
    Role,
    ScopeKind,
    TeamRoles,
    expand_roles,
    merge_roles,
)
from alkera_core.models import TeamRole

ROOT, ENG, DATA = uuid4(), uuid4(), uuid4()
#: Leaf-first, the way the ancestor chain arrives: Data → Eng → org root.
CHAIN: tuple[UUID, ...] = (DATA, ENG, ROOT)
ELSEWHERE = uuid4()

HUMAN_LADDER = frozenset({Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER})
ADMIN_DOWN = frozenset({Role.ADMIN, Role.MEMBER, Role.VIEWER})
MEMBER_DOWN = frozenset({Role.MEMBER, Role.VIEWER})


def _member(team_id: UUID, role: TeamRole = TeamRole.MEMBER) -> MembershipRow:
    return MembershipRow(team_id=team_id, role=role)


def _grant(scope_id: UUID, role: Role, kind: ScopeKind = ScopeKind.TEAM) -> AssignmentRow:
    return AssignmentRow(scope_kind=kind, scope_id=scope_id, role=role)


def _merge(
    *,
    subject_kind: PrincipalKind = PrincipalKind.USER,
    chain: tuple[UUID, ...] = CHAIN,
    memberships: tuple[MembershipRow, ...] = (),
    assignments: tuple[AssignmentRow, ...] = (),
) -> frozenset[Role]:
    return merge_roles(
        subject_kind=subject_kind,
        chain_ids=chain,
        memberships=memberships,
        assignments=assignments,
    )


# --------------------------------------------------------------------------- #
# memberships (user subjects)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("memberships", "expected"),
    [
        pytest.param((), frozenset(), id="no-rows-nothing"),
        pytest.param((_member(DATA),), MEMBER_DOWN, id="member-row-on-the-leaf"),
        pytest.param((_member(DATA, TeamRole.ADMIN),), ADMIN_DOWN, id="admin-row-on-the-leaf"),
        pytest.param(
            (_member(ROOT, TeamRole.ADMIN),), ADMIN_DOWN, id="org-admin-descends-to-grandchild"
        ),
        pytest.param((_member(ENG, TeamRole.ADMIN),), ADMIN_DOWN, id="parent-admin-descends"),
        pytest.param((_member(ENG),), frozenset(), id="ancestor-only-member-grants-nothing"),
        pytest.param((_member(ROOT),), frozenset(), id="root-only-member-grants-nothing"),
        pytest.param(
            (_member(ROOT), _member(ENG), _member(DATA)),
            MEMBER_DOWN,
            id="materialised-chain-of-member-rows-is-member-on-the-leaf",
        ),
        pytest.param(
            (_member(ROOT, TeamRole.ADMIN), _member(DATA)),
            ADMIN_DOWN,
            id="org-admin-plus-leaf-member",
        ),
        pytest.param(
            (_member(ELSEWHERE, TeamRole.ADMIN),),
            frozenset(),
            id="admin-row-off-the-chain-is-ignored",
        ),
        pytest.param((_member(ELSEWHERE),), frozenset(), id="member-row-off-the-chain-is-ignored"),
    ],
)
def test_memberships_merge_for_a_user(
    memberships: tuple[MembershipRow, ...], expected: frozenset[Role]
) -> None:
    assert _merge(memberships=memberships) == expected


@pytest.mark.parametrize(
    "subject_kind",
    [
        pytest.param(PrincipalKind.SERVICE, id="service"),
        pytest.param(PrincipalKind.AGENT, id="agent"),
        pytest.param(PrincipalKind.PAT, id="pat"),
    ],
)
def test_memberships_never_count_for_a_non_user_subject(subject_kind: PrincipalKind) -> None:
    """A token is never a member. Even a row bearing its id (impossible today,
    the column is a user foreign key) must grant it nothing."""
    rows = (_member(ROOT, TeamRole.ADMIN), _member(DATA, TeamRole.ADMIN))
    held = _merge(subject_kind=subject_kind, memberships=rows)
    assert Role.ADMIN not in held
    assert Role.MEMBER not in held


def test_a_service_subject_always_holds_the_service_marker_and_nothing_else_by_default() -> None:
    assert _merge(subject_kind=PrincipalKind.SERVICE) == frozenset({Role.SERVICE})


@pytest.mark.parametrize(
    "subject_kind",
    [
        pytest.param(PrincipalKind.USER, id="user"),
        pytest.param(PrincipalKind.AGENT, id="agent"),
        pytest.param(PrincipalKind.PAT, id="pat"),
    ],
)
def test_only_a_service_subject_gets_the_service_marker(subject_kind: PrincipalKind) -> None:
    assert Role.SERVICE not in _merge(subject_kind=subject_kind)


# --------------------------------------------------------------------------- #
# assignments
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("assignments", "expected"),
    [
        pytest.param(
            (_grant(ROOT, Role.OWNER, ScopeKind.ORG),),
            HUMAN_LADDER,
            id="owner-on-the-org-descends-as-the-whole-ladder",
        ),
        pytest.param((_grant(ENG, Role.ADMIN),), ADMIN_DOWN, id="admin-on-an-ancestor-descends"),
        pytest.param((_grant(DATA, Role.ADMIN),), ADMIN_DOWN, id="admin-on-the-leaf"),
        pytest.param((_grant(DATA, Role.MEMBER),), MEMBER_DOWN, id="member-on-the-leaf-applies"),
        pytest.param(
            (_grant(ENG, Role.MEMBER),), frozenset(), id="member-on-an-ancestor-does-not-descend"
        ),
        pytest.param(
            (_grant(DATA, Role.VIEWER),), frozenset({Role.VIEWER}), id="viewer-on-the-leaf-applies"
        ),
        pytest.param(
            (_grant(ENG, Role.VIEWER),), frozenset(), id="viewer-on-an-ancestor-does-not-descend"
        ),
        pytest.param(
            (_grant(ROOT, Role.VIEWER, ScopeKind.ORG),),
            frozenset(),
            id="viewer-on-the-org-does-not-descend",
        ),
        pytest.param(
            (_grant(ELSEWHERE, Role.OWNER, ScopeKind.ORG),),
            frozenset(),
            id="owner-of-another-scope-off-the-chain-is-ignored",
        ),
        pytest.param(
            (_grant(ELSEWHERE, Role.ADMIN),), frozenset(), id="admin-off-the-chain-is-ignored"
        ),
        pytest.param(
            (_grant(DATA, Role.AGENT),), frozenset({Role.AGENT}), id="agent-marker-on-the-leaf"
        ),
        pytest.param((_grant(ENG, Role.AGENT),), frozenset(), id="agent-marker-does-not-descend"),
        pytest.param(
            (_grant(DATA, Role.SERVICE),),
            frozenset({Role.SERVICE}),
            id="service-marker-on-the-leaf",
        ),
        pytest.param(
            (_grant(ENG, Role.SERVICE),), frozenset(), id="service-marker-does-not-descend"
        ),
        pytest.param(
            (_grant(DATA, Role.VIEWER), _grant(ROOT, Role.ADMIN, ScopeKind.ORG)),
            ADMIN_DOWN,
            id="the-union-closes-under-the-ladder",
        ),
    ],
)
def test_assignments_merge_for_a_user(
    assignments: tuple[AssignmentRow, ...], expected: frozenset[Role]
) -> None:
    assert _merge(assignments=assignments) == expected


def test_a_service_subject_adds_its_assignments_to_the_marker() -> None:
    held = _merge(subject_kind=PrincipalKind.SERVICE, assignments=(_grant(DATA, Role.ADMIN),))
    assert held == frozenset({Role.SERVICE}) | ADMIN_DOWN


def test_a_service_grant_on_an_ancestor_descends_like_a_users() -> None:
    held = _merge(subject_kind=PrincipalKind.SERVICE, assignments=(_grant(ENG, Role.ADMIN),))
    assert Role.ADMIN in held


def test_memberships_and_assignments_union_before_closing() -> None:
    held = _merge(
        memberships=(_member(DATA),),
        assignments=(_grant(ROOT, Role.OWNER, ScopeKind.ORG),),
    )
    assert held == HUMAN_LADDER


# --------------------------------------------------------------------------- #
# chain shapes
# --------------------------------------------------------------------------- #


def test_an_empty_chain_holds_nothing_whatever_the_rows_say() -> None:
    held = _merge(
        chain=(),
        memberships=(_member(ROOT, TeamRole.ADMIN),),
        assignments=(_grant(ROOT, Role.OWNER, ScopeKind.ORG),),
    )
    assert held == frozenset()


def test_an_empty_chain_holds_nothing_even_for_a_service() -> None:
    assert _merge(subject_kind=PrincipalKind.SERVICE, chain=()) == frozenset()


def test_a_single_team_chain_is_the_org_root_itself() -> None:
    """Asking about the root: only a row ON the root counts, which is what the
    org-admin check means — an admin of a sub-team holds a member row here."""
    assert _merge(chain=(ROOT,), memberships=(_member(ROOT, TeamRole.ADMIN),)) == ADMIN_DOWN
    assert _merge(chain=(ROOT,), memberships=(_member(ROOT),)) == MEMBER_DOWN
    assert _merge(chain=(ROOT,), memberships=(_member(ENG, TeamRole.ADMIN),)) == frozenset()


def test_the_result_is_always_closed_under_the_ladder() -> None:
    for role in Role:
        held = _merge(assignments=(_grant(DATA, role),))
        assert held == expand_roles(held)


def test_the_result_is_a_frozenset_of_roles() -> None:
    held = _merge(memberships=(_member(DATA, TeamRole.ADMIN),))
    assert isinstance(held, frozenset)
    assert all(isinstance(role, Role) for role in held)


def test_inputs_may_be_any_iterable_and_are_consumed_once() -> None:
    held = _merge(
        memberships=iter([_member(DATA)]),
        assignments=iter([_grant(ROOT, Role.ADMIN, ScopeKind.ORG)]),
    )
    assert held == ADMIN_DOWN


# --------------------------------------------------------------------------- #
# TeamRoles views
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("roles", "highest", "is_admin", "is_member"),
    [
        pytest.param(HUMAN_LADDER, Role.OWNER, True, True, id="owner"),
        pytest.param(ADMIN_DOWN, Role.ADMIN, True, True, id="admin"),
        pytest.param(MEMBER_DOWN, Role.MEMBER, False, True, id="member"),
        pytest.param(frozenset({Role.VIEWER}), Role.VIEWER, False, False, id="viewer"),
        pytest.param(frozenset({Role.SERVICE}), None, False, False, id="service-marker-only"),
        pytest.param(frozenset({Role.AGENT}), None, False, False, id="agent-marker-only"),
        pytest.param(frozenset(), None, False, False, id="nothing"),
        pytest.param(
            frozenset({Role.SERVICE}) | ADMIN_DOWN, Role.ADMIN, True, True, id="service-admin"
        ),
    ],
)
def test_team_roles_views(
    roles: frozenset[Role], highest: Role | None, is_admin: bool, is_member: bool
) -> None:
    view = TeamRoles(team_id=DATA, chain_ids=CHAIN, in_org=True, roles=roles)
    assert view.highest is highest
    assert view.is_admin is is_admin
    assert view.is_member is is_member
