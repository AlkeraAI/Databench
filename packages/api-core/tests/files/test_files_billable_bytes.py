"""``billable_bytes`` is a function of the org's own rows, and of nothing else."""

from __future__ import annotations

import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from alkera_core.files import billing
from alkera_core.files.errors import NotFound
from alkera_core.files.ids import DomainId, DriveId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

#: Bytes big enough that a double count is unmistakable in the total.
KIB: int = 1024


async def _write(
    session: AsyncSession,
    node: FileNode,
    *,
    content_hash: str,
    size: int,
    seq: int = 1,
    store_key: str | None = None,
    inline: bytes | None = None,
) -> FileVersion:
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=node.org_team_id,
        node_id=node.id,
        seq=seq,
        size_bytes=size,
        content_hash=content_hash,
        source="upload",
        store_key=store_key,
        inline_bytes=inline,
        created_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    node.head_version_id = version.id
    await session.commit()
    return version


async def _recomputed(session: AsyncSession, org: FilesOrg) -> int:
    """The same number, computed from the tables by the test's own query.

    Deliberately a different shape from the implementation's: it pulls every
    row and folds them in Python, so a bug in the SQL grouping shows up as a
    disagreement rather than being mirrored.
    """
    rows = await session.execute(
        select(FileVersion.content_hash, FileVersion.size_bytes).where(
            FileVersion.org_team_id == org.org_team_id
        )
    )
    by_hash: dict[str, int] = {}
    for content_hash, size in rows.all():
        by_hash[content_hash] = max(by_hash.get(content_hash, 0), size)
    return sum(by_hash.values())


async def _usage(repo: FilesRepo, drive_id: DriveId) -> billing.BillableUsage:
    """Every repo read runs inside a transaction — that is where the app role
    and the org setting are applied, so a caller cannot read unscoped."""
    async with repo.transaction():
        return await billing.billable_bytes(repo, drive_id)


async def _physical(repo: FilesRepo, domain_id: DomainId) -> int:
    async with repo.transaction():
        return await billing.physical_bytes(repo, domain_id)


