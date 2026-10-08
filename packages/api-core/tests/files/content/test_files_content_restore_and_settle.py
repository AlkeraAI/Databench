"""What a head swap owes the rest of the system once the bytes are already stored.

Three obligations, none of which the upload path can discharge for it:

* a restored version describes the **same bytes**, so it has to carry the
  per-block digests of the row it reinstates — otherwise every read of it is
  unverifiable and, since the reader fails closed, simply refused;
* a head swap changes the drive's logical size without ever taking a quota
  hold, so the ``file_dir_stats`` cache the quota decision reads has to be told
  about it or it drifts from a recount the moment a restore lands;
* the transaction that commits bytes is the transaction that has to decide the
  freeze, because everything that runs after it reads the number it wrote.

Every assertion here is read back from Postgres or from the bytes a consumer
actually collected, never from a return value the service just built.
"""

from __future__ import annotations

import asyncio

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import Conflict
from alkera_core.files.hashing import BLOCK_BYTES
from alkera_core.files.ids import DriveId, VersionId
from alkera_core.files.quota import FROZEN_CODE, OVER_QUOTA, QuotaService
from alkera_core.models.files.history import FileDirStats, FileDirStatsDelta
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select, update
from tests.files.content.conftest import ContentRig, content_ctx, stream

#: Two whole blocks and a tail, so "the digests were carried" is a statement
#: about a tuple with more than one entry in it.
BIG = 2 * BLOCK_BYTES + 11


