"""The holder-facet revision against the real schema.

Four nullable columns and no back-fill, so what is worth pinning is what
``alembic check`` does not compare: the round trip leaves the rows it found
untouched in both directions, the downgrade really removes the columns (a
no-op downgrade would pass a check that only compares heads), and the
revision the running code expects is this one.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import alembic_config, migration_scratch, script_head
from tests.test_files_share_uniqueness_migration import _seed_drive

pytestmark = pytest.mark.asyncio

_REVISION = "0154"
_PARENT = "0153"
_COLUMNS = ("holder_size", "holder_mtime_ns", "holder_hash", "holder_seq")


async def _columns(session: AsyncSession) -> dict[str, tuple[str, str]]:
    rows = (
        await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'file_nodes' AND column_name = ANY(:names)"
            ),
            {"names": list(_COLUMNS)},
        )
    ).all()
    return {row.column_name: (row.data_type, row.is_nullable) for row in rows}


async def _file(session: AsyncSession, *, org_id: uuid.UUID, drive_id: uuid.UUID) -> uuid.UUID:
    node_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, name, "
            "name_display, name_key, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
            "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, "
            "traversal_only, metadata) VALUES (:id, 7, :drive, :org, NULL, 'file', 'kept.txt', "
            "'kept.txt', 'kept.txt', '{}'::jsonb, CAST(:path AS ltree), 0, 420, 0, 0, 1, 3, 0, "
            "0, 17, 0, 0, '{}'::jsonb, 4, 0, false, '{}'::jsonb)"
        ),
        {"id": node_id, "drive": drive_id, "org": org_id, "path": node_id.hex},
    )
    return node_id


async def test_the_revision_is_below_the_head_the_code_expects() -> None:
    """Later revisions sit on top; this revision stays in the chain."""
    assert script_head(alembic_config()) == EXPECTED_SCHEMA_HEAD
    assert int(EXPECTED_SCHEMA_HEAD) >= 156


async def test_the_columns_are_four_nullable_holder_facts_at_head() -> None:
    async with AsyncSessionLocal() as session:
        assert await _columns(session) == {
            "holder_size": ("bigint", "YES"),
            "holder_mtime_ns": ("bigint", "YES"),
            "holder_hash": ("bytea", "YES"),
            "holder_seq": ("bigint", "YES"),
        }


async def test_down_and_up_again_drops_the_columns_and_keeps_every_row() -> None:
    org_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            drive_id = await _seed_drive(session, org_id)
            node_id = await _file(session, org_id=org_id, drive_id=drive_id)
            await session.execute(
                text(
                    "UPDATE file_nodes SET holder_size = 9, holder_mtime_ns = 8, "
                    "holder_hash = '\\x01'::bytea, holder_seq = 2 WHERE id = :id"
                ),
                {"id": node_id},
            )
            await session.commit()

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _columns(session) == {}
            row = (
                await session.execute(
                    text("SELECT name, size, mtime_ns, etag FROM file_nodes WHERE id = :id"),
                    {"id": node_id},
                )
            ).one()
            assert (bytes(row.name), row.size, row.mtime_ns, row.etag) == (b"kept.txt", 3, 17, 4)

        await db.upgrade()
        async with db.session() as session:
            assert set(await _columns(session)) == set(_COLUMNS)
            row = (
                await session.execute(
                    text(
                        "SELECT size, mtime_ns, holder_size, holder_mtime_ns, holder_hash, "
                        "holder_seq FROM file_nodes WHERE id = :id"
                    ),
                    {"id": node_id},
                )
            ).one()
            # The report went with the columns; the row itself did not.
            assert (row.size, row.mtime_ns) == (3, 17)
            assert (row.holder_size, row.holder_mtime_ns, row.holder_hash, row.holder_seq) == (
                None,
                None,
                None,
                None,
            )


async def test_alembic_check_reports_no_drift_at_head() -> None:
    """`make migrate-check`'s own gate, so a model/migration drift fails here too."""
    command.check(alembic_config())


async def test_a_round_trip_below_the_conflict_revisions_keeps_an_auto_conflict() -> None:
    """The drive's own settlements are rows a downgrade may not narrow away:
    through 0156, 0155 and this revision and back, an ``auto`` conflict is
    still there, still ``auto``, still naming the versions it named."""
    org_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            drive_id = await _seed_drive(session, org_id)
            node_id = await _file(session, org_id=org_id, drive_id=drive_id)
            versions = [uuid.uuid4(), uuid.uuid4()]
            for seq, version_id in enumerate(versions, start=1):
                await session.execute(
                    text(
                        "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
                        "content_hash, block_hash, source, scan_state, keep_forever, held, "
                        "lease_epoch, metadata) VALUES (:id, :org, :node, :seq, 3, :hash, '', "
                        "'upload', 'clean', false, false, 0, '{}'::jsonb)"
                    ),
                    {
                        "id": version_id,
                        "org": org_id,
                        "node": node_id,
                        "seq": seq,
                        "hash": f"h{seq}",
                    },
                )
            conflict_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO file_conflicts (id, org_team_id, node_id, base_version_id, "
                    "theirs_version_id, mine_version_id, actor, state, arrived_from, who) VALUES "
                    "(:id, :org, :node, :base, :theirs, :mine, :actor, 'auto', 'holder', 'box')"
                ),
                {
                    "id": conflict_id,
                    "org": org_id,
                    "node": node_id,
                    "base": versions[0],
                    "theirs": versions[0],
                    "mine": versions[1],
                    "actor": uuid.uuid4(),
                },
            )
            await session.commit()

        await db.downgrade(_PARENT)
        async with db.session() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM file_conflicts WHERE id = :id"), {"id": conflict_id}
                )
            ).scalar_one()
            assert count == 1

        await db.upgrade()
        async with db.session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, node_id, theirs_version_id, mine_version_id "
                        "FROM file_conflicts WHERE id = :id"
                    ),
                    {"id": conflict_id},
                )
            ).one()
        assert (row.state, row.node_id, row.theirs_version_id, row.mine_version_id) == (
            "auto",
            node_id,
            versions[0],
            versions[1],
        )
