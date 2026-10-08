"""A holder's tree report holds its own folder, not the org's drive row.

A box syncing thirty chat folders into one org drive has a tree report open
almost all the time. While the report took the drive row, a person's create,
rename or move anywhere in the org queued behind its batch until
``lock_timeout`` answered 503. The report now takes only the leased folder and
its lease row -- and the drive only when its batch trashes or moves something.

Each test parks a real report at a checkpoint after it has written its rows,
so it holds everything it will hold until commit, and drives other writers on
their own sessions with a short ``lock_timeout``: "did not wait" is a landed
write, "waited" is a raised ``55P03`` or a lock Postgres reports ungranted.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock, SystemClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.lease_tree import LeaseTreeService, TreeAnswer, TreeChange
from alkera_core.files.leases import LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.tree import FileNode
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by
from tests.files.concurrency._gate import _backend_pid, _wait_blocked

pytestmark = pytest.mark.asyncio

INSTANCE = "box-1"
WRITTEN = "lease_tree.rows_written"
LOCK_TIMEOUT = "55P03"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _sqlstate(exc: BaseException) -> str | None:
    seen: BaseException | None = exc
    while seen is not None:
        original = getattr(seen, "orig", None)
        state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if state is not None:
            return str(state)
        seen = seen.__cause__ or seen.__context__
    return None


class Box:
    """One drive: a live-leased ``box/`` and folders nobody leases."""

    def __init__(self, org: FilesOrg, drive: Any, tree: dict[str, FileNode], epoch: int) -> None:
        self.org = org
        self.drive = DriveId(drive.id)
        self.tree = tree
        self.epoch = epoch

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org.org_team_id)

    @property
    def lease(self) -> NodeId:
        return NodeId(self.tree["box"].id)


@pytest.fixture
async def box(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> Box:
    drive = await files_factory.drive()
    tree = await files_factory.tree(
        "box/ box/sub/ box/sub/x.bin box/old/ box/old/y.bin home/ home/a.txt home/b.txt elsewhere/",
        drive=drive,
    )
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(tree["box"].id), instance_id=INSTANCE, machine_id="box-mbp"
        )
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET live_cadence = CAST(:c AS jsonb) WHERE node_id = :n"),
            {"c": '{"metadataEveryMs": 300}', "n": tree["box"].id},
        )
    return Box(files_org, drive, tree, grant.epoch)


def _files(count: int) -> list[TreeChange]:
    return [
        TreeChange(op="upsert", path=f"src/f{index}.py".encode(), kind="file", size=1, mtime_ns=1)
        for index in range(count)
    ]


async def _report(
    session: Any, box: Box, changes: list[TreeChange], checkpoints: PausingCheckpoints
) -> TreeAnswer:
    repo = FilesRepo(session, box.scope)
    async with repo.transaction():
        return await LeaseTreeService(
            repo, _ctx(box.org), SystemClock(), checkpoints=checkpoints
        ).apply(
            box.lease,
            drive_id=box.drive,
            epoch=box.epoch,
            instance_id=INSTANCE,
            batch_id=uuid.uuid4(),
            changes=changes,
        )


async def _quick(session: Any, box: Box, write: Callable[[Namespace], Awaitable[Any]]) -> None:
    """One write on its own session that may wait at most 300 ms for a lock."""
    repo = FilesRepo(session, box.scope)
    async with repo.transaction():
        await session.execute(text("SET LOCAL lock_timeout = '300ms'"))
        await write(Namespace(repo, _ctx(box.org), SystemClock()))


def _create(box: Box, parent: str, name: bytes) -> Callable[[Namespace], Awaitable[Any]]:
    return lambda ns: ns.create(box.drive, NodeId(box.tree[parent].id), "folder", name)


def _rename(box: Box, path: str, name: bytes, *, fenced: bool = False) -> Any:
    node = box.tree[path]
    ctx = _ctx(box.org)
    return lambda ns: ns.rename(
        NodeId(node.id),
        name,
        if_match=int(node.etag),
        lease=held_by(ctx, box.epoch, INSTANCE) if fenced else None,
    )


def _move(box: Box, path: str, to: str) -> Callable[[Namespace], Awaitable[Any]]:
    node = box.tree[path]
    return lambda ns: ns.move(NodeId(node.id), NodeId(box.tree[to].id), if_match=int(node.etag))


async def _live_names(session: Any, parent: FileNode) -> set[bytes]:
    rows = await session.execute(
        text("SELECT name FROM file_nodes WHERE parent_id = :p AND trashed_at IS NULL"),
        {"p": parent.id},
    )
    return {bytes(row[0]) for row in rows.all()}


async def _paused(
    session: Any, box: Box, changes: list[TreeChange]
) -> tuple[asyncio.Task[TreeAnswer], PausingCheckpoints]:
    checkpoints = PausingCheckpoints(timeout=60)
    checkpoints.pause(WRITTEN)
    task = asyncio.ensure_future(_report(session, box, changes, checkpoints))
    waiting = asyncio.ensure_future(checkpoints.wait_paused(WRITTEN, timeout=60))
    await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        waiting.cancel()
        task.result()
        raise AssertionError("the report finished without reaching its checkpoint")
    return task, checkpoints


async def test_writes_in_other_folders_land_while_a_500_entry_report_is_mid_batch(
    box: Box, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    reporter, creator, renamer, mover, reader = await sessions(5)
    task, checkpoints = await _paused(reporter, box, _files(500))
    try:
        await _quick(creator, box, _create(box, "home", b"made"))
        await _quick(renamer, box, _rename(box, "home/a.txt", b"renamed.txt"))
        await _quick(mover, box, _move(box, "home/b.txt", "elsewhere"))
    finally:
        checkpoints.release(WRITTEN)
        answer = await task
    assert answer.applied == 500
    assert await _live_names(reader, box.tree["home"]) == {b"made", b"renamed.txt"}
    assert await _live_names(reader, box.tree["elsewhere"]) == {b"b.txt"}
    src = (
        await reader.execute(
            text(
                "SELECT count(*) FROM file_nodes c JOIN file_nodes s ON c.parent_id = s.id "
                "WHERE s.parent_id = :box AND s.name = 'src'::bytea AND c.trashed_at IS NULL"
            ),
            {"box": box.tree["box"].id},
        )
    ).scalar_one()
    assert src == 500


async def test_a_rename_inside_the_leased_folder_waits_for_the_batch_and_then_lands(
    box: Box, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """Queued on the leased folder the batch holds, for the batch's length and
    no longer: released, both land."""
    reporter, renamer, observer, reader = await sessions(4)
    task, checkpoints = await _paused(reporter, box, _files(20))
    renamer_pid = await _backend_pid(renamer)
    node = box.tree["box/sub/x.bin"]
    ctx = _ctx(box.org)

    async def rename() -> bytes:
        repo = FilesRepo(renamer, box.scope)
        async with repo.transaction():
            renamed = await Namespace(repo, ctx, SystemClock()).rename(
                NodeId(node.id),
                b"z.bin",
                if_match=int(node.etag),
                lease=held_by(ctx, box.epoch, INSTANCE),
            )
            return bytes(renamed.name)

    renaming = asyncio.ensure_future(rename())
    try:
        queued = await _wait_blocked(observer, renamer_pid, renaming)
    finally:
        checkpoints.release(WRITTEN)
    answer = await task
    assert queued, "the in-lease rename never waited for the batch holding its folder"
    assert await renaming == b"z.bin"
    assert answer.applied == 20
    assert await _live_names(reader, box.tree["box/sub"]) == {b"z.bin"}


async def test_a_batch_that_trashes_takes_the_drive_and_an_unrelated_rename_still_lands(
    box: Box, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """A delete of a folder goes to the trash, which takes the drive: the batch
    learns that inside its savepoint, starts again under the drive, and lands
    every entry. A rename elsewhere takes no drive and lands while it holds it;
    a create elsewhere, which does take it, is what that batch still delays."""
    reporter, renamer, creator, reader = await sessions(4)
    changes = [TreeChange(op="delete", path=b"old"), *_files(10)]
    task, checkpoints = await _paused(reporter, box, changes)
    try:
        await _quick(renamer, box, _rename(box, "home/a.txt", b"renamed.txt"))
        with pytest.raises(DBAPIError) as waited:
            await _quick(creator, box, _create(box, "home", b"made"))
        assert _sqlstate(waited.value) == LOCK_TIMEOUT
    finally:
        checkpoints.release(WRITTEN)
        answer = await task
    assert answer.applied == 11
    assert await _live_names(reader, box.tree["box"]) == {b"sub", b"src"}
    trashed = (
        await reader.execute(
            text("SELECT trashed_at IS NOT NULL FROM file_nodes WHERE id = :id"),
            {"id": box.tree["box/old"].id},
        )
    ).scalar_one()
    assert trashed
    assert await _live_names(reader, box.tree["home"]) == {b"renamed.txt", b"b.txt"}
