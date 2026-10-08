"""A copy batch does not hold the drive row while it does its work.

Inode numbers come off ``file_drives.next_ino``, and the row is what every
other writer in the org has to touch too. A batch that advanced the counter in
its own transaction kept the row locked for the whole batch — the insert, the
version copy, the stats — and every other write of the org queued behind it for
that long. The claim is now a statement in a transaction of its own, so the row
is held for one UPDATE and the batch's work runs with the row free.

The observer is a second real connection with a bounded ``lock_timeout``: while
the batch is paused inside its transaction, that connection must be able to
advance the counter itself. The bound is there to turn a regression into a
failure rather than a hang — the batch holds its checkpoint until this test
releases it, so a row held across the batch is never freed however long the
observer waits.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.copy import run_copy, start_copy
from alkera_core.files.ids import OperationId
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

TREE = "src/ src/a/ src/a/b/ src/a/b/deep.bin src/a/one.txt src/two.txt dst/"

#: What the observing writer gives the drive row before it gives up, in
#: milliseconds. A failure deadline, not a wait: the batch stays inside its
#: transaction until this test releases the checkpoint, so a row held across
#: the batch is never freed and the budget only decides how long the proof
#: takes. It is the deployment's own request budget, floored at ten seconds so
#: a runner serving a dozen other suites cannot lose the race while the row is
#: perfectly free — the wall-clock guess it replaces did.
LOCK_BUDGET_MS = max(settings.database_lock_timeout_ms, 10_000)

#: How long each side of the checkpoint handshake waits for the other. The
#: default is five seconds, which is the same kind of guess: on a loaded runner
#: the copy can take longer than that to reach the checkpoint, and the test
#: then reports a deadlock that never happened and abandons the copy task.
#: Three times the lock budget, so the ordering is fixed — a drive row that IS
#: held runs the observer out of lock budget first and fails as the lock
#: timeout it is, rather than as the copy's release deadline expiring behind it.
HANDSHAKE_BUDGET_SECONDS = LOCK_BUDGET_MS / 1000 * 3


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _live_under(session: AsyncSession, drive_id: uuid.UUID, root_name: bytes) -> int:
    """Live nodes at or under every node named ``root_name`` in ONE drive.

    A ``path_ids`` label is the node's inode number, which is numbered per
    drive: the same chain of labels names a node in every drive that has one
    that deep. So the descendant side of the join carries the drive too — a
    prefix match alone counts the neighbours' trees as this drive's.
    """
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM file_nodes AS n "
                    "JOIN file_nodes AS r "
                    "ON n.drive_id = r.drive_id AND n.path_ids <@ r.path_ids "
                    "WHERE r.drive_id = :drive AND r.name = :name AND r.parent_id IS NOT NULL "
                    "AND n.trashed_at IS NULL"
                ),
                {"drive": drive_id, "name": root_name},
            )
        ).scalar_one()
    )


async def test_another_writer_can_advance_the_counter_while_a_batch_is_mid_flight(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    repo: FilesRepo,
) -> None:
    drive = await files_factory.drive()
    made: dict[str, Any] = await files_factory.tree(TREE, drive=drive)
    drive_id = drive.id
    # A neighbour's drive holding the same tree. Its nodes carry the same inode
    # labels, so a count that reads the paths without the drive reads these
    # too — and the database this runs on already holds other people's drives.
    await files_factory.tree(TREE, drive=await files_factory.drive(org=await files_org_factory()))
    ctx = _ctx(files_org)
    ops = Operations(repo, ctx, FakeClock(now=EPOCH))
    async with repo.transaction():
        op_id: OperationId = await start_copy(repo, ops, node=made["src"], dest_parent=made["dst"])

    checkpoints = PausingCheckpoints(timeout=HANDSHAKE_BUDGET_SECONDS)
    checkpoints.pause("copy.in_batch")
    copying = asyncio.create_task(run_copy(repo, ctx, op_id, batch=2, checkpoints=checkpoints))
    await checkpoints.wait_paused("copy.in_batch")

    other = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    try:
        async with other.connect() as writer:
            await writer.execute(
                text("SELECT set_config('lock_timeout', :budget, true)"),
                {"budget": str(LOCK_BUDGET_MS)},
            )
            # The batch is inside its transaction, and stays there until the
            # checkpoint is released below. The drive row must be free.
            advanced = await writer.execute(
                text(
                    "UPDATE file_drives SET next_ino = next_ino + 1 "
                    "WHERE id = :drive RETURNING next_ino"
                ),
                {"drive": drive_id},
            )
            assert advanced.scalar_one() > 0
            await writer.rollback()
    finally:
        checkpoints.release("copy.in_batch")
        await other.dispose()

    await copying
    files_session.expire_all()
    assert (await ops.get(op_id)).state == "done"
    # The copy is whole: src's six nodes, copied under dst.
    assert await _live_under(files_session, drive_id, b"src") == 6 * 2


async def test_batches_never_share_an_inode_number(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """Claiming ahead of the batch must not hand two batches the same range."""
    drive = await files_factory.drive()
    made: dict[str, Any] = await files_factory.tree(TREE, drive=drive)
    drive_id = drive.id
    ctx = _ctx(files_org)
    ops = Operations(repo, ctx, FakeClock(now=EPOCH))
    async with repo.transaction():
        op_id = await start_copy(repo, ops, node=made["src"], dest_parent=made["dst"])
    await run_copy(repo, ctx, op_id, batch=1)
    files_session.expire_all()
    rows = (
        await files_session.execute(
            text("SELECT ino FROM file_nodes WHERE drive_id = :drive"), {"drive": drive_id}
        )
    ).scalars()
    inos = [int(row) for row in rows]
    assert len(inos) == len(set(inos))
