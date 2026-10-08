"""The pure allocation rules: deepest-first drawdown, leaf memberships, summed limits."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alkera_core.allocation_tree import (
    Ceiling,
    Term,
    chain_ceiling,
    deepest_first,
    depth_of,
    effective_terms,
    leaf_teams,
    raises_term,
    summed_limit,
    team_term,
)

ROOT, A, B, C, Z = (uuid4() for _ in range(5))
# root
# ├── A (Alpha)
# │   └── B (Beta)       (A/B)
# ├── C (Alpha)          same name as A, a tie on depth AND name
# └── Z (Aardvark)       sorts first by name among depth-1 teams
PARENT: dict[UUID, UUID | None] = {ROOT: None, A: ROOT, B: A, C: ROOT, Z: ROOT}
NAME = {ROOT: "Org", A: "Alpha", B: "Beta", C: "Alpha", Z: "Aardvark"}


def effective_limit(
    member_of: set[UUID],
    *,
    parent_of: dict[UUID, UUID | None],
    per_user: dict[UUID, int],
    team_allocation: dict[UUID, int],
) -> int | None:
    """A member's summed limit across their leaf terms, the way the budget
    readers total ``effective_terms`` (``None`` = no limit)."""
    return summed_limit(
        term.limit
        for term in effective_terms(
            member_of, parent_of=parent_of, per_user=per_user, team_allocation=team_allocation
        )
    )


@pytest.mark.parametrize(
    ("team", "depth"),
    [
        pytest.param(ROOT, 0, id="root"),
        pytest.param(A, 1, id="child"),
        pytest.param(B, 2, id="grandchild"),
    ],
)
def test_depth_counts_edges_to_the_root(team: UUID, depth: int) -> None:
    assert depth_of(team, PARENT) == depth


def test_depth_stops_at_a_cycle_instead_of_looping() -> None:
    x, y = uuid4(), uuid4()
    assert depth_of(x, {x: y, y: x}) == 1


def test_a_user_in_root_a_and_a_b_draws_b_then_a_then_root() -> None:
    assert deepest_first([ROOT, A, B], parent_of=PARENT, name_of=NAME) == [B, A, ROOT]


def test_input_order_does_not_change_the_draw_order() -> None:
    assert deepest_first([B, ROOT, A], parent_of=PARENT, name_of=NAME) == deepest_first(
        [A, B, ROOT], parent_of=PARENT, name_of=NAME
    )


def test_ties_on_depth_break_by_name_then_id() -> None:
    order = deepest_first([ROOT, A, C, Z], parent_of=PARENT, name_of=NAME)
    alpha = sorted([A, C], key=str)
    assert order == [Z, *alpha, ROOT]


@pytest.mark.parametrize(
    ("member_of", "leaves"),
    [
        pytest.param({ROOT, A, B}, {B}, id="materialized-chain-keeps-only-the-deepest"),
        pytest.param({ROOT, A, B, C}, {B, C}, id="two-branches-two-leaves"),
        pytest.param({ROOT}, {ROOT}, id="root-only-member-is-a-leaf-of-root"),
        pytest.param({ROOT, A}, {A}, id="one-level"),
        pytest.param(set(), set(), id="no-memberships"),
    ],
)
def test_leaf_teams_drop_materialized_ancestors(member_of: set[UUID], leaves: set[UUID]) -> None:
    assert leaf_teams(member_of, parent_of=PARENT) == leaves


@pytest.mark.parametrize(
    ("per_user", "allocation", "term"),
    [
        pytest.param(5, 100, 5, id="own-limit-below-the-allocation-binds"),
        pytest.param(10_000, 100, 100, id="the-allocation-caps-an-own-limit-above-it"),
        pytest.param(100, 100, 100, id="own-limit-equal-to-the-allocation"),
        pytest.param(0, 100, 0, id="a-zero-own-limit-is-a-limit-not-unset"),
        pytest.param(5, 0, 0, id="a-zero-allocation-caps-every-own-limit"),
        pytest.param(5, None, 5, id="own-limit-alone-when-no-allocation"),
        pytest.param(None, 100, 100, id="team-allocation-when-no-own-limit"),
        pytest.param(None, None, None, id="neither-is-unlimited"),
    ],
)
def test_team_term(per_user: int | None, allocation: int | None, term: int | None) -> None:
    assert team_term(per_user, allocation) == term


@pytest.mark.parametrize(
    ("current", "new", "allocation", "raises"),
    [
        pytest.param(10, 50, 100, True, id="higher-cap-under-the-allocation-raises"),
        pytest.param(10, 5, 100, False, id="lower-cap-narrows"),
        pytest.param(10, 10, 100, False, id="same-cap-is-not-a-raise"),
        pytest.param(100, 10_000, 100, False, id="above-the-ceiling-changes-nothing"),
        pytest.param(50, 10_000, 100, True, id="above-the-ceiling-from-below-raises-to-it"),
        pytest.param(None, 50, 100, False, id="first-cap-under-the-allocation-narrows"),
        pytest.param(None, 50, None, False, id="first-cap-with-no-allocation-narrows"),
        pytest.param(10, 50, None, True, id="higher-cap-with-no-allocation-raises"),
        pytest.param(10, None, 100, True, id="removing-a-cap-falls-back-to-the-allocation"),
        pytest.param(10, None, None, True, id="removing-the-only-bound-is-unlimited"),
        pytest.param(100, None, 100, False, id="removing-a-cap-at-the-allocation"),
        pytest.param(None, None, 100, False, id="removing-nothing"),
        pytest.param(None, None, None, False, id="unlimited-stays-unlimited"),
    ],
)
def test_raises_term(
    current: int | None, new: int | None, allocation: int | None, raises: bool
) -> None:
    assert raises_term(current, new, allocation) is raises


@pytest.mark.parametrize(
    ("terms", "total"),
    [
        pytest.param([1, 1], 2, id="sums"),
        pytest.param([3], 3, id="single"),
        pytest.param([1, None], None, id="one-unlimited-term-makes-it-unlimited"),
        pytest.param([], None, id="no-terms-is-unlimited"),
        pytest.param([0, 0], 0, id="zeros-sum-to-a-zero-limit"),
    ],
)
def test_summed_limit(terms: list[int | None], total: int | None) -> None:
    assert summed_limit(terms) == total


def test_effective_limit_sums_leaf_teams_and_ignores_the_materialized_root() -> None:
    # Root carries a huge allocation; it must not be added — the user is in root
    # only because they are in A/B and C.
    limit = effective_limit(
        {ROOT, A, B, C},
        parent_of=PARENT,
        per_user={B: 1, A: 50},
        team_allocation={C: 7, ROOT: 10_000},
    )
    assert limit == 1 + 7


def test_effective_limit_caps_each_own_limit_by_its_teams_allocation() -> None:
    limit = effective_limit(
        {ROOT, A, B, C},
        parent_of=PARENT,
        per_user={B: 10_000, C: 3},
        team_allocation={B: 100, C: 7},
    )
    assert limit == 100 + 3


def test_effective_limit_is_unlimited_when_a_leaf_team_sets_nothing() -> None:
    assert (
        effective_limit({ROOT, A, B, C}, parent_of=PARENT, per_user={B: 1}, team_allocation={})
        is None
    )


# --------------------------------------------------------------------------- #
# The ceiling descends: what binds a team is the tightest allocation on its chain
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("team", "allocation", "expected"),
    [
        pytest.param(B, {ROOT: 20, A: 8, B: 50}, (8, A), id="a-middle-team-caps-a-sub-team"),
        pytest.param(B, {ROOT: 20, A: 8, B: 3}, (3, B), id="the-teams-own-is-the-tightest"),
        pytest.param(B, {ROOT: 2, A: 8, B: 50}, (2, ROOT), id="the-root-caps-everything-below"),
        pytest.param(B, {ROOT: 5, A: 5, B: 5}, (5, B), id="a-tie-names-the-nearest-team"),
        pytest.param(B, {ROOT: 5, A: 5}, (5, A), id="a-tie-above-names-the-nearest-above"),
        pytest.param(B, {C: 1, Z: 1}, None, id="allocations-off-the-chain-do-not-count"),
        pytest.param(B, {}, None, id="no-allocation-anywhere-is-no-ceiling"),
        pytest.param(ROOT, {ROOT: 20}, (20, ROOT), id="the-root-alone"),
    ],
)
def test_chain_ceiling_is_the_tightest_allocation_on_the_chain(
    team: UUID, allocation: dict[UUID, int], expected: tuple[int, UUID] | None
) -> None:
    found = chain_ceiling(team, parent_of=PARENT, team_allocation=allocation)
    assert (None if found is None else (found.limit, found.team_id)) == expected


def test_chain_ceiling_ends_at_a_parent_the_map_does_not_know() -> None:
    orphan = uuid4()
    assert chain_ceiling(orphan, parent_of={}, team_allocation={ROOT: 1}) is None
    assert chain_ceiling(orphan, parent_of={orphan: ROOT}, team_allocation={ROOT: 1}) == Ceiling(
        limit=1, team_id=ROOT
    )


def test_chain_ceiling_cuts_a_cycle_instead_of_looping() -> None:
    x, y = uuid4(), uuid4()
    assert chain_ceiling(x, parent_of={x: y, y: x}, team_allocation={y: 7}) == Ceiling(
        limit=7, team_id=y
    )


def test_effective_limit_holds_a_leaf_to_the_allocation_above_it() -> None:
    """The middle admin's escape, closed at the read: B was handed 50 by A's
    admin while A itself has 8 and the root 20 — a member of B with a 9 cap
    is held to 8, the tightest figure on B's chain, not to B's 50."""
    limit = effective_limit(
        {ROOT, A, B},
        parent_of=PARENT,
        per_user={B: 9},
        team_allocation={ROOT: 20, A: 8, B: 50},
    )
    assert limit == 8


def test_effective_limit_still_sums_leaves_each_held_to_its_own_chain() -> None:
    limit = effective_limit(
        {ROOT, A, B, C},
        parent_of=PARENT,
        per_user={B: 9, C: 1},
        team_allocation={ROOT: 20, A: 8, B: 50, C: 7},
    )
    assert limit == 8 + 1


def test_the_root_allocation_alone_binds_a_member_placed_nowhere_deeper() -> None:
    assert effective_limit({ROOT}, parent_of=PARENT, per_user={}, team_allocation={ROOT: 20}) == 20
    assert (
        effective_limit({ROOT}, parent_of=PARENT, per_user={ROOT: 30}, team_allocation={ROOT: 20})
        == 20
    )


@pytest.mark.parametrize(
    ("per_user", "allocation", "limit", "bound_by"),
    [
        pytest.param({}, {A: 8}, 8, A, id="the-allowance-above-is-the-term"),
        pytest.param({B: 9}, {A: 8}, 8, A, id="a-cap-past-the-allowance-is-held-to-it"),
        pytest.param({B: 8}, {A: 8}, 8, A, id="a-cap-at-the-allowance-names-the-allowance"),
        pytest.param({B: 3}, {A: 8}, 3, None, id="a-cap-below-the-allowance-is-its-own"),
        pytest.param({B: 3}, {}, 3, None, id="a-cap-with-no-allowance-is-its-own"),
        pytest.param({}, {}, None, None, id="nothing-binds"),
    ],
)
def test_effective_terms_say_which_allocation_bound_a_leaf(
    per_user: dict[UUID, int], allocation: dict[UUID, int], limit: int | None, bound_by: UUID | None
) -> None:
    terms = effective_terms(
        {ROOT, A, B}, parent_of=PARENT, per_user=per_user, team_allocation=allocation
    )
    assert terms == [Term(team_id=B, limit=limit, bound_by=bound_by)]