async def test_the_same_bytes_in_two_orgs_charge_both_in_full(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """Cross-org dedup is the platform's margin, never a discount on a bill."""
    other = await files_org_factory()
    shared_hash = "blake3:the-same-dataset"

    mine_drive = await files_factory.drive()
    mine = (await files_factory.tree("data.csv", drive=mine_drive))["data.csv"]
    await _write(files_session, mine, content_hash=shared_hash, size=64 * KIB)

    their_drive = await files_factory.drive(org=other)
    theirs = (await files_factory.tree("data.csv", drive=their_drive))["data.csv"]
    await _write(files_session, theirs, content_hash=shared_hash, size=64 * KIB)

    mine_usage = await _usage(repo, DriveId(mine_drive.id))
    their_repo = FilesRepo(files_session, other.scope)
    their_usage = await _usage(their_repo, DriveId(their_drive.id))

    assert mine_usage.logical_bytes == 64 * KIB
    assert their_usage.logical_bytes == 64 * KIB


async def test_the_same_bytes_twice_in_one_org_charge_once(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """Two members with the same dataset pay for it once, and the count says why."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("alice.csv bob.csv", drive=drive)
    shared_hash = "blake3:the-same-dataset"
    await _write(files_session, tree["alice.csv"], content_hash=shared_hash, size=64 * KIB)
    await _write(files_session, tree["bob.csv"], content_hash=shared_hash, size=64 * KIB)

    usage = await _usage(repo, DriveId(drive.id))

    assert usage.logical_bytes == 64 * KIB
    assert usage.distinct_contents == 1


async def test_two_different_files_are_charged_separately(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The negative twin of the dedup case: distinct bytes are distinct money."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("alice.csv bob.csv", drive=drive)
    await _write(files_session, tree["alice.csv"], content_hash="blake3:a", size=64 * KIB)
    await _write(files_session, tree["bob.csv"], content_hash="blake3:b", size=64 * KIB)

    usage = await _usage(repo, DriveId(drive.id))

    assert usage.logical_bytes == 128 * KIB
    assert usage.distinct_contents == 2


async def test_deleting_in_one_org_never_moves_another_orgs_number(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A bill that moved with a stranger's deletes would be a side channel."""
    other = await files_org_factory()
    shared_hash = "blake3:the-same-dataset"

    mine_drive = await files_factory.drive()
    mine = (await files_factory.tree("data.csv", drive=mine_drive))["data.csv"]
    await _write(files_session, mine, content_hash=shared_hash, size=64 * KIB)

    their_drive = await files_factory.drive(org=other)
    theirs = (await files_factory.tree("data.csv", drive=their_drive))["data.csv"]
    their_version = await _write(files_session, theirs, content_hash=shared_hash, size=64 * KIB)

    before = await _usage(repo, DriveId(mine_drive.id))

    theirs.head_version_id = None
    await files_session.flush()
    await files_session.delete(their_version)
    await files_session.commit()

    after = await _usage(repo, DriveId(mine_drive.id))
    assert after == before


async def test_trashed_bytes_still_count_and_restoring_does_not_double_them(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """Trash is storage until it is purged, and a restore adds no rows."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("data.csv", drive=drive))["data.csv"]
    await _write(files_session, node, content_hash="blake3:a", size=64 * KIB)
    live = await _usage(repo, DriveId(drive.id))

    node.trashed_at = datetime.now(UTC)
    await files_session.commit()
    trashed = await _usage(repo, DriveId(drive.id))

    node.trashed_at = None
    await files_session.commit()
    restored = await _usage(repo, DriveId(drive.id))

    assert trashed.logical_bytes == live.logical_bytes == 64 * KIB
    assert restored.logical_bytes == 64 * KIB


async def test_inline_bytes_are_billed_like_any_other_content(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A file under the inline cap never touches the store, and is still storage."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("small.txt big.bin", drive=drive)
    payload = b"x" * 512
    await _write(
        files_session,
        tree["small.txt"],
        content_hash="blake3:small",
        size=len(payload),
        inline=payload,
    )
    await _write(
        files_session,
        tree["big.bin"],
        content_hash="blake3:big",
        size=64 * KIB,
        store_key="objects/big",
    )

    usage = await _usage(repo, DriveId(drive.id))

    assert usage.logical_bytes == 512 + 64 * KIB


async def test_retained_versions_are_billed_alongside_the_head(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """History is storage: the version chain is on the bill, not only the head."""
    drive = await files_factory.drive()
    node = (await files_factory.tree("data.csv", drive=drive))["data.csv"]
    await _write(files_session, node, content_hash="blake3:v1", size=10 * KIB, seq=1)
    await _write(files_session, node, content_hash="blake3:v2", size=20 * KIB, seq=2)

    usage = await _usage(repo, DriveId(drive.id))

    assert usage.logical_bytes == 30 * KIB


async def test_billing_a_drive_the_org_does_not_own_is_not_found(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A caller cannot bill an org for a drive that is not the org's."""
    other = await files_org_factory()
    foreign: FileDrive = await files_factory.drive(org=other)

    async with repo.transaction():
        with pytest.raises(NotFound):
            await billing.billable_bytes(repo, DriveId(foreign.id))


async def test_physical_bytes_counts_distinct_store_objects(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """Cost of goods counts objects; the bill counts content. They differ."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("alice.csv bob.csv carol.csv", drive=drive)
    # Two nodes share one object; the third has its own.
    await _write(
        files_session,
        tree["alice.csv"],
        content_hash="blake3:shared",
        size=64 * KIB,
        store_key="objects/shared",
    )
    await _write(
        files_session,
        tree["bob.csv"],
        content_hash="blake3:shared",
        size=64 * KIB,
        store_key="objects/shared",
    )
    await _write(
        files_session,
        tree["carol.csv"],
        content_hash="blake3:other",
        size=32 * KIB,
        store_key="objects/other",
    )

    assert await _physical(repo, DomainId(drive.dedup_domain_id)) == 96 * KIB
    assert (await _usage(repo, DriveId(drive.id))).logical_bytes == 96 * KIB


async def test_physical_bytes_rejects_a_domain_outside_the_org(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    other = await files_org_factory()
    foreign = await files_factory.drive(org=other)

    async with repo.transaction():
        with pytest.raises(NotFound):
            await billing.physical_bytes(repo, DomainId(foreign.dedup_domain_id))


@pytest.mark.parametrize("seed", [1, 7, 99])
async def test_billable_bytes_matches_an_independent_sum_over_a_random_corpus(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    repo: FilesRepo,
    seed: int,
) -> None:
    """Over a corpus of uploads, trashes and restores the two sums agree.

    A neighbouring org churns its own corpus throughout, so the property also
    pins the isolation: nothing that org does may move this one's number.
    """
    rng = random.Random(seed)
    other = await files_org_factory()
    drive = await files_factory.drive()
    noisy_drive = await files_factory.drive(org=other)
    names = " ".join(f"f{index}.bin" for index in range(8))
    tree = await files_factory.tree(names, drive=drive)
    noisy = await files_factory.tree(names, drive=noisy_drive)
    nodes = list(tree.values())
    noisy_nodes = list(noisy.values())
    # A small hash alphabet, so collisions (the dedup path) actually happen.
    hashes = [f"blake3:{letter}" for letter in "abcd"]
    sizes = {content_hash: (index + 1) * KIB for index, content_hash in enumerate(hashes)}
    seqs: dict[uuid.UUID, int] = {}

    for _ in range(40):
        node = rng.choice(nodes)
        action = rng.choice(["upload", "upload", "trash", "restore"])
        if action == "upload":
            content_hash = rng.choice(hashes)
            seqs[node.id] = seqs.get(node.id, 0) + 1
            await _write(
                files_session,
                node,
                content_hash=content_hash,
                size=sizes[content_hash],
                seq=seqs[node.id],
            )
        elif action == "trash":
            node.trashed_at = datetime.now(UTC)
            await files_session.commit()
        else:
            node.trashed_at = None
            await files_session.commit()

        # The neighbour writes the same hashes into its own rows every round.
        stranger = rng.choice(noisy_nodes)
        stranger_hash = rng.choice(hashes)
        seqs[stranger.id] = seqs.get(stranger.id, 0) + 1
        await _write(
            files_session,
            stranger,
            content_hash=stranger_hash,
            size=sizes[stranger_hash],
            seq=seqs[stranger.id],
        )

        usage = await _usage(repo, DriveId(drive.id))
        assert usage.logical_bytes == await _recomputed(files_session, files_org)

    final = await _usage(repo, DriveId(drive.id))
    assert final.logical_bytes == await _recomputed(files_session, files_org)
    assert final.logical_bytes > 0