def payload_of(size: int, seed: int) -> bytes:
    """Bytes no other case in this module produces, so a mix-up cannot pass."""
    return (bytes((seed + index) % 251 for index in range(251)) * (size // 251 + 1))[:size]


async def collect(rig: ContentRig, version_id: VersionId) -> bytes:
    return b"".join([chunk async for chunk in await rig.service.open(version_id)])


async def root_id_of(rig: ContentRig) -> object:
    return (
        await rig.session.execute(
            select(FileDrive.root_node_id).where(FileDrive.id == rig.drive_uuid)
        )
    ).scalar_one()


async def cached_root_bytes(rig: ContentRig) -> int:
    """The number the quota decision reads.

    The folded drive-root row PLUS the deltas the aggregator has not folded yet.
    The write path charges the changed node's PARENT and never the root, so the
    folded row alone would read zero here and pin the aggregator's cadence
    rather than the accounting.
    """
    root_id = await root_id_of(rig)
    folded = (
        await rig.session.execute(select(FileDirStats.bytes).where(FileDirStats.node_id == root_id))
    ).scalar_one_or_none()
    return (0 if folded is None else int(folded)) + sum(await delta_rows(rig))


async def delta_rows(rig: ContentRig) -> list[int]:
    """Every ``bytes_delta`` appended anywhere in the drive, oldest first."""
    rows = await rig.session.execute(
        select(FileDirStatsDelta.bytes_delta)
        .join(FileNode, FileNode.id == FileDirStatsDelta.node_id)
        .where(FileNode.drive_id == rig.drive_uuid)
        .order_by(FileDirStatsDelta.id)
    )
    return [int(value) for value in rows.scalars().all()]


async def set_quota(rig: ContentRig, quota_bytes: int) -> None:
    await rig.session.execute(
        update(FileDrive.__table__)
        .where(FileDrive.id == rig.drive_uuid)
        .values(quota_bytes=quota_bytes)
    )


async def frozen_reason(rig: ContentRig) -> str | None:
    reason = (
        await rig.session.execute(
            select(FileDrive.frozen_reason).where(FileDrive.id == rig.drive_uuid)
        )
    ).scalar_one()
    return None if reason is None else str(reason)


async def etag_of(rig: ContentRig, name: str) -> int:
    """The node's current etag, re-read: a publish bumps the row under us."""
    node = await rig.session.get(FileNode, rig.node_id(name))
    assert node is not None
    await rig.session.refresh(node)
    return int(node.etag)


# ---- the restored row describes the bytes it reinstates ---------------------


@pytest.mark.parametrize(
    "size",
    [pytest.param(1024, id="inline"), pytest.param(BIG, id="three-blocks")],
)
async def test_a_restored_version_is_readable_because_it_carries_the_block_digests(
    content_rig: ContentRig, size: int
) -> None:
    """Restore, then read: the bytes come back and every block was checked.

    The read is the assertion. A restored row with no ``block_hashes`` has
    nothing to check its blocks against, and the verifier refuses rather than
    handing over bytes it cannot vouch for — so a restore that drops the
    digests makes its own version permanently unreadable.
    """
    first = payload_of(size, seed=3)
    second = payload_of(size + 7, seed=91)
    node_id = content_rig.node_id("a.bin")
    original = await content_rig.service.put_version(
        node_id,
        stream(first, chunk=64 * 1024),
        size_declared=len(first),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    await content_rig.service.put_version(
        node_id,
        stream(second, chunk=64 * 1024),
        size_declared=len(second),
        if_match=await etag_of(content_rig, "a.bin"),
    )

    async with content_rig.repo.transaction():
        _, made = await content_rig.service.restore_version(node_id, original.id)

    assert await collect(content_rig, VersionId(made.id)) == first
    old = await content_rig.session.get(FileVersion, original.id)
    assert old is not None
    assert made.version_metadata["block_hashes"] == old.version_metadata["block_hashes"]
    assert made.version_metadata["dedup_domain_id"] == old.version_metadata["dedup_domain_id"]
    # Provenance is re-derived, never inherited: the restore still says what it
    # reinstated and when, on top of the carried digests.
    assert made.version_metadata["restored_from"] == str(original.id)
    assert made.version_metadata["restored_seq"] == old.seq


# ---- the head swap is accounted --------------------------------------------


async def test_a_restore_appends_the_dir_stats_delta_its_head_swap_causes(
    content_rig: ContentRig,
) -> None:
    """After a restore the cached size equals the head it just published.

    The upload path settles a hold; a restore never opens a session, so if it
    does not append its own delta the cache keeps the larger version's size
    forever and the quota decision reads a number no recount agrees with.
    """
    small = payload_of(400, seed=5)
    large = payload_of(1500, seed=17)
    node_id = content_rig.node_id("a.bin")
    base = await cached_root_bytes(content_rig)
    first = await content_rig.service.put_version(
        node_id,
        stream(small),
        size_declared=len(small),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    await content_rig.service.put_version(
        node_id,
        stream(large),
        size_declared=len(large),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    assert await cached_root_bytes(content_rig) == base + len(large)

    async with content_rig.repo.transaction():
        await content_rig.service.restore_version(node_id, first.id)

    assert await cached_root_bytes(content_rig) == base + len(small)
    # The fold is a cache; the append-only row is the truth the Layer 8
    # aggregator replays, so the shrink has to be in the deltas as well.
    assert (len(small) - len(large)) in await delta_rows(content_rig)


async def test_a_restore_that_puts_the_drive_over_its_ceiling_freezes_it(
    content_rig: ContentRig,
) -> None:
    """A restore commits bytes, so it owns the freeze decision for them."""
    small = payload_of(400, seed=5)
    large = payload_of(1500, seed=17)
    node_id = content_rig.node_id("a.bin")
    first = await content_rig.service.put_version(
        node_id,
        stream(large),
        size_declared=len(large),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    await content_rig.service.put_version(
        node_id,
        stream(small),
        size_declared=len(small),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    await set_quota(content_rig, await cached_root_bytes(content_rig))
    assert await frozen_reason(content_rig) is None

    async with content_rig.repo.transaction():
        await content_rig.service.restore_version(node_id, first.id)

    assert await frozen_reason(content_rig) == OVER_QUOTA


async def test_a_restore_back_under_the_ceiling_thaws_the_drive(
    content_rig: ContentRig, clock: FakeClock
) -> None:
    """The same seam runs the other way, so a shrink is not a one-way freeze."""
    small = payload_of(400, seed=5)
    large = payload_of(1500, seed=17)
    node_id = content_rig.node_id("a.bin")
    first = await content_rig.service.put_version(
        node_id,
        stream(small),
        size_declared=len(small),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    await content_rig.service.put_version(
        node_id,
        stream(large),
        size_declared=len(large),
        if_match=await etag_of(content_rig, "a.bin"),
    )
    await set_quota(content_rig, await cached_root_bytes(content_rig) - 1)
    quota = QuotaService(content_rig.repo, content_ctx(), clock)
    assert await quota.freeze_if_over(DriveId(content_rig.drive_uuid)) is True

    async with content_rig.repo.transaction():
        await content_rig.service.restore_version(node_id, first.id)

    assert await frozen_reason(content_rig) is None


# ---- the committing transaction decides the freeze --------------------------


async def test_the_commit_that_takes_the_drive_over_its_ceiling_freezes_it(
    content_rig: ContentRig,
) -> None:
    """A quota lowered while the bytes stream still fences the next write.

    The reserve passed against the old ceiling, so the only place left to catch
    it is the transaction that commits the bytes. If that transaction does not
    freeze, every write until the janitor's next pass is accepted against a
    ceiling the drive is already past — which is the whole reason the check
    lives on the commit rather than in a sweep.
    """
    payload = payload_of(1200, seed=41)
    node_id = content_rig.node_id("a.bin")
    content_rig.checkpoints.pause("content.before_commit")
    put = asyncio.create_task(
        content_rig.service.put_version(
            node_id,
            stream(payload),
            size_declared=len(payload),
            if_match=await etag_of(content_rig, "a.bin"),
        )
    )
    await content_rig.checkpoints.wait_paused("content.before_commit")
    await set_quota(content_rig, len(payload) - 1)
    content_rig.checkpoints.release("content.before_commit")
    await put

    assert await frozen_reason(content_rig) == OVER_QUOTA
    # And the freeze is not decoration: the next write is refused by it.
    with pytest.raises(Conflict) as refusal:
        await content_rig.service.put_version(
            content_rig.node_id("b.bin"),
            stream(b"x"),
            size_declared=1,
            if_match=await etag_of(content_rig, "b.bin"),
        )
    assert refusal.value.code == FROZEN_CODE


async def test_a_commit_inside_the_ceiling_leaves_the_drive_unfrozen(
    content_rig: ContentRig,
) -> None:
    """The negative twin: landing exactly on the ceiling is not over it."""
    payload = payload_of(1200, seed=41)
    node_id = content_rig.node_id("a.bin")
    content_rig.checkpoints.pause("content.before_commit")
    put = asyncio.create_task(
        content_rig.service.put_version(
            node_id,
            stream(payload),
            size_declared=len(payload),
            if_match=await etag_of(content_rig, "a.bin"),
        )
    )
    await content_rig.checkpoints.wait_paused("content.before_commit")
    await set_quota(content_rig, await cached_root_bytes(content_rig) + len(payload))
    content_rig.checkpoints.release("content.before_commit")
    await put

    assert await frozen_reason(content_rig) is None
