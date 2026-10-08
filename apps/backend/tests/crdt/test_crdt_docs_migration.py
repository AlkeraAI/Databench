"""Revision 0174 against the real schema: the CRDT lane's tables arrive empty
with no per-org switch beside them, and on the way down every draft goes back
to the op-log document the remaining lane reads it from."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch, seed_org_admin

_BACKEND = Path(__file__).resolve().parents[2]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0174_crdt_docs.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0173"
_TABLES = {"crdt_docs", "crdt_updates", "crdt_peers"}


def test_the_revision_is_the_schema_the_code_expects() -> None:
    assert _REVISION == "0174"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


async def _tables(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    )
    return {str(r[0]) for r in rows}


async def _has_flag_column(session: AsyncSession) -> bool:
    found = await session.scalar(
        text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'org_settings' "
            "AND column_name = 'chat_workspace_crdt_enabled'"
        )
    )
    return bool(found)


async def _chat_doc(
    session: AsyncSession, org: uuid.UUID, doc_id: str, meta: dict[str, Any]
) -> None:
    await session.execute(
        text(
            "INSERT INTO realtime_docs (org_id, doc_type, doc_id, state) "
            "VALUES (:org, 'chat', :doc, CAST(:state AS jsonb))"
        ),
        {"org": org, "doc": doc_id, "state": json.dumps({"meta": meta, "events": []})},
    )


async def _crdt_doc(
    session: AsyncSession, org: uuid.UUID, doc_id: str, projection: dict[str, Any]
) -> None:
    await session.execute(
        text(
            "INSERT INTO crdt_docs (org_id, doc_type, doc_id, loro_format, seeded_from, "
            "projection) VALUES (:org, 'chat_workspace', :doc, '1.16.2', 'empty', "
            "CAST(:projection AS jsonb))"
        ),
        {"org": org, "doc": doc_id, "projection": json.dumps(projection)},
    )


async def _meta(session: AsyncSession, org: uuid.UUID, doc_id: str) -> dict[str, Any]:
    value = await session.scalar(
        text(
            "SELECT state -> 'meta' FROM realtime_docs "
            "WHERE org_id = :org AND doc_type = 'chat' AND doc_id = :doc"
        ),
        {"org": org, "doc": doc_id},
    )
    assert isinstance(value, dict)
    return value


async def test_the_upgrade_adds_empty_tables_and_no_per_org_switch() -> None:
    async with migration_scratch() as db:
        await seed_org_admin(db)
        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert not (_TABLES & await _tables(session))
        await db.upgrade()
        async with db.session() as session:
            assert _TABLES <= await _tables(session)
            for table in sorted(_TABLES):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            # Every org is on the lane: nothing records an org's choice.
            assert not await _has_flag_column(session)
            retired = await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'crdt_docs' AND column_name = 'retired_at'"
                )
            )
            assert retired == 0


async def test_the_downgrade_hands_every_draft_back_to_the_op_log() -> None:
    async with migration_scratch() as db:
        org = await seed_org_admin(db)
        live, bare, orphan = (f"chat-{uuid.uuid4()}" for _ in range(3))
        async with db.session() as session:
            await _chat_doc(
                session,
                org.org_id,
                live,
                {
                    "title": "Kept",
                    "draft": {"text": "stale", "at": 1.0, "by_user_id": "", "peer_id": "p"},
                },
            )
            await _crdt_doc(
                session, org.org_id, live, {"text": "what about Q3?", "by_user_id": "u-1"}
            )
            # A chat whose op-log document has no meta at all yet.
            await session.execute(
                text(
                    "INSERT INTO realtime_docs (org_id, doc_type, doc_id, state) "
                    "VALUES (:org, 'chat', :doc, '{}'::jsonb)"
                ),
                {"org": org.org_id, "doc": bare},
            )
            await _crdt_doc(session, org.org_id, bare, {"text": "first words"})
            # A workspace document with no op-log document is nothing to write to.
            await _crdt_doc(session, org.org_id, orphan, {"text": "nowhere"})
            await session.commit()

        await db.downgrade(_PARENT)

        async with db.session() as session:
            assert not (_TABLES & await _tables(session))
            meta = await _meta(session, org.org_id, live)
            assert meta["title"] == "Kept"
            assert meta["draft"]["text"] == "what about Q3?"
            assert meta["draft"]["by_user_id"] == "u-1"
            assert meta["draft"]["peer_id"] == "srv:0"
            assert meta["draft"]["at"] > 1.0
            assert (await _meta(session, org.org_id, bare))["draft"]["text"] == "first words"
            orphans = await session.scalar(
                text("SELECT count(*) FROM realtime_docs WHERE doc_id = :doc"), {"doc": orphan}
            )
            assert orphans == 0

        await db.upgrade()
        async with db.session() as session:
            assert _TABLES <= await _tables(session)
