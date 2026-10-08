"""What roles a subject holds on a team, from memberships and assignments in
one chain walk.

Two tables answer the question. ``team_memberships`` is where the product has
always kept it: an ``admin`` row on any team up the ancestor chain grants admin
on the leaf (permission descent), and a row of any role on the leaf itself
makes the subject a member. ``role_assignments`` widens the vocabulary: an
``owner`` or ``admin`` grant descends like an admin membership; a ``member``,
``viewer`` or marker grant applies only on the team it names. A service token
holds ``service`` plus whatever it was granted and never a membership; an
agent carries the roles of the user it acts for plus the ``agent`` marker. The
set is closed under the role ladder before anyone reads it.

Fail closed: a team outside the subject's org — or one that does not resolve —
yields ``in_org=False`` and NO roles at all, even when a stray cross-org
membership row exists (the shape a moved account leaves behind). That is the
same precedence the team-admin dependency has always had: a foreign team is a
not-found before it is a forbidden.

The pure half, :func:`merge_roles`, takes rows in and gives a role set out so
every branch is reachable without a database. :class:`RoleResolver` is the
I/O shell: exactly one ancestor-chain fetch per distinct team, the two row
reads for that chain, and every later question about a team on a chain it
already walked answered from what it holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from alkera_core.authz.enums import PrincipalKind, Role, ScopeKind, expand_roles
from alkera_core.models._enums import TeamRole

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from alkera_core.authz.principal import ActingContext
    from alkera_core.models.team import Team

ChainLoader = Callable[["AsyncSession", UUID], Awaitable[Sequence["Team"]]]
"""Loads ``[team, parent, ..., org root]`` leaf-first; empty when the team does
not resolve. The backend binds ``team_service.ancestor_chain``."""

#: The human-role ladder, highest first — what :attr:`TeamRoles.highest` reads.
_LADDER_DESC: tuple[Role, ...] = (Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER)

#: Grants of these roles descend to every team below their scope; every other
#: role applies only on the team it names.
_DESCENDING: frozenset[Role] = frozenset({Role.OWNER, Role.ADMIN})


@dataclass(frozen=True, slots=True)
class TeamRoles:
    """The answer for one team: its ancestor chain (leaf first), whether the
    subject's org is on it, and the closed set of roles the subject holds."""

    team_id: UUID
    chain_ids: tuple[UUID, ...]
    in_org: bool
    roles: frozenset[Role]

    @property
    def is_admin(self) -> bool:
        return Role.ADMIN in self.roles

    @property
    def is_member(self) -> bool:
        return Role.MEMBER in self.roles

    @property
    def highest(self) -> Role | None:
        """The top rung of the human ladder held, ``None`` when only markers
        (or nothing) are held."""
        return next((role for role in _LADDER_DESC if role in self.roles), None)


@dataclass(frozen=True, slots=True)
class MembershipRow:
    team_id: UUID
    role: TeamRole


@dataclass(frozen=True, slots=True)
class AssignmentRow:
    scope_kind: ScopeKind
    scope_id: UUID
    role: Role


def merge_roles(
    *,
    subject_kind: PrincipalKind,
    chain_ids: Sequence[UUID],
    memberships: Iterable[MembershipRow],
    assignments: Iterable[AssignmentRow],
) -> frozenset[Role]:
    """The roles a subject holds on ``chain_ids[0]`` given its membership rows
    and live assignments, closed under the ladder.

    Memberships count only for a USER subject (a token is never a member): an
    ``admin`` row anywhere on the chain grants ADMIN, a row of any role on the
    leaf grants MEMBER, a row only on an ancestor grants nothing on the leaf.
    Assignments count for every subject kind, but only those whose scope is on
    the chain: OWNER and ADMIN descend, everything else applies on the leaf
    alone. A SERVICE subject always holds the SERVICE marker. An empty chain
    (an unresolvable team) holds nothing.
    """
    if not chain_ids:
        return frozenset()
    leaf = chain_ids[0]
    on_chain = set(chain_ids)
    held: set[Role] = set()
    if subject_kind is PrincipalKind.SERVICE:
        held.add(Role.SERVICE)
    if subject_kind is PrincipalKind.USER:
        for membership in memberships:
            if membership.team_id not in on_chain:
                continue
            if membership.role is TeamRole.ADMIN:
                held.add(Role.ADMIN)
            if membership.team_id == leaf:
                held.add(Role.MEMBER)
    for grant in assignments:
        if grant.scope_id not in on_chain:
            continue
        if grant.role in _DESCENDING or grant.scope_id == leaf:
            held.add(grant.role)
    return expand_roles(held)


@dataclass(frozen=True, slots=True)
class _ChainRows:
    """Everything one chain fetch learned, kept so any team on that chain can
    be answered again without a query."""

    chain_ids: tuple[UUID, ...]
    memberships: tuple[MembershipRow, ...]
    assignments: tuple[AssignmentRow, ...]


