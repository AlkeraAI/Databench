"""How a node's grants are read: the ``GrantSource`` seam and today's one impl.

A grant is engine-neutral by construction — a principal whose ``kind`` is an
open registry, a role that is a string, an ``expires_at`` and a ``conditions``
bag that exist from day one and are the hooks the later ABAC engine reads. When
that engine lands it ships its own :class:`GrantSource`; nothing above this
module knows which one it is talking to.

:class:`FilesGrantSource` reads the interned ``file_acls`` body through
``node.acl_id`` — the materialized union of the node's chain, which is what
makes a listing one join instead of one walk per row. That cache is only ever a
cache: while a node is ``acl_rewriting`` the body is known stale, so the source
recomputes from the chain's ``file_shares`` rows, which are the truth.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from alkera_core.files.ids import AclId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl, FileShare
from alkera_core.models.files.tree import FileNode

#: The node state that says "the cached ACL body is being rebuilt"; while it is
#: set the chain, not the cache, answers.
ACL_REWRITING = "acl_rewriting"

#: What ``origin`` says about where a grant came from.
ORIGIN_DIRECT = "direct"
ORIGIN_INHERITED = "inherited"
ORIGIN_DRIVE_DEFAULT = "drive_default"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a grant names. ``kind`` is an open registry — ``user``, ``team`` and
    ``org`` today, ``agent``/``service``/``link``/``public`` and whatever the
    engine invents later — so an unknown kind is data to ignore, never a crash."""

    kind: str
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """The caller, in the engine-neutral terms a principal matcher may read.

    Built by the decider from the caller's context and facts so a matcher never
    sees a request, a session or a role name — which is what lets a kind the
    ladder has never heard of be registered from outside this package.
    """

    user_id: uuid.UUID | None = None
    team_ids: frozenset[uuid.UUID] = frozenset()
    team_admin_ids: frozenset[uuid.UUID] = frozenset()
    org_id: uuid.UUID | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


#: Whether one grant's principal is this caller. Pure: the whole world it may
#: read is the :class:`CallerIdentity` it is handed.
PrincipalMatcher = Callable[["Principal", CallerIdentity], bool]

_MATCHERS: dict[str, PrincipalMatcher] = {}


def register_principal_kind(kind: str, matcher: PrincipalMatcher) -> None:
    """Teach the decider a principal kind.

    This is the registration point the ``Principal.kind`` open registry needs:
    ``user``, ``team`` and ``org`` are registered below exactly the way an
    ``agent``, a ``service``, a ``link`` or one of the coming engine's own kinds
    is, so admitting a new one is a call rather than a branch added to the
    decider. A duplicate is refused here, at wiring time, rather than silently
    changing who a live grant matches.
    """
    if not kind:
        raise ValueError("a principal kind may not be empty")
    if kind in _MATCHERS:
        raise ValueError(f"principal kind {kind!r} is already registered")
    _MATCHERS[kind] = matcher


def unregister_principal_kind(kind: str) -> None:
    """Forget ``kind``. For an engine that swaps the whole set at boot, and for
    tests that register one; a kind nobody registered is not an error."""
    _MATCHERS.pop(kind, None)


def principal_matcher(kind: str) -> PrincipalMatcher | None:
    """The matcher for ``kind``, or ``None`` — an unknown kind is data to
    ignore, never a crash."""
    return _MATCHERS.get(kind)


def principal_kinds() -> tuple[str, ...]:
    """Every registered kind, in registration order."""
    return tuple(_MATCHERS)


register_principal_kind(
    "user", lambda principal, caller: caller.user_id is not None and principal.id == caller.user_id
)
register_principal_kind("team", lambda principal, caller: principal.id in caller.team_ids)
register_principal_kind(
    "org", lambda principal, caller: caller.org_id is not None and principal.id == caller.org_id
)


@dataclass(frozen=True, slots=True)
class GrantOrigin:
    """Where a grant came from: a direct grant on the node, one inherited from
    a named ancestor, or the drive's default."""

    kind: str
    ancestor_id: uuid.UUID | None = None

    @staticmethod
    def direct() -> GrantOrigin:
        return GrantOrigin(ORIGIN_DIRECT)

    @staticmethod
    def inherited(ancestor_id: uuid.UUID) -> GrantOrigin:
        return GrantOrigin(ORIGIN_INHERITED, ancestor_id)

    @staticmethod
    def drive_default() -> GrantOrigin:
        return GrantOrigin(ORIGIN_DRIVE_DEFAULT)


@dataclass(frozen=True, slots=True)
class Grant:
    """One allow entry. There are no deny entries anywhere in Files."""

    principal: Principal
    role: str
    origin: GrantOrigin = field(default_factory=GrantOrigin.direct)
    expires_at: datetime | None = None
    #: Reserved for ABAC. Today the decider understands ``team_role`` and
    #: refuses anything else, so a condition it cannot evaluate fails closed.
    conditions: Mapping[str, Any] | None = None


