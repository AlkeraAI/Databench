"""The rung a chat's node in the file tree gives a caller.

A chat's ``visibility_scope`` says who it was ADDRESSED to; the node's ACL says
who may read it and what each of those people may DO with it. A rung is
something somebody granted, and a grant is only ever made on the node, so the
node's interned ACL is the only place this answer can come from.

It is spelled once, here, because both doors that carry a message need it and
they must not drift: the REST door (``POST /chats/{id}/messages`` and every
other chat decision) and the socket door (the doc-sync registry).
"""

from __future__ import annotations

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.db.tenant_session import restore_role, swap_role
from alkera_core.db.unwind import then_restore
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.grants import ACL_REWRITING as ACL_REWRITING_STATE
from alkera_core.files.authz.grants import (
    ORIGIN_DRIVE_DEFAULT,
    CallerIdentity,
    ace_to_grant,
    grant_admits,
)
from alkera_core.files.authz.ladder import DEFAULT_LADDER
from alkera_core.files.repo import APP_ROLE, ORG_SETTING, ancestor_chain_predicate
from alkera_core.models.files.acl import FileAcl, FileShare
from alkera_core.models.files.tree import FileNode
from sqlalchemy import Select, and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased


def rungs_for(body: Any, identity: CallerIdentity, *, node_id: UUID, now: datetime) -> list[str]:
    """Every rung an interned ACL body gives ``identity`` at ``now``.

    Each ACE is read and judged by the Files decider's own evaluator
    (:func:`~alkera_core.files.authz.grants.grant_admits`), so a grant that
    has run out, a condition that does not hold, and a principal kind nobody
    registered count for nothing here exactly as they count for nothing there.
    """
    rungs: list[str] = []
    for ace in body if isinstance(body, list) else ():
        grant = ace_to_grant(ace, node_id=node_id) if isinstance(ace, dict) else None
        if grant is not None and grant_admits(grant, identity, now=now):
            rungs.append(grant.role)
    return rungs


def _chain_shares(node_ids: Sequence[UUID], org_id: UUID) -> Select[Any]:
    """Every live grant on each node's root-to-node chain, in one statement,
    labelled with the node it was read for.

    The same rows, in the same shape, the Files decider falls back on when a
    cache cannot be trusted: the ``file_shares`` on the node itself and on
    every ancestor above it. One indexed self-join on the derived path rather
    than a read of the node followed by a read per ancestor — and one statement
    for every node of a page that needs it, not one per node.
    """
    anchor = aliased(FileNode, name="anchor")
    return (
        select(
            anchor.id.label("anchor_id"),
            FileShare.principal_kind,
            FileShare.principal_id,
            FileShare.role,
            FileShare.expires_at,
            FileShare.conditions,
        )
        .select_from(anchor)
        .join(
            FileNode,
            and_(FileNode.drive_id == anchor.drive_id, ancestor_chain_predicate(anchor)),
        )
        .join(FileShare, FileShare.node_id == FileNode.id)
        .where(
            anchor.id.in_(list(node_ids)),
            anchor.org_team_id == org_id,
            FileNode.org_team_id == org_id,
            FileShare.org_team_id == org_id,
            FileShare.revoked_at.is_(None),
        )
    )


def _chain_defaults(node_ids: Sequence[UUID], org_id: UUID) -> Select[Any]:
    """The interned bodies of every trustworthy ancestor on each node's chain,
    labelled with the node they were read for.

    A drive-default grant (the ``{user: owner}`` of a home, the team grant of a
    team folder) has no ``file_shares`` row: it lives only in the body of the
    folder it was minted on. The Files decider's own fallback reads it from
    there (``default_grants_of_chain``), and so does this one; a walk of
    ``file_shares`` alone would tell the owner of a home that a node inside it,
    mid-rewrite, carries no grant for them at all.
    """
    anchor = aliased(FileNode, name="anchor")
    return (
        select(anchor.id.label("anchor_id"), FileAcl.body)
        .select_from(anchor)
        .join(
            FileNode,
            and_(FileNode.drive_id == anchor.drive_id, ancestor_chain_predicate(anchor)),
        )
        .join(FileAcl, and_(FileAcl.id == FileNode.acl_id, FileAcl.org_team_id == org_id))
        .where(
            anchor.id.in_(list(node_ids)),
            anchor.org_team_id == org_id,
            FileNode.org_team_id == org_id,
            FileNode.state != ACL_REWRITING_STATE,
        )
    )


