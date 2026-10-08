"""The no-separator revision, driven against real Postgres.

Rows the old home and team derivations already wrote with a ``/`` in their
name are renamed to exactly the spelling the new derivation produces
(``escape_to_name``), so the lookup that identifies a home by its escaped name
finds the folder that is already there. Then the CHECK refuses any new one.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.names import escape_to_name, name_key
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch
from tests.test_files_share_uniqueness_migration import _seed_drive

pytestmark = pytest.mark.asyncio

_PARENT = "0152"
_CONSTRAINT = "ck_file_nodes_name_no_separator"

_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, name_display, name_key, flags_names, path_ids, depth, mode, "
    "uid, gid, nlink, size, rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, "
    "flags, traversal_only, metadata) VALUES (:id, :ino, :drive, :org, NULL, 'folder', NULL, "
    "NULL, :name, :display, :key, '{}'::jsonb, CAST(:path AS ltree), 0, 493, 0, 0, 1, 0, 0, "
    "0, 0, 0, 0, '{}'::jsonb, 0, 0, false, '{}'::jsonb)"
)


async def _node(
    session: AsyncSession, *, drive_id: uuid.UUID, org_id: uuid.UUID, ino: int, name: bytes
) -> uuid.UUID:
    """A folder written the way ``_ensure_child`` writes one: the display is the
    decoded name, the key its fold."""
    node_id = uuid.uuid4()
    await session.execute(
        text(_INSERT_NODE),
        {
            "id": node_id,
            "ino": ino,
            "drive": drive_id,
            "org": org_id,
            "name": name,
            "display": name.decode("utf-8", "replace"),
            "key": name_key(name),
            "path": str(node_id).replace("-", "_"),
        },
    )
    return node_id


async def _row(session: AsyncSession, node_id: uuid.UUID) -> tuple[bytes, str, str]:
    row = (
        await session.execute(
            text("SELECT name, name_display, name_key FROM file_nodes WHERE id = :id"),
            {"id": node_id},
        )
    ).one()
    return bytes(row[0]), str(row[1]), str(row[2])


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(b"a/b@x.com", id="a-home-named-after-an-address"),
        pytest.param(b"/@x.com", id="an-address-that-is-only-a-slash"),
        pytest.param(b"R/D", id="a-team-named-with-a-slash"),
        pytest.param(b"50%/off", id="the-escape-character-beside-it"),
        pytest.param(b"a\\b/c", id="a-backslash-survives-the-byte-round-trip"),
        pytest.param("café/Bar".encode(), id="non-ascii-and-case"),
    ],
)
async def test_the_upgrade_renames_a_separator_to_the_escape_the_derivation_now_writes(
    name: bytes,
) -> None:
    org_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            drive_id = await _seed_drive(session, org_id)
            plain = await _node(session, drive_id=drive_id, org_id=org_id, ino=1, name=b"plans")
            await session.commit()

        await db.downgrade(_PARENT)
        async with db.session() as session:
            broken = await _node(session, drive_id=drive_id, org_id=org_id, ino=2, name=name)
            await session.commit()

        await db.upgrade()
        async with db.session() as session:
            renamed, display, key = await _row(session, broken)
            assert renamed == escape_to_name(name)
            assert b"/" not in renamed
            assert display == renamed.decode("utf-8")
            assert key == name_key(renamed)
            # A name that never held a separator is not touched.
            assert await _row(session, plain) == (b"plans", "plans", "plans")


async def test_a_separator_is_refused_at_head() -> None:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        drive_id = await _seed_drive(session, org_id)
        await session.commit()
    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError, match=_CONSTRAINT):
            await _node(session, drive_id=drive_id, org_id=org_id, ino=1, name=b"a/b")
        await session.rollback()
    async with AsyncSessionLocal() as session:
        await _node(session, drive_id=drive_id, org_id=org_id, ino=1, name=b"a%2Fb")
        await session.commit()
