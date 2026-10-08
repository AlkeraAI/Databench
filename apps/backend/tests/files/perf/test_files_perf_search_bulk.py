"""The remaining Files performance budget rows: search, filtered listing, bulk, range.

Same shape as the neighbouring budget module — build the fixture the row names,
drive the real code repeatedly, print the p95, and assert the one half of the
row's budget the ``claim`` fixture names (the statement ceiling on every PR, the
latency allowance on the nightly quiet runner) — with two additions the rows
here need.

The first is that the search row has no HTTP surface yet, so it is measured on
the library function the route will call. That is deliberate rather than a
shortcut: the claim in the table is about the query the page costs, and the
query is the same one whether a router or a test calls it.

The second is that the download row's real claim is not a duration at all —
300 ms to first byte is trivially met by an implementation that reads the whole
object into memory first on a fast disk — so it is proved twice: by the p95,
and by peak RSS in a subprocess, where a whole-object read of a gibibyte cannot
hide behind the fixtures the rest of the module built.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _files_kit import Sample, counting, timed_repeats
from _fixtures import bulk_children, perf_sizes
from _peak_rss import PEAK_RSS_SOURCE
from alkera_core.authz.principal import ActingContext
from alkera_core.files.clock import SystemClock
from alkera_core.files.filters import OrderBy
from alkera_core.files.hashing import hash_stream
from alkera_core.files.ids import DomainId, DriveId
from alkera_core.files.search import search_names
from alkera_core.files.store.scoped import FilesystemScoped
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # the fixtures live in conftests, which are not importable packages
    from _files_kit import Bench

    from ..conftest import FilesFixtures

# One xdist worker for the whole perf directory: its conftest hands every module a
# module-scoped teardown that purges the deep-path rows the module built, and that
# purge scans the table — splitting a module per test pays it once per worker.
pytestmark = [
    pytest.mark.asyncio,
    # The Windows runner cannot hold these inside the 300 s faulthandler deadline,
    # which kills the worker mid-test and loses every other result it held; the
    # budgets are measured on the Linux runner.
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="perf budgets are measured on the Linux runner",
    ),
    pytest.mark.xdist_group("files_perf"),
    # Not the limit the rest of the suite keeps. Every module in this directory
    # ends by purging the deep paths it planted, and one row here asserts that
    # purge directly: it scans and deletes over the whole table on the one
    # Postgres every other worker is also using, which is minutes on a loaded box
    # where the rows it stands over are seconds. The scales that plant a hundred
    # thousand nodes or more ask for far longer still — see `Sizes.timeout_seconds`.
    pytest.mark.timeout(perf_sizes().timeout_seconds),
]

BASE = "/api/v1/files"

#: Timed repetitions per row, matching the neighbouring module: twenty samples
#: put the 95th percentile on the second-slowest observation.
REPEATS = 20

#: The rule is that a list or search page costs at most three queries (rows,
#: ACL cache rows, chain ids). The search row is measured without a route around
#: it, so it pays no request prologue and the ceiling is the rule itself.
SEARCH_STATEMENTS = 3

#: The filtered listing goes through the real route, so it pays the prologue
#: every request pays — principal, drive, context — on top of its page.
#:
#: 40 → 44, measured. `with_object_facets` now names the node an object-backed
#: folder's files live at as well as the object's live title, which is the one
#: `working_folder_nodes` statement per rendered page. The number is the whole
#: request re-measured rather than a decomposition: what this row pins is that
#: the count is a constant and that the filtered page costs exactly what the
#: unfiltered one does, and both of those are still asserted below — a chip that
#: started costing a query per matching row, or a facet that started costing one
#: per row, moves this number with the size of the folder and fails here.
FILTERED_STATEMENTS = 44

#: Bytes the download row asks for. Small enough that a process which read the
#: whole gibibyte cannot be mistaken for one that seeked.
RANGE_BYTES = 64 * 1024

#: Peak RSS a range read is allowed. A whole-object read of the fixture would
#: need well over a thousand times the range; a seek needs the range plus the
#: interpreter it runs in.
RSS_CEILING_BYTES = 400 * 1024 * 1024

#: The byte the download fixture is filled with, asserted on both sides so a
#: read at the wrong offset (or of the wrong object) fails rather than passes.
FILL_BYTE = 0x5A

#: The domain the download fixture is written under. Fixed rather than random so
#: the subprocess probe can address the same handle from its own interpreter.
PERF_DOMAIN = uuid.UUID(int=7)


def _nightly() -> bool:
    return os.environ.get("FILES_PERF_SIZE") == "nightly"


def _object_bytes() -> int:
    """The download fixture's size. The budget names ten gibibytes, every smaller scale
    one — both are far past any buffer a streaming read needs, which is the only
    property the RSS assertion depends on."""
    return 10 * 1024**3 if _nightly() else 1024**3


async def _measure(
    label: str,
    call: Callable[[], Awaitable[Any]],
    *,
    bench: Bench,
    most: int = REPEATS,
) -> Sample:
    """Time ``call``, then count the statements one more call costs.

    ``Bench.measure`` asserts an HTTP status, so it can only time a route; this
    one times any awaitable, which is what a row measured on the library needs.
    Reporting and both assertions still go through ``Bench.report``, so every
    budget row in the suite states its budget the same way.

    ``bench`` is here for its claim, the same way ``Bench.measure`` uses it: a
    case asserting a statement count skips the repetitions entirely, because the
    percentile they produce is never read and the requests they cost are the
    load-sensitive half of the row. ``most`` caps how many the wall-clock claim
    takes, for a row that is expensive to repeat or a control whose statement
    count is all anything reads. See :func:`timed_repeats`.
    """
    sample = Sample(label=label)
    await call()  # untimed warm-up: connection checkout and statement prepare
    for _ in range(timed_repeats(bench.claim, most=most)):
        started = time.perf_counter()
        await call()
        sample.durations_ms.append((time.perf_counter() - started) * 1000.0)
    with counting() as seen:
        await call()
    sample.statements = len(seen)
    return sample


async def _root_node(fx: FilesFixtures, session: AsyncSession) -> tuple[Any, Any]:
    """The drive row and its root node, refreshed: the drive handle the fixture
    cached was read before the skeleton allocated its inos."""
    drive = await fx.drive()
    await session.refresh(drive)
    assert drive.root_node_id is not None
    root = await session.get(FileNode, drive.root_node_id)
    assert root is not None
    return drive, root


async def _drive(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    # Writes go under the caller's home: the drive root is a signpost that takes no direct write.
    return str(body["id"]), str(body["homeId"])


def _stamp(when: datetime) -> str:
    """A timestamp for a query string. ``+00:00`` would decode as a space."""
    return when.isoformat().replace("+00:00", "Z")


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


# ---------------------------------------------------------------------------
# name search
# ---------------------------------------------------------------------------


async def test_name_search_over_a_large_drive(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: a name search over the org's whole drive in 200 ms.

    The statement ceiling is the row's real claim. A search that decided
    readability per candidate would cost a statement per row it considered; this
    one costs three however many rows it scanned, which is only possible because
    the readability filter is a join inside the query rather than a pass over
    the page afterwards.
    """
    sizes = perf_sizes()
    drive_row, _root = await _root_node(fx, real_session)
    haystack = await fx.node(b"haystack", kind="folder")
    await bulk_children(
        real_session,
        haystack,
        drive=drive_row,
        org_team_id=fx.org_team_id,
        count=sizes.search,
        prefix="hay",
    )
    needles = await fx.node(b"needles", kind="folder")
    await bulk_children(
        real_session,
        needles,
        drive=drive_row,
        org_team_id=fx.org_team_id,
        count=200,
        prefix="needle",
    )
    ctx = ActingContext.for_user(
        user_id=fx.actor_id, org_id=fx.org_team_id, email="perf@test.invalid"
    )

    async def call() -> Any:
        # The repo refuses to run outside a transaction, which is how a route
        # calls it too: the page and its authorization read one snapshot.
        async with fx.repo.transaction():
            return await search_names(
                fx.repo,
                ctx,
                DriveId(drive_row.id),
                "needle0000010",
                order_by=OrderBy(),
                limit=100,
            )

    page = await call()
    assert [row.name_display for row in page.items] == ["needle0000010"], (
        "the search did not find the one planted name — the row measured nothing"
    )

    sample = await _measure(f"name search over {sizes.search:,} nodes", call, bench=bench)
    bench.report(sample, claim=claim, budget_ms=200, statements=SEARCH_STATEMENTS)