class GrantSource(Protocol):
    """How grants are read for a node and its ancestor chain."""

    async def grants_for(
        self, repo: FilesRepo, node: FileNode, chain: Sequence[FileNode]
    ) -> Sequence[Grant]: ...


#: The condition key today's evaluator understands. A grant carrying anything
#: else is dropped rather than guessed at, so an ABAC clause written for the
#: later engine can never widen access here by being unreadable.
CONDITION_TEAM_ROLE = "team_role"
CONDITION_TEAM_ROLE_ADMIN = "admin"


def conditions_hold(grant: Grant, identity: CallerIdentity) -> bool:
    """Whether every condition on ``grant`` holds for ``identity``. A key or a
    value this evaluator does not know fails closed."""
    for key, value in (grant.conditions or {}).items():
        if key != CONDITION_TEAM_ROLE or value != CONDITION_TEAM_ROLE_ADMIN:
            return False
        if grant.principal.id not in identity.team_admin_ids:
            return False
    return True


def grant_admits(grant: Grant, identity: CallerIdentity, *, now: datetime) -> bool:
    """Whether ``grant`` gives ``identity`` its rung at ``now``.

    The one evaluation every door makes, so the Files decider, the chat and
    workspace doors and the socket cannot disagree about a grant: it has not
    run out, every condition on it holds, and its principal is this caller.
    A principal kind nobody registered matches nobody.
    """
    if grant.expires_at is not None and grant.expires_at <= now:
        return False
    if not conditions_hold(grant, identity):
        return False
    matcher = principal_matcher(grant.principal.kind)
    return matcher is not None and matcher(grant.principal, identity)


def _expiry(raw: object) -> tuple[bool, datetime | None]:
    """``(readable, instant)`` for an ACE's ``expires_at``: a datetime as read
    from ``file_shares``, or the ISO string an interned body stores. A value
    that cannot be read is unreadable, and the grant it is on is dropped."""
    if raw is None:
        return True, None
    if isinstance(raw, datetime):
        instant = raw
    elif isinstance(raw, str):
        try:
            instant = datetime.fromisoformat(raw)
        except ValueError:
            return False, None
    else:
        return False, None
    return True, instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)


def ace_to_grant(ace: Mapping[str, Any], *, node_id: uuid.UUID) -> Grant | None:
    """One ACE row of a ``file_acls`` body as a :class:`Grant`, or ``None`` when
    the row is too malformed to mean anything (a newer writer's shape). An
    expiry that cannot be read drops the grant rather than making it permanent."""
    kind = ace.get("principal_kind")
    raw_id = ace.get("principal_id")
    role = ace.get("role")
    if not isinstance(kind, str) or not isinstance(role, str) or raw_id is None:
        return None
    try:
        principal_id = raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(str(raw_id))
    except ValueError:
        return None
    origin_raw = ace.get("origin")
    origin = GrantOrigin.direct()
    if isinstance(origin_raw, str):
        if origin_raw.startswith(ORIGIN_INHERITED):
            ancestor = ace.get("origin_ancestor_id")
            origin = GrantOrigin.inherited(
                uuid.UUID(str(ancestor)) if ancestor is not None else node_id
            )
        elif origin_raw == ORIGIN_DRIVE_DEFAULT:
            origin = GrantOrigin.drive_default()
    readable, expires_at = _expiry(ace.get("expires_at"))
    if not readable:
        return None
    conditions = ace.get("conditions")
    return Grant(
        principal=Principal(kind=kind, id=principal_id),
        role=role,
        origin=origin,
        expires_at=expires_at,
        conditions=conditions if isinstance(conditions, Mapping) else None,
    )


def drive_default_grants(body: Iterable[Any], *, node_id: uuid.UUID) -> list[Grant]:
    """The drive-default grants in an interned ACL body.

    A drive-default grant — the ``{user: owner}`` of a home, the ``{org: reader}``
    of ``/Shared``, the team grant of a ``/Teams/<team>`` — is minted onto the
    folder's ACL body by :func:`~alkera_core.files.authz.defaults.default_acl`
    and has NO ``file_shares`` row: it lives only in the interned body of the
    folder it was created on. So a descendant whose own cache is not yet
    materialized (``acl_id`` still ``NULL``) inherits it only if the chain read
    picks it up here — a ``file_shares``-only walk would hand a member back their
    own home as if it carried no grant at all.

    Only ``drive_default`` ACEs are returned: an ancestor's ``direct`` grants are
    its ``file_shares`` (read separately, so returning them here would double
    them), and an ancestor's ``inherited`` ACEs are copies of a drive-default
    grant that sits higher in the same chain and is collected from its own body.
    """
    grants: list[Grant] = []
    for ace in body:
        if isinstance(ace, Mapping) and ace.get("origin") == ORIGIN_DRIVE_DEFAULT:
            grant = ace_to_grant(ace, node_id=node_id)
            if grant is not None:
                grants.append(grant)
    return grants


