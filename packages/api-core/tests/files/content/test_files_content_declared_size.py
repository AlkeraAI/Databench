"""A declared size is a budget, not a hint: the stream stops the byte it is broken.

A client that declares 1 MB and then streams 1 GB is the cheapest denial of
service there is — the quota hold was taken against the declaration, so reading
the body to its end would let one request write a gigabyte the org never had
room for. The refusal therefore has to happen *at* the boundary, with the rest of
the body never pulled off the socket, and it has to leave nothing behind: no
version, no held bytes, and a session that is closed rather than left open.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from alkera_core.files.clock import FakeClock
from alkera_core.files.errors import InvalidRequest
from alkera_core.files.quota import QuotaService
from alkera_core.models.files.uploads import FileUploadSession
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import func, select
from tests.files.content.conftest import ContentRig, content_ctx

CHUNK = 64 * 1024
DECLARED = 1_000_000
STREAMED = 2_000_000

#: The first chunk boundary that carries the stream past ``DECLARED``: 15 chunks
#: still fit, the 16th is the one that must be refused.
CHUNKS_UNTIL_REFUSAL = DECLARED // CHUNK + 1


class CountingStream:
    """A body that records exactly how much of it the service pulled."""

    def __init__(self, total: int, chunk: int = CHUNK) -> None:
        self._total = total
        self._chunk = chunk
        self.chunks = 0
        self.bytes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        sent = 0
        while sent < self._total:
            size = min(self._chunk, self._total - sent)
            self.chunks += 1
            self.bytes += size
            sent += size
            yield bytes(size)


async def usage_held(rig: ContentRig, clock: FakeClock) -> int:
    quota = QuotaService(rig.repo, content_ctx(), clock, rig.store)
    async with rig.repo.transaction():
        return (await quota.usage(rig.drive_id)).held_bytes


async def count_versions(rig: ContentRig, name: str) -> int:
    rows = await rig.session.execute(
        select(func.count())
        .select_from(FileVersion)
        .where(FileVersion.node_id == rig.node_id(name))
    )
    return int(rows.scalar_one())


async def session_states(rig: ContentRig) -> list[str]:
    rows = await rig.session.execute(
        select(FileUploadSession.state).where(FileUploadSession.drive_id == rig.drive_uuid)
    )
    return sorted(rows.scalars().all())


async def test_an_over_long_stream_is_refused_at_the_declared_boundary(
    content_rig: ContentRig,
) -> None:
    node = content_rig.files["a.bin"]
    body = CountingStream(STREAMED)

    with pytest.raises(InvalidRequest) as caught:
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"),
            body.__aiter__(),
            size_declared=DECLARED,
            if_match=node.etag,
        )

    assert caught.value.code == "files.size_mismatch"
    # The refusal lands on the first chunk that crosses the declaration, and the
    # remaining ~1 MB of the body is never read.
    assert body.chunks == CHUNKS_UNTIL_REFUSAL
    assert body.bytes == CHUNKS_UNTIL_REFUSAL * CHUNK
    assert body.bytes > DECLARED
    assert (body.chunks - 1) * CHUNK <= DECLARED


async def test_a_refused_stream_leaves_no_version_and_no_held_bytes(
    content_rig: ContentRig, clock: FakeClock
) -> None:
    node = content_rig.files["a.bin"]
    before = await usage_held(content_rig, clock)

    with pytest.raises(InvalidRequest):
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"),
            CountingStream(STREAMED).__aiter__(),
            size_declared=DECLARED,
            if_match=node.etag,
        )

    assert await count_versions(content_rig, "a.bin") == 0
    assert await usage_held(content_rig, clock) == before
    # Refused while staging, before any session opened or took room.
    assert await session_states(content_rig) == []
    await content_rig.session.refresh(node)
    assert node.head_version_id is None
    assert node.etag == 0


@pytest.mark.parametrize(
    "streamed",
    [
        pytest.param(DECLARED + 1, id="one-byte-over"),
        pytest.param(DECLARED - 1, id="one-byte-under"),
    ],
)
async def test_a_stream_off_by_one_either_way_is_refused(
    content_rig: ContentRig, streamed: int
) -> None:
    node = content_rig.files["a.bin"]

    with pytest.raises(InvalidRequest) as caught:
        await content_rig.service.put_version(
            content_rig.node_id("a.bin"),
            CountingStream(streamed, chunk=streamed).__aiter__(),
            size_declared=DECLARED,
            if_match=node.etag,
        )

    assert caught.value.code == "files.size_mismatch"
    assert await count_versions(content_rig, "a.bin") == 0
    assert await session_states(content_rig) == []


async def test_a_stream_that_matches_its_declaration_is_written(
    content_rig: ContentRig,
) -> None:
    # The negative twin: the same rig, the same size class, one byte different in
    # the declaration — so the refusals above cannot be an artefact of the rig.
    node = content_rig.files["a.bin"]
    body = CountingStream(DECLARED)

    info = await content_rig.service.put_version(
        content_rig.node_id("a.bin"),
        body.__aiter__(),
        size_declared=DECLARED,
        if_match=node.etag,
    )

    assert info.size == DECLARED
    assert body.bytes == DECLARED
    assert await count_versions(content_rig, "a.bin") == 1
    assert await session_states(content_rig) == ["done"]