# ---------------------------------------------------------------------------
# filtered listing
# ---------------------------------------------------------------------------


async def test_filtered_listing_with_three_chips(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: a page of a hundred-thousand-child folder under three chips, 300 ms.

    Every chip is a SQL predicate applied before the keyset cut, so the filtered
    page costs exactly what the unfiltered page costs. That equality is the
    claim: a filter evaluated after the page was read would have to read further
    to fill it, and one evaluated per row would cost a statement per row.
    """
    sizes = perf_sizes()
    drive_row, _root = await _root_node(fx, real_session)
    folder = await fx.node(b"filtered", kind="folder")
    ids = await bulk_children(
        real_session,
        folder,
        drive=drive_row,
        org_team_id=fx.org_team_id,
        count=sizes.children,
        prefix="f",
    )
    # Every second child carries all three chip values, so the filtered page is
    # cut out of a folder where half the rows have to be excluded by SQL.
    marked = [row for index, row in enumerate(ids) if index % 2 == 0]
    modified = datetime(2026, 3, 1, tzinfo=UTC)
    for start in range(0, len(marked), 5_000):
        await real_session.execute(
            update(FileNode)
            .where(FileNode.id.in_(marked[start : start + 5_000]))
            .values(
                created_by=fx.actor_id,
                mime_class="tabular",
                mtime_ns=int(modified.timestamp() * 1_000_000_000),
            )
        )
    await real_session.commit()

    drive_id, _ = await _drive(files_client)
    chips = (
        "owner=me"
        "&mimeClass=tabular"
        f"&modifiedAfter={_stamp(modified - timedelta(days=1))}"
        f"&modifiedBefore={_stamp(modified + timedelta(days=1))}"
    )
    listing = f"{BASE}/drives/{drive_id}/items/{folder.id}/children?orderBy=name&limit=500"

    async def call() -> Response:
        response = await files_client.get(f"{listing}&{chips}")
        assert response.status_code == 200, response.text[:300]
        return response

    body = (await call()).json()
    assert len(body["value"]) == 500
    assert body["value"][0]["name"] == "f0000000"
    assert body["value"][1]["name"] == "f0000002", (
        "an unmarked child came back — the chips were not applied in SQL"
    )

    sample = await _measure("filtered listing, three chips", call, bench=bench)

    async def unfiltered() -> Response:
        response = await files_client.get(listing)
        assert response.status_code == 200, response.text[:300]
        return response

    # The control exists to be counted, never timed: only its statement count is
    # read, by the equality below.
    control = await _measure("unfiltered listing", unfiltered, bench=bench, most=0)
    assert sample.statements == control.statements, (
        "the filtered page cost more statements than the unfiltered one — a "
        f"filter is being applied per row: {sample.statements} vs {control.statements}"
    )
    bench.report(sample, claim=claim, budget_ms=300, statements=FILTERED_STATEMENTS)


# ---------------------------------------------------------------------------
# many small files
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the tree route walks every path it was given rather than the distinct "
        "prefixes among them, and asks for a folder's siblings once per segment: "
        "a drop costs a couple of statements per FILE instead of a handful per "
        "folder, which is two orders of magnitude over the budget at every "
        "fixture size this runs at. The library already has `tree.prefixes()` "
        "for the deduplication; the budget is asserted here so this row turns "
        "green on its own the day it is used."
    ),
)
async def test_dropped_folder_skeleton_in_one_call(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: the skeleton of a dropped folder of many small files, in one call.

    The row's first half is "tree in 2 s". The statement claim is that the cost
    tracks the *folders* the drop contains rather than the files under them: a
    drop of many files spread fifty to a folder costs about what those folders
    cost on their own, because ``tree`` walks the distinct prefixes and not the
    leaves.
    """
    files = perf_sizes().drop
    per_folder = 50
    drive_id, root_id = await _drive(files_client)
    paths = [f"drop/{index // per_folder:05d}" for index in range(files)]
    distinct = sorted(set(paths))
    url = f"{BASE}/drives/{drive_id}/items/{root_id}/tree"

    async def call() -> Response:
        response = await files_client.post(url, json={"paths": paths}, headers=_idem())
        assert response.status_code == 201, response.text[:300]
        return response

    made = (await call()).json()
    # "drop" itself plus one folder per distinct leaf prefix, and nothing per file.
    assert len(made) == len(distinct) + 1, (
        f"tree created {len(made)} nodes for {len(distinct)} distinct folders"
    )

    # Three timed repetitions at most, not twenty: each one re-posts every path in
    # the drop. They run against the skeleton the first call built, which is the
    # shape a re-drop of the same folder has: `tree` walks in and creates nothing.
    sample = await _measure(f"tree skeleton for {files:,} files", call, bench=bench, most=3)
    ceiling = 4 * (len(distinct) + 2)
    assert sample.statements <= ceiling, (
        f"tree cost {sample.statements} statements for {len(distinct)} folders and "
        f"{files:,} files — it is walking the files, not the skeleton"
    )
    bench.report(sample, claim=claim, budget_ms=2_000, statements=ceiling)


# ---------------------------------------------------------------------------
# first byte of a large download
# ---------------------------------------------------------------------------


async def _write_object(store_root: Path, key: str, size: int) -> None:
    """Stream ``size`` bytes into the filesystem store under ``key``."""
    handle = await FilesystemScoped(store_root, clock=SystemClock()).for_domain(
        DomainId(PERF_DOMAIN)
    )
    chunk = bytes([FILL_BYTE]) * (4 * 1024 * 1024)

    def stream() -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            remaining = size
            while remaining > 0:
                take = min(len(chunk), remaining)
                remaining -= take
                yield chunk[:take]

        return gen()

    digests = await hash_stream(stream())
    await handle.put(key, stream(), size=size, checksum=digests.content_hash)


async def test_first_byte_of_a_large_download_at_an_arbitrary_offset(
    files_store: Path,
    timed_bench: Bench,
) -> None:
    """Budget: the first byte of a range at an arbitrary offset, in 300 ms.

    The duration alone would be met by an implementation that read the whole
    object into memory first on a fast disk, so the row is proved twice: by the
    p95 here, and by peak RSS in a subprocess, where a whole-object read of a
    gibibyte cannot hide. The offset is deliberately not on a block boundary.
    """
    size = _object_bytes()
    key = "content/perf/large.bin"
    await _write_object(files_store, key, size)
    offset = size // 3 + 12_345

    handle = await FilesystemScoped(files_store, clock=SystemClock()).for_domain(
        DomainId(PERF_DOMAIN)
    )

    async def call() -> None:
        stream = await handle.get(key, range=(offset, offset + RANGE_BYTES - 1))
        async for chunk in stream:
            assert chunk[0] == FILL_BYTE
            break

    sample = await _measure(
        f"first byte of a {size // 1024**3} GiB range read", call, bench=timed_bench
    )
    allowed = 300 * timed_bench.multiplier
    print(
        f"\n[files-perf] {sample.label}: p95={sample.p95_ms:.1f}ms "
        f"median={sample.median_ms:.1f}ms budget=300ms allowed={allowed:.0f}ms"
    )
    assert sample.p95_ms <= allowed, f"first byte took {sample.p95_ms:.1f}ms, over {allowed:.0f}ms"

    probe = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", _RSS_PROBE, str(files_store), key, str(offset), str(RANGE_BYTES)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr[-2000:]
    result = json.loads(probe.stdout.strip().splitlines()[-1])
    print(f"[files-perf] range read peak RSS: {result['rss'] / 1024**2:.0f} MiB")
    assert result["first_byte"] == FILL_BYTE, "the probe read the wrong offset"
    assert result["rss"] <= RSS_CEILING_BYTES, (
        f"a {RANGE_BYTES}-byte range read of a {size / 1024**3:.0f} GiB object peaked at "
        f"{result['rss'] / 1024**2:.0f} MiB — the whole object was read"
    )


#: Runs one range read in a fresh interpreter and reports its peak RSS. It has
#: to be a subprocess: the high-water mark is for the whole process, so a pytest
#: worker that has already built a hundred-thousand-row fixture would report
#: that instead of what the read cost. `peak_rss` is pasted in rather than
#: imported because the child has none of this suite on its path; its module
#: explains why each platform answers differently.
_RSS_PROBE = (
    """
import asyncio, json, sys, uuid
from pathlib import Path
from alkera_core.files.clock import SystemClock
from alkera_core.files.ids import DomainId
from alkera_core.files.store.scoped import FilesystemScoped

root, key, offset, length = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])

"""
    + PEAK_RSS_SOURCE
    + """

async def main() -> None:
    handle = await FilesystemScoped(Path(root), clock=SystemClock()).for_domain(
        DomainId(uuid.UUID(int=7))
    )
    stream = await handle.get(key, range=(offset, offset + length - 1))
    first = None
    async for chunk in stream:
        first = chunk[0]
        break
    print(json.dumps({"first_byte": first, "rss": peak_rss(sys.platform)}))


asyncio.run(main())
"""
)
