"""Who may reach a node: read the grants, add one, take one away.

Three rules shape this module.

**A share never confirms an outsider.** The grant route takes principal ids,
and a principal that is not in the caller's org answers the same opaque 404 a
principal id that was never issued answers — the library's in-org check raises
:class:`NotFound` before anything about the id is echoed, so a caller cannot
use the sharing route to enumerate the platform's users.

**A revoke is honest about inheritance.** Files has no deny entries, so a grant
a node merely inherits cannot be shadowed here; the refusal is a ``409
files.inherited_grant`` that names the ancestor the caller must revoke it at.
The ancestor id rides the body only when the caller may read that ancestor,
which is decided by running the ordinary read authorization on it rather than
by assuming a manager can see everything above them.

**Nothing branches before the policy.** Every handler resolves the node through
:func:`~alkera_core.files.authz.authorize.authorize` first, so a nonexistent
node, a node in another org and a node the caller may not read all leave this
module through the identical statement.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from alkera_core.files import acl
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.authorize import Authorized, Denied, authorize
from alkera_core.files.authz.decider import CHAT_SUBTYPE
from alkera_core.files.authz.grants import Principal, principal_kinds
from alkera_core.files.authz.ladder import (
    DEFAULT_LADDER,
    OFFERED_ROLES,
    ROLE_OWNER,
    role_label,
    shown_role,
)
from alkera_core.files.errors import Conflict, InvalidRequest, NotFound, PreconditionFailed
from alkera_core.files.ids import NodeId
from alkera_core.files.objects_bridge import (
    WORKSPACE_CHATS_FOLDER,
    WORKSPACE_TYPE,
    folder_object_kind,
)
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.tree import FileNode
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    as_platform,
    files_enforcer,
    idempotent_route,
    platform_wrap,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.services import workspaces
from backend.services.files.context import FilesContext
from backend.services.files.directory import share_candidates
from backend.services.files.items import principal_names

router = APIRouter(tags=["files"])


class PrincipalRef(BaseModel):
    """Who a grant is for: a ``user``, a ``team`` or the ``org`` itself.

    The kind is an open registry, so a kind the server has not registered is a
    422 rather than a row nothing can resolve. An ``org`` grant names the
    caller's own org; any other id is a 422 too. A ``user`` or ``team`` id that
    is not a principal of the caller's org (another org's, a stranger, an id
    never issued) is a 404, the same answer for every one of them, so the route
    never confirms who exists outside the org."""

    model_config = ConfigDict(populate_by_name=True)

    kind: str
    id: uuid.UUID


class FilesGrantRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    principal: PrincipalRef
    role: str
    expires_at: datetime | None = Field(default=None, alias="expiresAt")


class GrantEntry(BaseModel):
    """One line of the permissions answer.

    ``principal_name`` is the one field here that is not an id or an enum, and
    it is filled only on the listing: a share dialog that showed uuids would be
    unusable, and the listing is the one place the name discloses nothing —
    every row on it is a grant that already exists on a node the caller may
    read. It is ``None`` whenever the principal cannot be resolved inside the
    caller's org (a deleted account, a disbanded team, a share link), so the
    absence of a name never becomes an answer about who exists.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID | None = None
    principal: PrincipalRef
    principal_name: str | None = Field(default=None, serialization_alias="principalName")
    role: str
    origin: str
    granting_node_id: uuid.UUID | None = Field(default=None, serialization_alias="grantingNodeId")
    expires_at: datetime | None = Field(default=None, serialization_alias="expiresAt")
    #: The offered rung this grant reads and is selected as: its own, unless
    #: the ladder withdrew it from sharing. Filled on the listing.
    shown_role: str | None = Field(default=None, serialization_alias="shownRole")
    #: What the rung is called on screen. Filled on the listing.
    role_label: str | None = Field(default=None, serialization_alias="roleLabel")
    #: Whether this principal is the node's owner: the person a share has
    #: nothing to give, shown as the owner rather than as a grant.
    is_owner: bool = Field(default=False, serialization_alias="isOwner")
    #: Whether the caller may move this grant to another rung, or withdraw it,
    #: here. Decided by the policy that enforces the write, with the rung the
    #: row holds as the ceiling, so a dialog renders them and decides nothing.
    can_change: bool = Field(default=False, serialization_alias="canChange")
    can_remove: bool = Field(default=False, serialization_alias="canRemove")


