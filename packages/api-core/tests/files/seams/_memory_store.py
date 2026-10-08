"""An in-memory :class:`ObjectStore`: the second driver the seam has to admit.

This is the stub caller that proves a new store is a *registration*, not a
refactor. Its capability record is deliberately the weak end of the range — the
B2/SeaweedFS shape, where nothing but plain put/get is guaranteed — so the
portable-fallback branch of every capability-gated contract is exercised by a
driver that really cannot do the AWS-only thing, rather than by a mock that
pretends it cannot.

It is a test double, not a product driver: bytes live in a dict, so a caller
must not hand it more than it is willing to hold in RAM.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from typing import Final, Literal

from alkera_core.files.hashing import hash_bytes
from alkera_core.files.store.errors import (
    ChecksumMismatch,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
)
from alkera_core.files.store.keys import DOMAIN_PREFIX, validate_relative_key
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
    resolve_whole_object_checksum,
)

MIB: Final = 1 << 20

#: What a bucket that speaks only the portable subset can promise. Every AWS
#: extra is False, so a caller written against this record has to take the
#: fallback the spec names for it.
MEMORY_CAPABILITIES: Final = StoreCapabilities(
    conditional_write=False,
    presigned=False,
    range_signing=False,
    versioning=False,
    object_lock=False,
    lifecycle=False,
    scoped_credentials=False,
    storage_classes=False,
    strong_read_after_write=False,
    kms=False,
    max_object_bytes=48 * (1 << 40),
    min_part_bytes=1024,
    max_part_bytes=5 * (1 << 30),
    max_parts=10_000,
    # A stub is not a durable store; it claims no more than S3 does.
    atomic_move=False,
)


class MemoryStore:
    """A dict of key -> bytes that honours the object-store protocol exactly.

    ``key_namespace`` says which key space this handle owns: ``"relative"`` is a
    driver opened on one domain's own namespace (the shape the conformance suite
    holds every driver to), ``"absolute"`` is the bucket-wide admin handle a
    :class:`~alkera_core.files.store.scoped.PrefixGuard` writes ``domains/<id>/``
    keys through. It selects a key *space*, not a behaviour: every method does
    the same thing in both.

    ``delayed_visibility_reads`` models an eventually-consistent store: the
    first N ``head`` calls that would first observe a freshly written key answer
    ``None`` instead, which is exactly what ``strong_read_after_write=False``
    warns a caller about.
    """

    def __init__(
        self,
        *,
        capabilities: StoreCapabilities = MEMORY_CAPABILITIES,
        key_namespace: Literal["relative", "absolute"] = "relative",
        delayed_visibility_reads: int = 0,
    ) -> None:
        self.capabilities = capabilities
        self.key_namespace = key_namespace
        self.delayed_visibility_reads = delayed_visibility_reads
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self._lagging: dict[str, int] = {}

    # -- keys -------------------------------------------------------------

    def _validate(self, key: str) -> str:
        if self.key_namespace == "absolute" and key.startswith(DOMAIN_PREFIX):
            head, _, tail = key[len(DOMAIN_PREFIX) :].partition("/")
            if not head or not tail:
                raise ValueError(f"malformed domain key {key!r}")
            validate_relative_key(tail)
            return key
        validate_relative_key(key)
        return key

    # -- writes -----------------------------------------------------------

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
        storage_class: Literal["hot", "cold"] | None = None,
    ) -> PutResult:
        self._validate(key)
        if if_absent and key in self.objects:
            # No conditional write here, so the documented fallback applies: the
            # store keeps the object it already holds and the caller confirms by
            # head-after-put that content-addressed bytes are the same bytes.
            held = self.objects[key]
            return PutResult(
                key=key, size=len(held), checksum=hash_bytes(held).content_hash, etag=None
            )
        body = await self._drain(data)
        if len(body) != size:
            # A stream that does not match its declared length is a caller
            # error, not a store one: nothing is stored and nothing is charged.
            raise InvalidRequest(f"{key}: declared {size} bytes, streamed {len(body)}")
        digest = hash_bytes(body).content_hash
        if digest != checksum:
            raise ChecksumMismatch(f"{key}: declared {checksum.hex()}, computed {digest.hex()}")
        self.objects[key] = body
        if self.delayed_visibility_reads:
            self._lagging[key] = self.delayed_visibility_reads
        return PutResult(key=key, size=len(body), checksum=digest, etag=None)

    @staticmethod
    async def _drain(data: AsyncIterator[bytes]) -> bytes:
        buffer = bytearray()
        async for chunk in data:
            buffer.extend(chunk)
            await asyncio.sleep(0)
        return bytes(buffer)

    # -- reads ------------------------------------------------------------

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        self._validate(key)
        body = self.objects.get(key)
        if body is None:
            raise NotFound(key)
        if range is None:
            start, end = 0, len(body) - 1
        else:
            start, end = range
            if start < 0 or end < start or start >= len(body):
                raise PreconditionFailed(f"{key}: range {range!r} is not satisfiable")
            end = min(end, len(body) - 1)
        return self._stream(body[start : end + 1])

    @staticmethod
    async def _stream(body: bytes) -> AsyncIterator[bytes]:
        for offset in range(0, max(len(body), 1), MIB):
            yield body[offset : offset + MIB]

    async def head(self, key: str) -> ObjectInfo | None:
        self._validate(key)
        remaining = self._lagging.get(key, 0)
        if remaining:
            self._lagging[key] = remaining - 1
            return None
        body = self.objects.get(key)
        if body is None:
            return None
        return ObjectInfo(
            size=len(body),
            checksum=hash_bytes(body).content_hash,
            etag=None,
            storage_class=None,
        )

    async def delete(self, key: str) -> None:
        self._validate(key)
        self.objects.pop(key, None)
        self._lagging.pop(key, None)

    async def move(self, src: str, dst: str) -> None:
        self._validate(src)
        self._validate(dst)
        body = self.objects.get(src)
        if body is None:
            raise NotFound(src)
        # One statement pair with no await between them: a concurrent reader
        # sees the source or the destination, never both and never neither.
        self.objects[dst] = body
        del self.objects[src]
        self._lagging.pop(src, None)

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        found = sorted(
            key for key in self.objects if key.startswith(prefix) and (after is None or key > after)
        )
        page = found[:limit]
        return ListPage(keys=page, next_after=page[-1] if len(found) > limit else None)

    # -- multipart ---------------------------------------------------------

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        self._validate(key)
        upload_id = f"upload-{len(self.uploads)}-{key}"
        self.uploads[upload_id] = {}
        return UploadHandle(key=key, upload_id=upload_id, size=size)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        parts = self.uploads.get(handle.upload_id)
        if parts is None:
            raise NotFound(f"{handle.key}: no open multipart session")
        if part_no < 1 or part_no > self.capabilities.max_parts:
            raise PreconditionFailed(f"part number {part_no} is out of range")
        if size > self.capabilities.max_part_bytes:
            # Refused on the declared size, before a single byte is read.
            raise PreconditionFailed(
                f"part {part_no} declares {size} bytes, over {self.capabilities.max_part_bytes}"
            )
        body = await self._drain(data)
        digest = hash_bytes(body).content_hash
        if digest != checksum:
            raise ChecksumMismatch(f"{handle.key} part {part_no}: checksum does not match")
        parts[part_no] = body
        return PartResult(part_no=part_no, size=len(body), checksum=digest, etag=None)

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        staged = self.uploads.get(handle.upload_id)
        if staged is None:
            raise NotFound(f"{handle.key}: no open multipart session")
        declared = sorted(parts, key=lambda part: part.part_no)
        if [part.part_no for part in declared] != sorted(staged):
            raise PreconditionFailed(f"{handle.key}: the part list does not match what was staged")
        for part in declared:
            body = staged[part.part_no]
            if len(body) != part.size or hash_bytes(body).content_hash != part.checksum:
                raise PreconditionFailed(f"{handle.key}: part {part.part_no} does not match")
        joined = b"".join(staged[part.part_no] for part in declared)
        digest = resolve_whole_object_checksum(
            handle.key,
            declared=checksum,
            assembled=hash_bytes(joined).content_hash,
            parts=declared,
        )
        self.objects[handle.key] = joined
        del self.uploads[handle.upload_id]
        return PutResult(key=handle.key, size=len(joined), checksum=digest, etag=None)

    async def multipart_abort(self, handle: UploadHandle) -> None:
        self.uploads.pop(handle.upload_id, None)

    # -- capabilities this store does not have ----------------------------

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        raise NotImplementedError("the memory store cannot presign; use proxied transfer")

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        raise NotImplementedError("the memory store cannot presign; use proxied transfer")

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        raise NotImplementedError("the memory store vends no credentials; use PrefixGuard")
