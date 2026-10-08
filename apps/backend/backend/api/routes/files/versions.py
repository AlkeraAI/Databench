"""A file's version history: list it, fetch one version's bytes, put one back.

Restoring is a forward write, never a rewind: the old version's bytes are
already in the store and already referenced, so the restore appends a NEW head
version pointing at the same object rather than deleting the versions above it.
That is what makes it undoable — the version it replaced is still the row above
it in the same chain — and it is why the reachability sweep never sees the
object become unreferenced in between.

Bytes are served the way the head's are: a 302 to a short-lived signed URL on
the content domain, so a version download never crosses the API origin that
holds the session cookie.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from alkera_core.db.session import get_db
from alkera_core.files import lease_snapshots
from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.content import ContentService
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import NodeId, VersionId
from alkera_core.files.signed_urls import mint_content_url, minting_user
from alkera_core.models.files.versions import FileVersion
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

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
from backend.api.deps.files_lane import authorized
from backend.api.routes.files.content import fence, session_binding
from backend.services.files import member_names
from backend.services.files.context import FilesContext
from backend.services.files.items import to_item

router = APIRouter(tags=["files"])


def _drive_scoped(ctx: FilesContext, drive_id: UUID) -> None:
    if ctx.drive.id != drive_id:
        raise NotFound()


async def _version_of(ctx: FilesContext, node_id: NodeId, version_id: UUID) -> FileVersion:
    """The named version, but only if it belongs to the named node.

    A version id from another node — or another org — is a 404 for the same
    reason a foreign node is: the caller was not entitled to learn it exists.
    """
    version = await ctx.repo.version(VersionId(version_id))
    if version is None or version.node_id != node_id:
        raise NotFound()
    return version


class VersionWire(BaseModel):
    """One row of a file's history. `isHead` is the version the node points at."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    seq: int
    size: int
    content_hash: str | None = Field(default=None, serialization_alias="contentHash")
    mime_type: str | None = Field(default=None, serialization_alias="mimeType")
    scan_state: str | None = Field(default=None, serialization_alias="scanState")
    source: str | None = None
    created_at: str = Field(serialization_alias="createdAt")
    is_head: bool = Field(default=False, serialization_alias="isHead")
    #: Who wrote this version, by name, when they are a member of the drive's
    #: org with a name; for a co-edited file's write back, the person it was
    #: written as.
    author: str | None = None
    #: The machine whose agent wrote it, when the folder's lease holder sent it.
    machine: str | None = None


class VersionList(BaseModel):
    """A file's whole history, oldest first."""

    versions: list[VersionWire] = Field(default_factory=list)


def _version_body(
    version: FileVersion, *, is_head: bool, names: dict[UUID, str] | None = None
) -> VersionWire:
    machine = (version.version_metadata or {}).get("machine_id")
    return VersionWire(
        author=(names or {}).get(version.created_by) if version.created_by else None,
        machine=machine if isinstance(machine, str) and machine else None,
        id=str(version.id),
        seq=version.seq,
        size=version.size_bytes,
        content_hash=version.content_hash,
        mime_type=version.mime_sniffed,
        scan_state=version.scan_state,
        source=version.source,
        created_at=version.created_at.isoformat(),
        is_head=is_head,
    )


@router.get(
    "/drives/{drive_id}/items/{node_id}/versions",
    response_model=VersionList,
    response_model_by_alias=True,
    dependencies=[Depends(ratelimited("versions"))],
)
async def list_versions(
    request: Request,
    drive_id: UUID,
    node_id: UUID,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> VersionList:
    """Every version of this file, oldest first."""
    _drive_scoped(ctx, drive_id)
    facts = await facts_for(request, db, ctx.ctx, ctx.drive)
    async with ctx.repo.transaction():
        allowed = await authorized(
            request, db, ctx.repo, ctx.ctx, NodeId(node_id), FilesAction.READ, facts=facts
        )
        # The repo hands back newest-first; the wire order is the chain's own,
        # so a client renders history without re-sorting it.
        rows = sorted(await ctx.repo.versions_of(NodeId(node_id)), key=lambda row: row.seq)
        head = allowed.node.head_version_id
    # Outside the Files transaction: its role reads no ``users``.
    names = await member_names(
        db, ctx.drive.org_team_id, {row.created_by for row in rows if row.created_by}
    )
    return VersionList(
        versions=[_version_body(row, is_head=row.id == head, names=names) for row in rows]
    )


@router.get(
    "/drives/{drive_id}/items/{node_id}/versions/{version_id}/content",
    dependencies=[Depends(ratelimited("versions"))],
)
async def read_version_content(
    request: Request,
    drive_id: UUID,
    node_id: UUID,
    version_id: UUID,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> RedirectResponse:
    """A 302 to a short-lived signed URL for this version's bytes."""
    _drive_scoped(ctx, drive_id)
    facts = await facts_for(request, db, ctx.ctx, ctx.drive)
    async with ctx.repo.transaction():
        await authorized(
            request, db, ctx.repo, ctx.ctx, NodeId(node_id), FilesAction.READ, facts=facts
        )
        version = await _version_of(ctx, NodeId(node_id), version_id)
    url = await mint_content_url(
        ctx.repo,
        version_id=VersionId(version.id),
        # The signed URL is bound to the session that minted it, exactly as the
        # head-content route binds its own: a link handed to another session —
        # forwarded, pasted, or lifted from a log — is refused at redemption
        # rather than serving the bytes to whoever holds the string.
        session_id=session_binding(ctx.ctx),
        clock=ctx.clock,
        key=ctx.settings.effective_files_content_signing_key.encode(),
        user_id=minting_user(ctx.ctx),
    )
    return RedirectResponse(url, status_code=302)


@router.post(
    "/drives/{drive_id}/items/{node_id}/versions/{version_id}/restore",
    dependencies=[Depends(ratelimited("versions"))],
)
@idempotent_route("files.versions.restore_version")
async def restore_version(
    request: Request,
    drive_id: UUID,
    node_id: UUID,
    version_id: UUID,
    ctx: FilesCtx,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    idempotency_key: Idempotency,
    if_match: IfMatch,
    lease: Lease,
) -> Any:
    """Make an older version the head again, by appending it as a new one."""
    _drive_scoped(ctx, drive_id)
    service = ContentService(ctx.repo, ctx.ctx, ctx.clock, ctx.store)
    async with ctx.repo.transaction():
        async with as_platform(db):
            facts = await facts_for(request, db, ctx.ctx, ctx.drive)
        allowed = await authorized(
            request, db, ctx.repo, ctx.ctx, NodeId(node_id), FilesAction.WRITE, facts=facts
        )
        node, made = await service.restore_version(
            NodeId(node_id), VersionId(version_id), if_match=if_match, lease=fence(lease, ctx)
        )
        chain = await ctx.repo.chain(node)
        snapshot = await lease_snapshots.lease_facet(
            ctx.repo, node, chain, ctx=ctx.ctx, now=ctx.clock.now()
        )
        item = to_item(node, chain, allowed.access, snapshot, version=made)
    return item


__all__ = [
    "list_versions",
    "read_version_content",
    "restore_version",
    "router",
]
