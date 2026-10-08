"""Resolving an org's drive must not queue behind whoever holds the drive row.

Every Files request resolves the org's drive before it does anything else. The
drive row is also the row every writer advances — inode blocks, the quota
counters — so a long write holds it for as long as its transaction runs. If
resolving the drive writes to (or locks) that row, one member's long copy parks
every other request of the org behind it, each pinning a pooled connection, and
the pool is what every tenant shares.

The holder here is a second real session with an uncommitted update on the
drive row — exactly what a running copy looks like to the rest of the org. The
session under test runs with a short ``lock_timeout``, so "would have waited"
is a raised ``55P03`` rather than a hang or a sleep.
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
from alkera_core.files import drives
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.files._kit.factory import FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _store(session: AsyncSession) -> uuid.UUID:
    store = FileStore(
        id=uuid.uuid4(),
        driver="filesystem",
        bucket="",
        endpoint=f"/tmp/files-test/{uuid.uuid4().hex}",
        region="",
        capabilities={},
        transfer_modes=["single"],
    )
    session.add(store)
    await session.commit()
    return store.id


async def _next_ino(session: AsyncSession, drive_id: uuid.UUID) -> int:
    row = await session.execute(
        text("SELECT next_ino FROM file_drives WHERE id = :d"), {"d": drive_id}
    )
    return int(row.scalar_one())


async def test_resolving_an_existing_drive_does_not_wait_on_the_drive_rows_holder(
    files_session: AsyncSession,
    files_org: FilesOrg,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    store_id = await _store(files_session)
    builder, holder, reader = await sessions(3)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    ctx = _ctx(files_org)

    build_repo = FilesRepo(builder, scope)
    async with build_repo.transaction():
        built = await drives.ensure_org_drive(
            build_repo, ctx, files_org.org_team_id, store_id=store_id
        )
        # Read inside the transaction: its commit expires the instance.
        drive_id, root_id = built.id, built.root_node_id

    # A long writer: the drive row is updated and the transaction stays open.
    await holder.execute(
        text("UPDATE file_drives SET next_ino = next_ino + 1 WHERE id = :d"), {"d": drive_id}
    )
    try:
        read_repo = FilesRepo(reader, scope)
        async with read_repo.transaction():
            # A statement that has to wait for the holder fails instead of
            # hanging, so a regression is a raised lock timeout, not a stuck test.
            await reader.execute(text("SET LOCAL lock_timeout = '300ms'"))
            resolved = await drives.ensure_org_drive(
                read_repo, ctx, files_org.org_team_id, store_id=store_id
            )
            answer = (resolved.id, resolved.root_node_id)
        assert answer == (drive_id, root_id)
    finally:
        await holder.rollback()


async def test_resolving_an_existing_drive_spends_no_inode_numbers(
    files_session: AsyncSession,
    files_org: FilesOrg,
) -> None:
    """The counter is the org's hottest row: a request that only needs to know
    the drive exists must leave it exactly where it found it."""
    store_id = await _store(files_session)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    ctx = _ctx(files_org)
    repo = FilesRepo(files_session, scope)
    async with repo.transaction():
        built = await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    before = await _next_ino(files_session, built.id)
    await files_session.commit()

    for _ in range(3):
        async with repo.transaction():
            again = await drives.ensure_org_drive(
                repo, ctx, files_org.org_team_id, store_id=store_id
            )
        assert again.id == built.id

    assert await _next_ino(files_session, built.id) == before
    await files_session.commit()


async def test_a_half_built_skeleton_is_still_completed(
    files_session: AsyncSession,
    files_org: FilesOrg,
) -> None:
    """The read-only answer is only for a WHOLE drive: one that lost a skeleton
    folder between the drive row and ``/Teams`` is repaired, not returned."""
    store_id = await _store(files_session)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    ctx = _ctx(files_org)
    repo = FilesRepo(files_session, scope)
    async with repo.transaction():
        built = await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    await files_session.execute(
        # No longer live, which is all "missing" means to the skeleton: the
        # history row the folder owns keeps the row itself from being deleted.
        text("UPDATE file_nodes SET trashed_at = now() WHERE parent_id = :root AND name = :name"),
        {"root": built.root_node_id, "name": drives.TEAMS_NAME},
    )
    await files_session.commit()

    async with repo.transaction():
        await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
    names = (
        await files_session.execute(
            text("SELECT name FROM file_nodes WHERE parent_id = :root AND trashed_at IS NULL"),
            {"root": built.root_node_id},
        )
    ).scalars()
    assert {bytes(name) for name in names} == {
        drives.SHARED_NAME,
        drives.HOME_NAME,
        drives.TEAMS_NAME,
    }
    await files_session.commit()


#: How many people create a folder in one drive at the same moment.
WRITERS = 20
#: How often the watcher reads the lock graph while the writers run. Only the
#: cadence: the verdict is the graph, never how long anything took.
WATCH_EVERY_S = 0.05


async def test_drive_writes_do_not_serialize(
    files_session: AsyncSession,
    files_org: FilesOrg,
) -> None:
    """Twenty creates in twenty different folders of one drive, each holding its
    transaction open until all twenty have created (a request's transaction
    lasts to its end): no create ever waits on a lock another create holds.

    A create used to lock the org's one drive row and keep it to its commit,
    so the first create held the row while the other nineteen queued on it.
    That queue is read off Postgres' own lock graph: a writer still creating
    that ``pg_blocking_pids`` says is blocked by a writer parked on the
    barrier is waiting for a commit that cannot come until it finishes. A
    lock taken and let go inside a statement (a relation extension, a page
    of an index) never has a parked holder, so a slow machine is not a
    finding, and nothing here is bounded by the clock."""
    store_id = await _store(files_session)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    ctx = _ctx(files_org)
    repo = FilesRepo(files_session, scope)
    async with repo.transaction():
        built = await drives.ensure_org_drive(repo, ctx, files_org.org_team_id, store_id=store_id)
        drive_id = DriveId(built.id)
        shared = NodeId(
            (
                await files_session.execute(
                    text(
                        "SELECT id FROM file_nodes WHERE parent_id = :root AND name = :name "
                        "AND trashed_at IS NULL"
                    ),
                    {"root": built.root_node_id, "name": drives.SHARED_NAME},
                )
            ).scalar_one()
        )
    parents: list[NodeId] = []
    for index in range(WRITERS):
        async with repo.transaction():
            made = await Namespace(repo, ctx, SystemClock()).create(
                drive_id, shared, "folder", f"folder-{index}".encode()
            )
            parents.append(NodeId(made.id))

    engine = create_async_engine(settings.database_url, pool_size=WRITERS + 4, max_overflow=0)
    everyone_created = asyncio.Barrier(WRITERS)
    creating: set[int] = set()
    parked: set[int] = set()

    async def create_in(parent: NodeId) -> uuid.UUID:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            writer = FilesRepo(session, scope)
            try:
                async with writer.transaction():
                    pid = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
                    creating.add(pid)
                    created = await Namespace(writer, ctx, SystemClock()).create(
                        drive_id, parent, "folder", b"made"
                    )
                    creating.discard(pid)
                    parked.add(pid)
                    await everyone_created.wait()
                    return created.id
            except BaseException:
                # The others would wait on the barrier for a writer that is gone.
                await everyone_created.abort()
                raise

    async def queued_on_a_parked_writer(
        writers: asyncio.Future[Any],
    ) -> list[tuple[int, list[int]]]:
        async with engine.connect() as watcher:
            live = await watcher.execution_options(isolation_level="AUTOCOMMIT")
            while not writers.done():
                rows = (
                    await live.execute(
                        text(
                            "SELECT p, pg_blocking_pids(p) FROM unnest(CAST(:pids AS int[])) AS p"
                        ),
                        {"pids": sorted(creating)},
                    )
                ).all()
                queued = [(int(pid), list(by)) for pid, by in rows if set(by) & parked]
                if queued:
                    await everyone_created.abort()
                    return queued
                await asyncio.wait({writers}, timeout=WATCH_EVERY_S)
        return []

    try:
        writers = asyncio.gather(*(create_in(parent) for parent in parents), return_exceptions=True)
        queued = await queued_on_a_parked_writer(writers)
        landed = await writers
    finally:
        await engine.dispose()
    assert queued == [], f"creates queued on a create that holds its lock to commit: {queued}"
    failed = [one for one in landed if isinstance(one, BaseException)]
    assert failed == []
    assert len(set(landed)) == WRITERS
