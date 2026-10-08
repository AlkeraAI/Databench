"""A conflict-renaming move must not collide in the folder it is leaving.

The shrunk sequence from the namespace reference-model machine: a folder holds
both ``readme`` and the ``readme (1)`` an earlier restore minted, and the
destination folder holds a ``readme`` of its own. ``move(conflict="rename")``
performs the rename as a real write *before* the re-parent — it has to, because
the re-parent itself would violate the destination's live-sibling index — so the
name it picks has to be free where the node is standing as well as where it is
going. Picking it against the destination alone made the intermediate rename
violate ``uq_file_nodes_parent_name_live`` in the *source* folder and surfaced as
a 409 from a call the contract says never fails on a name.
"""

from __future__ import annotations

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _namespace(repo: FilesRepo, org: FilesOrg, clock: FakeClock) -> Namespace:
    return Namespace(
        repo,
        ActingContext(
            acting_principal=Principal(
                kind=PrincipalKind.USER,
                id=str(org.admin_id),
                org_id=org.org_team_id,
                credential=CredentialKind.JWT,
            )
        ),
        clock,
        None,
    )


async def _live_names(session: AsyncSession, parent_id: NodeId) -> list[bytes]:
    rows = (
        await session.execute(
            select(FileNode.name)
            .where(FileNode.parent_id == parent_id, FileNode.trashed_at.is_(None))
            .order_by(FileNode.name)
        )
    ).scalars()
    return list(rows)


async def test_conflict_renaming_move_skips_a_name_taken_in_the_source_folder(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    drive = await files_factory.drive()
    seeded = await files_factory.tree("dest/ readme", drive=drive)
    await files_session.commit()

    assert drive.root_node_id is not None
    source_root = NodeId(drive.root_node_id)
    destination = NodeId(seeded["dest"].id)
    moving = NodeId(seeded["readme"].id)
    namespace = _namespace(repo, files_org, clock)

    async with repo.transaction():
        # Creates and a move in one transaction take the tree exclusive first,
        # as a batch does.
        await repo.lock_namespace(DriveId(drive.id), exclusive=True)
        # The source folder is already using the first candidate the conflict
        # rename would reach for, and the destination already holds the name the
        # node is carrying — so the move has to rename, and cannot rename to
        # ``readme (1)``.
        await namespace.create(DriveId(drive.id), source_root, "file", b"readme", conflict="rename")
        await namespace.create(DriveId(drive.id), destination, "file", b"readme")
        moved = await namespace.move(
            moving,
            destination,
            if_match=seeded["readme"].etag,
            conflict="rename",
        )

    assert isinstance(moved, FileNode)
    assert moved.name == b"readme (2)"
    assert moved.parent_id == destination
    assert sorted(await _live_names(files_session, destination)) == [b"readme", b"readme (2)"]
    # The node that made ``readme (1)`` unavailable is untouched and still there.
    assert await _live_names(files_session, source_root) == [b"dest", b"readme (1)"]
