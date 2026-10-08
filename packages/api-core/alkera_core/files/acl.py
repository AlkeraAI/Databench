"""Interning an ACL body, granting, revoking, and the background rewrite.

Three facts hold this module together.

**The chain is the truth; ``acl_id`` is a cache.** A node's effective permission
set is the union of the direct ``file_shares`` rows on every node of its
ancestor chain plus the drive's defaults. :func:`effective_body` computes that
union and is the only definition of it. ``file_nodes.acl_id`` points at an
interned copy so a listing joins one row per *distinct* permission set rather
than walking the chain per row — and while a node is ``acl_rewriting`` that
copy is known stale, which is exactly when
:class:`~alkera_core.files.authz.grants.FilesGrantSource` ignores it and reads
the chain instead. Nothing here ever lets the cache outrank the chain.

**Interning is insert-or-read, never update.** ``file_acls`` is content
addressed by ``body_hash``: identical sets collapse onto one row that a million
nodes point at. Rewriting that row in place would silently change the meaning
of every one of them, so :func:`intern` is ``ON CONFLICT DO NOTHING`` then read
and the members explosion happens once, for the row that was actually created.
The hash folds in the org id, because the unique index on ``body_hash`` is
global while reads are RLS-scoped per tenant: without the fold, a second org
interning the same set would collide with a row it is not allowed to see.

**A grant is instant at its node and eventual below it.** The grant's own node
gets its new ``acl_id`` in the granting transaction; the subtree is marked
``acl_rewriting`` in one statement and repaired by :func:`rewrite` under a
``file_ops`` cursor. That is the only way a grant near a drive root stays a
request rather than a million-row write — and it is safe precisely because a
marked node reads through to the chain in the meantime.

A body carrying an expiry is deliberately **not** interned. The cache reader
parses ``expires_at`` only when it survives as a real ``datetime``, and JSONB
round-trips it to a string, so an interned time-boxed grant would read back as
permanent. Such a node keeps ``acl_id = NULL`` and answers from the chain,
which fails closed instead of widening access.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import CursorResult, case, select, text

from alkera_core.authz.principal import ActingContext
from alkera_core.db.tenant_session import stepped_out
from alkera_core.files.authz.grants import (
    ACL_REWRITING,
    ORIGIN_DIRECT,
    ORIGIN_DRIVE_DEFAULT,
    ORIGIN_INHERITED,
    Grant,
    GrantOrigin,
    Principal,
    default_grants_of_chain,
    principal_kinds,
)
from alkera_core.files.authz.ladder import DEFAULT_LADDER, RoleLadder
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.history import actor_ref, emit_node_changed, record
from alkera_core.files.ids import AclId, DriveId, NodeId, OperationId
from alkera_core.files.membership import active_member_of
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl, FileAclMember, FileShare
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from alkera_core.models.team import Team
from alkera_core.models.user import User

#: The checkpoints object production runs with: every point is free.
NO_CHECKPOINTS: Final[Checkpoints] = NoopCheckpoints()

#: The ``file_ops.kind`` the subtree repair runs under.
OP_ACL_REWRITE: Final = "acl_rewrite"

#: How many nodes one :func:`rewrite` call repairs before it returns. A batch
#: is one transaction, so the number bounds how much work a crash loses — never
#: how much work the rewrite can do, which is unbounded across calls.
ACL_REWRITE_BATCH: Final = 5000

#: How deep a team's parent chain is walked when checking that a grantee team
#: belongs to this org. Deeper than any real hierarchy; the bound exists so a
#: cycle introduced elsewhere cannot spin here.
MAX_TEAM_DEPTH: Final = 64


#: A check on one rung a share change would take away, made under the node
#: lock; it raises to refuse. The route builds it from the sharing policy.
RungCheck = Callable[[str], Awaitable[None]]


class InheritedGrant(Conflict):
    """A revoke was aimed at a grant this node inherited rather than owns.

    Carries the granting ancestor in ``detail`` because the caller's next move
    is to revoke it *there*, and it cannot find "there" from a bare 409.
    """

    def __init__(self, *, share_id: uuid.UUID, ancestor_id: uuid.UUID) -> None:
        super().__init__(
            "files.inherited_grant",
            f"share {share_id} is inherited from {ancestor_id}; revoke it there",
        )
        self.detail: dict[str, str] = {
            "share_id": str(share_id),
            "granting_node_id": str(ancestor_id),
        }


def _json_ace(grant_: Grant) -> dict[str, Any]:
    """One :class:`Grant` as the ACE shape the cache reader parses back."""
    ace: dict[str, Any] = {
        "principal_kind": grant_.principal.kind,
        "principal_id": str(grant_.principal.id),
        "role": grant_.role,
        "origin": grant_.origin.kind,
    }
    if grant_.origin.ancestor_id is not None:
        ace["origin_ancestor_id"] = str(grant_.origin.ancestor_id)
    if grant_.conditions is not None:
        ace["conditions"] = dict(grant_.conditions)
    return ace


@dataclass(frozen=True, slots=True)
class AclBody:
    """A canonical permission set: what gets hashed, stored and exploded.

    Canonical means *sorted and deduplicated*, which is what makes the hash a
    set identity rather than a record of the order the union happened to be
    built in. Two chains that grant the same thing intern to the same row.
    """

    aces: tuple[Mapping[str, Any], ...]
    #: Grants carrying an expiry, kept out of ``aces`` because JSONB cannot
    #: round-trip a ``datetime`` back through the cache reader. Their presence
    #: is what makes a body uninternable.
    expiring: tuple[Grant, ...] = ()

    @staticmethod
    def from_grants(grants: Iterable[Grant]) -> AclBody:
        seen: dict[str, dict[str, Any]] = {}
        expiring: list[Grant] = []
        for grant_ in grants:
            if grant_.expires_at is not None:
                expiring.append(grant_)
                continue
            ace = _json_ace(grant_)
            seen[json.dumps(ace, sort_keys=True)] = ace
        ordered = tuple(seen[key] for key in sorted(seen))
        return AclBody(aces=ordered, expiring=tuple(expiring))

    @property
    def internable(self) -> bool:
        """Whether this body can be cached at all (see the module docstring)."""
        return not self.expiring

    def as_json(self) -> list[dict[str, Any]]:
        return [dict(ace) for ace in self.aces]

    def hash_for(self, org_team_id: uuid.UUID) -> str:
        """The content address, folded with the tenant.

        The tenant is in the hash because ``uq_file_acls_body_hash`` is global
        while every read is RLS-scoped: two orgs sharing a hash would mean the
        second one's ``ON CONFLICT DO NOTHING`` collides with a row its own
        ``SELECT`` cannot see, and it would intern nothing at all.
        """
        payload = json.dumps(
            {"org": str(org_team_id), "aces": self.as_json()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def members(
        self, ladder: RoleLadder = DEFAULT_LADDER
    ) -> tuple[tuple[str, uuid.UUID, str], ...]:
        """``(principal_kind, principal_id, max_role)`` — the exploded rows.

        One row per principal carrying the *strongest* role the body gives it,
        so the listing filter is a single indexed join and never has to reduce
        several rungs per principal in Python.
        """
        best: dict[tuple[str, uuid.UUID], str] = {}
        for ace in self.aces:
            kind = str(ace["principal_kind"])
            principal_id = uuid.UUID(str(ace["principal_id"]))
            role = str(ace["role"])
            key = (kind, principal_id)
            current = best.get(key)
            if current is None or ladder.rank(role) > ladder.rank(current):
                best[key] = role
        return tuple((kind, pid, role) for (kind, pid), role in sorted(best.items()))


@asynccontextmanager
async def as_login_role(repo: FilesRepo) -> AsyncIterator[None]:
    """Step out of the Files role for a read outside the Files tables, then back.

    ``users`` and ``teams`` belong to another subsystem and the unprivileged
    ``alkera_files_app`` role deliberately holds no grant on them. The window
    goes to the session's outer role (the tenant role on a bound request, see
    :func:`alkera_core.db.tenant_session.stepped_out`) and is
    transaction-scoped, so every Files statement around it still runs under
    RLS.
    """
    session = repo.session
    async with stepped_out(session):
        yield


async def _assert_in_org(repo: FilesRepo, principal: Principal) -> None:
    """Refuse a grantee that is not this tenant's, before the trigger does.

    The database trigger is the backstop; this check is what turns "the write
    blew up" into a typed refusal that names nothing about the other tenant.
    ``NotFound`` rather than a forbidden: a caller who may not grant to a
    principal may not learn it exists either.

    The kind is checked against what the decider has actually been taught, not
    against a list spelled here: a grant naming a kind with no matcher is a row
    no reader can ever resolve, and the database trigger refuses it with a raw
    integrity error rather than a typed one. That refusal is an invalid request
    rather than a not-found, because the kind is the caller's own input and the
    answer says nothing about who was named — so it is the same answer for a
    member of this org, a stranger and an id that was never issued.
    """
    if principal.kind not in principal_kinds():
        raise InvalidRequest(
            "files.unknown_principal_kind", f"unknown principal kind {principal.kind!r}"
        )
    org_id = repo.scope.org_team_id
    if principal.kind == "org":
        # An org grant has one valid id, the caller's own org, which the caller
        # already knows, so naming any other is the caller's input error and is
        # answered the same way for every other id: it confirms nothing.
        if principal.id != org_id:
            raise InvalidRequest(
                "files.org_principal_not_own", "An org grant names your own organization"
            )
        return
    async with as_login_role(repo):
        if principal.kind == "user":
            found = (
                await repo.session.execute(
                    select(User.id).where(User.id == principal.id, active_member_of(org_id))
                )
            ).scalar_one_or_none()
            if found is None:
                raise NotFound(f"no principal user:{principal.id}")
            return
        team_id: uuid.UUID | None = principal.id
        for _ in range(MAX_TEAM_DEPTH):
            if team_id == org_id:
                return
            parent = (
                await repo.session.execute(select(Team.parent_team_id).where(Team.id == team_id))
            ).scalar_one_or_none()
            if parent is None:
                break
            team_id = parent
    raise NotFound(f"no principal team:{principal.id}")


async def effective_body(
    repo: FilesRepo,
    node: FileNode,
    chain: Sequence[FileNode],
    *,
    defaults: Sequence[Grant] = (),
) -> AclBody:
    """The union over the chain: the truth a cached ``acl_id`` only copies.

    Every ancestor's live ``file_shares`` rows contribute, tagged
    ``inherited(<that ancestor>)`` so a caller can see *where* a grant it cannot
    revoke here comes from; the node's own rows are ``direct``. ``defaults`` is
    the drive's default grants — supplied by the drive-provisioning caller
    rather than derived here, because which default table applies is a fact
    about the folder's role in the drive, not about the ACL algebra.

    A drive default that was minted higher up — the ``{user: owner}`` of a
    home, the ``{org: reader}`` of ``/Shared``, a team's grant on its folder —
    has no ``file_shares`` row anywhere: it lives only in the interned body of
    the folder it was minted on. It is read from there and carried down as
    ``inherited(<that folder>)``, exactly as the chain read does, because this
    body is what every cache below that folder is rebuilt from — a grant made
    on a folder in a home, and the rewrite that follows it, would otherwise
    write caches the home's owner is not in and lock them out of the folder
    they just shared. The node's own drive default keeps its ``drive_default``
    origin, so re-interning a home with no shares yields the body it had.
    """
    grants: list[Grant] = list(defaults)
    minted = await default_grants_of_chain(repo, chain)
    for ancestor in chain:
        origin = (
            GrantOrigin.direct() if ancestor.id == node.id else GrantOrigin.inherited(ancestor.id)
        )
        for share in await repo.shares_of(NodeId(ancestor.id)):
            grants.append(
                Grant(
                    principal=Principal(kind=share.principal_kind, id=share.principal_id),
                    role=share.role,
                    origin=origin,
                    expires_at=share.expires_at,
                    conditions=share.conditions,
                )
            )
        for default in minted.get(ancestor.id, ()):
            grants.append(
                Grant(
                    principal=default.principal,
                    role=default.role,
                    origin=GrantOrigin.drive_default() if ancestor.id == node.id else origin,
                    expires_at=default.expires_at,
                    conditions=default.conditions,
                )
            )
    return AclBody.from_grants(grants)


async def intern(repo: FilesRepo, body: AclBody) -> AclId | None:
    """The id of the row holding ``body``, creating it at most once.

    ``ON CONFLICT DO NOTHING`` then read, never ``DO UPDATE``: the row is shared
    by every node with this permission set, so updating it would rewrite their
    meaning too. The members explosion runs only on the insert that actually
    created the row — ``RETURNING`` is empty on the conflict path, which is how
    a concurrent second interner knows not to duplicate the rows.

    Returns ``None`` for a body that must not be cached (one with an expiry).
    """
    if not body.internable:
        return None
    org_id = repo.scope.org_team_id
    body_hash = body.hash_for(org_id)
    acl_id = AclId(uuid.uuid4())
    created = await repo.intern_acl_row(acl_id=acl_id, body=body.as_json(), body_hash=body_hash)
    if not created:
        existing = (
            await repo.execute_scoped(select(FileAcl.id).where(FileAcl.body_hash == body_hash))
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - only if another tenant owns the hash
            raise Conflict("files.held", "an ACL body hash is held by another tenant")
        return AclId(existing)
    for kind, principal_id, role in body.members():
        await repo.add(
            FileAclMember(
                acl_id=acl_id,
                principal_kind=kind,
                principal_id=principal_id,
                org_team_id=org_id,
                max_role=role,
            )
        )
    await repo.flush()
    return acl_id


async def _reintern_node(
    repo: FilesRepo,
    node: FileNode,
    *,
    defaults: Sequence[Grant] = (),
) -> tuple[AclId | None, int]:
    """Recompute one node's body from its chain and point it at the interned row.

    Returns the interned id and the etag the UPDATE actually wrote, taken from
    the statement's ``RETURNING`` rather than computed from the value the caller
    read: the announcement downstream has to carry the etag a later read of the
    node returns, and only the database knows it.
    """
    chain = await repo.chain(node)
    body = await effective_body(repo, node, chain, defaults=defaults)
    acl_id = await intern(repo, body)
    written = await repo.session.execute(
        repo.update_nodes()
        .where(FileNode.id == node.id)
        .values(
            acl_id=acl_id,
            etag=FileNode.etag + 1,
            # This node's cache is now correct, so its own ``acl_rewriting``
            # mark is lifted here — in the same statement, so there is no
            # instant where the body is fresh but the node still reads as
            # stale. It is also what makes a rewrite batch that computed its
            # body before this grant lose the compare-and-swap below instead
            # of overwriting the grant with a body that predates it. Any other
            # state (``moving``, ``locked``) belongs to another operation and
            # is left exactly as it was.
            state=case((FileNode.state == ACL_REWRITING, "live"), else_=FileNode.state),
        )
        .returning(FileNode.etag)
    )
    return acl_id, int(written.scalar_one())


async def _mark_subtree_rewriting(repo: FilesRepo, node: FileNode) -> int:
    """Flag every descendant's cache stale, in one statement.

    One statement rather than a walk because the mark must be atomic with
    respect to a reader: there is no instant at which some descendants trust a
    cache the grant has already invalidated. A node already in another state
    (``moving``, ``locked``) is left alone — its own operation owns it, and the
    rewrite picks it up on a later pass.
    """
    result = cast(
        "CursorResult[Any]",
        await repo.session.execute(
            repo.update_nodes()
            .where(
                # The shared predicate, so the expression this filters on
                # stays the one the subtree index is built on.
                repo.subtree_predicate(node),
                FileNode.id != node.id,
                FileNode.state == "live",
            )
            .values(state=ACL_REWRITING)
        ),
    )
    return int(result.rowcount or 0)


async def invalidate_subtree(repo: FilesRepo, ctx: ActingContext, node: FileNode) -> OperationId:
    """Mark this node and everything under it stale, and queue the repair.

    :func:`move_rederive` is the version for a move a request can afford: it
    recomputes the moved node's own body on the spot, so that one node is right
    before the answer is returned, and pays a history row and an outbox event
    for it. A batched move of twenty thousand nodes cannot: it deliberately
    emits ONE event for the root and none for the subtree, and its root is no
    more special than any other node in it.

    So this one only marks. That is enough, because a marked node's read falls
    through to the ancestor chain — the truth — until the queued rewrite lands
    (:class:`~alkera_core.files.authz.grants.FilesGrantSource`). The window is
    slower, never wrong. Without it the whole subtree keeps the interned bodies
    of the ancestors it has left, and a move out of a shared folder would not
    take the sharing away.
    """
    await repo.session.execute(
        repo.update_nodes()
        .where(repo.subtree_predicate(node), FileNode.state == "live")
        .values(state=ACL_REWRITING)
    )
    return await _enqueue_rewrite(repo, ctx, node)


async def _enqueue_rewrite(repo: FilesRepo, ctx: ActingContext, node: FileNode) -> OperationId:
    """The ``file_ops`` row the background rewrite advances under."""
    op_id = uuid.uuid4()
    await repo.add(
        FileOp(
            id=op_id,
            org_team_id=repo.scope.org_team_id,
            drive_id=node.drive_id,
            kind=OP_ACL_REWRITE,
            actor=actor_ref(ctx),
            state="queued",
            result_node_id=node.id,
            progress={"root_path_ids": node.path_ids, "cursor": None, "done": 0},
        )
    )
    await repo.flush()
    return OperationId(op_id)


async def _after_grant_change(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    *,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    checkpoints: Checkpoints,
    defaults: Sequence[Grant],
    withdrawn: Sequence[tuple[str, uuid.UUID]] = (),
) -> OperationId:
    """Re-point this node, invalidate the subtree, enqueue its repair, announce.

    The order matters and is the reason for the checkpoint: the node's own cache
    is correct before any descendant is marked, and the marks land before the
    job row exists — so a crash between them leaves descendants reading the
    chain (correct, slower) rather than trusting a cache nobody will repair.
    """
    acl_id, etag = await _reintern_node(repo, node, defaults=defaults)
    await _mark_subtree_rewriting(repo, node)
    await checkpoints.reach("acl.after_mark_rewriting")
    op_id = await _enqueue_rewrite(repo, ctx, node)
    await record(
        repo,
        ctx,
        node_id=NodeId(node.id),
        kind="acl",
        before=before,
        after={
            **(dict(after) if after is not None else {}),
            "acl_id": str(acl_id) if acl_id is not None else None,
        },
        op_id=op_id,
    )
    await emit_node_changed(
        repo,
        ctx,
        node_id=NodeId(node.id),
        drive_id=DriveId(node.drive_id),
        version=etag,
        withdrawn=withdrawn,
    )
    return op_id


async def grant(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    principal: Principal,
    role: str,
    *,
    expires_at: datetime | None = None,
    if_match: int | None = None,
    defaults: Sequence[Grant] = (),
    checkpoints: Checkpoints = NO_CHECKPOINTS,
    ladder: RoleLadder = DEFAULT_LADDER,
    replacing: RungCheck | None = None,
) -> FileShare:
    """Grant ``role`` to ``principal`` on ``node``, and schedule the descent.

    ``node`` is the node a route has already authorized the caller to share.
    The capability handle that will carry that proof (``Authorized[Share]``) is
    not built yet, so the row is taken directly and the authorization stays the
    caller's job until the handle lands.

    A principal already holding a live grant here keeps that row and moves to
    the new rung, because a person has one access to one node: a second row
    would say two things at once, and since a body takes the strongest of them
    the weaker one is invisible right up until somebody withdraws the other and
    the access stays. The ladder is not consulted — a share says what the
    sharer chose, up OR down, and the descent is scheduled either way.

    ``if_match`` is the etag the caller's precondition named. It is compared
    against the node read under its row lock, so two grants racing under the
    same etag cannot both pass: the first bumps the etag, and the second sees
    the new one and is refused with :class:`PreconditionFailed`.

    ``replacing`` holds the rung being replaced to the sharer's ceiling. A
    move down is also a withdrawal of the rung above it, so a sharer who may
    hand out "Can view" but not "Owner" must not turn an owner grant (an
    expiring one included) into a view grant. It is called under the node
    lock with the standing rung, and raises to refuse.
    """
    if ladder.rank(role) < 0:
        raise InvalidRequest(f"unknown role {role!r}")
    await _assert_in_org(repo, principal)
    # Drive first, then the node: a share is one more mutation on the node, so
    # it takes the same rows in the same fixed order every other Files mutation
    # uses. Locking the node alone deadlocks against a concurrent move or trash,
    # which reaches the node only after the drive.
    _, _, locked = await repo.lock_chain(DriveId(node.drive_id), None, NodeId(node.id))
    if locked is None:
        raise NotFound(f"no node {node.id}")
    if if_match is not None and int(locked.etag) != if_match:
        raise PreconditionFailed()
    # Read under the node lock, so two role changes in flight at once are
    # serialized rather than both inserting.
    standing = (
        (
            await repo.execute_scoped(
                repo.select_shares()
                .where(
                    FileShare.node_id == node.id,
                    FileShare.principal_kind == principal.kind,
                    FileShare.principal_id == principal.id,
                    FileShare.revoked_at.is_(None),
                )
                .order_by(FileShare.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if standing is not None and replacing is not None:
        await replacing(standing.role)
    # The ``files_share_principal_in_org`` trigger reads ``teams`` and
    # ``team_memberships`` and is not SECURITY DEFINER, so under
    # ``alkera_files_app`` — which holds no grant on either — it raises
    # "permission denied" instead of validating. The write therefore runs as
    # the login role, which is also what makes the trigger able to do its job;
    # the tenant is not taken on trust, because ``_assert_in_org`` above has
    # already refused a foreign principal and ``org_team_id`` comes from the
    # repo's scope rather than from the caller.
    if standing is not None:
        before: dict[str, Any] | None = {"granted": str(standing.id), "role": standing.role}
        async with as_login_role(repo):
            moved = (
                await repo.session.execute(
                    repo.update_shares()
                    .where(FileShare.id == standing.id, FileShare.revoked_at.is_(None))
                    .values(
                        role=role,
                        expires_at=expires_at,
                        # Whoever set the rung that is actually in force; the
                        # earlier grant stays in the history the change writes.
                        granted_by=actor_ref(ctx),
                    )
                    .returning(FileShare)
                )
            ).scalar_one()
        share = moved
    else:
        before = None
        share = FileShare(
            id=uuid.uuid4(),
            org_team_id=repo.scope.org_team_id,
            node_id=node.id,
            principal_kind=principal.kind,
            principal_id=principal.id,
            role=role,
            expires_at=expires_at,
            granted_by=actor_ref(ctx),
        )
        async with as_login_role(repo):
            await repo.add(share)
            await repo.flush()
    await _after_grant_change(
        repo,
        ctx,
        locked,
        before=before,
        after={"granted": str(share.id), "role": role},
        checkpoints=checkpoints,
        defaults=defaults,
    )
    return share


async def revoke(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    share_id: uuid.UUID,
    *,
    if_match: int | None = None,
    defaults: Sequence[Grant] = (),
    checkpoints: Checkpoints = NO_CHECKPOINTS,
    withdrawing: RungCheck | None = None,
) -> FileShare:
    """Withdraw a grant that lives on ``node``.

    ``if_match`` is checked under the node's row lock, as in :func:`grant`.

    ``withdrawing`` holds the rung being withdrawn to the caller's ceiling. It
    is called under the node lock with the rung the share holds at that
    moment, not the one read before the lock: a role change keeps the share's
    id, so a grant raised between a caller's check and the revoke is judged
    at the rung it now has. It raises to refuse.

    A grant the node merely *inherits* is refused rather than shadowed: Files
    has no deny entries, so the only honest way to remove an inherited grant is
    to revoke it where it was made, and the refusal names that ancestor.
    """
    row = (
        await repo.execute_scoped(repo.select_shares().where(FileShare.id == share_id))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise NotFound(f"no share {share_id}")
    if row.node_id != node.id:
        chain_ids = {ancestor.id for ancestor in await repo.chain(node)}
        if row.node_id in chain_ids:
            raise InheritedGrant(share_id=share_id, ancestor_id=row.node_id)
        raise NotFound(f"no share {share_id}")
    # Same fixed order as ``grant``: drive, then node.
    _, _, locked = await repo.lock_chain(DriveId(node.drive_id), None, NodeId(node.id))
    if locked is None:
        raise NotFound(f"no node {node.id}")
    if if_match is not None and int(locked.etag) != if_match:
        raise PreconditionFailed()
    if withdrawing is not None:
        held = (
            await repo.execute_scoped(
                repo.select_shares().where(FileShare.id == share_id, FileShare.revoked_at.is_(None))
            )
        ).scalar_one_or_none()
        if held is None:
            raise NotFound(f"no share {share_id}")
        await withdrawing(held.role)
    # Same reason as the insert in ``grant``: the principal-in-org trigger fires
    # on UPDATE too and cannot read ``team_memberships`` as ``alkera_files_app``.
    # The row is addressed by id and constrained to this scope's org, so the
    # widened role cannot reach another tenant's share.
    async with as_login_role(repo):
        revoked = (
            await repo.session.execute(
                repo.update_shares()
                .where(
                    FileShare.id == share_id,
                    FileShare.revoked_at.is_(None),
                )
                .values(revoked_at=text("now()"))
                .returning(FileShare)
            )
        ).scalar_one_or_none()
    if revoked is None:  # pragma: no cover - the row lock above makes this unreachable
        raise NotFound(f"no share {share_id}")
    await _after_grant_change(
        repo,
        ctx,
        locked,
        before={"granted": str(share_id), "role": row.role},
        after={"revoked": str(share_id)},
        checkpoints=checkpoints,
        defaults=defaults,
        withdrawn=((row.principal_kind, row.principal_id),),
    )
    return revoked


async def rewrite(
    repo: FilesRepo,
    op_id: OperationId,
    *,
    batch: int = ACL_REWRITE_BATCH,
    defaults: Sequence[Grant] = (),
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> int:
    """Repair one batch of a subtree's caches. Returns how many nodes it fixed.

    Resumable by construction: the cursor is ``(depth, id)`` of the last node
    considered, stored on the operation in the same transaction as the repairs
    it describes. A process killed mid-batch loses that batch and no more,
    because a node's ``state`` only leaves ``acl_rewriting`` in the very
    statement that also sets its new ``acl_id`` — so a node is never both
    marked clean and left stale.
    """
    op = (
        await repo.execute_scoped(repo.select_ops().where(FileOp.id == op_id))
    ).scalar_one_or_none()
    if op is None:
        raise NotFound(f"no operation {op_id}")
    if op.kind != OP_ACL_REWRITE:
        raise InvalidRequest(f"operation {op_id} is a {op.kind}, not an {OP_ACL_REWRITE}")
    progress = dict(op.progress or {})
    prefix = str(progress.get("root_path_ids") or "")
    if not prefix:
        raise InvalidRequest(f"operation {op_id} carries no subtree root")
    cursor = progress.get("cursor")
    stmt = (
        repo.select_nodes()
        .where(repo._subtree_predicate_for(prefix), FileNode.state == ACL_REWRITING)
        .order_by(FileNode.depth, FileNode.id)
        .limit(batch)
    )
    if cursor is not None:
        depth, last_id = int(cursor[0]), uuid.UUID(str(cursor[1]))
        stmt = stmt.where(
            (FileNode.depth > depth) | ((FileNode.depth == depth) & (FileNode.id > last_id))
        )
    pending = list((await repo.execute_scoped(stmt)).scalars().all())
    done = 0
    last: tuple[int, uuid.UUID] | None = None
    for candidate in pending:
        await checkpoints.reach("acl.rewrite_before_node")
        chain = await repo.chain(candidate)
        body = await effective_body(repo, candidate, chain, defaults=defaults)
        acl_id = await intern(repo, body)
        cleared = cast(
            "CursorResult[Any]",
            await repo.session.execute(
                repo.update_nodes()
                .where(
                    FileNode.id == candidate.id,
                    # The compare-and-swap: a node that left ``acl_rewriting``
                    # while this batch was computing belongs to a newer grant's
                    # rewrite, and writing this now-stale body over it would
                    # lose that grant. Losing the update is the correct
                    # outcome; the newer operation repairs the node itself.
                    FileNode.state == ACL_REWRITING,
                )
                .values(acl_id=acl_id, state="live", etag=FileNode.etag + 1)
            ),
        )
        if cleared.rowcount:
            done += 1
        last = (int(candidate.depth), candidate.id)
    if last is not None:
        progress["cursor"] = [last[0], str(last[1])]
    progress["done"] = int(progress.get("done") or 0) + done
    await repo.session.execute(
        repo.update_ops()
        .where(FileOp.id == op_id)
        .values(
            progress=progress,
            state="done" if len(pending) < batch else "running",
            # Postgres time, not the process's: a heartbeat the watchdog
            # compares against ``now()`` must be measured on the same clock.
            heartbeat_at=text("now()"),
        )
    )
    await checkpoints.reach("acl.after_rewrite_batch")
    return done


async def move_rederive(
    repo: FilesRepo,
    ctx: ActingContext,
    node: FileNode,
    *,
    defaults: Sequence[Grant] = (),
    checkpoints: Checkpoints = NO_CHECKPOINTS,
) -> OperationId:
    """Re-derive a moved node's inherited ACEs from its new parent.

    The node's *direct* ``file_shares`` rows are untouched (they moved with it,
    which is what a user means by "this stays shared with Ana"), while every
    inherited ACE is recomputed from wherever the node now hangs. The subtree
    below it is marked and repaired exactly as a grant's is.

    Exported for a namespace move to call; nothing inside this module calls it.
    """
    return await _after_grant_change(
        repo,
        ctx,
        node,
        before=None,
        after={"rederived_under": str(node.parent_id) if node.parent_id is not None else None},
        checkpoints=checkpoints,
        defaults=defaults,
    )


__all__ = [
    "ACL_REWRITE_BATCH",
    "MAX_TEAM_DEPTH",
    "NO_CHECKPOINTS",
    "OP_ACL_REWRITE",
    "ORIGIN_DIRECT",
    "ORIGIN_DRIVE_DEFAULT",
    "ORIGIN_INHERITED",
    "AclBody",
    "InheritedGrant",
    "RungCheck",
    "effective_body",
    "grant",
    "intern",
    "invalidate_subtree",
    "move_rederive",
    "revoke",
    "rewrite",
]
