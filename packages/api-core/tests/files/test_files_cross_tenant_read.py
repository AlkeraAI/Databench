"""The one seam a platform job reads a FORCE-RLS table through.

Real Postgres and the real ``file_drives`` policy: the stand-in for a login the
policy binds is ``SET ROLE alkera_files_app`` (no superuser, no BYPASSRLS,
SELECT granted), which is exactly what an ordinary application login is to
these tables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db.cross_tenant import CrossTenantReadRefused, cross_tenant_read
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = pytest.mark.asyncio

COUNT_DRIVES = text("SELECT count(*) FROM file_drives")


@pytest.fixture
async def bound_session() -> AsyncIterator[AsyncSession]:
    """A session whose login the policy binds, on an engine of its own.

    ``SET ROLE`` is per connection. On the suite's shared pool the statement
    after a commit may run on another connection -- as the superuser -- while
    the connection that took the role goes back to the pool and poisons the
    next test that borrows it. One connection, disposed with the fixture, is
    the only shape in which the role is both in force and gone afterwards.
    """
    engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        await session.execute(text("SET ROLE alkera_files_app"))
        await session.commit()
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def test_a_bypassing_login_sees_every_tenants_rows(
    files_session: AsyncSession, files_factory: Any
) -> None:
    drive = await files_factory.drive()

    async with cross_tenant_read(files_session, reason="test") as read:
        seen = (
            await read.execute(
                text("SELECT count(*) FROM file_drives WHERE id = :id"), {"id": drive.id}
            )
        ).scalar_one()

    assert seen == 1
    assert not files_session.in_transaction(), "the window is the seam's own and it ends it"


async def test_a_login_the_policy_binds_is_refused_not_filtered(
    bound_session: AsyncSession, files_factory: Any
) -> None:
    """The hazard in one case: unscoped, this login reads the table as empty
    with no error. Through the seam the same read is a refusal."""
    await files_factory.drive()
    filtered = (await bound_session.execute(COUNT_DRIVES)).scalar_one()
    await bound_session.rollback()
    assert filtered == 0, "the stand-in login is supposed to be bound by the policy"

    with pytest.raises(CrossTenantReadRefused):
        async with cross_tenant_read(bound_session, reason="test") as read:
            await read.execute(COUNT_DRIVES)


async def test_the_seam_refuses_a_session_with_work_in_flight(
    files_session: AsyncSession,
) -> None:
    """It ends its window with a rollback, so it must never be handed a
    transaction whose writes that rollback would discard."""
    await files_session.execute(text("SELECT 1"))
    assert files_session.in_transaction()
    try:
        with pytest.raises(RuntimeError, match="no open transaction"):
            async with cross_tenant_read(files_session, reason="test"):
                pass
    finally:
        await files_session.rollback()
