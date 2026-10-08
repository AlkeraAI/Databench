"""The team allocation tree: which team comes first, and how much a member may use.

Teams nest, and a membership is materialized on every ancestor of the team a
user was added to. Two rules follow from that tree, and both live here:

- **Drawdown order.** Team pools are drawn deepest team first — the most
  specific budget before the broader one — with ties broken by team name, then
  id, so the order never depends on row order or UUID luck.
- **The ceiling descends.** A team's allocation is set from above it and bounds
  everything below it: a sub-team's allocation, a member's cap, a member's
  allowance. What binds a team is therefore the TIGHTEST allocation on the team
  or any team above it (:func:`chain_ceiling`) — an admin of a middle team can
  hand a sub-team no more than the middle team itself was given, and a cap
  written inside the sub-team is held to that same figure.
- **Summed limits.** A member of several teams may spend the SUM of what each
  team allows them: per team, their own limit there when one is set — never
  more than the team's ceiling — else that ceiling, else no limit (and one
  unlimited term makes the sum unlimited). Only *leaf* memberships contribute a
  term — the materialized ancestor rows are how the tree grants access, not
  extra allowances — but every ancestor's allocation still bounds the term
  through the ceiling.

A leaf membership is told apart from a materialized one by the tree alone: a
team the user belongs to is a leaf when the user belongs to none of its
sub-teams. (A user added by hand to both a team and one of its sub-teams is,
by this rule, in the sub-team only — the broader row adds no allowance.)

These are pure functions over the team tree, shared by every figure the tree
bounds: credit spent through a pool, and bytes stored in a team's folder.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

ParentMap = Mapping[UUID, UUID | None]


def depth_of(team_id: UUID, parent_of: ParentMap) -> int:
    """Edges between ``team_id`` and the highest ancestor ``parent_of`` knows.

    A parent missing from the map ends the walk there; a cycle (which the team
    tree never holds) is cut rather than looped forever."""
    depth = 0
    seen = {team_id}
    parent = parent_of.get(team_id)
    while parent is not None and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = parent_of.get(parent)
    return depth


def deepest_first(
    team_ids: Iterable[UUID], *, parent_of: ParentMap, name_of: Mapping[UUID, str]
) -> list[UUID]:
    """``team_ids`` ordered for drawdown: depth descending, then name, then id."""
    return sorted(
        set(team_ids),
        key=lambda t: (-depth_of(t, parent_of), name_of.get(t, ""), str(t)),
    )


def leaf_teams(member_of: Iterable[UUID], *, parent_of: ParentMap) -> set[UUID]:
    """The teams in ``member_of`` the user was placed in, not inherited up to.

    A team is dropped when some other team in the set sits below it — that row
    is the materialized ancestor of the deeper membership."""
    teams = set(member_of)
    has_member_below: set[UUID] = set()
    for team in teams:
        seen = {team}
        parent = parent_of.get(team)
        while parent is not None and parent not in seen:
            seen.add(parent)
            has_member_below.add(parent)
            parent = parent_of.get(parent)
    return teams - has_member_below


@dataclass(frozen=True, slots=True)
class Ceiling:
    """The tightest allocation on a team or any team above it, and the team
    that set it. ``team_id`` is the nearest team when several tie."""

    limit: int
    team_id: UUID


def chain_ceiling(
    team_id: UUID, *, parent_of: ParentMap, team_allocation: Mapping[UUID, int]
) -> Ceiling | None:
    """What binds ``team_id``: the lowest allocation on it or on any ancestor
    ``parent_of`` knows, or ``None`` when no team on the chain carries one.

    Walked from the team upward; a higher team replaces the answer only when
    its figure is strictly lower, so a tie names the team nearest the caller
    — the one whose admins are closest to the person reading the refusal. A
    parent missing from the map ends the walk; a cycle is cut, never looped."""
    found: Ceiling | None = None
    seen: set[UUID] = set()
    current: UUID | None = team_id
    while current is not None and current not in seen:
        seen.add(current)
        limit = team_allocation.get(current)
        if limit is not None and (found is None or limit < found.limit):
            found = Ceiling(limit=limit, team_id=current)
        current = parent_of.get(current)
    return found


def team_term(per_user: int | None, team_allocation: int | None) -> int | None:
    """What one team allows one member: their own limit, capped by the team's
    ceiling when both are set; else whichever is set; else ``None`` (no
    limit). The ceiling is set from above the team, so no cap a team admin
    writes — on a member or on themselves — can lift a member past it."""
    if per_user is None:
        return team_allocation
    if team_allocation is None:
        return per_user
    return min(per_user, team_allocation)


def raises_term(current_cap: int | None, new_cap: int | None, team_allocation: int | None) -> bool:
    """Whether replacing a member's own cap ``current_cap`` with ``new_cap``
    (``None`` = removing it) widens what the team allows them."""
    before = team_term(current_cap, team_allocation)
    after = team_term(new_cap, team_allocation)
    if after is None:
        return before is not None
    return before is not None and after > before


def summed_limit(terms: Iterable[int | None]) -> int | None:
    """The sum of per-team terms; ``None`` (no limit) when any term is
    unlimited or there are no terms at all."""
    total = 0
    any_term = False
    for term in terms:
        if term is None:
            return None
        total += term
        any_term = True
    return total if any_term else None


@dataclass(frozen=True, slots=True)
class Term:
    """What one leaf team allows a member, and where the figure came from.

    ``bound_by`` is the team whose allocation is the term (the leaf itself or
    an ancestor above it); ``None`` when the member's own cap is the term, or
    when nothing bounds them there."""

    team_id: UUID
    limit: int | None
    bound_by: UUID | None


def effective_terms(
    member_of: Iterable[UUID],
    *,
    parent_of: ParentMap,
    per_user: Mapping[UUID, int],
    team_allocation: Mapping[UUID, int],
) -> list[Term]:
    """One term per leaf team, in a stable order: the member's own cap there,
    held to the ceiling on the leaf's chain; else that ceiling; else no limit."""
    terms: list[Term] = []
    for team_id in sorted(leaf_teams(member_of, parent_of=parent_of), key=str):
        ceiling = chain_ceiling(team_id, parent_of=parent_of, team_allocation=team_allocation)
        own = per_user.get(team_id)
        limit = team_term(own, None if ceiling is None else ceiling.limit)
        bound_by = (
            ceiling.team_id
            if ceiling is not None and limit == ceiling.limit and (own is None or own >= limit)
            else None
        )
        terms.append(Term(team_id=team_id, limit=limit, bound_by=bound_by))
    return terms


__all__ = [
    "Ceiling",
    "ParentMap",
    "Term",
    "chain_ceiling",
    "deepest_first",
    "depth_of",
    "effective_terms",
    "leaf_teams",
    "raises_term",
    "summed_limit",
    "team_term",
]
