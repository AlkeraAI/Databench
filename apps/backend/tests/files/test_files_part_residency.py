"""What one in-flight upload part really costs the process, measured.

``FILES_UPLOAD_PART_RESIDENT_BYTES`` is the number every part is admitted by,
so it has to be the number a part actually holds: a 128 MiB part streamed
through each store driver in uvicorn-sized wire chunks, with ``tracemalloc``
reading the high-water mark of everything the driver allocated on the way. The
S3 driver is the expensive one -- it re-packs the stream into 5 MiB multipart
buffers and, at the cut, holds the bytearray it accumulates in, the slice it
takes, the copy it sends and the remainder at the same moment -- and the
filesystem driver writes each chunk straight through. Both
must fit under the constant, and the S3 measurement must SEE the staging buffer
(be at least one of them) so a stub that short-circuited the driver could not
pass this by measuring nothing.

The SDK's own request framing is outside what is measured here: aiohttp sends a
``bytes`` payload by reference and botocore hashes it in a streaming pass, so
the driver's buffers are the resident cost -- which is why the constant carries
headroom over the measured peak rather than equalling it.
"""

from __future__ import annotations

import tracemalloc
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from alkera_core.config import FILES_UPLOAD_PART_RESIDENT_BYTES, settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.protocol import PartResult, PutResult, UploadHandle
from alkera_core.files.store.s3_compatible import MIN_PART_BYTES, S3CompatibleStore, S3Config
from blake3 import blake3

#: What uvicorn hands the app per ``receive`` under its flow control.
WIRE_CHUNK = 64 * 1024


async def _wire(total: int, *, chunk: int = WIRE_CHUNK) -> AsyncIterator[bytes]:
    """A part arriving off the socket: a fresh chunk object each time, the way
    the protocol allocates one per read, so the measurement includes the chunk
    that is alive while the driver takes it."""
    sent = 0
    while sent < total:
        size = min(chunk, total - sent)
        yield bytes(size)
        sent += size


def _digest(total: int, *, chunk: int = WIRE_CHUNK) -> bytes:
    hasher = blake3()
    sent = 0
    while sent < total:
        size = min(chunk, total - sent)
        hasher.update(bytes(size))
        sent += size
    return hasher.digest()


class _Peak:
    """The high-water mark of Python allocations across one ``with`` block,
    net of what was already allocated when it opened."""

    def __enter__(self) -> _Peak:
        tracemalloc.start()
        tracemalloc.reset_peak()
        self._baseline = tracemalloc.get_traced_memory()[0]
        return self

    def __exit__(self, *exc: object) -> None:
        self.bytes = tracemalloc.get_traced_memory()[1] - self._baseline
        tracemalloc.stop()


def _s3_store_with_stubbed_wire(sent_parts: list[int]) -> S3CompatibleStore:
    """The real S3 driver with the endpoint stubbed out one call above the
    wire: every buffer the driver builds is built, and what would go to S3 is
    measured by length and dropped."""

    @asynccontextmanager
    async def never_opened() -> AsyncIterator[Any]:
        raise AssertionError("the stubbed driver must not open a client")
        yield  # pragma: no cover

    store = S3CompatibleStore(
        S3Config(endpoint_url="http://stub.invalid", region="us-east-1", bucket="parts"),
        clock=SystemClock(),
        client_factory=never_opened,
    )

    async def multipart_create(key: str, *, size: int, storage_class: Any = None) -> UploadHandle:
        return UploadHandle(key=key, upload_id="u1", size=size)

    async def upload_part(handle: UploadHandle, part_no: int, body: bytes) -> PartResult:
        sent_parts.append(len(body))
        return PartResult(part_no=part_no, size=len(body), checksum=b"", etag=None)

    async def complete_raw(handle: UploadHandle, parts: Any, *, if_absent: bool = False) -> None:
        return None

    async def verify(key: str, *, size: int, digest: bytes, declared: bytes, **_: Any) -> PutResult:
        return PutResult(key=key, size=size, checksum=digest, etag=None)

    store.multipart_create = multipart_create  # type: ignore[method-assign]
    store._upload_part = upload_part  # type: ignore[method-assign]
    store._complete_raw = complete_raw  # type: ignore[method-assign]
    store._verify = verify  # type: ignore[method-assign]
    return store


async def test_a_max_size_part_through_the_s3_driver_fits_the_resident_constant() -> None:
    """The driver's staging for one 128 MiB part peaks at four 5 MiB regions
    plus a wire chunk -- under the constant, and at least one buffer, so the
    measurement is of the driver and not of a stub."""
    size = settings.files_part_max_bytes
    sent: list[int] = []
    store = _s3_store_with_stubbed_wire(sent)
    with _Peak() as peak:
        await store.put("incoming/part", _wire(size), size=size, checksum=b"\0" * 32)
    assert sum(sent) == size, "the whole part reached the stubbed wire"
    assert all(chunk == MIN_PART_BYTES for chunk in sent[:-1]), "re-packed into multipart buffers"
    assert peak.bytes >= MIN_PART_BYTES, f"measured {peak.bytes} bytes: the buffer was not seen"
    assert peak.bytes <= FILES_UPLOAD_PART_RESIDENT_BYTES, (
        f"one in-flight part holds {peak.bytes} bytes; the admission constant "
        f"is {FILES_UPLOAD_PART_RESIDENT_BYTES} -- raise it and re-derive the budget"
    )


async def test_a_max_size_part_through_the_filesystem_driver_fits_with_room(
    tmp_path: Path,
) -> None:
    """The filesystem driver writes each wire chunk through and never stages a
    part: its peak is a chunk or two, far under the S3-sized constant."""
    size = settings.files_part_max_bytes
    store = FilesystemStore(tmp_path / "store", clock=SystemClock())
    checksum = _digest(size)
    with _Peak() as peak:
        result = await store.put(
            f"incoming/{uuid.uuid4().hex}", _wire(size), size=size, checksum=checksum
        )
    assert result.size == size
    assert peak.bytes <= 4 * (1 << 20), f"the filesystem driver staged {peak.bytes} bytes"
    assert peak.bytes <= FILES_UPLOAD_PART_RESIDENT_BYTES


def test_the_constant_is_the_measured_peak_with_headroom_not_a_round_guess() -> None:
    """Four 5 MiB regions and a wire chunk is the arithmetic -- the bytearray
    the driver accumulates in, the slice it cuts, the ``bytes`` copy of the
    slice it sends and the remainder -- and the constant sits within a fifth
    above it. A constant far above the measurement would admit fewer people
    than the memory allows; one below it is caught by the measurement above."""
    arithmetic = 4 * MIN_PART_BYTES + WIRE_CHUNK
    assert arithmetic < FILES_UPLOAD_PART_RESIDENT_BYTES <= arithmetic * 1.2


@pytest.mark.parametrize(
    "chunk",
    [pytest.param(WIRE_CHUNK, id="uvicorn-flow-control"), pytest.param(1 << 20, id="one-mib")],
)
async def test_the_s3_peak_does_not_grow_with_the_wire_chunk_a_proxy_forwards(
    chunk: int,
) -> None:
    """A proxy that coalesces the body into larger reads changes the chunk the
    driver is handed, not the part it stages: the peak stays under the
    constant at a 1 MiB wire chunk too."""
    size = 32 * (1 << 20)
    store = _s3_store_with_stubbed_wire([])
    with _Peak() as peak:
        await store.put("incoming/part", _wire(size, chunk=chunk), size=size, checksum=b"\0" * 32)
    assert peak.bytes <= FILES_UPLOAD_PART_RESIDENT_BYTES