class RoleOption(BaseModel):
    """A rung the caller may hand out here, with what it is called."""

    role: str
    label: str


class GrantList(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    value: list[GrantEntry]
    #: The rungs the caller may grant on this node, weakest first: what a new
    #: share and a role change may pick from. Empty for a caller who may not
    #: share.
    assignable_roles: list[RoleOption] = Field(
        default_factory=list, serialization_alias="assignableRoles"
    )


class ShareCandidate(BaseModel):
    """Someone the caller may name on a share of this node."""

    model_config = ConfigDict(populate_by_name=True)

    principal: PrincipalRef
    name: str
    email: str | None = None
    is_org: bool = Field(default=False, serialization_alias="isOrg")


class ShareCandidateList(BaseModel):
    value: list[ShareCandidate]


async def authorize_node(
    request: Request,
    fctx: FilesContext,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    wanted: FilesAction,
    *,
    grant_role: str | None = None,
    share_rungs: Sequence[str] = (),
) -> Authorized[Any]:
    """Resolve and authorize one node, with no branch the policy does not make.

    The drive in the path is checked *after* the policy has spoken, so naming a
    real node under the wrong drive costs the same work — and returns the same
    answer — as naming a node that does not exist.

    ``grant_role`` is the rung a grant or a revoke is about, held by the policy
    to the caller's own; the roster read and every other verb pass none.
    ``share_rungs`` asks which rungs a share here could grant or withdraw.
    """
    db = fctx.repo.session
    async with fctx.repo.transaction():
        # Read inside the transaction, as the platform: on a mutating route the
        # whole body already runs inside the idempotency claim's transaction,
        # under the Files role that cannot read the membership tables.
        async with as_platform(db):
            facts = await facts_for(request, db, fctx.ctx, fctx.drive)
        authorized = await authorize(
            fctx.ctx,
            fctx.repo,
            NodeId(node_id),
            wanted,
            facts=facts,
            enforce=files_enforcer(request, db, wrap=platform_wrap(db)),
            grant_role=grant_role,
            share_rungs=share_rungs,
        )
    if authorized.node.drive_id != drive_id:
        raise NotFound()
    return authorized


def _rung_check(request: Request, fctx: FilesContext, node_id: uuid.UUID) -> acl.RungCheck:
    """The sharing policy's ceiling on one more rung, for ``acl`` to apply
    under the node lock to the rung a change would take away. Decided and
    recorded like the first check, inside the transaction already open, so
    the rung judged is the one the row holds when the write lands."""

    async def check(role: str) -> None:
        db = fctx.repo.session
        async with as_platform(db):
            facts = await facts_for(request, db, fctx.ctx, fctx.drive)
        await authorize(
            fctx.ctx,
            fctx.repo,
            NodeId(node_id),
            FilesAction.SHARE,
            facts=facts,
            enforce=files_enforcer(request, db, wrap=platform_wrap(db)),
            grant_role=role,
        )

    return check


def _entry(share: FileShare, *, node_id: uuid.UUID) -> GrantEntry:
    return GrantEntry(
        id=share.id,
        principal=PrincipalRef(kind=share.principal_kind, id=share.principal_id),
        role=share.role,
        origin="direct" if share.node_id == node_id else "inherited",
        granting_node_id=None if share.node_id == node_id else share.node_id,
        expires_at=share.expires_at,
    )


def _live(shares: Sequence[FileShare]) -> list[FileShare]:
    return [row for row in shares if row.revoked_at is None]


@router.get(
    "/drives/{drive_id}/items/{item_id}/permissions",
    response_model=GrantList,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("permissions"))],
)
async def list_permissions(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    fctx: FilesCtx,
    effective: bool = False,
) -> GrantList:
    """The direct grants on this node, or the effective set with its origins.

    Each row says what the caller may do to it and the list says which rungs
    the caller may hand out, so a share dialog renders the server's answer
    rather than re-deciding the ladder.
    """
    authorized = await authorize_node(
        request, fctx, drive_id, item_id, FilesAction.READ, share_rungs=DEFAULT_LADDER.roles
    )
    node = authorized.node
    async with fctx.repo.transaction():
        if not effective:
            rows = _live(await fctx.repo.shares_of(NodeId(node.id)))
            entries = [_entry(row, node_id=node.id) for row in rows]
        else:
            entries = _effective_entries(
                await acl.effective_body(fctx.repo, node, authorized.chain)
            )
    return _verdicts(await _named(fctx, entries), node, authorized.shareable)


