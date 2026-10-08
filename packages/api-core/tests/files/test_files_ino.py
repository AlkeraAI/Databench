"""Inode allocation: unique per drive, block-allocated, never reused."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.ino import DEFAULT_BLOCK, InoAllocator
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


async def _next_ino(session: AsyncSession, drive_id: uuid.UUID) -> int:
    value = (
        await session.execute(
            text("SELECT next_ino FROM file_drives WHERE id = :id"), {"id": drive_id}
        )
    ).scalar_one()
    return int(value)


async def test_a_block_bumps_next_ino_exactly_once(
    repo: FilesRepo, files_factory: FilesFactory, files_session: AsyncSession
) -> None:
    """A thousand inos cost one UPDATE, and the counter moves by exactly a block."""
    drive = await files_factory.drive()
    before = await _next_ino(files_session, drive.id)
    allocator = InoAllocator(repo)

    async with repo.transaction():
        handed = [await allocator.allocate(DriveId(drive.id)) for _ in range(DEFAULT_BLOCK)]

    after = await _next_ino(files_session, drive.id)
    assert after == before + DEFAULT_BLOCK
    assert len(set(handed)) == DEFAULT_BLOCK
    assert handed == sorted(handed)
    assert min(handed) >= before


async def test_a_block_boundary_takes_the_next_block(
    repo: FilesRepo, files_factory: FilesFactory, files_session: AsyncSession
) -> None:
    """The negative twin: one past the block is a second UPDATE, not a reused number."""
    drive = await files_factory.drive()
    before = await _next_ino(files_session, drive.id)
    allocator = InoAllocator(repo, block=4)

    async with repo.transaction():
        handed = [await allocator.allocate(DriveId(drive.id)) for _ in range(5)]

    assert len(set(handed)) == 5
    assert await _next_ino(files_session, drive.id) == before + 8


async def test_two_drives_have_independent_sequences(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org_factory: Any,
    files_session: AsyncSession,
) -> None:
    """Ino is per drive, so the same numbers live in both and neither moves the other."""
    first = await files_factory.drive()
    other_org = await files_org_factory()
    second = await files_factory.drive(org=other_org)
    other_repo = FilesRepo(files_session, OrgScope(org_team_id=other_org.org_team_id))

    async with repo.transaction():
        left = [await InoAllocator(repo).allocate(DriveId(first.id)) for _ in range(3)]
    async with other_repo.transaction():
        right = [await InoAllocator(other_repo).allocate(DriveId(second.id)) for _ in range(3)]

    assert left == right
    assert await _next_ino(files_session, first.id) == await _next_ino(files_session, second.id)


async def test_two_racing_allocators_never_share_a_number(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """Each reserver adds to what it finds, so the blocks cannot overlap.

    The second allocator is a second session, so it reads the first one's
    committed counter rather than its own uncommitted view of it, and each
    keeps its blocks apart, as two backend processes do.
    """
    drive = await files_factory.drive()
    other = AsyncSession(bind=files_engine, expire_on_commit=False)
    second_repo = FilesRepo(other, OrgScope(org_team_id=files_org.org_team_id))
    try:
        async with repo.transaction():
            mine = await InoAllocator(repo, cache={}).allocate(DriveId(drive.id))
        async with second_repo.transaction():
            theirs = await InoAllocator(second_repo, cache={}).allocate(DriveId(drive.id))
    finally:
        await other.close()

    assert theirs == mine + DEFAULT_BLOCK


async def test_a_purged_ino_is_never_handed_out_again(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    clock: object,
) -> None:
    """Deleting the highest node does not lower the counter."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ b/ c/", drive=drive)
    issued = {node.ino for node in made.values()}
    await files_session.execute(text("DELETE FROM file_nodes WHERE id = :id"), {"id": made["c"].id})
    await files_session.commit()

    async with repo.transaction():
        fresh = await InoAllocator(repo).allocate(DriveId(drive.id))

    assert fresh > max(issued)
    assert fresh not in issued


async def test_an_unknown_drive_is_refused(repo: FilesRepo) -> None:
    """The negative twin of the reservation: no drive, no ino."""
    async with repo.transaction():
        with pytest.raises(LookupError):
            await InoAllocator(repo).allocate(DriveId(uuid.uuid4()))


async def test_inos_are_unique_across_a_created_tree(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """Every node the namespace makes carries its own number."""
    from alkera_core.authz.enums import CredentialKind, PrincipalKind
    from alkera_core.authz.principal import ActingContext, Principal
    from alkera_core.files.clock import FakeClock
    from alkera_core.files.namespace import Namespace
    from tests.files._kit.factory import EPOCH

    drive = await files_factory.drive()
    ctx = ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(files_org.admin_id),
            org_id=files_org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )
    namespace = Namespace(repo, ctx, FakeClock(now=EPOCH), None)
    assert drive.root_node_id is not None

    async with repo.transaction():
        made = [
            await namespace.create(
                DriveId(drive.id),
                NodeId(drive.root_node_id),
                "file",
                f"f{index}".encode(),
                conflict="fail",
            )
            for index in range(12)
        ]

    inos = [node.ino for node in made]
    assert len(set(inos)) == len(inos)


async def test_a_spent_pool_reserves_in_the_callers_own_transaction(
    files_factory: FilesFactory, files_org: FilesOrg, files_engine: AsyncEngine
) -> None:
    """A reservation apart needs a second connection while the caller holds
    one. With the pool spent it would wait for a connection no caller like
    this one hands back, so the block is reserved in the caller's own
    transaction, at once."""
    drive = await files_factory.drive()
    one = create_async_engine(files_engine.url, pool_size=1, max_overflow=0, pool_timeout=2)
    session = AsyncSession(bind=one, expire_on_commit=False)
    try:
        repo = FilesRepo(session, OrgScope(org_team_id=files_org.org_team_id))
        async with repo.transaction():
            ino = await asyncio.wait_for(
                InoAllocator(repo, cache={}).allocate(DriveId(drive.id)), 10
            )
        assert ino >= 1
    finally:
        await session.close()
        await one.dispose()
