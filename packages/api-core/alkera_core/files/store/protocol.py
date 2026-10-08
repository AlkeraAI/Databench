"""The object-store protocol: the single surface every driver implements.

Everything above the store is written against :class:`ObjectStore` and
:class:`StoreCapabilities`; a vendor feature is a capability with a portable
fallback, never an ``isinstance`` check on a driver.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from alkera_core.files.store.errors import ChecksumMismatch, InvalidRequest


@dataclass(frozen=True)
class StoreCapabilities:
    """What a driver can actually do, read at runtime to pick the fallback."""

    conditional_write: bool
    presigned: bool
    range_signing: bool
    versioning: bool
    object_lock: bool
    lifecycle: bool
    scoped_credentials: bool
    storage_classes: bool
    strong_read_after_write: bool
    kms: bool
    max_object_bytes: int
    min_part_bytes: int
    max_part_bytes: int
    max_parts: int
    conditional_complete: bool = False
    """Whether ``IfNoneMatch: *`` is honoured on ``CompleteMultipartUpload``.

    ``conditional_write`` speaks only for a single ``PutObject``; several
    endpoints accept the header on *complete* and assemble anyway. Where this
    is false a large conditional put is a head-then-complete with a window
    between the two, so it is a measured answer (the driver probes once) and
    never a restatement of ``conditional_write``."""

    tagging: bool = False
    """Whether the endpoint records an object tag set the driver can read back.

    The lifecycle safety nets filter on a tag rather than a key prefix, so a
    driver that cannot tag has to write no ``Tagging`` at all — sending one to
    an endpoint that refuses the header fails every classed write — and leans
    on the reconciliation worker instead, which is the truth on every driver."""

    atomic_move: bool = False
    """Whether ``move`` publishes the destination and drops the source in one
    step. Copy-then-delete stores leave both keys readable for a moment, so a
    caller that needs "exactly one of the two keys" must read this rather than
    assume it; every caller may still rely on never seeing neither."""


@dataclass(frozen=True)
class ObjectInfo:
    """What ``head`` can say about a stored object."""

    size: int
    checksum: bytes | None
    etag: str | None
    storage_class: str | None


@dataclass(frozen=True)
class PutResult:
    """The outcome of a completed write, as the store itself observed it."""

    key: str
    size: int
    checksum: bytes
    etag: str | None


@dataclass(frozen=True)
class PartResult:
    """One accepted multipart part, replayed back at complete time."""

    part_no: int
    size: int
    checksum: bytes
    etag: str | None


@dataclass(frozen=True)
class UploadHandle:
    """A multipart session; opaque to callers apart from its key."""

    key: str
    upload_id: str
    size: int


@dataclass(frozen=True)
class ListPage:
    """One keyset page of a prefix listing (reconciliation only)."""

    keys: Sequence[str]
    next_after: str | None


@dataclass(frozen=True)
class ScopedCredentials:
    """A prefix-scoped, time-bounded credential vended to a client."""

    access_key_id: str
    # A vended credential is logged, put into exception context and echoed by
    # debugging that reprs the object; only the id may ever be printed.
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(repr=False)
    expires_at: datetime
    prefix: str
    read_only: bool


@runtime_checkable
class ObjectStore(Protocol):
    """The portable object-store surface. Keys are domain-relative."""

    capabilities: StoreCapabilities

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
        if_absent: bool = True,
        storage_class: Literal["hot", "cold"] | None = None,
    ) -> PutResult: ...

    async def get(
        self, key: str, *, range: tuple[int, int] | None = None
    ) -> AsyncIterator[bytes]: ...

    async def head(self, key: str) -> ObjectInfo | None: ...

    async def delete(self, key: str) -> None: ...

    async def move(self, src: str, dst: str) -> None: ...

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage: ...

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle: ...

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult: ...

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        """Assemble ``parts``; ``checksum`` is the whole object's content hash.

        No part's checksum is the object's, and the store's ETag is not a
        content hash either, so the caller — which hashed the bytes as it
        streamed them — is the only party that knows it. See
        :func:`resolve_whole_object_checksum`.
        """
        ...

    async def multipart_abort(self, handle: UploadHandle) -> None: ...

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str: ...

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str: ...

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials: ...


def resolve_whole_object_checksum(
    key: str,
    *,
    declared: bytes | None,
    assembled: bytes | None,
    parts: Sequence[PartResult],
) -> bytes:
    """The content hash a completed multipart object is published under.

    The caller hashed the whole object while it streamed the parts, so its
    ``checksum`` is the one authority on what the assembled bytes are; the
    driver verifies its own assembly against it where it has one. When the
    caller passes none,
    the only case where a *part* checksum is also the whole-object checksum is
    a single-part upload, so anything else is refused rather than published
    under a hash that is not the object's.
    """
    if declared is not None:
        if assembled is not None and declared != assembled:
            raise ChecksumMismatch(f"{key}: declared {declared.hex()}, assembled {assembled.hex()}")
        return declared
    if len(parts) == 1:
        # One part IS the whole object, and its checksum was verified against
        # the bytes on the way in, so this is the one shape where the driver
        # can name the object's hash without having been told it.
        return parts[0].checksum
    raise InvalidRequest(
        f"{key}: a {len(parts)}-part upload has no whole-object checksum; "
        "pass the hash the caller computed while streaming the parts"
    )