def _effective_entries(body: acl.AclBody) -> list[GrantEntry]:
    entries: list[GrantEntry] = []
    for ace in body.aces:
        ancestor = ace.get("origin_ancestor_id")
        entries.append(
            GrantEntry(
                principal=PrincipalRef(
                    kind=str(ace["principal_kind"]), id=uuid.UUID(str(ace["principal_id"]))
                ),
                role=str(ace["role"]),
                origin=str(ace["origin"]),
                granting_node_id=uuid.UUID(str(ancestor)) if ancestor else None,
            )
        )
    for grant_ in body.expiring:
        entries.append(
            GrantEntry(
                principal=PrincipalRef(kind=grant_.principal.kind, id=grant_.principal.id),
                role=grant_.role,
                origin=grant_.origin.kind,
                granting_node_id=grant_.origin.ancestor_id,
                expires_at=grant_.expires_at,
            )
        )
    return entries


def _verdicts(
    entries: Sequence[GrantEntry], node: FileNode, shareable: frozenset[str]
) -> GrantList:
    """The rows with what the caller may do to each, and the rungs on offer.

    A row is changed or withdrawn only where it was made (a direct share, with
    the id a revoke is addressed by), never on the owner, and only when the
    policy allows a share at the rung the row holds: a manager may not move or
    take away an owner's grant, which the write would refuse anyway.
    """
    offered = [
        RoleOption(role=one, label=role_label(one)) for one in OFFERED_ROLES if one in shareable
    ]
    owner = str(node.created_by) if node.created_by is not None else None
    rows: list[GrantEntry] = []
    for row in entries:
        is_owner = row.principal.kind == "user" and str(row.principal.id) == owner
        movable = (
            not is_owner and row.origin == "direct" and row.id is not None and row.role in shareable
        )
        rows.append(
            row.model_copy(
                update={
                    "shown_role": shown_role(row.role),
                    # The owner reads as the owner whatever rung a row of theirs
                    # holds: a share has nothing to give them.
                    "role_label": role_label(ROLE_OWNER if is_owner else shown_role(row.role)),
                    "is_owner": is_owner,
                    "can_remove": movable,
                    "can_change": movable and bool(offered),
                }
            )
        )
    return GrantList(value=rows, assignable_roles=offered)


async def _named(fctx: FilesContext, entries: Sequence[GrantEntry]) -> list[GrantEntry]:
    """The same rows, each carrying its grantee's name — two statements for all.

    It runs once the Files transaction has closed and steps out to the platform
    role for exactly its own lookups, the way every other name on a Files
    payload is resolved: inside the transaction the session is the Files app
    role, which has no read on ``users`` or ``teams``, and a person's name is
    not a Files fact.
    """
    if not entries:
        return list(entries)
    session = fctx.repo.session
    async with as_platform(session):
        resolved = await principal_names(
            session,
            [(row.principal.kind, row.principal.id) for row in entries],
            org_team_id=fctx.repo.scope.org_team_id,
        )
    return [
        row.model_copy(
            update={"principal_name": resolved.get((row.principal.kind, row.principal.id))}
        )
        for row in entries
    ]


@router.get(
    "/drives/{drive_id}/items/{item_id}/share-candidates",
    response_model=ShareCandidateList,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("permissions"))],
)
async def list_share_candidates(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    fctx: FilesCtx,
    q: str = "",
) -> ShareCandidateList:
    """The org's people and teams matching ``q``, for a caller who may share this node.

    Decided as a share of the node, because that is the one thing the answer is
    for: whoever may add a grant here may name any principal the grant route
    admits, and that set is exactly what this returns. A caller who may only
    read the node gets the visible refusal the grant route would give them, and
    a caller who cannot read it the opaque not-found, so the read is no wider a
    window onto the directory than the share button already is. ``q`` is
    unbounded on the wire and cut in the service, so a stranger's drive id is
    refused by the policy before anything about the query is looked at. The
    node's owner is left out.
    """
    authorized = await authorize_node(request, fctx, drive_id, item_id, FilesAction.SHARE)
    owner = authorized.node.created_by
    session = fctx.repo.session
    async with as_platform(session):
        found = await share_candidates(session, org_team_id=fctx.repo.scope.org_team_id, query=q)
    # The owner is never a candidate: a share has nothing to give them, and the
    # grant route refuses one.
    return ShareCandidateList(
        value=[
            ShareCandidate(
                principal=PrincipalRef(kind=one.kind, id=one.id),
                name=one.name,
                email=one.email,
                is_org=one.is_org,
            )
            for one in found
            if not (one.kind == "user" and owner is not None and str(one.id) == str(owner))
        ]
    )