class RoleResolver:
    """Resolves a subject's roles per team for one request.

    Memoised per team id: asking about the same team twice, or about a team
    that sits on a chain already walked (an ancestor, the org root), costs no
    further query — an ancestor's chain is the tail of the chain that passed
    through it, and its rows are a subset of the rows already read.
    """

    def __init__(
        self, db: AsyncSession, ctx: ActingContext, *, ancestor_chain: ChainLoader
    ) -> None:
        self._db = db
        self._ctx = ctx
        self._ancestor_chain = ancestor_chain
        self._answers: dict[UUID, TeamRoles] = {}
        self._walked: list[_ChainRows] = []

    @property
    def ctx(self) -> ActingContext:
        return self._ctx

    async def for_team(self, team_id: UUID) -> TeamRoles:
        cached = self._answers.get(team_id)
        if cached is not None:
            return cached
        rows = self._from_walked(team_id)
        if rows is None:
            rows = await self._walk(team_id)
        answer = self._answer(team_id, rows)
        self._answers[team_id] = answer
        return answer

    async def org_roles(self) -> frozenset[Role]:
        return (await self.for_team(self._ctx.org_id)).roles

    async def is_org_admin(self) -> bool:
        """Admin of the org ROOT itself — the root row only, no descent, which
        is what the org-admin dependency has always meant (a sub-team admin
        holds a member row at the root and is refused)."""
        return Role.ADMIN in await self.org_roles()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _answer(self, team_id: UUID, rows: _ChainRows) -> TeamRoles:
        in_org = self._ctx.org_id in rows.chain_ids
        if not in_org:
            # A foreign or unresolvable team: nothing, not even the markers.
            return TeamRoles(
                team_id=team_id, chain_ids=rows.chain_ids, in_org=False, roles=frozenset()
            )
        held = set(
            merge_roles(
                subject_kind=self._ctx.subject.kind,
                chain_ids=rows.chain_ids,
                memberships=rows.memberships,
                assignments=rows.assignments,
            )
        )
        if self._ctx.is_agent:
            held.add(Role.AGENT)
        return TeamRoles(
            team_id=team_id, chain_ids=rows.chain_ids, in_org=True, roles=frozenset(held)
        )

    def _from_walked(self, team_id: UUID) -> _ChainRows | None:
        """The chain rows for ``team_id`` when it lies on a chain already
        walked: its chain is that chain from itself upward, its rows the subset
        on those teams."""
        for walked in self._walked:
            if team_id in walked.chain_ids:
                index = walked.chain_ids.index(team_id)
                chain = walked.chain_ids[index:]
                on_chain = set(chain)
                return _ChainRows(
                    chain_ids=chain,
                    memberships=tuple(m for m in walked.memberships if m.team_id in on_chain),
                    assignments=tuple(a for a in walked.assignments if a.scope_id in on_chain),
                )
        return None

    async def _walk(self, team_id: UUID) -> _ChainRows:
        chain = tuple(team.id for team in await self._ancestor_chain(self._db, team_id))
        if self._ctx.org_id not in chain:
            # A foreign or unresolvable team: no seat or grant there can count
            # for this request, so none is read.
            rows = _ChainRows(chain_ids=chain, memberships=(), assignments=())
        else:
            rows = _ChainRows(
                chain_ids=chain,
                memberships=await self._memberships(chain),
                assignments=await self._assignments(chain),
            )
        self._walked.append(rows)
        return rows

    async def _memberships(self, chain: tuple[UUID, ...]) -> tuple[MembershipRow, ...]:
        subject = self._ctx.subject
        if subject.kind is not PrincipalKind.USER:
            return ()
        # Imported here, not at module level: ``alkera_core.models`` imports the
        # authorization enums for the assignment model, so a top-level import
        # of the models from inside the authz package would be a cycle.
        from alkera_core.models.team_membership import TeamMembership

        result = await self._db.execute(
            select(TeamMembership.team_id, TeamMembership.role).where(
                TeamMembership.user_id == UUID(subject.id),
                TeamMembership.org_team_id == self._ctx.org_id,
                TeamMembership.team_id.in_(list(chain)),
            )
        )
        return tuple(MembershipRow(team_id=row[0], role=row[1]) for row in result.all())

    async def _assignments(self, chain: tuple[UUID, ...]) -> tuple[AssignmentRow, ...]:
        subject = self._ctx.subject
        from alkera_core.models.role_assignment import RoleAssignment

        result = await self._db.execute(
            select(RoleAssignment.scope_kind, RoleAssignment.scope_id, RoleAssignment.role).where(
                RoleAssignment.principal_kind == subject.kind,
                RoleAssignment.principal_id == UUID(subject.id),
                RoleAssignment.revoked_at.is_(None),
                RoleAssignment.scope_id.in_(list(chain)),
            )
        )
        return tuple(
            AssignmentRow(scope_kind=row[0], scope_id=row[1], role=row[2]) for row in result.all()
        )


__all__ = [
    "AssignmentRow",
    "ChainLoader",
    "MembershipRow",
    "RoleResolver",
    "TeamRoles",
    "merge_roles",
]
