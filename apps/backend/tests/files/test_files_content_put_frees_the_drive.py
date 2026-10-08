"""A content PUT that is still writing to the store holds up no other write.

Every write in an org queues on its drive row, and the content PUT runs in one
transaction whose locks last to its end. It used to take the drive before it
wrote the bytes to the store, so a folder created while a large file was being
written waited for the whole write and, past ``lock_timeout``, answered 503
``db_lock_timeout`` — which is what a few concurrent uploads turned the rest of
the org's Files traffic into. The store is gated here so the PUT is held exactly
while it writes, through the real route.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.config import settings
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DomainId
from alkera_core.files.store.scoped import DomainStore, FilesystemScoped, StoreAdmin
from backend.services.files.store import set_store_factory
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

#: Far above one create, far below forever.
WAIT_SECONDS = 10.0

PAYLOAD = b"p" * (settings.files_inline_max_bytes + 1)


class _Gate:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()


class _GatedDomain:
    """A domain handle whose first write under ``incoming/`` waits at the gate."""

    def __init__(self, inner: DomainStore, gate: _Gate) -> None:
        self._inner = inner
        self._gate = gate

    async def put(self, key: str, data: Any, **kwargs: Any) -> Any:
        if key.startswith("incoming/") and not self._gate.reached.is_set():
            self._gate.reached.set()
            await self._gate.release.wait()
        return await self._inner.put(key, data, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _GatedScoped:
    def __init__(self, inner: FilesystemScoped, gate: _Gate) -> None:
        self._inner = inner
        self._gate = gate

    async def for_domain(self, domain_id: DomainId) -> DomainStore:
        gated: Any = _GatedDomain(await self._inner.for_domain(domain_id), self._gate)
        return gated

    def admin(self) -> StoreAdmin:
        return self._inner.admin()


@pytest.fixture
def gate(files_on: None, files_store: Path) -> Iterator[_Gate]:
    held = _Gate()
    set_store_factory(_GatedScoped(FilesystemScoped(files_store, clock=SystemClock()), held))
    yield held
    held.release.set()


@pytest_asyncio.fixture
async def node(fx: Any, real_session: AsyncSession) -> Any:
    created = await fx.node(b"big.bin", parent=await fx.shared())
    drive = await fx.drive()
    await real_session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": drive.id,
        },
    )
    await real_session.commit()
    return created


async def test_a_folder_is_created_while_a_content_put_writes_to_the_store(
    files_client: AsyncClient, fx: Any, node: Any, gate: _Gate
) -> None:
    drive = await fx.drive()
    shared = await fx.shared()
    put = asyncio.create_task(
        files_client.put(
            f"/api/v1/files/drives/{drive.id}/items/{node.id}/content",
            content=PAYLOAD,
            headers={
                "Idempotency-Key": uuid.uuid4().hex,
                "If-Match": f'"{node.etag}"',
                "Content-Type": "application/octet-stream",
            },
        )
    )
    try:
        await asyncio.wait_for(gate.reached.wait(), WAIT_SECONDS)
        made = await asyncio.wait_for(
            files_client.post(
                f"/api/v1/files/drives/{drive.id}/items/{shared.id}/children",
                json={"name": "made-meanwhile", "folder": {}},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            ),
            WAIT_SECONDS,
        )
        assert made.status_code == 201, made.text
        assert not put.done(), "the PUT finished before its store write was released"
    finally:
        gate.release.set()
        await asyncio.gather(put, return_exceptions=True)
    assert put.result().status_code == 201, put.result().text
