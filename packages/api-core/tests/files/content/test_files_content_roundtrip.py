"""Bytes in, the same bytes out — for every size class and every range boundary.

The oracle is never the code under test: each case is checked against the bytes
the test itself built and against digests computed by a second, independent
implementation (``hashlib.blake3`` through ``hash_bytes`` for the whole stream,
and a hand-written SHA-256-of-blocks for the block digest), so a bug in the
service's hashing cannot make the assertion agree with it.
"""

from __future__ import annotations

import hashlib
import os

import pytest
from alkera_core.config import settings
from alkera_core.files.hashing import BLOCK_BYTES
from alkera_core.files.ids import VersionId
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from blake3 import blake3
from tests.files.content.conftest import ContentRig, stream

INLINE_MAX = settings.files_inline_max_bytes


def payload_of(size: int) -> bytes:
    """Deterministic, incompressible-ish bytes so a truncation cannot pass."""
    if size == 0:
        return b""
    return (hashlib.sha256(b"content-roundtrip").digest() * (size // 32 + 1))[:size]


def expected_block_hash(payload: bytes) -> str:
    """SHA-256 over the concatenated 4 MiB block digests, computed independently."""
    digests = [
        hashlib.sha256(payload[offset : offset + BLOCK_BYTES]).digest()
        for offset in range(0, len(payload), BLOCK_BYTES)
    ]
    return hashlib.sha256(b"".join(digests)).hexdigest()


async def collect(rig: ContentRig, version_id: VersionId, span: tuple[int, int] | None) -> bytes:
    chunks = [chunk async for chunk in await rig.service.open(version_id, range=span)]
    return b"".join(chunks)


SIZE_CLASSES = [
    pytest.param(0, id="empty"),
    pytest.param(1, id="one-byte"),
    pytest.param(INLINE_MAX - 1, id="inline-max-minus-one"),
    pytest.param(INLINE_MAX, id="inline-max"),
    pytest.param(INLINE_MAX + 1, id="inline-max-plus-one"),
    pytest.param(BLOCK_BYTES + 3, id="over-one-block"),
]


@pytest.mark.parametrize("size", SIZE_CLASSES)
async def test_put_then_open_returns_the_same_bytes(content_rig: ContentRig, size: int) -> None:
    payload = payload_of(size)
    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(payload), size_declared=size, if_match=0
    )

    assert info.unchanged is False
    assert info.size == size
    assert info.content_hash == blake3(payload).digest().hex()
    assert info.block_hash == expected_block_hash(payload)
    assert await collect(content_rig, info.id, None) == payload


@pytest.mark.parametrize("size", SIZE_CLASSES)
async def test_the_size_class_decides_inline_versus_object(
    content_rig: ContentRig, size: int
) -> None:
    """At or below the cap the bytes are a column; past it they are one object."""
    payload = payload_of(size)
    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(payload), size_declared=size, if_match=0
    )

    version = await content_rig.session.get(FileVersion, info.id)
    assert version is not None
    if size <= INLINE_MAX:
        assert version.inline_bytes == payload
        assert version.store_key is None
    else:
        assert version.inline_bytes is None
        assert version.store_key is not None
        assert content_rig.object_path(version.store_key).read_bytes() == payload


async def test_an_empty_file_is_a_real_version_with_the_empty_hash(
    content_rig: ContentRig,
) -> None:
    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(b""), size_declared=0, if_match=0
    )

    assert info.content_hash == blake3(b"").digest().hex()
    # No blocks, so the block hash is SHA-256 of the empty string by construction.
    assert info.block_hash == hashlib.sha256(b"").hexdigest()
    version = await content_rig.session.get(FileVersion, info.id)
    assert version is not None
    assert version.inline_bytes == b""
    assert version.store_key is None
    assert await collect(content_rig, info.id, None) == b""


@pytest.mark.parametrize(
    "size",
    [
        pytest.param(INLINE_MAX, id="inline"),
        pytest.param(INLINE_MAX + 1024, id="object"),
        pytest.param(BLOCK_BYTES + 5, id="multi-block-object"),
    ],
)
async def test_range_reads_at_every_boundary(content_rig: ContentRig, size: int) -> None:
    payload = payload_of(size)
    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(payload), size_declared=size, if_match=0
    )

    end = size - 1
    spans = [
        (0, 0),
        (0, 1),
        (1, 1),
        (0, min(INLINE_MAX - 1, end)),
        (0, min(INLINE_MAX, end)),
        (min(INLINE_MAX, end), end),
        (min(INLINE_MAX + 1, end), end),
        (end - 1, end - 1),
        (end, end),
        (0, end),
    ]
    for start, stop in spans:
        got = await collect(content_rig, info.id, (start, stop))
        assert got == payload[start : stop + 1], f"range {(start, stop)} on size {size}"


async def test_the_head_swap_publishes_the_version_and_bumps_the_etag(
    content_rig: ContentRig,
) -> None:
    payload = payload_of(1024)
    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"), stream(payload), size_declared=1024, if_match=0
    )

    node = await content_rig.session.get(FileNode, content_rig.node_id("a.bin"))
    assert node is not None
    await content_rig.session.refresh(node)
    assert node.head_version_id == info.id
    assert node.etag == 1
    assert node.size == 1024


async def test_a_stale_if_match_refuses_before_anything_is_written(
    content_rig: ContentRig,
) -> None:
    from alkera_core.files.errors import PreconditionFailed

    with pytest.raises(PreconditionFailed):
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"), stream(b"x"), size_declared=1, if_match=7
        )

    assert content_rig.store.calls == []


async def test_more_bytes_than_declared_are_refused_and_the_hold_is_released(
    content_rig: ContentRig,
) -> None:
    """A 1 KiB declaration that streams far more stops at the declared boundary."""
    from alkera_core.files.errors import InvalidRequest
    from alkera_core.models.files.uploads import FileUploadSession

    async def liar():
        for _ in range(64):
            yield os.urandom(4096)

    with pytest.raises(InvalidRequest) as raised:
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"), liar(), size_declared=1024, if_match=0
        )
    assert raised.value.code == "files.size_mismatch"

    sessions = (
        await content_rig.session.execute(
            FileUploadSession.__table__.select().where(
                FileUploadSession.drive_id == content_rig.drive_uuid
            )
        )
    ).all()
    # The bytes are staged before the session opens, so a stream refused
    # while staging never took room on the drive at all.
    assert sessions == []
    node = await content_rig.session.get(FileNode, content_rig.node_id("a.bin"))
    assert node is not None
    await content_rig.session.refresh(node)
    assert node.head_version_id is None
