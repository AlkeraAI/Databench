"""The cleanup that shortens a worker's tree has to survive a real tree.

A migration test that steps over ``0107`` rebuilds a GiST index keyed on the
whole of ``file_nodes.path_ids``, and that index cannot be built at all while a
path much past sixty labels exists — so the harness deletes the deep rows first,
and the perf directory does the same at the end of every module. Both used to do
it with one statement over ``file_nodes``, which works for exactly as long as
the deep rows are raw inserts a benchmark builder made.

They stopped being that. A worker running the perf directory beside the modules
that drive the real routes has deep nodes written *through the services*, and a
node written that way is named by its history, its versions, its dir-stat
deltas, its leases. Every one of those foreign keys is ``NO ACTION``, so the one
statement raised ``ForeignKeyViolation`` and took the migration test and the
perf module's teardown with it.

This module stands over the fix with a tree the routes built: folders and a file
with content deeper than the index can hold, a shallow sibling that must not be
touched, and the catalogue itself — so a foreign key someone adds later is a
named failure here rather than a red CI job on a table nobody was thinking
about.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from _files_kit import node_etag
from alkera_core.config import settings
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import (
    DEEPEST_INDEXABLE_PATH,
    PURGE_ROOTS,
    purge_paths_the_old_index_cannot_hold,
    purged_reference_tables,
)

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

#: How many folders the deep chain is. ``/Shared`` sits two labels down, so this
#: puts the bottom of the chain comfortably past what the old index can hold
#: while leaving most of the chain shallow enough to survive — which is what
#: makes "the purge took only the deep tail" an assertion rather than a tautology.
CHAIN = 70

#: The tables a node written through the services fills in behind it. Each one
#: is a foreign key the old single statement tripped over, and each is counted
#: before and after so the purge is held to the doomed set exactly.
DEPENDENTS = ("file_history", "file_versions", "file_dir_stats_deltas", "file_leases")


async def _drive_and_shared(client: AsyncClient) -> tuple[str, str]:
    """The drive id and ``/Shared`` — the top folder a caller may write into."""
    drive = (await client.get(f"{BASE}/drives")).json()
    drive_id, root = str(drive["id"]), str(drive["rootId"])
    children = (await client.get(f"{BASE}/drives/{drive_id}/items/{root}/children")).json()
    shared = str(next(row for row in children["value"] if row["name"] == "Shared")["id"])
    return drive_id, shared


async def _folder(
    client: AsyncClient,
    drive: str,
    parent: str,
    name: str,
    idem: Callable[[], dict[str, str]],
) -> str:
    response = await client.post(
        f"{BASE}/drives/{drive}/items/{parent}/children",
        json={"name": name, "kind": "folder"},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _file_with_content(
    client: AsyncClient,
    fx: Any,
    session: AsyncSession,
    drive: str,
    parent: str,
    name: bytes,
    payload: bytes,
    idem: Callable[[], dict[str, str]],
) -> uuid.UUID:
    """An empty node, then a version onto it through the content route.

    The node itself is seeded — no route makes an empty file, a client opens an
    upload or puts content — but everything the purge trips over is written by
    the library the route calls: the version, the history rows, the dir-stat
    deltas for every folder above it.
    """
    node = await fx.node(name, parent=await fx.folder(uuid.UUID(parent)))
    # Read before the write: the content PUT rolls this session back, which
    # expires the handle, and touching it afterwards is a lazy load with no
    # loop under it.
    node_id: uuid.UUID = node.id
    await _write(client, session, drive, str(node_id), payload, idem)
    return node_id


async def _write(
    client: AsyncClient,
    session: AsyncSession,
    drive: str,
    node: str,
    payload: bytes,
    idem: Callable[[], dict[str, str]],
) -> None:
    """Content through the real route, so a version and its history exist."""
    etag = await node_etag(session, uuid.UUID(node))
    await session.rollback()
    response = await client.put(
        f"{BASE}/drives/{drive}/items/{node}/content",
        content=payload,
        headers={
            **idem(),
            "If-Match": f'"{etag}"',
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
        },
    )
    assert response.status_code in (200, 201), response.text


async def _lease(
    client: AsyncClient,
    session: AsyncSession,
    drive: str,
    node: str,
    instance: str,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A mount on a folder, so the lease rows that hang off it exist."""
    precondition = await node_etag(session, uuid.UUID(node))
    await session.rollback()
    response = await client.post(
        f"{BASE}/drives/{drive}/items/{node}/lease",
        json={"instanceId": instance, "machineId": "machine-a", "purpose": "mount"},
        headers={**idem(), "If-Match": precondition},
    )
    assert response.status_code == 200, response.text


async def _raise_the_quota(session: AsyncSession, drive: str) -> None:
    """A drive made for a route test starts small; this tree is not small."""
    await session.execute(
        text("UPDATE file_drives SET quota_bytes = :b, quota_nodes = :n WHERE id = :id"),
        {
            "b": settings.files_quota_default_bytes,
            "n": settings.files_quota_default_nodes,
            "id": uuid.UUID(drive),
        },
    )
    await session.commit()


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> int:
    value = (await session.execute(text(sql), params)).scalar_one()
    await session.rollback()
    return int(value)