@router.post(
    "/drives/{drive_id}/items/{item_id}/permissions",
    response_model=GrantEntry,
    response_model_by_alias=True,
    status_code=201,
    dependencies=[Depends(ratelimited("permissions"))],
)
@idempotent_route("files.sharing.create_permission", status=201)
async def create_permission(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: FilesGrantRequest,
    fctx: FilesCtx,
    idempotency: Idempotency,
    if_match: IfMatch,
) -> GrantEntry:
    """Grant a role to a principal of this org."""
    if payload.principal.kind not in principal_kinds():
        raise InvalidRequest("files.unknown_principal_kind", "That principal kind does not exist")
    if payload.expires_at is not None and _expired(payload.expires_at):
        raise InvalidRequest("files.expiry_in_past", "The expiry is already past")
    authorized = await authorize_node(
        request, fctx, drive_id, item_id, FilesAction.SHARE, grant_role=payload.role
    )
    node = authorized.node
    if if_match is not None and int(node.etag) != if_match:
        raise PreconditionFailed()
    async with fctx.repo.transaction():
        await _refuse_owner(fctx, node, authorized.chain, payload.principal)
        await _refuse_a_chat_in_a_shared_workspace(fctx, node, authorized.chain)
        share = await acl.grant(
            fctx.repo,
            fctx.ctx,
            node,
            Principal(kind=payload.principal.kind, id=payload.principal.id),
            payload.role,
            expires_at=payload.expires_at,
            if_match=if_match,
            replacing=_rung_check(request, fctx, node.id),
        )
    return _entry(share, node_id=node.id)


SHARE_THE_WORKSPACE = "Share the workspace instead."


async def _refuse_a_chat_in_a_shared_workspace(
    fctx: FilesContext, node: FileNode, chain: Sequence[FileNode]
) -> None:
    """Refuse a share that would reach one chat of a workspace that holds
    several, rather than the workspace: its agent works in the workspace's
    whole tree, which a rung on the chat alone does not reach.

    Decided on the node's whole path, not the node alone: a share on the
    workspace's ``.chats`` records folder, or on anything inside a chat's
    folder, reaches the chats below it through the drive's inheritance just as
    a share on the chat would. A chat that is a workspace of one is shared as
    it always was, and so is anything in the workspace's shared tree.
    """
    path = _path_of(node, chain)
    if any(_is_workspace_records(path[i - 1], path[i]) for i in range(1, len(path))):
        raise Conflict("files.share_the_workspace", SHARE_THE_WORKSPACE)
    chat_ids = [
        target
        for one in path
        if one.subtype == CHAT_SUBTYPE and (target := one.target_object_id) is not None
    ]
    if not chat_ids:
        return
    async with as_platform(fctx.repo.session):
        for chat_id in chat_ids:
            if await workspaces.refuses_chat_share(fctx.repo.session, chat_id):
                raise Conflict("files.share_the_workspace", SHARE_THE_WORKSPACE)


def _path_of(node: FileNode, chain: Sequence[FileNode]) -> list[FileNode]:
    """``node``'s ancestors and itself, root first, whether or not the chain
    the authorization resolved already ends at the node."""
    path = list(chain)
    if not path or path[-1].id != node.id:
        path.append(node)
    return path


def _is_workspace_records(parent: FileNode, folder: FileNode) -> bool:
    """``folder`` is the ``.chats`` records folder of a native workspace's own
    folder ``parent``: where that workspace's chats keep their folders."""
    return (
        folder.kind == "folder"
        and bytes(folder.name) == WORKSPACE_CHATS_FOLDER
        and parent.subtype == WORKSPACE_TYPE
        and folder_object_kind(parent) is not None
    )


