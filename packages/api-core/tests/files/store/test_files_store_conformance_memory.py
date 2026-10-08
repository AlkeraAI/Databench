"""The conformance suite bound to a driver that was added by registration alone.

``MemoryStore`` lives under ``packages/api-core/tests/files/seams/`` and shares no code with the
filesystem driver; binding it here is the whole proof that the seam admits a new
implementation without editing the contract. It runs twice: once bare, and once
as the ``PrefixedDomainStore`` handle a request path actually holds, wired the
way :class:`~alkera_core.files.store.scoped.S3CompatScoped` wires a store that
vends no credentials (``PrefixGuard`` plus the domain prefix).

The domain-bound half skips only what a ``DomainStore`` genuinely does not have
— listing — and re-states every other invariant through ``head`` and ``get``
instead of the admin-only ``list_prefix``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

import pytest
from alkera_core.files.store.errors import InvalidKey, PreconditionFailed, StoreError
from alkera_core.files.store.protocol import ObjectStore
from alkera_core.files.store.scoped import PrefixedDomainStore, PrefixGuard
from conformance import (
    HOSTILE_KEYS,
    KEY,
    StoreConformance,
    body,
    put_bytes,
    read_all,
    stream,
    upload_parts,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "seams"))

from _memory_store import MemoryStore

DOMAIN = UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


class TestMemoryStoreConformance(StoreConformance):
    """A second driver, held to the same contract as the reference one."""


@pytest.fixture
def domain_store() -> PrefixedDomainStore:
    guarded = PrefixGuard(MemoryStore(key_namespace="absolute"), f"domains/{DOMAIN}/")
    return PrefixedDomainStore(guarded, DOMAIN)


class TestMemoryDomainStoreConformance(StoreConformance):
    """The same driver behind the per-request domain handle."""

    @pytest.fixture
    def store(self, domain_store: PrefixedDomainStore) -> PrefixedDomainStore:
        return domain_store

    async def test_list_prefix_pages_through_after_without_repeating_or_skipping(
        self, store: ObjectStore
    ) -> None:
        pytest.skip("a DomainStore has no listing: reconciliation holds the admin handle")

    async def test_delete_removes_the_object_and_a_missing_key_is_silent(
        self, store: ObjectStore
    ) -> None:
        await put_bytes(store, KEY, body(64))
        await store.delete(KEY)
        assert await store.head(KEY) is None
        await store.delete(KEY)
        assert await store.head(KEY) is None

    async def test_multipart_abort_removes_every_part_and_leaves_no_object(
        self, store: ObjectStore
    ) -> None:
        parts = [body(store.capabilities.min_part_bytes), body(64)]
        handle, accepted = await upload_parts(store, KEY, parts)

        await store.multipart_abort(handle)

        assert await store.head(KEY) is None
        with pytest.raises(StoreError):
            await store.multipart_complete(handle, accepted)

    async def test_a_put_whose_checksum_does_not_match_leaves_no_object(
        self, store: ObjectStore
    ) -> None:
        data = body(4096)
        with pytest.raises(StoreError):
            await store.put(KEY, stream(data), size=len(data), checksum=body(32))
        assert await store.head(KEY) is None

    async def test_five_concurrent_puts_of_one_key_leave_exactly_one_object(
        self, store: ObjectStore
    ) -> None:
        data = body(8192)
        results = await asyncio.gather(
            *(put_bytes(store, KEY, data) for _ in range(5)), return_exceptions=True
        )
        for outcome in results:
            if isinstance(outcome, BaseException):
                assert isinstance(outcome, PreconditionFailed), outcome
        info = await store.head(KEY)
        assert info is not None
        assert info.size == len(data)
        assert await read_all(store, KEY) == data

    @pytest.mark.parametrize("hostile", HOSTILE_KEYS)
    async def test_a_hostile_key_is_refused_and_stores_nothing(
        self, store: ObjectStore, hostile: str
    ) -> None:
        data = body(32)
        with pytest.raises(InvalidKey):
            await store.put(hostile, stream(data), size=len(data), checksum=body(32))
        assert await store.head(KEY) is None