def _with_defaults(
    bodies: dict[UUID, list[dict[str, Any]]], rows: Sequence[Any]
) -> dict[UUID, list[dict[str, Any]]]:
    """``bodies`` with each anchor's drive-default ACEs from its ancestors'
    bodies added: only those, since an ancestor's direct grants are already its
    ``file_shares`` and its inherited ACEs copy a default collected higher up."""
    for row in rows:
        for ace in row.body if isinstance(row.body, list) else ():
            if isinstance(ace, dict) and ace.get("origin") == ORIGIN_DRIVE_DEFAULT:
                bodies.setdefault(row.anchor_id, []).append(ace)
    return bodies


def _reads_chain(row: Any) -> bool:
    """Whether a node's grants must be read off its chain rather than its
    cached body: mid-rewrite, or with no body materialized."""
    return bool(row.state == ACL_REWRITING_STATE or row.body is None)


def _as_bodies(rows: Sequence[Any]) -> dict[UUID, list[dict[str, Any]]]:
    """Chain grants in the shape an interned body has, one body per anchor
    node, so both paths go through the same reducer and an expiry or a
    condition is skipped identically."""
    bodies: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        bodies.setdefault(row.anchor_id, []).append(
            {
                "principal_kind": row.principal_kind,
                "principal_id": row.principal_id,
                "role": row.role,
                "expires_at": row.expires_at,
                "conditions": row.conditions,
            }
        )
    return bodies


async def shared_object_role(
    db: AsyncSession,
    *,
    org_id: UUID,
    object_id: UUID,
    user_id: UUID | None,
    team_ids: AbstractSet[UUID],
) -> str | None:
    """The rung the node backing ``object_id`` gives this caller.

    The interned body is the same cache every listing joins against, so the
    ordinary answer is one indexed read rather than a walk of the ancestor
    chain. A node the subtree repair has marked ``acl_rewriting`` is the one
    exception: its cached body describes a chain that may no longer exist, so
    this reads the chain's ``file_shares`` rows instead — the truth, and the
    same fallback the Files decider itself makes. Slower, never wrong, in
    either direction: a grant that was revoked is gone from the chain the
    instant it is revoked, and a grant that still stands still counts.

    Falling closed here instead would not have been a short window. A move
    marks its whole subtree and a grant on a folder marks every descendant,
    and the repair that lifts the mark is a background job, so a chat under a
    moved or shared folder would have answered "no rung" — no read at all, now
    that a chat is private until shared — to every deliberate grant for as
    long as the mark stood, on both doors, while the share dialog still said
    "Can edit".

    Both paths are reduced with the ladder and the principal registry the Files
    decider uses, so a new principal kind is admitted here by registering it
    there. ``None`` is no rung at all (no read, no send): an object with no
    node (an id minted outside the REST surface), a node with neither a cached
    body nor a grant on its chain, or a node whose every grant has run out or
    names someone else. Expiry and conditions are judged by the decider's own
    evaluator at the wall clock; a team-admin condition fails closed here,
    since this read carries no team-admin facts.

    A caller with no user behind it — an agent or a token with no delegating
    user — holds no grant at all: a grant is made to a person or a team, and
    there is nobody here for one to name.
    """
    roles = await shared_object_roles(
        db, org_id=org_id, object_ids=(object_id,), user_id=user_id, team_ids=team_ids
    )
    return roles.get(object_id)


