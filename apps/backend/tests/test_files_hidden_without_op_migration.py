"""The repair of nodes a lapsed lease hid with no trash op, against real Postgres.

The reaper used to set ``trashed_at`` on a holder's unlanded files and nothing
else, so they were in no listing, not in the trash, and nothing could restore
them. Revision 0205 files each one in the trash under an op of its own, naming
the machine its folder's lease named, and takes it out of its parent's child
count. A node trashed the ordinary way is not touched, and a second run finds
nothing to do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch
from tests.test_files_share_uniqueness_migration import _seed_drive

pytestmark = pytest.mark.asyncio

_PARENT = "0204"
MACHINE = "box-many"
HIDDEN_AT = datetime(2026, 10, 6, 5, 30, tzinfo=UTC)

_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, name_display, name_key, flags_names, path_ids, depth, mode, "
    "uid, gid, nlink, size, rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, "
    "flags, traversal_only, metadata, trashed_at, trash_op_id, holder_size) VALUES (:id, "
    ":ino, :drive, :org, :parent, :kind, NULL, NULL, :name, :display, :display, '{}'::jsonb, "
    "CAST(:path AS ltree), :depth, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, 0, 0, false, "
    "'{}'::jsonb, :trashed_at, :op, :holder_size)"
)


class Seeded:
    def __init__(self, org_id: uuid.UUID, drive_id: uuid.UUID) -> None:
        self.org_id = org_id
        self.drive_id = drive_id
        self.ids: dict[str, uuid.UUID] = {}
        self.paths: dict[str, str] = {}
        self.ino = 0


async def _node(
    session: AsyncSession,
    seeded: Seeded,
    name: str,
    *,
    parent: str | None,
    kind: str = "file",
    trashed_at: datetime | None = None,
    op: uuid.UUID | None = None,
    holder_size: int | None = None,
) -> uuid.UUID:
    seeded.ino += 1
    node_id = uuid.uuid4()
    label = f"i{seeded.ino}"
    path = label if parent is None else f"{seeded.paths[parent]}.{label}"
    await session.execute(
        text(_INSERT_NODE),
        {
            "id": node_id,
            "ino": seeded.ino,
            "drive": seeded.drive_id,
            "org": seeded.org_id,
            "parent": None if parent is None else seeded.ids[parent],
            "kind": kind,
            "name": name.encode(),
            "display": name,
            "path": path,
            "depth": path.count("."),
            "trashed_at": trashed_at,
            "op": op,
            "holder_size": holder_size,
        },
    )
    seeded.ids[name] = node_id
    seeded.paths[name] = path
    return node_id


async def _trash_op(session: AsyncSession, seeded: Seeded, root: uuid.UUID) -> uuid.UUID:
    op_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_trash_ops (id, org_team_id, drive_id, root_node_id, actor_id, "
            "deleted_at, purge_after) VALUES (:id, :org, :drive, :root, :actor, :at, :purge)"
        ),
        {
            "id": op_id,
            "org": seeded.org_id,
            "drive": seeded.drive_id,
            "root": root,
            "actor": uuid.uuid4(),
            "at": HIDDEN_AT,
            "purge": HIDDEN_AT + timedelta(days=30),
        },
    )
    return op_id


async def _seed(session: AsyncSession) -> Seeded:
    """A folder a box held, three of its files hidden by the reap, one trashed
    by a person, one live; and a folder nobody leased with one hidden file."""
    org_id = uuid.uuid4()
    seeded = Seeded(org_id, await _seed_drive(session, org_id))
    await _node(session, seeded, "root", parent=None, kind="folder")
    await _node(session, seeded, "many", parent="root", kind="folder")
    await _node(session, seeded, "loose", parent="root", kind="folder")
    await session.execute(
        text(
            "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
            "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at, "
            "reaped_at) VALUES (:node, :org, 3, 'user', :actor, 'box-7:ws', :machine, 'mount', "
            ":at, :at)"
        ),
        {
            "node": seeded.ids["many"],
            "org": org_id,
            "actor": uuid.uuid4(),
            "machine": MACHINE,
            "at": HIDDEN_AT,
        },
    )
    for name in ("f1.txt", "f2.txt", "f3.txt"):
        await _node(session, seeded, name, parent="many", trashed_at=HIDDEN_AT, holder_size=3)
    await _node(session, seeded, "live.txt", parent="many")
    await _node(session, seeded, "deleted.txt", parent="many")
    op_id = await _trash_op(session, seeded, seeded.ids["deleted.txt"])
    await session.execute(
        text("UPDATE file_nodes SET trashed_at = :at, trash_op_id = :op WHERE id = :id"),
        {"at": HIDDEN_AT, "op": op_id, "id": seeded.ids["deleted.txt"]},
    )
    await _node(session, seeded, "stray.txt", parent="loose", trashed_at=HIDDEN_AT)
    await session.commit()
    return seeded


async def _rows(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    rows = list((await session.execute(text(sql), params)).all())
    await session.commit()
    return rows


async def _ops(session: AsyncSession, seeded: Seeded) -> dict[str, Any]:
    rows = await _rows(
        session,
        "SELECT n.name, n.trash_op_id, n.holder_size, o.reason, o.reason_machine, o.deleted_at, "
        "o.purge_after, o.root_node_id, o.org_team_id FROM file_nodes n "
        "LEFT JOIN file_trash_ops o ON o.id = n.trash_op_id WHERE n.drive_id = :d",
        d=seeded.drive_id,
    )
    return {bytes(row.name).decode(): row for row in rows}


async def _deltas(session: AsyncSession, seeded: Seeded) -> dict[str, int]:
    rows = await _rows(
        session,
        "SELECT node_id, sum(direct_children_delta) AS d FROM file_dir_stats_deltas "
        "WHERE org_team_id = :org GROUP BY node_id",
        org=seeded.org_id,
    )
    names = {node_id: name for name, node_id in seeded.ids.items()}
    return {names[row.node_id]: int(row.d) for row in rows}


async def test_every_hidden_node_gets_a_restorable_op_and_nothing_else_moves() -> None:
    async with migration_scratch() as db:
        await db.downgrade(_PARENT)
        async with db.session() as session:
            seeded = await _seed(session)
            before = await _ops(session, seeded)
            started = (await _rows(session, "SELECT now() AS now"))[0].now

        await db.upgrade()

        async with db.session() as session:
            after = await _ops(session, seeded)
            for name in ("f1.txt", "f2.txt", "f3.txt"):
                row = after[name]
                assert row.trash_op_id is not None, name
                assert (row.reason, row.reason_machine) == ("left_on_machine", MACHINE)
                assert row.root_node_id == seeded.ids[name]
                assert row.org_team_id == seeded.org_id
                assert row.deleted_at == HIDDEN_AT
                assert row.purge_after >= started + timedelta(days=30)
                assert row.holder_size is None
            # Nothing names a machine for a folder no lease row is left on.
            assert after["stray.txt"].trash_op_id is not None
            assert after["stray.txt"].reason_machine is None
            # One op per node, so a restore of one brings back that one.
            ops = {after[name].trash_op_id for name in ("f1.txt", "f2.txt", "f3.txt")}
            assert len(ops) == 3
            # A person's trash and the live tree are left exactly as they were.
            assert after["deleted.txt"].trash_op_id == before["deleted.txt"].trash_op_id
            assert after["deleted.txt"].reason is None
            assert after["live.txt"].trash_op_id is None
            assert await _deltas(session, seeded) == {"many": -3, "loose": -1}


async def test_a_second_run_finds_nothing_to_repair() -> None:
    async with migration_scratch() as db:
        await db.downgrade(_PARENT)
        async with db.session() as session:
            seeded = await _seed(session)
        await db.upgrade()
        async with db.session() as session:
            first = await _ops(session, seeded)
            first_deltas = await _deltas(session, seeded)

        await db.downgrade(_PARENT)
        await db.upgrade()

        async with db.session() as session:
            again = await _ops(session, seeded)
            assert {name: row.trash_op_id for name, row in again.items()} == {
                name: row.trash_op_id for name, row in first.items()
            }
            assert await _deltas(session, seeded) == first_deltas
            count = await _rows(
                session,
                "SELECT count(*) AS c FROM file_trash_ops WHERE org_team_id = :org",
                org=seeded.org_id,
            )
            assert count[0].c == 5
