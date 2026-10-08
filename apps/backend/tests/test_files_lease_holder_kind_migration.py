"""The holder-kind revision's back-fill, driven by Alembic against real Postgres.

``alembic check`` compares columns and types. It says nothing about what a new
column is filled with on the way up — and this one decides, for every lease that
is live across the deploy, whether its holder is still recognised as its holder.
A box's lease and a person's lease are the same shape: both record a uuid and
both were written with ``holder_principal_kind = 'user'``. What tells them apart
is that a box sends the machine it asserts as its own label, so its
``machine_id`` IS its ``holder_principal_id``; a person's mount carries the name
of their laptop there.

The case seeds one of each at the parent revision, upgrades, and reads the two
kinds back. Filling them both ``user`` — the shape this revision shipped with —
leaves the box fenced out of the chat it is holding: its writes, its beat and
its release all answer ``files.lease_fenced`` and it cannot re-acquire either.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = pytest.mark.asyncio

_PARENT = "0138"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _seed_drive(session: AsyncSession, org_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A store, a dedup domain and a drive; returns ``(drive_id, domain_id)``."""
    store_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', :bucket, '{}'::jsonb, ARRAY['proxied'])"
        ),
        {"id": store_id, "bucket": f"kind-seed-{store_id.hex}"},
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
    return drive_id, domain_id


_INSERT_NODE = (
    "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, subtype, "
    "target_object_id, name, flags_names, path_ids, depth, mode, uid, gid, nlink, size, "
    "rdev, atime_ns, mtime_ns, ctime_ns, birthtime_ns, xattrs, etag, flags, traversal_only, "
    "metadata) VALUES (:id, :ino, :drive, :org, NULL, 'folder', NULL, NULL, :name, "
    "'{}'::jsonb, CAST(:path AS ltree), 0, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, "
    "0, 0, false, '{}'::jsonb)"
)


async def _folder(
    session: AsyncSession, *, drive_id: uuid.UUID, org_id: uuid.UUID, ino: int, name: bytes
) -> uuid.UUID:
    node_id = uuid.uuid4()
    await session.execute(
        text(_INSERT_NODE),
        {
            "id": node_id,
            "ino": ino,
            "drive": drive_id,
            "org": org_id,
            "name": name,
            "path": str(node_id).replace("-", "_"),
        },
    )
    return node_id


#: The acquire statement as it stood at the parent revision: no ``holder_kind``
#: column to write, and ``holder_principal_kind`` the literal ``'user'`` for
#: every holder, a box included — which is exactly why it cannot be read as the
#: kind of the id beside it.
_INSERT_LEASE = (
    "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
    "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
    "VALUES (:node, :org, 1, 'user', :holder, :instance, :machine, :purpose, "
    "now() + interval '1 hour')"
)

_INSERT_SESSION = (
    "INSERT INTO file_upload_sessions (id, org_team_id, drive_id, parent_id, name, "
    "dedup_domain_id, declared_size, bytes_received, lease_epoch, lease_holder, "
    "quota_hold_bytes, quota_hold_nodes, expires_at) "
    "VALUES (:id, :org, :drive, :parent, :name, :domain, 0, 0, 1, :holder, 0, 0, "
    "now() + interval '1 hour')"
)


async def test_the_upgrade_kinds_a_box_as_a_machine_and_a_person_as_a_user() -> None:
    """Two live leases across the deploy, one of each, plus the upload session
    each of them has open.

    The box's row is distinguishable from the person's by nothing but its own
    signature — it published the machine it asserts as its label, so the label
    and the holder are the same uuid — and that is what the back-fill reads.
    Nothing else about the two rows differs: both say ``holder_principal_kind =
    'user'``, because that is the literal the acquire wrote for every holder.
    """
    org_id = uuid.uuid4()
    machine_id = uuid.uuid4()
    person_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            drive_id, domain_id = await _seed_drive(session, org_id)
            chat = await _folder(
                session, drive_id=drive_id, org_id=org_id, ino=1, name=b"Kickoff.alkerachat"
            )
            mount = await _folder(session, drive_id=drive_id, org_id=org_id, ino=2, name=b"project")
            await session.commit()

        box_session = uuid.uuid4()
        person_session = uuid.uuid4()
        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _column(session, "file_leases", "holder_kind") is None, (
                "the column survived its own downgrade"
            )
            assert await _column(session, "file_upload_sessions", "lease_holder_kind") is None
            # The box: the machine it asserts is both its holder and its label.
            await session.execute(
                text(_INSERT_LEASE),
                {
                    "node": chat,
                    "org": org_id,
                    "holder": machine_id,
                    "instance": f"{machine_id}:chat-a",
                    "machine": str(machine_id),
                    "purpose": "chat",
                },
            )
            # The person: a laptop's name, which is not a uuid at all.
            await session.execute(
                text(_INSERT_LEASE),
                {
                    "node": mount,
                    "org": org_id,
                    "holder": person_id,
                    "instance": "instance-a",
                    "machine": "MacBook Pro",
                    "purpose": "mount",
                },
            )
            for session_id, parent, holder in (
                (box_session, chat, machine_id),
                (person_session, mount, person_id),
            ):
                await session.execute(
                    text(_INSERT_SESSION),
                    {
                        "id": session_id,
                        "org": org_id,
                        "drive": drive_id,
                        "parent": parent,
                        "name": b"report.md",
                        "domain": domain_id,
                        "holder": holder,
                    },
                )
            await session.commit()

        await db.upgrade()
        async with db.session() as session:
            assert await _kind(session, chat) == "machine", (
                "the box's own chat lease came back kinded as a person, which fences "
                "the box out of the folder it is holding"
            )
            assert await _kind(session, mount) == "user"
            assert await _session_kind(session, box_session) == "machine"
            assert await _session_kind(session, person_session) == "user"


async def test_the_downgrade_clears_what_the_back_fill_wrote() -> None:
    """The inverse. A lease kinded ``machine`` on the way up carries no trace of
    it at the parent revision — the concept does not exist there — and the
    upgrade derives it again from the row rather than from anything kept."""
    org_id = uuid.uuid4()
    machine_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            drive_id, _domain = await _seed_drive(session, org_id)
            chat = await _folder(
                session, drive_id=drive_id, org_id=org_id, ino=1, name=b"Retro.alkerachat"
            )
            await session.execute(
                text(_INSERT_LEASE),
                {
                    "node": chat,
                    "org": org_id,
                    "holder": machine_id,
                    "instance": f"{machine_id}:chat-b",
                    "machine": str(machine_id),
                    "purpose": "chat",
                },
            )
            await session.execute(
                text("UPDATE file_leases SET holder_kind = 'machine' WHERE node_id = :n"),
                {"n": chat},
            )
            await session.commit()

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _column(session, "file_leases", "holder_kind") is None
            assert (
                await _scalar(session, "SELECT purpose FROM file_leases WHERE node_id = :n", n=chat)
                == "chat"
            ), "the downgrade took the lease row with it"

        await db.upgrade()
        async with db.session() as session:
            assert await _kind(session, chat) == "machine"


async def _kind(session: AsyncSession, node_id: uuid.UUID) -> Any:
    return await _scalar(
        session, "SELECT holder_kind FROM file_leases WHERE node_id = :n", n=node_id
    )


async def _session_kind(session: AsyncSession, session_id: uuid.UUID) -> Any:
    return await _scalar(
        session, "SELECT lease_holder_kind FROM file_upload_sessions WHERE id = :i", i=session_id
    )


async def _column(session: AsyncSession, table: str, column: str) -> Any:
    return await _scalar(
        session,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c",
        t=table,
        c=column,
    )
