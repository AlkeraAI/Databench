"""Folder stats: deltas in the write path, folding in the job, nothing hot."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from alkera_core.files import stats
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.ids import NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileDirStats, FileDirStatsDelta
from alkera_core.models.files.tree import FileNode
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


async def _row_xmin(session: AsyncSession, node_id: Any) -> int:
    return int(
        (
            await session.execute(
                text("SELECT xmin::text::bigint FROM file_nodes WHERE id = :id"),
                {"id": str(node_id)},
            )
        ).scalar_one()
    )


async def _delta_count(session: AsyncSession, org: FilesOrg) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(FileDirStatsDelta)
                .where(FileDirStatsDelta.org_team_id == org.org_team_id)
            )
        ).scalar_one()
    )


async def test_a_leaf_write_leaves_the_parent_row_untouched(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """The parent's `xmin` is the proof: an UPDATE of the ancestor would make
    it a new row version and serialize every writer beneath the folder."""
    drive = await files_factory.drive()
    made = await files_factory.tree("f/ f/a.txt", drive=drive)
    parent, leaf = made["f"], made["f/a.txt"]
    before = await _row_xmin(files_session, parent.id)

    async with repo.transaction():
        await repo.session.execute(update(FileNode).where(FileNode.id == leaf.id).values(size=512))
        await stats.add_delta(
            repo,
            node_id=NodeId(parent.id),
            bytes_delta=512,
            files_delta=1,
            direct_children_delta=1,
            child_change_at=EPOCH,
        )

    assert await _row_xmin(files_session, parent.id) == before
    assert await _row_xmin(files_session, leaf.id) != before
    assert await _delta_count(files_session, files_org) == 1


async def test_deltas_accumulate_and_aggregate_folds_them_exactly_once(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Three appends, one cached row; a second pass folds nothing, because the
    fold and the delete of the rows that fed it are one transaction."""
    drive = await files_factory.drive()
    made = await files_factory.tree("f/", drive=drive)
    parent = made["f"]
    for n, when in enumerate((EPOCH, EPOCH + timedelta(minutes=5), EPOCH + timedelta(minutes=1))):
        async with repo.transaction():
            await stats.add_delta(
                repo,
                node_id=NodeId(parent.id),
                bytes_delta=100 * (n + 1),
                files_delta=1,
                direct_children_delta=1,
                child_change_at=when,
            )

    assert await stats.aggregate([repo]) == 3
    row = (
        await files_session.execute(select(FileDirStats).where(FileDirStats.node_id == parent.id))
    ).scalar_one()
    # 100 + 200 + 300 — independently summed, not read back off the fold.
    assert (row.bytes, row.files, row.direct_children) == (600, 3, 3)
    # The newest child change wins even though it was folded before an older one.
    assert row.last_child_change_at == EPOCH + timedelta(minutes=5)
    assert await _delta_count(files_session, files_org) == 0

    assert await stats.aggregate([repo]) == 0
    again = (
        await files_session.execute(
            select(FileDirStats.bytes).where(FileDirStats.node_id == parent.id)
        )
    ).scalar_one()
    assert again == 600


async def test_a_crash_between_the_fold_and_the_delete_folds_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, files_session: AsyncSession
) -> None:
    """Resumable: the killed pass leaves the deltas, and the next pass folds
    them once — never twice, which double counting would show as 200."""
    drive = await files_factory.drive()
    made = await files_factory.tree("f/", drive=drive)
    parent_id = made["f"].id
    async with repo.transaction():
        await stats.add_delta(
            repo,
            node_id=NodeId(parent_id),
            bytes_delta=100,
            files_delta=1,
            direct_children_delta=1,
            child_change_at=EPOCH,
        )

    checkpoints = PausingCheckpoints()
    checkpoints.kill("stats.after_fold_before_delete")
    with pytest.raises(CheckpointKilled):
        await stats.aggregate([repo], checkpoints=checkpoints)

    assert (
        await files_session.execute(select(FileDirStats).where(FileDirStats.node_id == parent_id))
    ).scalar_one_or_none() is None
    assert await _delta_count(files_session, files_org) == 1

    assert await stats.aggregate([repo]) == 1
    row = (
        await files_session.execute(
            select(FileDirStats.bytes).where(FileDirStats.node_id == parent_id)
        )
    ).scalar_one()
    assert row == 100
    assert await _delta_count(files_session, files_org) == 0


async def test_folder_mtime_prefers_the_explicit_stamp(
    repo: FilesRepo, files_factory: FilesFactory
) -> None:
    """A client that stamped the folder means it; a later child change must not
    move the stamp."""
    drive = await files_factory.drive()
    made = await files_factory.tree("f/", drive=drive)
    parent = made["f"]
    async with repo.transaction():
        await stats.add_delta(
            repo,
            node_id=NodeId(parent.id),
            bytes_delta=1,
            files_delta=1,
            direct_children_delta=1,
            child_change_at=EPOCH,
        )
    await stats.aggregate([repo])

    parent.mtime_ns = 1_234_000_000_000
    async with repo.transaction():
        assert await stats.folder_mtime(repo, parent) == 1_234_000_000_000


