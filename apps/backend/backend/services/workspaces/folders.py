"""Where a workspace lives in Files: its folder, its drive and its shared tree.

A native workspace's folder is its own node and its tree the ``files`` child; an
adopted workspace (a workspace of one) uses its chat's folder and the chat's
working directory. Read for a page of workspaces in one scoped transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.files.objects_bridge import (
    CHAT_TYPE,
    WORKSPACE_TYPE,
    working_folder_nodes,
)
from alkera_core.models import WorkspaceObject
from alkera_core.models.files.leases import FileLease
from alkera_core.models.files.tree import FileNode
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import files_transaction
from backend.services.workspaces.workspace_service import folder_object_id


@dataclass(frozen=True, slots=True)
class WorkspaceNodes:
    """A workspace's folder, the drive it is on, and its shared working tree."""

    folder: UUID | None
    drive: UUID | None
    working: UUID | None
    #: The machine holding a live lease on the folder, and when that lease
    #: last beat: what the box last proved about syncing it. ``None`` when
    #: no machine holds it.
    lease_machine_id: str | None = None
    lease_beat_at: datetime | None = None


async def nodes_for(
    db: AsyncSession, ctx: ActingContext, rows: Sequence[WorkspaceObject]
) -> dict[UUID, WorkspaceNodes]:
    """Each workspace's folder and shared working tree, in one scoped read.

    A native workspace's folder is its own node and its tree the ``files``
    child; an adopted workspace's folder is its chat's, and its tree the
    chat's working directory. A workspace whose folder is trashed, or that has
    none (Files off), is simply absent."""
    if not settings.files_enabled or not rows:
        return {}
    by_folder = {folder_object_id(ws): ws for ws in rows}
    found: dict[UUID, WorkspaceNodes] = {}
    async with files_transaction(db, ctx) as repo:
        result: Any = await repo.execute_scoped(
            repo.select_nodes().where(
                FileNode.target_object_id.in_(list(by_folder)),
                FileNode.trashed_at.is_(None),
                FileNode.kind.in_(("folder", "object")),
            )
        )
        folders = {
            node.target_object_id: node
            for node in result.scalars().all()
            if node.subtype in (WORKSPACE_TYPE, CHAT_TYPE)
        }
        # A native workspace's tree is its folder's ``files`` child and an
        # adopted one's is its chat's working directory: each folder answers
        # by its own kind, in one lookup.
        folder_ids = [UUID(str(node.id)) for node in folders.values()]
        working_of = await working_folder_nodes(repo.session, folder_ids)
        held: dict[UUID, tuple[str, datetime]] = {}
        if folder_ids:
            leases: Any = await repo.execute_scoped(
                select(FileLease.node_id, FileLease.machine_id, FileLease.heartbeat_at).where(
                    FileLease.node_id.in_(folder_ids),
                    FileLease.holder_kind == "machine",
                    FileLease.released_at.is_(None),
                    FileLease.reaped_at.is_(None),
                    FileLease.expires_at > func.now(),
                )
            )
            held = {
                UUID(str(node_id)): (str(machine_id), beat_at)
                for node_id, machine_id, beat_at in leases.all()
            }
    for owner_id, node in folders.items():
        workspace = by_folder[owner_id]
        node_id = UUID(str(node.id))
        lease_machine_id, lease_beat_at = held.get(node_id, (None, None))
        found[workspace.id] = WorkspaceNodes(
            folder=node_id,
            drive=UUID(str(node.drive_id)),
            working=working_of.get(node_id),
            lease_machine_id=lease_machine_id,
            lease_beat_at=lease_beat_at,
        )
    return found


__all__ = ["WorkspaceNodes", "nodes_for"]
