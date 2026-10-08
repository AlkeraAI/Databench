"""Admitting a notebook route's caller: the Files policy, then the fence.

A dependency module rather than a route module, so the notebook routes reach
the Files route helpers they share (deciding a node, its fresh bytes, a
download URL) without one route module importing another."""

from __future__ import annotations

import uuid

from alkera_core.files.authz.actions import FilesAction
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import LeaseConflict
from alkera_core.notebooks import edits as edit_log
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.files import LeaseContext
from backend.api.routes.files.content import authorized, fence, fresh_bytes, mint_download_url
from backend.services.files import FilesContext, NotEditableError, holder_peer
from backend.services.notebooks import (
    Caller,
    Target,
    is_notebook,
    lease_admits_writes,
    runner_of,
    system_name,
)


def _node_id(raw: str) -> NodeId:
    try:
        return NodeId(uuid.UUID(raw))
    except ValueError:
        raise NotFound() from None


async def admit(
    request: Request, db: AsyncSession, files: FilesContext, item_id: str, action: FilesAction
) -> Target:
    """The notebook ``item_id``, decided for ``action`` by the Files policy;
    the opaque 404 for anything that is not a notebook the caller may reach."""
    allowed = await authorized(request, db, files, _node_id(item_id), action)
    if not is_notebook(allowed.node):
        raise NotFound()
    return Target(
        org_id=files.repo.scope.org_team_id,
        drive_id=uuid.UUID(str(allowed.node.drive_id)),
        item_id=uuid.UUID(str(allowed.node.id)),
        allowed=allowed,
    )


async def caller_of(
    db: AsyncSession, files: FilesContext, target: Target, lease: LeaseContext
) -> Caller:
    """Who writes, once the Files policy said WRITE: a box through the fence
    of the lease it names (raises ``LeaseConflict`` when it is not the
    holder), anyone else only while a write without a fence lands on the
    notebook (``files.leased`` otherwise)."""
    ctx = files.ctx
    if lease.fenced:
        try:
            machine = await holder_peer(db, ctx, target.item_id, lease=fence(lease, files))
        except NotEditableError:
            raise NotFound() from None
        # The box on its own (a change to the file, not its agent's batch,
        # which names its chat): the platform, named as such.
        return Caller(
            ctx=ctx,
            kind="system",
            actor_key=edit_log.actor_key(machine_id=machine),
            display=system_name(),
            user=None,
            agent_id=machine,
            machine_id=machine,
        )
    if ctx.is_machine:
        raise LeaseConflict("files.lease_fenced", "a machine writes a notebook under its lease")
    if not await lease_admits_writes(files, target.node):
        raise LeaseConflict("files.leased", "the notebook's folder is held by another writer")
    return await runner_of(db, files)


__all__ = ["admit", "authorized", "caller_of", "fresh_bytes", "mint_download_url"]