async def default_grants_of_chain(
    repo: FilesRepo, chain: Sequence[FileNode]
) -> Mapping[uuid.UUID, Sequence[Grant]]:
    """Every ancestor's drive-default grants, in ONE statement, grouped by the
    ancestor they sit on.

    Only an ancestor whose cache is trustworthy (not ``acl_rewriting``, with an
    ``acl_id``) carries a readable drive-default ACE; a rewriting or cache-less
    ancestor contributes through its ``file_shares`` like any other node, so it
    is skipped here rather than read from a body known to be stale.
    """
    by_acl: dict[uuid.UUID, uuid.UUID] = {
        ancestor.id: ancestor.acl_id
        for ancestor in chain
        if ancestor.state != ACL_REWRITING and ancestor.acl_id is not None
    }
    if not by_acl:
        return {}
    stmt = repo.select_acls().where(FileAcl.id.in_(list(set(by_acl.values()))))
    bodies = {row.id: row.body for row in (await repo.session.execute(stmt)).scalars().all()}
    grouped: dict[uuid.UUID, Sequence[Grant]] = {}
    for node_id, acl_id in by_acl.items():
        body = bodies.get(acl_id)
        if not body:
            continue
        defaults = drive_default_grants(body, node_id=node_id)
        if defaults:
            grouped[node_id] = defaults
    return grouped


async def shares_of_many(
    repo: FilesRepo, node_ids: Iterable[uuid.UUID]
) -> Mapping[uuid.UUID, Sequence[FileShare]]:
    """Every live grant on every one of ``node_ids``, in ONE statement, grouped
    by the node it sits on.

    One ``shares_of`` per ancestor would cost a node at depth 256 that many
    round trips for a single access question. The rows are the same; the
    grouping ``shares_of`` does in the database happens here in Python. A node
    with no grants is absent from the mapping.
    """
    wanted = list(dict.fromkeys(node_ids))
    if not wanted:
        return {}
    stmt = repo.select_shares().where(
        FileShare.node_id.in_(wanted),
        FileShare.revoked_at.is_(None),
    )
    grouped: dict[uuid.UUID, list[FileShare]] = {}
    for share in (await repo.session.execute(stmt)).scalars().all():
        grouped.setdefault(share.node_id, []).append(share)
    return grouped


class FilesGrantSource:
    """Today's source: the interned ACL cache when it is trustworthy, the
    chain's ``file_shares`` rows when it is not."""

    async def grants_for(
        self, repo: FilesRepo, node: FileNode, chain: Sequence[FileNode]
    ) -> Sequence[Grant]:
        if node.state != ACL_REWRITING and node.acl_id is not None:
            cached = await self._from_cache(repo, node)
            if cached is not None:
                return cached
        return await self._from_chain(repo, node, chain)

    async def _from_cache(self, repo: FilesRepo, node: FileNode) -> Sequence[Grant] | None:
        assert node.acl_id is not None
        acl = await repo.acl(AclId(node.acl_id))
        if acl is None:
            return None
        grants = [ace_to_grant(ace, node_id=node.id) for ace in acl.body if isinstance(ace, dict)]
        return [grant for grant in grants if grant is not None]

    async def _from_chain(
        self, repo: FilesRepo, node: FileNode, chain: Sequence[FileNode]
    ) -> Sequence[Grant]:
        by_node = await shares_of_many(repo, (ancestor.id for ancestor in chain))
        defaults = await default_grants_of_chain(repo, chain)
        grants: list[Grant] = []
        for ancestor in chain:
            origin = (
                GrantOrigin.direct()
                if ancestor.id == node.id
                else GrantOrigin.inherited(ancestor.id)
            )
            for share in by_node.get(ancestor.id, ()):
                grants.append(
                    Grant(
                        principal=Principal(kind=share.principal_kind, id=share.principal_id),
                        role=share.role,
                        origin=origin,
                        expires_at=share.expires_at,
                        conditions=share.conditions,
                    )
                )
            for grant in defaults.get(ancestor.id, ()):
                grants.append(
                    Grant(
                        principal=grant.principal,
                        role=grant.role,
                        origin=origin,
                        expires_at=grant.expires_at,
                        conditions=grant.conditions,
                    )
                )
        return grants


__all__ = [
    "ACL_REWRITING",
    "CONDITION_TEAM_ROLE",
    "CONDITION_TEAM_ROLE_ADMIN",
    "ORIGIN_DIRECT",
    "ORIGIN_DRIVE_DEFAULT",
    "ORIGIN_INHERITED",
    "CallerIdentity",
    "FilesGrantSource",
    "Grant",
    "GrantOrigin",
    "GrantSource",
    "Principal",
    "PrincipalMatcher",
    "ace_to_grant",
    "conditions_hold",
    "default_grants_of_chain",
    "drive_default_grants",
    "grant_admits",
    "principal_kinds",
    "principal_matcher",
    "register_principal_kind",
    "shares_of_many",
    "unregister_principal_kind",
]
