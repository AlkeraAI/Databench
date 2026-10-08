"""The Files side of an erasure: a deleted object's folder, and a closing org's drive.

Both go through the Trash service the product already deletes with, so every
foreign key into a purged subtree gets the decision that service writes down
for it, the purge reaches the store only through the reachability sweep, and a
legal hold still beats the erasure. A subtree a hold, a
locked node or a kept version refuses is left in the trash rather than failing
the whole erasure; the certificate counts it.

Each step runs in its own savepoint (``FilesRepo.joined``), so a refused purge
rolls back only itself and the trash before it stands.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.authz.enums import CredentialKind
from alkera_core.authz.principal import ActingContext
from alkera_core.files.clock import SystemClock
from alkera_core.files.errors import Conflict
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.objects_bridge import live_node_for
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import Trash
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode

#: The reason a purged object's trash row records.
REASON_ACCOUNT_ERASED = "account_erased"


@dataclass
class FilesOutcome:
    purged: int = 0
    held: int = 0


def _ctx(org_id: uuid.UUID, user_id: uuid.UUID) -> ActingContext:
    """The context the erasure files its trash history under: a service acting
    for the org, named for what it is, never the erased person."""
    return ActingContext.for_service(
        token_id=user_id,
        org_id=org_id,
        label="account.erasure",
        credential=CredentialKind.CI_TOKEN,
    )


async def _drive(db: AsyncSession, org_id: uuid.UUID) -> FileDrive | None:
    return await FilesRepo.org_drive_anywhere(db, org_id)


async def _purge(trash: Trash, repo: FilesRepo, node_id: NodeId, outcome: FilesOutcome) -> None:
    try:
        async with repo.transaction():
            await trash.purge(node_id)
        outcome.purged += 1
    except Conflict:
        outcome.held += 1


async def purge_object_folders(
    db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, object_ids: Sequence[uuid.UUID]
) -> FilesOutcome:
    """Trash, then purge, the folder of each deleted object that has one."""
    outcome = FilesOutcome()
    if not object_ids or await _drive(db, org_id) is None:
        return outcome
    repo = FilesRepo.joined(db, OrgScope(org_team_id=org_id))
    trash = Trash(repo, _ctx(org_id, user_id), SystemClock())
    for object_id in object_ids:
        async with repo.transaction():
            node = await live_node_for(repo, object_id)
            if node is None:
                continue
            await trash.trash_for_deleted_object(NodeId(node.id), reason=REASON_ACCOUNT_ERASED)
            node_id = NodeId(node.id)
        await _purge(trash, repo, node_id, outcome)
    return outcome


async def empty_drive(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> FilesOutcome:
    """Trash every top-level entry of a closing org's drive, then purge the
    drive's whole trash (including whatever was in it before)."""
    outcome = FilesOutcome()
    drive = await _drive(db, org_id)
    if drive is None or drive.root_node_id is None:
        return outcome
    repo = FilesRepo.joined(db, OrgScope(org_team_id=org_id))
    trash = Trash(repo, _ctx(org_id, user_id), SystemClock())
    async with repo.transaction():
        children = (
            (
                await db.execute(
                    select(FileNode.id, FileNode.etag).where(
                        FileNode.org_team_id == org_id,
                        FileNode.parent_id == drive.root_node_id,
                        FileNode.trashed_at.is_(None),
                    )
                )
            )
            .tuples()
            .all()
        )
        for node_id, etag in children:
            await trash.trash(NodeId(node_id), if_match=etag)
    async with repo.transaction():
        roots = (await trash.list_trash(DriveId(drive.id), marker=None, limit=1_000)).entries
    for entry in roots:
        await _purge(trash, repo, NodeId(entry.node.id), outcome)
    return outcome


__all__ = ["REASON_ACCOUNT_ERASED", "FilesOutcome", "empty_drive", "purge_object_folders"]
