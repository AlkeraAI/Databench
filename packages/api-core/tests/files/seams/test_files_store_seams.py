"""The store seam, exercised through its registry rather than one driver.

Every driver in ``STORE_FACTORIES`` is held to the same three things: it *is* an
``ObjectStore`` at runtime, it publishes a real capability record, and every
capability it declares ``False`` has a portable fallback that a caller can take
without knowing which driver it holds. Adding a driver here is the only edit a
new implementation should need — that is what "covered by registration alone"
means.

The remaining case is the stub caller for a use the seam must already admit: a
customer-hosted bucket on a foreign endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from _memory_store import MEMORY_CAPABILITIES, MemoryStore
from alkera_core.files.clock import FakeClock
from alkera_core.files.hashing import hash_bytes
from alkera_core.files.store.errors import InvalidKey
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.protocol import (
    ObjectStore,
    StoreCapabilities,
)
from alkera_core.files.store.s3_compatible import S3CompatibleStore, S3Config
from alkera_core.files.store.scoped import (
    PrefixedDomainStore,
    PrefixGuard,
    S3CompatConfig,
    S3CompatScoped,
)
from alkera_test_support.files.faulty_store import FaultSchedule, FaultyStore

KEY = "objects/ab/cd/abcdef"
DOMAIN = UUID("11111111-2222-3333-4444-555555555555")

StoreFactory = Callable[[Path, FakeClock], ObjectStore]

#: The registry the seam's tests run over. A new driver is one line here.
STORE_FACTORIES: tuple[tuple[str, StoreFactory], ...] = (
    ("filesystem", lambda root, clock: FilesystemStore(root / "fs", clock=clock.now)),
    (
        "faulty(filesystem)",
        lambda root, clock: FaultyStore(
            FilesystemStore(root / "faulty", clock=clock.now), FaultSchedule([])
        ),
    ),
    ("memory", lambda root, clock: MemoryStore()),
)

FACTORY_PARAMS = [pytest.param(factory, id=name) for name, factory in STORE_FACTORIES]


async def one_chunk(data: bytes) -> Any:
    yield data


# -- (1) every registered driver satisfies the seam ------------------------


@pytest.mark.parametrize("factory", FACTORY_PARAMS)
def test_every_registered_driver_is_an_object_store_at_runtime(
    factory: StoreFactory, tmp_path: Path, clock: FakeClock
) -> None:
    store = factory(tmp_path, clock)
    assert isinstance(store, ObjectStore)
    assert isinstance(store.capabilities, StoreCapabilities)
    assert store.capabilities.min_part_bytes <= store.capabilities.max_part_bytes
    assert store.capabilities.max_parts > 0


@pytest.mark.parametrize("factory", FACTORY_PARAMS)
async def test_every_registered_driver_round_trips_the_same_bytes(
    factory: StoreFactory, tmp_path: Path, clock: FakeClock
) -> None:
    """The registry is not a type check: each driver really stores and returns."""
    store = factory(tmp_path, clock)
    data = b"the same bytes out as in" * 64
    digest = hash_bytes(data).content_hash

    await store.put(KEY, one_chunk(data), size=len(data), checksum=digest)

    assert b"".join([chunk async for chunk in await store.get(KEY)]) == data


def test_a_class_missing_a_protocol_method_is_not_an_object_store() -> None:
    """The runtime check has to be able to say no, or (1) proves nothing."""

    class HalfAStore:
        capabilities = MEMORY_CAPABILITIES

        async def put(self, *args: object, **kwargs: object) -> None: ...

    assert not isinstance(HalfAStore(), ObjectStore)


# -- (2) each capability that may be False has a portable fallback ---------


async def test_conditional_write_absent_makes_put_if_absent_a_no_op() -> None:
    store = MemoryStore()
    assert store.capabilities.conditional_write is False
    first = b"the bytes that were already there"
    await store.put(KEY, one_chunk(first), size=len(first), checksum=hash_bytes(first).content_hash)

    second = b"a second writer of the same content-addressed key"
    result = await store.put(
        KEY, one_chunk(second), size=len(second), checksum=hash_bytes(second).content_hash
    )

    info = await store.head(KEY)
    assert info is not None
    assert info.size == len(first)
    assert result.size == len(first)
    assert store.objects[KEY] == first


@pytest.mark.parametrize(
    "presign",
    [
        pytest.param(
            lambda store: store.presign_get(KEY, range=None, ttl=timedelta(minutes=5)),
            id="presign_get",
        ),
        pytest.param(
            lambda store: store.presign_put_part(None, 1, size=1024, ttl=timedelta(minutes=5)),
            id="presign_put_part",
        ),
    ],
)
def test_presigning_absent_refuses_instead_of_returning_an_unusable_url(
    presign: Callable[[MemoryStore], str],
) -> None:
    store = MemoryStore()
    assert store.capabilities.presigned is False
    assert store.capabilities.range_signing is False
    with pytest.raises(NotImplementedError):
        presign(store)


async def test_scoped_credentials_absent_makes_the_domain_handle_a_prefix_guard() -> None:
    """The portable isolation: no vendor, so ``S3CompatScoped`` guards in-process."""
    store = MemoryStore(key_namespace="absolute")
    factory = S3CompatScoped(
        S3CompatConfig(store=store), clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    )

    handle = await factory.for_domain(DOMAIN)

    assert isinstance(handle, PrefixedDomainStore)
    assert handle.capabilities.scoped_credentials is False
    data = b"guarded"
    await handle.put(KEY, one_chunk(data), size=len(data), checksum=hash_bytes(data).content_hash)
    assert f"domains/{DOMAIN}/{KEY}" in store.objects

    escaping = PrefixGuard(store, f"domains/{DOMAIN}/")
    with pytest.raises(InvalidKey):
        await escaping.head("domains/00000000-0000-0000-0000-000000000000/objects/x")


async def test_lifecycle_absent_leaves_the_expired_object_for_the_reconciler() -> None:
    """No lifecycle rules, so nothing expires by itself: the sweep is the truth."""
    store = MemoryStore()
    assert store.capabilities.lifecycle is False
    data = b"an aborted multipart part nobody completed"
    await store.put(
        "incoming/abandoned",
        one_chunk(data),
        size=len(data),
        checksum=hash_bytes(data).content_hash,
    )

    page = await store.list_prefix("incoming/")

    assert list(page.keys) == ["incoming/abandoned"]
    await store.delete("incoming/abandoned")
    assert await store.head("incoming/abandoned") is None


async def test_reads_lag_writes_when_the_store_is_not_read_after_write_consistent() -> None:
    store = MemoryStore(delayed_visibility_reads=2)
    assert store.capabilities.strong_read_after_write is False
    data = b"written, not yet visible"
    await store.put(KEY, one_chunk(data), size=len(data), checksum=hash_bytes(data).content_hash)

    assert await store.head(KEY) is None
    assert await store.head(KEY) is None
    info = await store.head(KEY)
    assert info is not None and info.size == len(data)


# -- (3) the future customer-hosted bucket --------------------------------


async def test_a_foreign_endpoint_is_carried_into_the_client_config() -> None:
    """No AWS assumption in the constructor: the endpoint the caller gave wins."""
    seen: list[dict[str, Any]] = []

    store = S3CompatibleStore(
        S3Config(
            endpoint_url="https://s3.customer.example:9000",
            region="eu-central-1",
            bucket="customer-owned",
            addressing="virtual",
        ),
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    seen.append(store._client_kwargs())

    assert seen[0]["endpoint_url"] == "https://s3.customer.example:9000"
    assert seen[0]["region_name"] == "eu-central-1"
    assert seen[0]["config"].s3["addressing_style"] == "virtual"


async def test_a_customer_hosted_bucket_is_driven_through_the_injected_client() -> None:
    """The driver talks to whatever client it is handed — no vendor client baked in."""
    calls: list[str] = []

    class Client:
        async def put_object(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(str(kwargs["Bucket"]))
            # The driver streams a single put through a spool, so the body is
            # the seekable file botocore signs, not a buffer.
            self.stored = bytes(kwargs["Body"].read())
            return {"ETag": '"deadbeef"'}

        async def head_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"ContentLength": len(self.stored), "ETag": '"deadbeef"'}

    client = Client()

    @asynccontextmanager
    async def client_factory() -> Any:
        yield client

    store = S3CompatibleStore(
        S3Config(
            endpoint_url="https://s3.customer.example:9000",
            region="eu-central-1",
            bucket="customer-owned",
            conditional_write=False,
        ),
        clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
        client_factory=client_factory,
    )
    data = b"bytes bound for a bucket we do not own"

    await store.put(KEY, one_chunk(data), size=len(data), checksum=hash_bytes(data).content_hash)

    assert calls == ["customer-owned"]
    assert client.stored == data
