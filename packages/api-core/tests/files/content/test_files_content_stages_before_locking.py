"""A content write stages its bytes before it takes the drive row.

Every write in an org queues on its drive row, and a route runs the whole
content write in one transaction, so every lock it takes lasts to its end. The
write used to take the drive (for the fence and the quota hold) and only then
stream the bytes to the store: every folder created, file moved or upload
opened in the org waited behind the upload, and past ``lock_timeout`` answered
503. Here the write runs on a joined repo inside an open transaction — the
route's shape — with its body held mid-stream, while a second backend takes the
drive row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

from alkera_core.config import settings
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import DomainStore, _RootedDomainStore
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files.content.conftest import ContentRig

#: Far above taking one row, far below forever.
WAIT_SECONDS = 10.0

PAYLOAD = b"s" * (settings.files_inline_max_bytes + 1)


async def test_the_drive_row_is_free_while_the_bytes_stream(
    content_rig: ContentRig, files_engine: AsyncEngine, clock: FakeClock
) -> None:
    rig = content_rig
    started, release = asyncio.Event(), asyncio.Event()

    async def held_open() -> AsyncIterator[bytes]:
        yield PAYLOAD[:1024]
        started.set()
        await release.wait()
        yield PAYLOAD[1024:]

    joined = FilesRepo.joined(rig.session, rig.repo.scope)
    store = cast(DomainStore, _RootedDomainStore(rig.store, rig.domain_id))
    service = ContentService(joined, rig.ctx, clock, store)
    write = asyncio.create_task(
        service.put_version(
            rig.node_id("a.bin"), held_open(), size_declared=len(PAYLOAD), if_match=0
        )
    )
    other = AsyncSession(bind=files_engine)
    try:
        await asyncio.wait_for(started.wait(), WAIT_SECONDS)
        other_repo = FilesRepo(other, rig.repo.scope)

        async def take_the_drive() -> None:
            async with other_repo.transaction():
                assert await other_repo.lock_drive(rig.drive_id) is not None

        await asyncio.wait_for(take_the_drive(), WAIT_SECONDS)
        assert not write.done(), "the write finished before its body was released"
    finally:
        release.set()
        await asyncio.gather(write, return_exceptions=True)
        await other.close()

    info = write.result()
    await rig.session.commit()
    assert info.size == len(PAYLOAD)
    chunks = [chunk async for chunk in await rig.service.open(info.id, range=None)]
    assert b"".join(chunks) == PAYLOAD
