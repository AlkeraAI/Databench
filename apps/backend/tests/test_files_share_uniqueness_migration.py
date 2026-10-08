"""The one-live-share-per-principal revision, driven against real Postgres.

``alembic check`` compares indexes; it says nothing about the rows a table
already holds. This revision has to collapse them, and which row it keeps
decides who still has access after the deploy: the strongest rung survives, so
nobody is quietly demoted, and the weaker rows are withdrawn rather than
deleted, because a share row is the record of a grant.

The index itself is the other half. Without it two live rows for one principal
come back the moment two requests race, and the weaker one is invisible — a
body takes the strongest live grant — right until somebody removes the person
and the revoke takes only the row the panel was holding.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.db.session import AsyncSessionLocal
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = pytest.mark.asyncio

_PARENT = "0141"
_INDEX = "uq_file_shares_live_principal"


async def _seed_drive(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    """A store, a dedup domain and a drive; returns the drive id."""
    store_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO file_stores (id, driver, bucket, capabilities, transfer_modes) "
            "VALUES (:id, 'filesystem', :bucket, '{}'::jsonb, ARRAY['proxied'])"
        ),
        {"id": store_id, "bucket": f"share-seed-{store_id.hex}"},
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
    "metadata) VALUES (:id, :ino, :drive, :org, NULL, 'folder', NULL, NULL, :name, "
    "'{}'::jsonb, CAST(:path AS ltree), 0, 493, 0, 0, 1, 0, 0, 0, 0, 0, 0, '{}'::jsonb, "
    "0, 0, false, '{}'::jsonb)"
)

#: A share as the table took it before this revision: no uniqueness anywhere, so
#: two of these for one principal both landed.
_INSERT_SHARE = (
    "INSERT INTO file_shares (id, org_team_id, node_id, principal_kind, principal_id, "
    "role, granted_by) VALUES (:id, :org, :node, :kind, :principal, :role, :by)"
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


async def _share(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    role: str,
    kind: str = "org",
    principal_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One live share.

    The principal is the org itself (or the org root read as a team), which is
    the one shape the ``files_share_principal_in_org`` trigger admits without a
    membership graph behind it — and the trigger is not what is under test here.
    """
    share_id = uuid.uuid4()
    await session.execute(
        text(_INSERT_SHARE),
        {
            "id": share_id,
            "org": org_id,
            "node": node_id,
            "kind": kind,
            "principal": principal_id or org_id,
            "role": role,
            "by": org_id,
        },
    )
    return share_id


async def _live(session: AsyncSession, share_id: uuid.UUID) -> bool:
    revoked = (
        await session.execute(
            text("SELECT revoked_at FROM file_shares WHERE id = :id"), {"id": share_id}
        )
    ).scalar()
    return revoked is None


async def _role(session: AsyncSession, share_id: uuid.UUID) -> Any:
    return (
        await session.execute(text("SELECT role FROM file_shares WHERE id = :id"), {"id": share_id})
    ).scalar()


async def _has_index(session: AsyncSession) -> bool:
    return bool(
        (
            await session.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :name"), {"name": _INDEX}
            )
        ).scalar()
    )


async def test_the_upgrade_keeps_the_strongest_of_a_principals_live_shares() -> None:
    """Three live rows for one principal on one node collapse to the strongest.

    Keeping the newest instead would take access away from somebody the deploy
    never meant to touch; keeping the weakest would too. Only the strongest is
    a rung the person already had.
    """
    org_id = uuid.uuid4()
    async with migration_scratch() as db:
        async with db.session() as session:
            drive_id = await _seed_drive(session, org_id)
            node = await _folder(session, drive_id=drive_id, org_id=org_id, ino=1, name=b"plans")
            elsewhere = await _folder(
                session, drive_id=drive_id, org_id=org_id, ino=2, name=b"elsewhere"
            )
            await session.commit()

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert not await _has_index(session), "the index survived its own downgrade"
            weak = await _share(session, org_id=org_id, node_id=node, role="reader")
            strong = await _share(session, org_id=org_id, node_id=node, role="manager")
            middle = await _share(session, org_id=org_id, node_id=node, role="writer")
            # A different principal on the same node, and the same principal on a
            # different node: neither is a duplicate of anything.
            other_principal = await _share(
                session, org_id=org_id, node_id=node, role="reader", kind="team"
            )
            other_node = await _share(session, org_id=org_id, node_id=elsewhere, role="reader")
            await session.commit()

        await db.upgrade()
        async with db.session() as session:
            assert await _live(session, strong), "the deploy took away access the person had"
            assert not await _live(session, weak)
            assert not await _live(session, middle)
            assert await _role(session, strong) == "manager"
            assert await _live(session, other_principal), "a second principal is not a duplicate"
            assert await _live(session, other_node), "the same principal on another node is not one"


async def test_a_second_live_share_for_one_principal_is_refused_at_head() -> None:
    """The index, not the collapse: a duplicate cannot come back after the deploy."""
    org_id = uuid.uuid4()
    async with AsyncSessionLocal() as session:
        drive_id = await _seed_drive(session, org_id)
        node = await _folder(session, drive_id=drive_id, org_id=org_id, ino=1, name=b"plans")
        assert await _has_index(session)
        first = await _share(session, org_id=org_id, node_id=node, role="reader")
        await session.commit()

    async with AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await _share(session, org_id=org_id, node_id=node, role="writer")
            await session.flush()
        await session.rollback()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE file_shares SET revoked_at = now() WHERE id = :id"), {"id": first}
        )
        again = await _share(session, org_id=org_id, node_id=node, role="writer")
        await session.commit()
        assert await _live(session, again), "a withdrawn grant blocks re-sharing with the person"