async def test_the_purge_takes_a_tree_the_routes_wrote(
    files_client: AsyncClient,
    files_on: None,
    fx: Any,
    real_session: AsyncSession,
    idem: Callable[[], dict[str, str]],
) -> None:
    """A chain past the index's reach goes, with everything that named it, and
    the shallow part of the drive is left exactly as it was."""
    drive, shared = await _drive_and_shared(files_client)
    await _raise_the_quota(real_session, drive)

    # The control: a folder and a file near the top of the drive, which the
    # purge has no business touching.
    keep = await _folder(files_client, drive, shared, "keep", idem)
    kept_file = await _file_with_content(
        files_client, fx, real_session, drive, keep, b"kept.txt", b"shallow", idem
    )

    # The chain, in one call through the tree route, and a file with content at
    # the bottom of it so the deepest rows carry a version and its history.
    made = await files_client.post(
        f"{BASE}/drives/{drive}/items/{shared}/tree",
        json={"paths": ["/".join(f"d{level:03d}" for level in range(CHAIN))]},
        headers=idem(),
    )
    assert made.status_code == 201, made.text
    deepest_folder = str(made.json()[-1]["id"])
    await _file_with_content(
        files_client, fx, real_session, drive, deepest_folder, b"deep.txt", b"deep", idem
    )

    # A mount on each end of the drive. The deep one's rows are what the old
    # single statement never had to think about; the shallow one is the control
    # that tells "cleared the doomed set" from "cleared the table".
    await _lease(files_client, real_session, drive, keep, "instance-shallow", idem)
    await _lease(files_client, real_session, drive, deepest_folder, "instance-deep", idem)

    doomed = "nlevel(path_ids) > :depth"
    deep_nodes = await _scalar(
        real_session,
        f"SELECT count(*) FROM file_nodes WHERE {doomed}",
        depth=DEEPEST_INDEXABLE_PATH,
    )
    assert deep_nodes > 0, "the tree this test builds is not deeper than the index can hold"

    # Counted on both sides of the purge: without a row naming a doomed node the
    # old single statement would have succeeded and this test would prove
    # nothing, and a purge that cleared a table wholesale instead of by the
    # doomed set would leave the shallow tree's rows gone too.
    held: dict[str, tuple[int, int]] = {}
    for table in DEPENDENTS:
        total = await _scalar(real_session, f"SELECT count(*) FROM {table}")
        referencing = await _scalar(
            real_session,
            f"SELECT count(*) FROM {table} WHERE node_id IN "
            f"(SELECT id FROM file_nodes WHERE {doomed})",
            depth=DEEPEST_INDEXABLE_PATH,
        )
        assert referencing > 0, f"no {table} row names a doomed node, so the purge is unopposed"
        assert total > referencing, (
            f"every {table} row names a doomed node, so this test cannot tell a purge "
            "that cleared the table from one that cleared the doomed set"
        )
        held[table] = (total, referencing)

    kept_history = await _scalar(
        real_session,
        "SELECT count(*) FROM file_history WHERE node_id = :n",
        n=kept_file,
    )
    assert kept_history > 0

    result = purge_paths_the_old_index_cannot_hold()

    assert result.deleted == deep_nodes
    assert result.deepest_remaining <= DEEPEST_INDEXABLE_PATH
    assert (
        await _scalar(
            real_session,
            f"SELECT count(*) FROM file_nodes WHERE {doomed}",
            depth=DEEPEST_INDEXABLE_PATH,
        )
        == 0
    )

    # Exactly the rows that named a doomed node went, and not one more.
    for table, (total, referencing) in held.items():
        left = await _scalar(real_session, f"SELECT count(*) FROM {table}")
        assert left == total - referencing, (
            f"{table} went from {total} rows to {left}; only the {referencing} that named "
            "a node past the depth should have gone"
        )

    # The shallow half of the drive is untouched, history and content included.
    assert (
        await _scalar(
            real_session,
            "SELECT count(*) FROM file_nodes WHERE id = :n",
            n=kept_file,
        )
        == 1
    )
    assert (
        await _scalar(
            real_session,
            "SELECT count(*) FROM file_history WHERE node_id = :n",
            n=kept_file,
        )
        == kept_history
    )
    read = await files_client.get(f"{BASE}/drives/{drive}/items/{kept_file}")
    assert read.status_code == 200, read.text
    assert read.json()["id"] == str(kept_file)

    # The chain's shallow end survived too: the purge cut at the depth, not at
    # the subtree.
    survivors = await _scalar(
        real_session,
        "SELECT count(*) FROM file_nodes WHERE drive_id = :d AND name_key LIKE 'd0%'",
        d=uuid.UUID(drive),
    )
    assert survivors == DEEPEST_INDEXABLE_PATH - 2, (
        "the purge should have left every chain folder the old index can hold"
    )

    assert purge_paths_the_old_index_cannot_hold().deleted == 0, (
        "a second purge found something to delete, so the first left a deep row"
    )


async def test_the_purge_names_every_table_that_points_at_what_it_deletes(
    real_session: AsyncSession,
) -> None:
    """The coverage is read off the live catalogue, not off a memory of it.

    The purge deletes nodes, and with them their versions, leases and upload
    sessions. Any table holding a foreign key onto one of those has to be
    cleared first or the whole purge raises — so a key added later shows up here
    as a named missing table instead of as a migration test dying half way down
    its trip on some future CI run.
    """
    rows = (
        await real_session.execute(
            text("""
            SELECT DISTINCT src.relname AS referencing
            FROM pg_constraint c
            JOIN pg_class src ON src.oid = c.conrelid
            JOIN pg_class tgt ON tgt.oid = c.confrelid
            WHERE c.contype = 'f' AND tgt.relname = ANY(:roots)
            """),
            {"roots": list(PURGE_ROOTS)},
        )
    ).scalars()
    referencing = set(rows)
    await real_session.rollback()

    assert referencing, "no foreign key onto the purged tables — the query is wrong, not the schema"
    # ``file_upload_parts`` cascades from its session and is cleared with it;
    # every other referencing table is named by a statement of its own.
    missing = referencing - purged_reference_tables()
    assert not missing, (
        f"these tables point at a row the purge deletes and nothing clears them: {sorted(missing)}"
    )
