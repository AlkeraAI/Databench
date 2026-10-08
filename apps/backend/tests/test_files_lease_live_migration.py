"""The live-plane revision against the real schema.

``alembic check`` compares columns and types. It does not compare constraint
names, server defaults, row-level security or the inverse a downgrade is
supposed to be — and this revision carries all four, plus a back-fill that
marks a chat's own records so nobody but the machine holding the lease can
rewrite them. Those are what this module pins.

The back-fill case is the sharp one: it drives the revision down and back up
against a seeded tree and asserts the mark landed on exactly the three records
and everything the runtime folder holds — and on nothing in the working
directory, and on nothing wearing the same names outside a chat.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.decider import RECORD_BIT
from alkera_core.models.files.leases import LIVE_ENTRY_STATES
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0133_lease_live_sync.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0132"
_TABLE = "file_lease_live_entries"

#: The chat's own records: the transcript's manifest, the trace digest and the
#: runtime directory beside the working directory. A person may read them; only
#: the machine holding the lease may rewrite them.
_RECORD_NAMES = (b"manifest.json", b"trace.digest.json", b".runtime")
_SANDBOX = b"scratch"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


# ---------------------------------------------------------------- the seeds


async def _seed_drive(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    store_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', :bucket, '{}'::jsonb, ARRAY['proxied'])"
        ),
        {"id": store_id, "bucket": f"live-seed-{store_id.hex}"},
    )
    domain_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO dedup_domains (id, org_team_id, store_id, chunker_seed) "
            "VALUES (:id, :org, :store, '\\x00'::bytea)"
        ),
        {"id": domain_id, "org": org_id, "store": store_id},
    )
    drive_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_drives "
            "(id, org_team_id, store_id, dedup_domain_id, quota_bytes, quota_nodes, next_ino) "
            "VALUES (:id, :org, :store, :domain, 0, 0, 1)"
        ),
        {"id": drive_id, "org": org_id, "store": store_id, "domain": domain_id},
    )
    return drive_id


_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
    "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, "
    "metadata) VALUES (:id, :ino, :drive, :org, :parent, :kind, :subtype, :object, :name, "
    "'{}'::jsonb, CAST(:path AS ltree), :depth, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, "
    "0, 0, false, '{}'::jsonb)"
)


async def _node(
    session: AsyncSession,
    *,
    drive_id: uuid.UUID,
    org_id: uuid.UUID,
    ino: int,
    name: bytes,
    kind: str = "folder",
    parent: tuple[uuid.UUID, str, int] | None = None,
    subtype: str | None = None,
    target_object_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str, int]:
    """Insert one node; return ``(id, path_ids, depth)`` for its children."""
    node_id = uuid.uuid4()
    label = str(node_id).replace("-", "_")
    path = label if parent is None else f"{parent[1]}.{label}"
    depth = 0 if parent is None else parent[2] + 1
    await session.execute(
        text(_INSERT_NODE),
        {
            "id": node_id,
            "ino": ino,
            "drive": drive_id,
            "org": org_id,
            "parent": None if parent is None else parent[0],
            "kind": kind,
            "subtype": subtype,
            "object": target_object_id,
            "name": name,
            "path": path,
            "depth": depth,
        },
    )
    return node_id, path, depth


async def _seed_a_chat_and_a_lookalike(
    session: AsyncSession, org_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    """A chat folder with its records and its working directory, plus an
    ordinary folder holding files under the very same names."""
    drive_id = await _seed_drive(session, org_id)
    root = await _node(session, drive_id=drive_id, org_id=org_id, ino=1, name=b"root")
    chat = await _node(
        session,
        drive_id=drive_id,
        org_id=org_id,
        ino=2,
        name=b"Wednesday.chat",
        parent=root,
        subtype="chat",
        target_object_id=uuid.uuid4(),
    )
    out: dict[str, uuid.UUID] = {"chat": chat[0]}
    ino = 3
    for name in (b"manifest.json", b"trace.digest.json"):
        node = await _node(
            session,
            drive_id=drive_id,
            org_id=org_id,
            ino=ino,
            name=name,
            kind="file",
            parent=chat,
        )
        out[name.decode()] = node[0]
        ino += 1
    runtime = await _node(
        session, drive_id=drive_id, org_id=org_id, ino=ino, name=b".runtime", parent=chat
    )
    ino += 1
    out[".runtime"] = runtime[0]
    deep = await _node(
        session,
        drive_id=drive_id,
        org_id=org_id,
        ino=ino,
        name=b"session.sock.json",
        kind="file",
        parent=runtime,
    )
    ino += 1
    out[".runtime/session.sock.json"] = deep[0]
    sandbox = await _node(
        session, drive_id=drive_id, org_id=org_id, ino=ino, name=_SANDBOX, parent=chat
    )
    ino += 1
    out["scratch"] = sandbox[0]
    # A working file wearing a record's name: the mark follows the chat's own
    # records, never a name anywhere beneath the chat.
    for name in (b"report.html", b"manifest.json"):
        node = await _node(
            session,
            drive_id=drive_id,
            org_id=org_id,
            ino=ino,
            name=name,
            kind="file",
            parent=sandbox,
        )
        out[f"scratch/{name.decode()}"] = node[0]
        ino += 1
    # An ordinary folder outside any chat, holding the same three names.
    plain = await _node(
        session, drive_id=drive_id, org_id=org_id, ino=ino, name=b"notes", parent=root
    )
    ino += 1
    for name in _RECORD_NAMES:
        node = await _node(
            session,
            drive_id=drive_id,
            org_id=org_id,
            ino=ino,
            name=name,
            kind="file" if name != b".runtime" else "folder",
            parent=plain,
        )
        out[f"notes/{name.decode()}"] = node[0]
        ino += 1
    await session.commit()
    return out


async def _flags(session: AsyncSession, node_id: uuid.UUID) -> int:
    return int(await _scalar(session, "SELECT flags FROM file_nodes WHERE id = :id", id=node_id))


# --------------------------------------------------------------- the pins


def test_the_revision_is_part_of_the_schema_the_code_expects() -> None:
    assert _REVISION == "0133"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


async def test_every_constraint_and_index_reaches_postgres_under_its_name() -> None:
    async with AsyncSessionLocal() as session:
        constraints = {
            str(row[0])
            for row in await session.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)"),
                {"t": _TABLE},
            )
        }
        indexes = {
            str(row[0])
            for row in await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": _TABLE}
            )
        }
    assert {
        "pk_file_lease_live_entries",
        "fk_file_lease_live_entries_lease_node_id_file_leases",
        "fk_file_lease_live_entries_node_id_file_nodes",
        "ck_file_lease_live_entries_state",
    } <= constraints
    assert {
        "ix_file_lease_live_entries_org_lease",
        "ix_file_lease_live_entries_node",
    } <= indexes
    anonymous = {
        name
        for name in constraints | indexes
        if not name.startswith(("pk_", "uq_", "fk_", "ck_", "ix_"))
    }
    assert anonymous == set(), anonymous


async def test_the_lease_gains_its_live_columns_with_the_defaults_a_bare_insert_relies_on() -> None:
    """``compare_server_default`` is off, so a default the model declares and
    the migration forgot is invisible to ``alembic check``. An acquire written
    before the live plane inserts no value for any of the three."""
    async with AsyncSessionLocal() as session:
        defaults = {
            str(row[0]): (None if row[1] is None else str(row[1]))
            for row in await session.execute(
                text(
                    "SELECT column_name, column_default FROM information_schema.columns "
                    "WHERE table_name = 'file_leases'"
                )
            )
        }
    assert defaults["accepts_inbound"] == "false"
    assert defaults["live_seq"] == "0"
    assert defaults["live_cadence"] == "'{}'::jsonb"


async def test_the_live_table_is_tenant_isolated_the_way_every_files_table_is() -> None:
    async with AsyncSessionLocal() as session:
        forced, enabled = (
            await session.execute(
                text("SELECT relforcerowsecurity, relrowsecurity FROM pg_class WHERE relname = :t"),
                {"t": _TABLE},
            )
        ).one()
        policies = {
            str(row[0])
            for row in await session.execute(
                text("SELECT polname FROM pg_policy WHERE polrelid = to_regclass(:t)"),
                {"t": _TABLE},
            )
        }
    assert enabled and forced
    # The Files policy, and the tenant role's (every Files table carries both).
    assert policies == {"files_tenant_isolation", "tenant_isolation"}


@pytest.mark.parametrize("state", LIVE_ENTRY_STATES)
async def test_the_column_admits_every_state_the_holder_can_report(state: str) -> None:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        seeded = await _seed_a_chat_and_a_lookalike(session, org_id)
        node = seeded["scratch/report.html"]
        await session.execute(
            text(
                "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
                "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
                "VALUES (:node, :org, 1, 'user', :actor, 'inst', 'box', 'chat', "
                "now() + interval '1 hour')"
            ),
            {"node": seeded["chat"], "org": org_id, "actor": uuid.uuid4()},
        )
        await session.execute(
            text(
                f"INSERT INTO {_TABLE} (lease_node_id, node_id, org_team_id, state, "
                "lease_epoch, seq) VALUES (:lease, :node, :org, :state, 1, 1)"
            ),
            {"lease": seeded["chat"], "node": node, "org": org_id, "state": state},
        )
        await session.commit()
        assert (
            await _scalar(
                session,
                f"SELECT state FROM {_TABLE} WHERE node_id = :node",
                node=node,
            )
            == state
        )
        # The lease going away takes its live rows with it: the in-flight plane
        # never outlives the claim that made it meaningful.
        await session.execute(
            text("DELETE FROM file_leases WHERE node_id = :node"), {"node": seeded["chat"]}
        )
        await session.commit()
        assert (
            await _scalar(
                session,
                f"SELECT count(*) FROM {_TABLE} WHERE node_id = :node",
                node=node,
            )
            == 0
        )


async def test_a_state_the_vocabulary_does_not_name_is_refused_by_the_database() -> None:
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        seeded = await _seed_a_chat_and_a_lookalike(session, org_id)
        await session.execute(
            text(
                "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
                "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
                "VALUES (:node, :org, 1, 'user', :actor, 'inst', 'box', 'chat', "
                "now() + interval '1 hour')"
            ),
            {"node": seeded["chat"], "org": org_id, "actor": uuid.uuid4()},
        )
        await session.commit()
        with pytest.raises((IntegrityError, DBAPIError)):
            await session.execute(
                text(
                    f"INSERT INTO {_TABLE} (lease_node_id, node_id, org_team_id, state, "
                    "lease_epoch, seq) VALUES (:lease, :node, :org, 'renaming', 1, 1)"
                ),
                {
                    "lease": seeded["chat"],
                    "node": seeded["scratch/report.html"],
                    "org": org_id,
                },
            )
        await session.rollback()


async def test_the_back_fill_marks_a_chats_records_and_nothing_it_is_working_on() -> None:
    """Down and back up against a seeded tree.

    The downgrade's inverse clears the mark, the upgrade puts it back, and it
    lands on the three records and everything the runtime folder holds — never
    on the working directory, never on a working file wearing a record's name,
    and never on the same three names outside a chat.
    """
    org_id = uuid.uuid4()
    marked = {
        "manifest.json",
        "trace.digest.json",
        ".runtime",
        ".runtime/session.sock.json",
    }
    untouched = {
        "chat",
        "scratch",
        "scratch/report.html",
        "scratch/manifest.json",
        "notes/manifest.json",
        "notes/trace.digest.json",
        "notes/.runtime",
    }
    async with migration_scratch() as db:
        async with db.session() as session:
            seeded = await _seed_a_chat_and_a_lookalike(session, org_id)
        assert marked | untouched == set(seeded)

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert (await _scalar(session, "SELECT to_regclass(:t)", t=_TABLE)) is None, (
                "the live table survived its own downgrade"
            )
            for key in marked | untouched:
                assert await _flags(session, seeded[key]) & RECORD_BIT == 0, key

        await db.upgrade()
        async with db.session() as session:
            for key in marked:
                assert await _flags(session, seeded[key]) & RECORD_BIT == RECORD_BIT, key
            for key in untouched:
                assert await _flags(session, seeded[key]) & RECORD_BIT == 0, key
            # Nothing else on the bitfield moved: the back-fill is an OR of one bit.
            assert await _flags(session, seeded["scratch/report.html"]) == 0