async def test_folder_mtime_falls_back_to_the_latest_child_change(
    repo: FilesRepo, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("f/", drive=drive)
    parent = made["f"]
    latest = EPOCH + timedelta(hours=3)
    for when in (EPOCH, latest):
        async with repo.transaction():
            await stats.add_delta(
                repo,
                node_id=NodeId(parent.id),
                bytes_delta=1,
                files_delta=1,
                direct_children_delta=1,
                child_change_at=when,
            )
    await stats.aggregate([repo])

    async with repo.transaction():
        assert await stats.folder_mtime(repo, parent) == int(latest.timestamp() * 1_000_000_000)


async def test_folder_mtime_is_none_when_nothing_is_known(
    repo: FilesRepo, files_factory: FilesFactory
) -> None:
    """The negative twin: no stamp and no folded child change is not zero."""
    drive = await files_factory.drive()
    made = await files_factory.tree("f/", drive=drive)
    async with repo.transaction():
        assert await stats.folder_mtime(repo, made["f"]) is None


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(0, 1, id="zero-to-one"),
        pytest.param(1, 2, id="adjacent"),
        pytest.param(15, 16, id="hex-carry"),
        pytest.param(1, 1 << 40, id="far-apart"),
    ],
)
def test_folder_ctag_changes_with_the_drive_sequence(left: int, right: int) -> None:
    assert stats.folder_ctag(left) != stats.folder_ctag(right)
    assert stats.folder_ctag(left) == stats.folder_ctag(left)


def test_folder_ctag_refuses_a_negative_sequence() -> None:
    with pytest.raises(ValueError, match="never negative"):
        stats.folder_ctag(-1)


async def test_aggregate_refuses_a_batch_below_one(repo: FilesRepo) -> None:
    with pytest.raises(ValueError, match="batch must be >= 1"):
        await stats.aggregate([repo], batch=0)


async def _stat_row(session: AsyncSession, node_id: Any) -> tuple[int, int, int]:
    row = (
        await session.execute(
            select(FileDirStats.bytes, FileDirStats.files, FileDirStats.direct_children).where(
                FileDirStats.node_id == node_id
            )
        )
    ).one_or_none()
    return (0, 0, 0) if row is None else (int(row[0]), int(row[1]), int(row[2]))


async def _root_of(session: AsyncSession, drive_id: Any) -> Any:
    return (
        await session.execute(
            text("SELECT root_node_id FROM file_drives WHERE id = :d"), {"d": str(drive_id)}
        )
    ).scalar_one()


async def test_one_delta_reaches_every_ancestor_and_no_cousin(
    repo: FilesRepo, files_factory: FilesFactory, files_session: AsyncSession
) -> None:
    """The fold carries bytes and files all the way to the drive root, so the
    root row is the subtree total a recount would produce - and it stops at the
    path: a sibling branch nothing wrote to stays at zero."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c/ z/", drive=drive)
    async with repo.transaction():
        await stats.add_delta(
            repo,
            node_id=NodeId(made["a/b/c"].id),
            bytes_delta=700,
            files_delta=3,
            direct_children_delta=3,
            child_change_at=EPOCH,
        )
    assert await stats.aggregate([repo]) == 1

    root_id = await _root_of(files_session, drive.id)
    assert await _stat_row(files_session, made["a/b/c"].id) == (700, 3, 3)
    # `direct_children` stays where it was charged; bytes and files climb.
    assert await _stat_row(files_session, made["a/b"].id) == (700, 3, 0)
    assert await _stat_row(files_session, made["a"].id) == (700, 3, 0)
    assert await _stat_row(files_session, root_id) == (700, 3, 0)
    assert await _stat_row(files_session, made["z"].id) == (0, 0, 0)


async def test_the_root_total_equals_a_recount_of_every_charge(
    repo: FilesRepo, files_factory: FilesFactory, files_session: AsyncSession
) -> None:
    """The invariant the soak asserts, in the small: after the fold the root
    equals an independent sum of the same numbers, wherever they were charged,
    and each mid-folder holds its own subtree rather than the drive total."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c/ z/ z/y/", drive=drive)
    charges = [("a", 10, 1), ("a/b", 250, 2), ("a/b/c", 3, 0), ("z", 40, 1), ("z/y", 7, 4)]
    for where, size, files in charges:
        async with repo.transaction():
            await stats.add_delta(
                repo,
                node_id=NodeId(made[where].id),
                bytes_delta=size,
                files_delta=files,
                direct_children_delta=1,
                child_change_at=EPOCH,
            )
    assert await stats.aggregate([repo]) == len(charges)

    root_id = await _root_of(files_session, drive.id)
    assert await _stat_row(files_session, root_id) == (
        sum(one[1] for one in charges),
        sum(one[2] for one in charges),
        0,
    )
    assert await _stat_row(files_session, made["a"].id) == (10 + 250 + 3, 3, 1)
    assert await _stat_row(files_session, made["z"].id) == (40 + 7, 5, 1)
