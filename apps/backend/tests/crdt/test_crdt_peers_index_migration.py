"""Revision 0175 against the real schema: a person's peers for one document
are found through an index that names the person, and the document-only index
it replaces is gone (its prefix serves those reads)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch


async def _indexes(session: AsyncSession) -> dict[str, str]:
    rows = await session.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'crdt_peers'")
    )
    return {str(name): str(definition) for name, definition in rows}


async def test_the_upgrade_indexes_a_persons_peers_and_the_downgrade_restores_the_old_index() -> (
    None
):
    async with migration_scratch() as db:
        async with db.session() as session:
            at_head = await _indexes(session)
        assert "ix_crdt_peers_doc" not in at_head
        assert at_head["ix_crdt_peers_doc_user"].endswith("(org_id, doc_type, doc_id, user_id)")

        await db.downgrade("0174")
        async with db.session() as session:
            below = await _indexes(session)
        assert "ix_crdt_peers_doc_user" not in below
        assert below["ix_crdt_peers_doc"].endswith("(org_id, doc_type, doc_id)")

        await db.upgrade()
        async with db.session() as session:
            assert await _indexes(session) == at_head