def _expired(expires_at: datetime) -> bool:
    """Whether a requested expiry is already over.

    A grant that ends before it is written would be filed, listed and audited
    as access nobody ever had. A timestamp without an offset is read as UTC,
    which is how the column stores it.
    """
    moment = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return moment <= datetime.now(UTC)


ALREADY_OWNER_SELF = "You already own this."
ALREADY_OWNER_OTHER = "They already own this."


async def _refuse_owner(
    fctx: FilesContext, node: FileNode, chain: Sequence[FileNode], principal: PrincipalRef
) -> None:
    """A share to the item's owner is refused rather than filed.

    Owning is above every rung a share hands out, so the row would change
    nothing the owner can do — and the dialog would list them twice, once as
    Owner and once at the rung just given. A share to anyone else who already
    holds a grant here moves their grant (``acl.grant`` upserts), so only the
    owner needs this.
    """
    body = await acl.effective_body(fctx.repo, node, chain)
    owns = any(
        str(ace["principal_kind"]) == principal.kind
        and str(ace["principal_id"]) == str(principal.id)
        and str(ace["role"]) == ROLE_OWNER
        for ace in body.aces
    )
    if not owns:
        return
    person = fctx.ctx.delegating_user or fctx.ctx.acting_principal
    raise Conflict(
        "files.already_owner",
        ALREADY_OWNER_SELF if person.id == str(principal.id) else ALREADY_OWNER_OTHER,
    )


@router.delete(
    "/drives/{drive_id}/items/{item_id}/permissions/{share_id}",
    status_code=204,
    dependencies=[Depends(ratelimited("permissions"))],
)
@idempotent_route("files.sharing.delete_permission", status=204)
async def delete_permission(
    request: Request,
    drive_id: uuid.UUID,
    item_id: uuid.UUID,
    share_id: uuid.UUID,
    fctx: FilesCtx,
    idempotency: Idempotency,
    if_match: IfMatch,
) -> Response:
    """Withdraw a direct grant. An inherited one is refused, with the ancestor."""
    # The rung being withdrawn is read first so the decision can hold it to the
    # caller's own: a manager may not take an owner's grant away. A share that
    # is not there has no rung, and the revoke below answers for it exactly as
    # it did before, so the read reveals nothing the response does not.
    async with fctx.repo.transaction():
        held = (
            await fctx.repo.execute_scoped(
                fctx.repo.select_shares().where(FileShare.id == share_id)
            )
        ).scalar_one_or_none()
    authorized = await authorize_node(
        request,
        fctx,
        drive_id,
        item_id,
        FilesAction.SHARE,
        grant_role=None if held is None else held.role,
    )
    node = authorized.node
    if if_match is not None and int(node.etag) != if_match:
        raise PreconditionFailed()
    try:
        async with fctx.repo.transaction():
            await acl.revoke(
                fctx.repo,
                fctx.ctx,
                node,
                share_id,
                if_match=if_match,
                withdrawing=_rung_check(request, fctx, node.id),
            )
    except acl.InheritedGrant as inherited:
        raise await _inherited_conflict(request, fctx, drive_id, inherited) from None
    return Response(status_code=204)


async def _inherited_conflict(
    request: Request,
    fctx: FilesContext,
    drive_id: uuid.UUID,
    inherited: acl.InheritedGrant,
) -> Conflict:
    """The 409, carrying the ancestor only when the caller may read it.

    A manager here is usually a reader there, but "usually" is not a security
    property: the ancestor is authorized like any other node, and a caller who
    cannot read it is told only that the grant is inherited.
    """
    detail = inherited.detail or {}
    ancestor_id = uuid.UUID(str(detail["granting_node_id"]))
    refusal = Conflict(inherited.code, str(inherited))
    try:
        await authorize_node(request, fctx, drive_id, ancestor_id, FilesAction.READ)
    except (NotFound, Denied):
        return refusal
    refusal.detail = {"ancestor_id": str(ancestor_id), "node_id": str(ancestor_id)}
    refusal.may_read_named_node = True  # type: ignore[attr-defined]
    return refusal


__all__ = [
    "FilesGrantRequest",
    "GrantEntry",
    "GrantList",
    "PrincipalRef",
    "RoleOption",
    "ShareCandidate",
    "ShareCandidateList",
    "authorize_node",
    "facts_for",
    "router",
]