async def shared_object_roles(
    db: AsyncSession,
    *,
    org_id: UUID,
    object_ids: Sequence[UUID],
    user_id: UUID | None,
    team_ids: AbstractSet[UUID],
) -> dict[UUID, str]:
    """:func:`shared_object_role` for a whole page in ONE indexed read: the
    rung each of ``object_ids`` gives this caller, keyed by object id. An
    object that answers ``None`` (see the three cases above) is absent from the
    map, so a listing of fifty chats costs one Files query, not fifty — plus
    one more, for the page's nodes mid-rewrite, when there are any.
    """
    if user_id is None or not object_ids:
        return {}
    node_stmt = (
        select(FileNode.id, FileNode.target_object_id, FileNode.state, FileAcl.body)
        .outerjoin(FileAcl, and_(FileAcl.id == FileNode.acl_id, FileAcl.org_team_id == org_id))
        .where(
            FileNode.org_team_id == org_id,
            FileNode.target_object_id.in_(list(object_ids)),
            FileNode.trashed_at.is_(None),
        )
    )
    # The Files tables answer only to the Files role with the tenant stamped:
    # row-level security is FORCEd on them and the policy names that role, so
    # an ordinary session reads nothing at all rather than reading across
    # tenants. Both settings are transaction-scoped and the role is handed
    # straight back, so the caller's next statement is its own again and a
    # rollback of the operation takes this with it.
    prior_role = await swap_role(db, APP_ROLE)
    async with then_restore(lambda: restore_role(db, prior_role)):
        await db.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": ORG_SETTING, "value": str(org_id)},
        )
        rows = (await db.execute(node_stmt)).all()
        # The chain is the truth wherever the cache cannot be trusted: a node
        # mid-rewrite, and a node with no materialized body (one carrying an
        # expiring grant has none), exactly where the Files decider reads it.
        rewriting = [row.id for row in rows if _reads_chain(row)]
        chains = (
            _with_defaults(
                _as_bodies((await db.execute(_chain_shares(rewriting, org_id))).all()),
                (await db.execute(_chain_defaults(rewriting, org_id))).all(),
            )
            if rewriting
            else {}
        )
    identity = CallerIdentity(
        user_id=user_id,
        team_ids=frozenset(team_ids),
        team_admin_ids=frozenset(),
        org_id=org_id,
    )
    roles: dict[UUID, str] = {}
    for row in rows:
        body: Any = chains.get(row.id) if _reads_chain(row) else row.body
        if row.target_object_id is None or not body:
            continue
        rung = DEFAULT_LADDER.max(
            rungs_for(body, identity, node_id=UUID(str(row.id)), now=datetime.now(UTC))
        )
        if rung is not None:
            roles[UUID(str(row.target_object_id))] = rung
    return roles


def rung_writes(rung: str | None) -> bool:
    """Whether a rung on an object's node carries the ladder's WRITE.

    Spelled here, next to the read that produces the rung, because the two
    doors that let a person change a chat ask it: the REST gate on
    ``POST /chats/{id}/messages`` and the socket's ``subscribed.can_write``.
    A ``reader`` or a ``commenter`` answers False; ``writer`` and above, True.
    """
    return DEFAULT_LADDER.allows(rung, FilesAction.WRITE)


class SharedRungCache:
    """One connection's memory of the rung each object's node gives it.

    The socket asks this question once per subscribe, and a person with a
    dozen chats open would otherwise pay an ACL read per channel per
    reconnect. What is cached is only what the client is TOLD
    (``subscribed.can_write``) — never what DECIDES a write: the registry
    re-reads the node inside the transaction that applies the operation, so a
    grant this cache has not caught up with can delay good news but can never
    admit a write the ACL refuses.

    It is dropped wholesale on any ``file_node.changed`` the connection sees,
    because that announcement carries ids only and the node it names is not
    the chat's object id. Over-forgetting costs one indexed read; remembering
    too long would leave a fresh editor staring at a closed composer.
    """

    __slots__ = ("_rungs",)

    def __init__(self) -> None:
        self._rungs: dict[UUID, str | None] = {}

    async def rung(
        self,
        db: AsyncSession,
        *,
        org_id: UUID,
        object_id: UUID,
        user_id: UUID | None,
        team_ids: AbstractSet[UUID],
    ) -> str | None:
        if object_id in self._rungs:
            return self._rungs[object_id]
        rung = await shared_object_role(
            db, org_id=org_id, object_id=object_id, user_id=user_id, team_ids=team_ids
        )
        self._rungs[object_id] = rung
        return rung

    def invalidate(self) -> None:
        """Forget every rung; the next ask reads the ACL again."""
        self._rungs.clear()


__all__ = [
    "SharedRungCache",
    "rung_writes",
    "rungs_for",
    "shared_object_role",
    "shared_object_roles",
]
