"""A content PUT inside a leased folder is fenced by the lease, not by hope.

The lease is what lets one machine own a folder's writes while everyone else
reads the last synced state. That only means anything if the *server* refuses
the writes the lease excludes, so these drive the real ``ContentService``
against a real lease row and assert on what the database holds afterwards: the
holder's epoch writes a version, a superseded epoch writes nothing at all, and
an unleased folder is untouched by any of it.
"""

from __future__ import annotations

import hashlib

import pytest
from alkera_core.config import settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import Lease, LeaseConflict, LeaseService
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import func, select
from tests.files._kit.fencing import held_by
from tests.files.content.conftest import ContentRig, stream

#: Past the inline cap so the whole publish path runs, not just the column write.
OBJECT_SIZE = settings.files_inline_max_bytes + 4096
PAYLOAD = (hashlib.sha256(b"fence").digest() * (OBJECT_SIZE // 32 + 1))[:OBJECT_SIZE]

HOLDER = "box-1"


async def folder_of(rig: ContentRig, name: str) -> NodeId:
    """The folder the test file lives in — the thing a mount actually leases."""
    async with rig.repo.transaction():
        node = await rig.repo.node(rig.node_id(name))
        assert node is not None
        assert node.parent_id is not None
        return NodeId(node.parent_id)


async def hold(rig: ContentRig, clock: FakeClock, folder: NodeId) -> Lease:
    async with rig.repo.transaction():
        service = LeaseService(rig.repo, rig.ctx, clock)
        return await service.acquire(folder, instance_id=HOLDER, machine_id="laptop")


async def version_count(rig: ContentRig, node_id: NodeId) -> int:
    async with rig.repo.transaction():
        rows = await rig.repo.session.execute(
            select(func.count()).select_from(FileVersion).where(FileVersion.node_id == node_id)
        )
        return int(rows.scalar_one())


async def etag_of(rig: ContentRig, node_id: NodeId) -> int:
    async with rig.repo.transaction():
        rows = await rig.repo.session.execute(select(FileNode.etag).where(FileNode.id == node_id))
        return int(rows.scalar_one())


async def test_the_holders_epoch_writes_a_version(
    content_rig: ContentRig, clock: FakeClock
) -> None:
    """The machine that holds the mount keeps writing, exactly as before."""
    node = content_rig.node_id("a.bin")
    lease = await hold(content_rig, clock, await folder_of(content_rig, "a.bin"))

    info = await content_rig.service.put_version(
        node,
        stream(PAYLOAD),
        size_declared=OBJECT_SIZE,
        if_match=0,
        lease=held_by(content_rig.ctx, lease.epoch, HOLDER),
    )

    assert info.unchanged is False
    assert info.size == OBJECT_SIZE
    assert await version_count(content_rig, node) == 1
    assert await etag_of(content_rig, node) == 1


@pytest.mark.parametrize(
    ("epoch_shift", "instance"),
    [
        pytest.param(-1, HOLDER, id="a-superseded-epoch"),
        pytest.param(1, HOLDER, id="an-epoch-that-was-never-issued"),
        pytest.param(0, "box-2", id="another-instance-under-the-right-epoch"),
    ],
)
async def test_a_stale_epoch_is_fenced_and_writes_nothing(
    content_rig: ContentRig, clock: FakeClock, epoch_shift: int, instance: str
) -> None:
    """A fenced writer leaves the node byte-for-byte as it found it.

    The assertion is on the rows, not on the exception alone: a refusal that
    still appended a version — or bumped the etag — would be worse than no fence
    at all, because the holder's next sync would silently overwrite it.
    """
    node = content_rig.node_id("a.bin")
    lease = await hold(content_rig, clock, await folder_of(content_rig, "a.bin"))
    before = await etag_of(content_rig, node)

    with pytest.raises(LeaseConflict) as caught:
        await content_rig.service.put_version(
            node,
            stream(PAYLOAD),
            size_declared=OBJECT_SIZE,
            if_match=0,
            lease=held_by(content_rig.ctx, lease.epoch + epoch_shift, instance),
        )

    assert caught.value.code == "files.lease_fenced"
    assert await version_count(content_rig, node) == 0
    assert await etag_of(content_rig, node) == before


async def test_no_epoch_inside_somebody_elses_mount_is_refused(
    content_rig: ContentRig, clock: FakeClock
) -> None:
    """A plain write into a leased folder is ``files.leased``, not ``lease_fenced``.

    The two refusals are different on purpose: "someone has this folder" is a
    thing a client offers to resolve, "you have been superseded" is a thing it
    must stop writing about.
    """
    node = content_rig.node_id("a.bin")
    await hold(content_rig, clock, await folder_of(content_rig, "a.bin"))

    with pytest.raises(LeaseConflict) as caught:
        await content_rig.service.put_version(
            node, stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0, lease=None
        )

    assert caught.value.code == "files.leased"
    assert await version_count(content_rig, node) == 0


async def test_an_unleased_folder_takes_every_write(content_rig: ContentRig) -> None:
    """No lease anywhere above the node means no fence — with or without headers.

    The negative twin of the three above: if the fence refused a write merely
    because the caller named no epoch, every ordinary upload in the product
    would 409.
    """
    node = content_rig.node_id("a.bin")

    plain = await content_rig.service.put_version(
        node, stream(PAYLOAD), size_declared=OBJECT_SIZE, if_match=0, lease=None
    )
    assert plain.unchanged is False

    # An epoch nobody issued is equally harmless when nothing is leased: the
    # fence is a property of the FOLDER, not of the headers.
    claimed = await content_rig.service.put_version(
        content_rig.node_id("b.bin"),
        stream(PAYLOAD),
        size_declared=OBJECT_SIZE,
        if_match=0,
        lease=held_by(content_rig.ctx, 999, HOLDER),
    )
    assert claimed.unchanged is False
    assert await version_count(content_rig, content_rig.node_id("b.bin")) == 1
