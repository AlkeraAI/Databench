"""Revision 0177 against the real schema: a document records where its source
stands (whole or not at all), ``seeded_from`` takes a file version's name, a
chat's draft row loads exactly as before, and on the way down the file rows
(which the old schema cannot describe) go while every draft stays."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin
from tests.migration_harness import migration_scratch, seed_org_admin

_VERSION_NAME = "file_version:" + str(uuid.uuid4()) + ":" + "a" * 64


async def _insert(
    session: AsyncSession, org: uuid.UUID, doc_type: str, doc_id: str, **source: object
) -> None:
    columns = ", ".join(["org_id", "doc_type", "doc_id", "loro_format", "seeded_from", *source])
    values = ", ".join([":org", ":type", ":doc", "'1.16.2'", ":seeded", *(f":{k}" for k in source)])
    await session.execute(
        text(f"INSERT INTO crdt_docs ({columns}) VALUES ({values})"),
        {
            "org": org,
            "type": doc_type,
            "doc": doc_id,
            "seeded": _VERSION_NAME if doc_type == "file" else "legacy_draft",
            **source,
        },
    )


async def _columns(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'crdt_docs'")
    )
    return {str(r[0]) for r in rows}


async def test_a_source_is_recorded_whole_or_not_at_all(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    org = org_admin.org_id
    await _insert(
        real_session,
        org,
        "file",
        f"node-{uuid.uuid4()}",
        source_etag=3,
        source_version_id=uuid.uuid4(),
        source_sha256="b" * 64,
        source_vv=b"\x01",
        source_epoch=1,
    )
    # A file with no version yet: an etag, no version id.
    await _insert(
        real_session,
        org,
        "file",
        f"node-{uuid.uuid4()}",
        source_etag=1,
        source_sha256="c" * 64,
        source_vv=b"\x01",
        source_epoch=1,
    )
    await _insert(real_session, org, "chat_workspace", f"chat-{uuid.uuid4()}")
    await real_session.commit()
    with pytest.raises(IntegrityError):
        await _insert(real_session, org, "file", f"node-{uuid.uuid4()}", source_etag=3)
    await real_session.rollback()


async def test_the_downgrade_refuses_while_a_file_session_holds_unsaved_edits() -> None:
    """A session whose content is not the version it last wrote to the drive
    is the only copy of what people typed since: the downgrade refuses rather
    than drop it, and goes through once it is written back."""
    async with migration_scratch() as db:
        org = (await seed_org_admin(db)).org_id
        async with db.session() as session:
            await _insert(
                session,
                org,
                "file",
                "node-1",
                source_etag=3,
                source_version_id=uuid.uuid4(),
                source_sha256="b" * 64,
                source_vv=b"\x01",
                source_epoch=1,
            )
            await session.execute(
                text("UPDATE crdt_docs SET projection = CAST(:p AS jsonb) WHERE doc_id = 'node-1'"),
                {"p": '{"sha256": "' + "c" * 64 + '"}'},
            )
            await session.commit()
        with pytest.raises(Exception, match="not yet written to the drive"):
            await db.downgrade("0176")
        async with db.session() as session:
            assert "source_etag" in await _columns(session)
            await session.execute(
                text("UPDATE crdt_docs SET source_sha256 = :s WHERE doc_id = 'node-1'"),
                {"s": "c" * 64},
            )
            await session.commit()
        await db.downgrade("0176")
        async with db.session() as session:
            assert "source_etag" not in await _columns(session)


async def test_the_downgrade_drops_file_sessions_and_keeps_every_draft() -> None:
    async with migration_scratch() as db:
        org = (await seed_org_admin(db)).org_id
        async with db.session() as session:
            await _insert(
                session,
                org,
                "file",
                "node-1",
                source_etag=3,
                source_version_id=uuid.uuid4(),
                source_sha256="b" * 64,
                source_vv=b"\x01",
                source_epoch=1,
            )
            await _insert(session, org, "chat_workspace", "chat-1")
            await session.commit()
        await db.downgrade("0176")
        async with db.session() as session:
            assert "source_etag" not in await _columns(session)
            # This org's rows: the copy also holds whatever the template had.
            kept = await session.execute(
                text("SELECT doc_type, doc_id, seeded_from FROM crdt_docs WHERE org_id = :org"),
                {"org": org},
            )
            assert [tuple(row) for row in kept] == [("chat_workspace", "chat-1", "legacy_draft")]
        await db.upgrade()
        async with db.session() as session:
            assert {"source_etag", "source_vv", "source_epoch"} <= await _columns(session)
            still = await session.scalar(
                text("SELECT source_etag FROM crdt_docs WHERE doc_id = 'chat-1'")
            )
            assert still is None
