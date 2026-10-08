"""The divergences a sync could not merge, and the three ways out of one.

A conflict is never resolved by throwing a side away silently: ``mine`` and
``theirs`` each promote one existing version to the head, and ``both`` promotes
theirs while the caller's side is kept as a real sibling file under the
conflicted-copy name every façade already agrees on. A conflict the drive
settled itself (``auto``) already has its copy and needs nothing from anyone:
both files are kept and the portal offers no choice. Resolving one through the
API still reads as keep this (trash the copy), keep the other (its bytes
become the head again, the copy is trashed), keep both (close the row). All
three leave the conflict row closed and the losing bytes still addressable
through the version history, so "resolve" never means "lose".

Listing is filtered per item before the page is cut: a conflict on a node the
caller cannot read is simply not in the list — its absence is the same as the
absence of a conflict, which is what keeps the list from becoming a directory
of nodes the caller may not see.
"""

from __future__ import annotations

import uuid
from typing import Literal, cast

from alkera_core.files import conflict_resolution
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.authz.readable import readable_ids
from alkera_core.files.conflict_resolution import CONFLICT_PAGE, ConflictRecord
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import DriveId, NodeId
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps.files import (
    Idempotency,
    IfMatch,
    Lease,
    as_platform,
    idempotent_route,
    ratelimited,
)
from backend.api.deps.files_context import FilesCtx
from backend.api.deps.files_facts import facts_for
from backend.api.routes.files.content import fence
from backend.api.routes.files.sharing import authorize_node
from backend.services.files.context import FilesContext

router = APIRouter(tags=["files"])

__all__ = [
    "CONFLICT_PAGE",
    "ConflictEntry",
    "ConflictList",
    "ResolveRequest",
    "ResolveResult",
    "router",
]


class ConflictEntry(BaseModel):
    """One divergence, by id. File names never appear here: errors and
    listings carry ids, never names.

    ``state`` is ``open`` for a divergence a sync recorded and ``auto`` for one
    the drive settled itself: the last write to arrive kept the name (``who``
    wrote it, from the side ``arrivedFrom`` names) and the displaced bytes went
    to ``copyNodeId`` — or, when that is null, stayed a version of the node.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    node_id: uuid.UUID = Field(serialization_alias="nodeId")
    base_version_id: uuid.UUID | None = Field(serialization_alias="baseVersionId")
    theirs_version_id: uuid.UUID = Field(serialization_alias="theirsVersionId")
    mine_version_id: uuid.UUID = Field(serialization_alias="mineVersionId")
    state: str
    copy_node_id: uuid.UUID | None = Field(default=None, serialization_alias="copyNodeId")
    arrived_from: Literal["web", "holder"] | None = Field(
        default=None, serialization_alias="arrivedFrom"
    )
    who: str | None = None
    displaced_by: str | None = Field(default=None, serialization_alias="displacedBy")
    #: Who closed it; null while it is still open or awaiting confirmation.
    resolved_by: uuid.UUID | None = Field(default=None, serialization_alias="resolvedBy")


class ConflictList(BaseModel):
    value: list[ConflictEntry]


class ResolveRequest(BaseModel):
    keep: Literal["mine", "theirs", "both"]


class ResolveResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    state: str
    node_id: uuid.UUID = Field(serialization_alias="nodeId")
    head_version_id: uuid.UUID = Field(serialization_alias="headVersionId")
    #: The sibling ``both`` created for the caller's side, when it created one.
    copy_node_id: uuid.UUID | None = Field(default=None, serialization_alias="copyNodeId")


def _drive(fctx: FilesContext, drive_id: uuid.UUID) -> DriveId:
    """The drive the caller named, or the opaque 404.

    A drive id that is not this org's own is indistinguishable, from outside,
    from one that does not exist — so the listing resolves it before it reads a
    single conflict row, and an empty page can only ever mean "no conflicts".
    """
    if fctx.drive.id != drive_id:
        raise NotFound()
    return DriveId(fctx.drive.id)


def _entry(row: ConflictRecord) -> ConflictEntry:
    return ConflictEntry(
        id=row.id,
        node_id=row.node_id,
        base_version_id=row.base_version_id,
        theirs_version_id=row.theirs_version_id,
        mine_version_id=row.mine_version_id,
        state=row.state,
        copy_node_id=row.copy_node_id,
        arrived_from=cast(Literal["web", "holder"] | None, row.arrived_from),
        who=row.who,
        displaced_by=row.displaced_by,
        resolved_by=row.resolved_by,
    )


@router.get(
    "/drives/{drive_id}/conflicts",
    response_model=ConflictList,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("conflicts"))],
)
async def list_conflicts(
    request: Request,
    drive_id: uuid.UUID,
    fctx: FilesCtx,
) -> ConflictList:
    """This drive's open conflicts, filtered to the nodes the caller can read."""
    # Decided in one batch and filtered, the way every listing is: a listing is
    # a rendering pass, not an access attempt on every row it looks past, so it
    # neither records a decision per row nor stops at a refusal.
    drive = _drive(fctx, drive_id)
    db = fctx.repo.session
    async with fctx.repo.transaction():
        rows = await conflict_resolution.list_open_conflicts(fctx.repo, drive, limit=CONFLICT_PAGE)
        async with as_platform(db):
            facts = await facts_for(request, db, fctx.ctx, fctx.drive)
        found = [_entry(row) for row in rows]
        readable = await readable_ids(
            fctx.repo, fctx.ctx, [NodeId(entry.node_id) for entry in found], facts=facts
        )
    return ConflictList(value=[entry for entry in found if entry.node_id in readable])


@router.post(
    "/drives/{drive_id}/conflicts/{conflict_id}/resolve",
    response_model=ResolveResult,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("conflicts"))],
)
@idempotent_route("files.conflicts.resolve_conflict")
async def resolve_conflict(
    request: Request,
    drive_id: uuid.UUID,
    conflict_id: uuid.UUID,
    payload: ResolveRequest,
    fctx: FilesCtx,
    idempotency: Idempotency,
    if_match: IfMatch,
    lease: Lease,
) -> ResolveResult:
    """Close one conflict by promoting a side, keeping the other where asked."""
    async with fctx.repo.transaction():
        found = await conflict_resolution.get_conflict(fctx.repo, conflict_id)
    # The node is authorized whether or not the conflict row was found, so a
    # conflict id from another drive costs the same work as a real one and
    # cannot be told apart by timing or by the number of queries.
    node_id = found.node_id if found is not None else uuid.uuid4()
    await authorize_node(request, fctx, drive_id, node_id, FilesAction.WRITE)
    if found is None:
        raise NotFound()

    async with fctx.repo.transaction():
        done = await conflict_resolution.resolve_conflict(
            fctx.repo,
            fctx.ctx,
            conflict_id,
            choice=payload.keep,
            if_match=if_match,
            clock=fctx.clock,
            lease=fence(lease, fctx),
        )
    return ResolveResult(
        id=done.id,
        state=done.state,
        node_id=done.node_id,
        head_version_id=done.head_version_id,
        copy_node_id=done.copy_node_id,
    )
