"""Revision 0180 against the real schema: the three parking columns and the
sweep's partial index come and go together, and a parked session's columns
read back as written."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = [pytest.mark.asyncio]

COLUMNS = {"save_paused_reason", "save_retry_at", "save_failures"}


async def _columns(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'crdt_docs'")
    )
    return {str(r[0]) for r in rows}


async def _index(session: AsyncSession) -> str | None:
    found = await session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_crdt_docs_unsaved'")
    )
    return None if found is None else str(found)


async def test_the_parking_columns_and_the_sweep_index_come_and_go_together() -> None:
    async with migration_scratch() as db:
        async with db.session() as session:
            assert COLUMNS <= await _columns(session)
            index = await _index(session)
            assert index is not None
            assert "save_retry_at, updated_at" in index and "source_etag IS NOT NULL" in index
        await db.downgrade("0179")
        async with db.session() as session:
            assert not COLUMNS & await _columns(session)
            assert await _index(session) is None
        await db.upgrade()
        async with db.session() as session:
            assert COLUMNS <= await _columns(session)
            assert await _index(session) is not None
            default = await session.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'crdt_docs' AND column_name = 'save_failures'"
                )
            )
            assert default == "0"
