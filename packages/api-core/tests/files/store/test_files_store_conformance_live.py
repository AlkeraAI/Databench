"""The store conformance suite bound to real S3-compatible endpoints.

The endpoint list is data, not code: ``FILES_LIVE_ENDPOINTS`` carries a JSON
list in the shape ``make files-live-local`` already builds, so the local
SeaweedFS, real AWS S3 and a non-AWS endpoint are the same parametrization with
different rows. Real endpoints keep their credentials out of the JSON by naming
an environment variable (``access_key_env`` / ``secret_key_env``) instead of the
value, and a row may declare what its endpoint cannot do (``conditional_write``,
``presigned``, ``addressing``) so an endpoint limitation is recorded in the list
rather than hidden behind a skipped test.

Every case runs under a key prefix unique to that test, so two runs against one
bucket never collide, and the prefix is deleted object by object afterwards —
the protocol has no bulk delete.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import aioboto3
import pytest
from alkera_core.files.clock import SystemClock
from alkera_core.files.store.keys import validate_relative_key
from alkera_core.files.store.protocol import (
    ListPage,
    ObjectInfo,
    ObjectStore,
    PartResult,
    PutResult,
    ScopedCredentials,
    StoreCapabilities,
    UploadHandle,
)
from alkera_core.files.store.s3_compatible import (
    MAX_SINGLE_COPY_BYTES,
    MIN_PART_BYTES,
    S3CompatibleStore,
    S3Config,
)
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore
from blake3 import blake3
from botocore.config import Config as BotoConfig
from conformance import StoreConformance

pytestmark = pytest.mark.live

ENDPOINTS_ENV = "FILES_LIVE_ENDPOINTS"
LARGE_COPY_ENV = "ALKERA_FILES_LIVE_LARGE_COPY"


@dataclass(frozen=True)
class LiveEndpoint:
    """One row of ``FILES_LIVE_ENDPOINTS``, with its credentials resolved."""

    name: str
    endpoint_url: str | None
    region: str
    bucket: str
    access_key: str | None
    secret_key: str | None
    addressing: Literal["path", "virtual"]
    conditional_write: bool
    presigned: bool

    def config(self) -> S3Config:
        return S3Config(
            endpoint_url=self.endpoint_url,
            region=self.region,
            bucket=self.bucket,
            access_key=self.access_key,
            secret_key=self.secret_key,
            addressing=self.addressing,
            conditional_write=self.conditional_write,
            presigned=self.presigned,
        )


def _credential(entry: dict[str, Any], field: str) -> str | None:
    """A credential given inline, or read from the env var the row names."""
    env_name = entry.get(f"{field}_env")
    if env_name is not None:
        value = os.environ.get(str(env_name))
        if not value:
            pytest.skip(
                f"{ENDPOINTS_ENV} names {env_name} for {field}, which is unset",
                allow_module_level=True,
            )
        return value
    inline = entry.get(field)
    return None if inline is None else str(inline)


def parse_endpoints(parsed: object) -> list[LiveEndpoint]:
    """Turn the decoded ``FILES_LIVE_ENDPOINTS`` list into endpoint rows."""
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{ENDPOINTS_ENV} is not a non-empty JSON list")
    endpoints: list[LiveEndpoint] = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValueError(f"{ENDPOINTS_ENV}[{index}] is not a JSON object")
        provider = entry.get("provider", "s3_compatible")
        if provider != "s3_compatible":
            raise ValueError(f"{ENDPOINTS_ENV}[{index}] has unsupported provider {provider!r}")
        addressing = entry.get("addressing", "path")
        if addressing not in ("path", "virtual"):
            raise ValueError(f"{ENDPOINTS_ENV}[{index}] has unsupported addressing {addressing!r}")
        if "bucket" not in entry:
            raise ValueError(f"{ENDPOINTS_ENV}[{index}] has no bucket")
        endpoints.append(
            LiveEndpoint(
                name=str(entry.get("name", f"endpoint-{index}")),
                endpoint_url=entry.get("endpoint"),
                region=str(entry.get("region", "us-east-1")),
                bucket=str(entry["bucket"]),
                access_key=_credential(entry, "access_key"),
                secret_key=_credential(entry, "secret_key"),
                addressing=addressing,
                conditional_write=bool(entry.get("conditional_write", True)),
                presigned=bool(entry.get("presigned", True)),
            )
        )
    return endpoints


def _configured_endpoints() -> list[LiveEndpoint]:
    import json

    raw = os.environ.get(ENDPOINTS_ENV)
    if not raw:
        pytest.skip(
            f"{ENDPOINTS_ENV} is unset — run `make files-live-local`, or set it to a "
            "JSON list of live endpoints",
            allow_module_level=True,
        )
    return parse_endpoints(json.loads(raw))


ENDPOINTS = _configured_endpoints()


class PrefixedStore:
    """A live store confined to one key prefix, so runs cannot collide.

    A key is validated with the same authority the driver uses *before* the
    prefix goes on, so a hostile key is still refused for the reason it is
    hostile instead of being laundered into a legal key by the prefix.
    """

    def __init__(self, inner: ObjectStore, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix
        self.capabilities: StoreCapabilities = inner.capabilities

    def _out(self, key: str) -> str:
        validate_relative_key(key)
        return f"{self._prefix}{key}"

    def _in(self, key: str) -> str:
        return key[len(self._prefix) :]

    def _handle_out(self, handle: UploadHandle) -> UploadHandle:
        return UploadHandle(key=self._out(handle.key), upload_id=handle.upload_id, size=handle.size)

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
        result = await self._inner.put(
            self._out(key),
            data,
            size=size,
            checksum=checksum,
            if_absent=if_absent,
            storage_class=storage_class,
        )
        return PutResult(
            key=self._in(result.key), size=result.size, checksum=result.checksum, etag=result.etag
        )

    async def get(self, key: str, *, range: tuple[int, int] | None = None) -> AsyncIterator[bytes]:
        return await self._inner.get(self._out(key), range=range)

    async def head(self, key: str) -> ObjectInfo | None:
        return await self._inner.head(self._out(key))

    async def delete(self, key: str) -> None:
        await self._inner.delete(self._out(key))

    async def move(self, src: str, dst: str) -> None:
        await self._inner.move(self._out(src), self._out(dst))

    async def list_prefix(
        self, prefix: str, *, after: str | None = None, limit: int = 1000
    ) -> ListPage:
        page = await self._inner.list_prefix(
            f"{self._prefix}{prefix}",
            after=None if after is None else f"{self._prefix}{after}",
            limit=limit,
        )
        return ListPage(
            keys=[self._in(key) for key in page.keys],
            next_after=None if page.next_after is None else self._in(page.next_after),
        )

    async def multipart_create(self, key: str, *, size: int) -> UploadHandle:
        handle = await self._inner.multipart_create(self._out(key), size=size)
        return UploadHandle(key=self._in(handle.key), upload_id=handle.upload_id, size=handle.size)

    async def multipart_put_part(
        self,
        handle: UploadHandle,
        part_no: int,
        data: AsyncIterator[bytes],
        *,
        size: int,
        checksum: bytes,
    ) -> PartResult:
        return await self._inner.multipart_put_part(
            self._handle_out(handle), part_no, data, size=size, checksum=checksum
        )

    async def multipart_complete(
        self,
        handle: UploadHandle,
        parts: Sequence[PartResult],
        *,
        checksum: bytes | None = None,
    ) -> PutResult:
        result = await self._inner.multipart_complete(
            self._handle_out(handle), parts, checksum=checksum
        )
        return PutResult(
            key=self._in(result.key), size=result.size, checksum=result.checksum, etag=result.etag
        )

    async def multipart_abort(self, handle: UploadHandle) -> None:
        await self._inner.multipart_abort(self._handle_out(handle))

    def presign_get(self, key: str, *, range: tuple[int, int] | None, ttl: timedelta) -> str:
        return self._inner.presign_get(self._out(key), range=range, ttl=ttl)

    def presign_put_part(
        self, handle: UploadHandle, part_no: int, *, size: int, ttl: timedelta
    ) -> str:
        return self._inner.presign_put_part(self._handle_out(handle), part_no, size=size, ttl=ttl)

    async def vend_scoped_credentials(
        self, prefix: str, *, ttl: timedelta, read_only: bool
    ) -> ScopedCredentials:
        return await self._inner.vend_scoped_credentials(
            f"{self._prefix}{prefix}", ttl=ttl, read_only=read_only
        )


async def purge(store: PrefixedStore) -> None:
    """Delete every object the test wrote — the protocol has no bulk delete."""
    after: str | None = None
    for _ in range(256):
        page = await store.list_prefix("", after=after, limit=1000)
        for key in page.keys:
            await store.delete(key)
        if page.next_after is None:
            return
        after = page.next_after
    raise AssertionError("the run prefix never emptied")


@pytest.fixture(params=ENDPOINTS, ids=[endpoint.name for endpoint in ENDPOINTS])
def live_endpoint(request: pytest.FixtureRequest) -> LiveEndpoint:
    endpoint: LiveEndpoint = request.param
    return endpoint


async def open_uploads(endpoint: LiveEndpoint, prefix: str) -> list[str]:
    """The keys the endpoint itself still considers open multipart sessions.

    The object-store protocol cannot enumerate these — deliberately, nothing
    above Layer 1 needs to — so the check reaches for the endpoint's own
    ``ListMultipartUploads``. It is the only way to see an upload the driver
    should have aborted: an abandoned session holds staged parts that no
    listing of objects will ever show.
    """
    config = endpoint.config()
    session = aioboto3.Session()
    async with session.client(
        "s3",
        region_name=config.region,
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        config=BotoConfig(s3={"addressing_style": config.addressing}),
    ) as client:
        response = await client.list_multipart_uploads(Bucket=config.bucket, Prefix=prefix)
        found = [str(upload["Key"]) for upload in response.get("Uploads", [])]
        for upload in response.get("Uploads", []):
            await client.abort_multipart_upload(
                Bucket=config.bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
    return found


# Two cases end with a session open on purpose: `presign_put_part` opens one
# only to sign a URL for it, and a refused `complete` leaves the staged parts
# in place so the caller can complete with the list it really staged. Every
# other case must leave the bucket with no open upload — that is what proves
# the driver aborts the sessions it refuses.
LEAVES_A_SESSION_OPEN = (
    "presign_put_part",
    "multipart_complete_refuses",
    # A complete the driver refuses before the endpoint is asked leaves the
    # session staged on purpose: the caller can re-drive it with the right
    # checksum, so the sweeper — not the driver — is what closes it.
    "multipart_complete_without_a_whole_object_checksum",
)


@pytest.fixture
async def live_store(
    live_endpoint: LiveEndpoint, request: pytest.FixtureRequest
) -> AsyncIterator[PrefixedStore]:
    inner = S3CompatibleStore(live_endpoint.config(), clock=SystemClock())
    prefix = f"live/{uuid.uuid4()}/"
    prefixed = PrefixedStore(inner, prefix)
    yield prefixed
    await purge(prefixed)
    abandoned = await open_uploads(live_endpoint, prefix)
    if not any(name in request.node.name for name in LEAVES_A_SESSION_OPEN):
        assert abandoned == [], f"the driver left multipart sessions open on {abandoned}"


class TestLiveStoreConformance(StoreConformance):
    """Every configured endpoint against the portable contract, over the wire."""

    @pytest.fixture
    def store(self, live_store: PrefixedStore) -> PrefixedStore:
        return live_store


class TestLiveFaultyStoreConformance(StoreConformance):
    """The fault wrapper is transparent on a real wire when nothing is scheduled."""

    @pytest.fixture
    def store(self, live_store: PrefixedStore) -> FaultyStore:
        return FaultyStore(live_store, FaultSchedule([]))


# --- The copy AWS refuses in one call ---------------------------------------


def _pattern(size: int) -> bytes:
    """A 8 MiB block whose byte at any absolute offset ``i`` is ``i % 256``."""
    block = bytes(range(256)) * (1 << 15)
    assert len(block) % 256 == 0
    return block[:size] if size < len(block) else block


async def _drain(chunks: AsyncIterator[bytes]) -> bytes:
    out = bytearray()
    async for chunk in chunks:
        out += chunk
    return bytes(out)


@pytest.mark.skipif(
    os.environ.get(LARGE_COPY_ENV) != "1",
    reason=f"{LARGE_COPY_ENV} is not 1 — staging and copying >5 GiB costs real storage time",
)
async def test_a_move_over_the_single_copy_ceiling_lands_the_whole_object(
    live_store: PrefixedStore,
) -> None:
    """A real move of a source S3 refuses to copy in one call.

    Every promotion out of ``incoming/`` is a move, and with a 1000 GB maximum
    file most of them are over the 5 GiB ceiling, so the range-by-range copy is
    the ordinary path rather than the exotic one. Opt-in — the guard is on the
    case rather than in it, so a run without it never opens the fixture's
    prefix. The free driver test pins the ranges on every PR; this is what
    proves a real endpoint assembles them in order.
    """
    size = MAX_SINGLE_COPY_BYTES + 1
    block = _pattern(size)
    hasher = blake3()
    remaining = size
    while remaining:
        hasher.update(block[: min(len(block), remaining)])
        remaining -= min(len(block), remaining)

    async def body() -> AsyncIterator[bytes]:
        left = size
        while left:
            take = min(len(block), left)
            yield block[:take]
            left -= take

    src = f"incoming/{uuid.uuid4()}/blob"
    dst = f"objects/ab/cd/{uuid.uuid4()}"
    await live_store.put(src, body(), size=size, checksum=hasher.digest())

    await live_store.move(src, dst)

    assert await live_store.head(src) is None
    info = await live_store.head(dst)
    assert info is not None
    assert info.size == size
    # Size alone would pass on parts assembled out of order, so read across the
    # seam between the first two copied ranges and at the very last byte.
    # One byte over the ceiling is cut at the minimum part size, so the first
    # boundary between two copied ranges is there.
    seam = MIN_PART_BYTES
    window = await _drain(await live_store.get(dst, range=(seam - 1, seam + 1)))
    assert window == bytes(offset % 256 for offset in range(seam - 1, seam + 2))
    tail = await _drain(await live_store.get(dst, range=(size - 1, size - 1)))
    assert tail == bytes([(size - 1) % 256])
