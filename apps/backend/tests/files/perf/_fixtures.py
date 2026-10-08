"""Bulk tree builders and the size dial for the Files performance budgets.

The builders exist because the budgets are only meaningful at the sizes they
name: a keyset page is fast on ten children however it is written, and only a
folder with a hundred thousand of them tells a sequential scan apart from an
index-only one. Rows go in as multi-row core inserts rather than one ORM add
per node — a hundred thousand round trips would dominate the measurement — but
every column is computed exactly the way ``FilesFixtures.node`` computes it, so
a benchmark measures the tree the product builds.

``FILES_PERF_SIZE`` picks the scale. The pr-gate runs ``smoke`` (the statement
budgets at twenty thousand nodes; the ``pr`` build ran past thirty minutes under
sixteen workers on the runner); ``pr`` is for a quiet machine;
``nightly`` is the large size the schedule runs, where the subtree row grows to
the million nodes its budget names and the latency allowance drops to the published
budget itself. ``smoke`` is neither: it is the scale at which the whole
directory runs in one process in a few minutes, so a developer can check that
every row still holds its statement budget without paying for the ``pr``
fixture.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from alkera_core.files.names import name_key
from alkera_core.files.path_labels import ino_label
from alkera_core.models.files.tree import FileNode
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

#: How many rows go into one ``INSERT``: large enough that round trips stop
#: mattering, small enough to stay inside the driver's parameter limit for a
#: row this wide.
CHUNK = 2_000


@dataclass(frozen=True, slots=True)
class Sizes:
    """The fixture sizes and the latency allowance for one run scale."""

    #: Children in the folder the listing row pages through.
    children: int
    #: Depth of the chain the path-resolution row walks.
    depth: int
    #: Nodes under the folder the subtree-count row asks about.
    subtree: int
    #: Nodes the small-move row relocates.
    move: int
    #: Changes the delta row reads back.
    delta: int
    #: Nodes the drive the name-search row searches holds.
    search: int
    #: Files the many-small-files row drops in one call.
    drop: int
    #: What a p95 budget is multiplied by before it is asserted. Developer
    #: laptops and CI runners are shared and noisy, so the PR scale allows
    #: three times the published budget; the nightly scale asserts the budget itself.
    multiplier: float
    #: Seconds a row may take at this scale, in place of the suite's own per-test
    #: limit. Every module here pays a purge of the deep paths it planted — one
    #: row asserts it, and every module's teardown runs it — and that purge scans
    #: and deletes over the whole table on the one Postgres every other worker is
    #: also using: 76 s at the smoke sizes on a box at load average 25, where the
    #: rows it stands over take seconds. The larger scales add minutes of planting
    #: on top, and a build killed part way through leaves a cancelled statement
    #: behind that the next attempt then waits on.
    timeout_seconds: int


SIZES: dict[str, Sizes] = {
    "smoke": Sizes(
        # Small enough that the whole directory runs in one process in minutes,
        # large enough that every budget row still means what it says: the listing
        # row pages 500 out of a folder that does not fit in one page, the
        # resolution row still walks the 256 segments it is named for, the
        # search row still looks for one name in a drive it cannot hold in a
        # page, and the drop row still spreads its files over folders it has to
        # deduplicate. Shrinking `children` below the page size instead turns
        # those rows into failures about the fixture rather than about the query.
        #
        # It proves SHAPE, not speed: the statement counts are the same ones the
        # `pr` scale asserts, while the latency multiplier is deliberately so
        # loose that the wall-clock half asserts nothing. This is the scale the
        # pr-gate runs; `pr` is for a quiet machine measuring latency.
        children=20_000,
        depth=256,
        subtree=20_000,
        move=5_000,
        delta=2_000,
        search=20_000,
        drop=1_000,
        multiplier=50.0,
        timeout_seconds=300,
    ),
    "pr": Sizes(
        children=100_000,
        depth=256,
        subtree=50_000,
        move=10_000,
        delta=10_000,
        search=200_000,
        drop=5_000,
        multiplier=3.0,
        timeout_seconds=1_800,
    ),
    "nightly": Sizes(
        children=100_000,
        depth=256,
        subtree=1_000_000,
        move=10_000,
        delta=10_000,
        search=5_000_000,
        drop=50_000,
        multiplier=1.0,
        timeout_seconds=1_800,
    ),
}


def perf_sizes() -> Sizes:
    """The sizes for this run.

    An unknown value fails rather than falling back to ``pr``: a nightly run
    that silently asserted the loose PR multiplier would report a green that
    proves nothing.
    """
    scale = os.environ.get("FILES_PERF_SIZE", "pr")
    if scale not in SIZES:
        raise ValueError(f"FILES_PERF_SIZE must be one of {sorted(SIZES)}, got {scale!r}")
    return SIZES[scale]


async def bulk_children(
    session: AsyncSession,
    parent: FileNode,
    *,
    drive: Any,
    org_team_id: uuid.UUID,
    count: int,
    kind: str = "file",
    prefix: str = "n",
) -> list[uuid.UUID]:
    """Write ``count`` children of ``parent``; return their ids in name order.

    Names are zero-padded so lexicographic order is numeric order: a test that
    pages with ``orderBy=name`` can name the row it expects first without
    re-deriving the collation the database used.
    """
    start = _reserve(drive, count)
    ids: list[uuid.UUID] = []
    rows: list[dict[str, Any]] = []
    for offset in range(count):
        ino = start + offset
        node_id = uuid.uuid4()
        ids.append(node_id)
        raw = f"{prefix}{offset:07d}"
        rows.append(
            _row(
                node_id=node_id,
                ino=ino,
                parent=parent,
                org_team_id=org_team_id,
                kind=kind,
                raw=raw,
                path_ids=f"{parent.path_ids}.{ino_label(ino)}",
                depth=parent.depth + 1,
            )
        )
        if len(rows) >= CHUNK:
            await _flush(session, rows)
            rows = []
    if rows:
        await _flush(session, rows)
    await session.commit()
    await analyzed(session)
    return ids


async def deep_chain(
    session: AsyncSession,
    root: FileNode,
    *,
    drive: Any,
    org_team_id: uuid.UUID,
    depth: int,
    segment: str = "d",
) -> tuple[list[str], uuid.UUID]:
    """A chain of ``depth`` folders under ``root``.

    Returns the segment names and the deepest node's id, so a caller can ask
    for the path and check the answer against the row it built rather than
    against whatever the route happened to return.
    """
    start = _reserve(drive, depth)
    names: list[str] = []
    rows: list[dict[str, Any]] = []
    parent_id = root.id
    path_ids = root.path_ids
    node_depth = root.depth
    node_id = root.id
    for level in range(depth):
        ino = start + level
        node_id = uuid.uuid4()
        raw = f"{segment}{level:04d}"
        names.append(raw)
        path_ids = f"{path_ids}.{ino_label(ino)}"
        node_depth += 1
        rows.append(
            _row(
                node_id=node_id,
                ino=ino,
                parent=root,
                org_team_id=org_team_id,
                kind="folder",
                raw=raw,
                path_ids=path_ids,
                depth=node_depth,
                parent_id=parent_id,
            )
        )
        parent_id = node_id
    for index in range(0, len(rows), CHUNK):
        await _flush(session, rows[index : index + CHUNK])
    await session.commit()
    await analyzed(session)
    return names, node_id


def _row(
    *,
    node_id: uuid.UUID,
    ino: int,
    parent: FileNode,
    org_team_id: uuid.UUID,
    kind: str,
    raw: str,
    path_ids: str,
    depth: int,
    parent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    name = raw.encode()
    return {
        "id": node_id,
        "ino": ino,
        "drive_id": parent.drive_id,
        "org_team_id": org_team_id,
        "parent_id": parent.id if parent_id is None else parent_id,
        "kind": kind,
        "name": name,
        "name_display": raw,
        "name_key": name_key(name),
        "path_ids": path_ids,
        "depth": depth,
    }


def _reserve(drive: Any, count: int) -> int:
    """Take ``count`` inos out of the drive's counter in one block.

    The drive row is the allocator in production too, so taking a block keeps
    the "no two nodes in a drive share an ino" invariant without a round trip
    per row.
    """
    start = int(drive.next_ino)
    drive.next_ino = start + count
    return start


async def _flush(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    await session.execute(insert(FileNode), rows)


async def analyzed(session: AsyncSession) -> None:
    """Give the planner statistics for the tree that was just built.

    A builder writes tens of thousands of rows in one burst and the row is
    measured at once; the runner's autovacuum has not analysed the table by
    then, and a listing planned over "no rows" ran for the whole test timeout
    on an otherwise idle machine. In production the same tree grows over
    weeks under autovacuum, so this is the fixture's concern, not the code's,
    and it runs outside every statement count.
    """
    await session.execute(text("ANALYZE file_nodes"))
    await session.commit()


__all__ = ["CHUNK", "SIZES", "Sizes", "analyzed", "bulk_children", "deep_chain", "perf_sizes"]
