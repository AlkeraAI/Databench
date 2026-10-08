"""``FilesRepo.joined`` — Files work that lives or dies with the caller's transaction.

The owned ``transaction()`` commits when it exits, which is right for a route
that owns its unit of work and wrong for a bridge call or for a route that runs
several collaborators inside one idempotency claim. The joined handle is the
seam for that: the same role and org stamp, a SAVEPOINT instead of a commit,
and re-entry that is the same unit of work rather than a refusal.

Each test names the owned behaviour it contrasts with, so a seam that quietly
started committing (or quietly stopped stamping the role) fails here.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from alkera_core.files.ids import NodeId
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo, RepoUsageError
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _child(drive: FileDrive, root_id: uuid.UUID, root_path: str, ino: int, name: str) -> FileNode:
    return FileNode(
        id=uuid.uuid4(),
        ino=ino,
        drive_id=drive.id,
        org_team_id=drive.org_team_id,
        parent_id=root_id,
        kind="file",
        name=name.encode(),
        name_display=name,
        name_key=name.casefold(),
        path_ids=f"{root_path}.{ino_label(ino)}",
        depth=1,
    )


async def _seeded_drive(session: AsyncSession, factory: FilesFactory) -> FileDrive:
    """A committed drive, so a later rollback has something to roll back to."""
    drive = await factory.drive()
    await session.commit()
    return drive


async def _root(session: AsyncSession, drive: FileDrive) -> FileNode:
    assert drive.root_node_id is not None
    root = await session.get(FileNode, drive.root_node_id)
    assert root is not None
    return root


async def test_joined_write_is_undone_by_the_callers_rollback(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """The point of the seam: the caller owns the commit, so its rollback wins."""
    drive = await _seeded_drive(files_session, files_factory)
    root = await _root(files_session, drive)
    node = _child(drive, root.id, root.path_ids, 2, "joined.txt")

    repo = FilesRepo.joined(files_session, files_org.scope)
    async with repo.transaction() as joined:
        await joined.add(node)
        await joined.flush()
        assert await joined.node(NodeId(node.id)) is not None

    # The joined block exited cleanly and still committed nothing.
    await files_session.rollback()
    assert await files_session.get(FileNode, node.id) is None


async def test_an_owned_repo_commits_the_same_write(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """The contrast that makes the test above mean something.

    Identical write through the owned handle: the caller's rollback afterwards
    cannot reach it, because ``transaction()`` already committed.
    """
    drive = await _seeded_drive(files_session, files_factory)
    root = await _root(files_session, drive)
    node = _child(drive, root.id, root.path_ids, 3, "owned.txt")

    repo = FilesRepo(files_session, files_org.scope)
    async with repo.transaction() as owned:
        await owned.add(node)
        await owned.flush()

    await files_session.rollback()
    assert await files_session.get(FileNode, node.id) is not None


async def test_joined_still_assumes_the_app_role_and_stamps_the_org(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
) -> None:
    """RLS is the second net behind the org predicate, and the join keeps it.

    The raw count deliberately bypasses the repo's predicate: under the app
    role with this org stamped it can only see this org's rows, so a seam that
    forgot either statement would count the other org's node too.
    """
    other = await files_org_factory()
    other_factory = FilesFactory(files_session, other)
    mine = await _seeded_drive(files_session, files_factory)
    theirs = await other_factory.drive()
    await files_session.commit()
    their_root_id = (await _root(files_session, theirs)).id
    my_root_id = (await _root(files_session, mine)).id

    repo = FilesRepo.joined(files_session, files_org.scope)
    async with repo.transaction() as joined:
        assert await joined.node(NodeId(their_root_id)) is None
        assert await joined.node(NodeId(my_root_id)) is not None
        visible = (
            await files_session.execute(text("SELECT count(*) FROM file_nodes"))
        ).scalar_one()
        owners = (
            (await files_session.execute(text("SELECT DISTINCT org_team_id FROM file_nodes")))
            .scalars()
            .all()
        )
    await files_session.rollback()

    assert visible == 1  # my drive's root only — theirs is behind the policy
    assert owners == [files_org.org_team_id]


async def test_reentering_a_joined_transaction_is_one_unit_of_work(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """A collaborator opening its own transaction joins rather than aborting.

    The inner block exiting must not commit either: the caller's rollback still
    undoes both writes.
    """
    drive = await _seeded_drive(files_session, files_factory)
    root = await _root(files_session, drive)
    outer_node = _child(drive, root.id, root.path_ids, 4, "outer.txt")
    inner_node = _child(drive, root.id, root.path_ids, 5, "inner.txt")

    repo = FilesRepo.joined(files_session, files_org.scope)
    async with repo.transaction() as joined:
        await joined.add(outer_node)
        async with repo.transaction() as nested:
            assert nested is joined
            await nested.add(inner_node)
            await nested.flush()
        # Still inside the caller's unit of work after the inner block.
        assert await joined.node(NodeId(inner_node.id)) is not None

    await files_session.rollback()
    assert await files_session.get(FileNode, outer_node.id) is None
    assert await files_session.get(FileNode, inner_node.id) is None


async def test_an_owned_repo_still_refuses_a_second_transaction(
    files_session: AsyncSession,
    files_org: FilesOrg,
) -> None:
    """Re-entry is the joined handle's contract, not a relaxation for everyone."""
    repo = FilesRepo(files_session, files_org.scope)
    async with repo.transaction():
        with pytest.raises(RepoUsageError):
            async with repo.transaction():
                pass
    await files_session.rollback()


async def test_a_failed_joined_block_costs_only_its_own_savepoint(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
) -> None:
    """The caller's earlier work survives a Files failure and can still commit."""
    drive = await _seeded_drive(files_session, files_factory)
    root = await _root(files_session, drive)
    doomed = _child(drive, root.id, root.path_ids, 6, "doomed.txt")
    kept = _child(drive, root.id, root.path_ids, 7, "kept.txt")

    await files_session.execute(
        FileNode.__table__.insert().values(
            id=kept.id,
            ino=kept.ino,
            drive_id=kept.drive_id,
            org_team_id=kept.org_team_id,
            parent_id=kept.parent_id,
            kind="file",
            name=kept.name,
            name_display=kept.name_display,
            name_key=kept.name_key,
            path_ids=kept.path_ids,
            depth=kept.depth,
        )
    )

    repo = FilesRepo.joined(files_session, files_org.scope)
    with pytest.raises(RuntimeError, match="files blew up"):
        async with repo.transaction() as joined:
            await joined.add(doomed)
            await joined.flush()
            raise RuntimeError("files blew up")

    await files_session.commit()
    assert await files_session.get(FileNode, doomed.id) is None
    assert await files_session.get(FileNode, kept.id) is not None
