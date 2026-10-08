"""A restore that lands mid-prune must not cost the node its head.

The pruner picks its victims in Python from ``node.head_version_id`` as it was
at read time, then deletes them in one statement. Between those two moments a
restore (or an undo) can make one of the doomed versions the node's head. The
DELETE re-asserts the pin and the hold, so this test asks the same of the head:
the version a second backend has just made current survives the pass, and the
node still serves it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from alkera_core.files import retention
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.repo import FilesRepo
from alkera_core.files.retention import KEEP_NEWEST, KEEP_WINDOW
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg
from tests.files.test_files_retention import _versions


async def test_a_restore_between_the_read_and_the_delete_keeps_its_version(
    files_session: AsyncSession,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[AsyncSession]]],
) -> None:
    """The version a concurrent restore made head is not in the prune's reap."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("f.bin", drive=drive))["f.bin"]
    clock.move_to(EPOCH + timedelta(days=400))
    # Every version is older than the keep window, so the count bound is the only
    # one keeping any: the newest `KEEP_NEWEST` survive and the rest are doomed.
    # Both bounds are read from the rule rather than spelled here, so raising
    # either setting moves this case with it instead of turning it red.
    count = KEEP_NEWEST + 50
    versions = await _versions(
        files_session,
        node,
        count=count,
        newest_at=clock.now() - KEEP_WINDOW - timedelta(days=1),
        step=timedelta(days=1),
    )
    restored = versions[0]

    pruner_session, restorer_session = await sessions(2)
    checkpoints = PausingCheckpoints()
    checkpoints.pause("retention.prune.selected")

    async def prune() -> int:
        repo = FilesRepo(pruner_session, files_org.scope)
        async with repo.transaction():
            return await retention.prune_versions(repo, now=clock.now(), checkpoints=checkpoints)

    async def restore() -> None:
        # The restore commits while the pruner is parked with `restored` already
        # in its doomed list — the exact window the DELETE has to re-check.
        await checkpoints.wait_paused("retention.prune.selected")
        await restorer_session.execute(
            update(FileNode.__table__)
            .where(FileNode.id == node.id)
            .values(head_version_id=restored.id, etag=FileNode.__table__.c.etag + 1)
        )
        await restorer_session.commit()
        checkpoints.release("retention.prune.selected")

    pruned, _ = await asyncio.gather(prune(), restore())

    surviving = {
        row[0]
        for row in (
            await files_session.execute(
                select(FileVersion.id).where(FileVersion.node_id == node.id)
            )
        ).all()
    }
    # The head the restore installed is still there, and the node still points
    # at it — a customer who restored an old revision keeps it.
    assert restored.id in surviving
    head = (
        await files_session.execute(select(FileNode.head_version_id).where(FileNode.id == node.id))
    ).scalar_one()
    assert head == restored.id
    # The rest of the batch still went: the race exempts one version, not the pass.
    assert pruned == count - KEEP_NEWEST - 1
    assert len(surviving) == KEEP_NEWEST + 1
