"""The Files performance budgets, as tests, at PR size.

Each test here is one row of the budget table: it builds the fixture the row
names, drives the real route through ``ASGITransport`` against real Postgres
twenty times, prints the p95, and asserts one of two claims — the statement
count is inside the row's budget, or the p95 is inside the published budget
times the scale's allowance (three times the budget at PR size, where the
machine is shared and noisy; the budget itself nightly; see
``Sizes.multiplier``).

Which claim is asserted is the ``claim`` fixture from this directory's
conftest, so every row runs as two cases. The statement half is the one that
survives a slow laptop and stays in the default suite: latency drifts with the
machine, but a page that starts issuing one query per row does it everywhere.
The wall-clock half is marked ``perf_wallclock`` and deselected by the default
addopts, because a box running twenty test lanes at once makes a timing
assertion a flake rather than a proof; the nightly perf job selects it with
``-m perf_wallclock`` on a quiet runner.

The listing row states its statement claim as an equality rather than a ceiling
— a page over a hundred thousand children must cost exactly what the same page
over two hundred costs — which no per-row query pattern can satisfy.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import pytest
from _fixtures import bulk_children, deep_chain, perf_sizes
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:  # the shared helpers, under a name unique in the repository
    from _files_kit import Bench, FilesFixtures

# One xdist worker for the whole perf directory: its conftest hands every module a
# module-scoped teardown that purges the deep-path rows the module built, and that
# purge scans the table — splitting a module per test pays it once per worker.
pytestmark = [
    pytest.mark.asyncio,
    # The budgets are measured on the Linux runner, which is where the
    # published numbers come from. The Windows runner takes long enough
    # building the hundred-thousand-child fixture to pass the suite's
    # faulthandler deadline, which kills the worker mid-test and loses
    # every other result that worker was holding.
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

# Statement budgets per row. The rule is "every list endpoint fetches its
# page in at most three queries (rows, ACL cache rows, chain ids)"; a request
# also pays a fixed prologue no route can avoid — resolving the principal, the
# drive and the idempotency record — so each number below is that prologue plus
# the row's own allowance, and the listing row additionally pins that the count
# does not move with the size of the folder.
# 40 → 42, measured. An object-backed folder row no longer renders only its
# object's live title: `with_object_facets` now also names the node the object's
# files live at, which is the `working_folder_nodes` lookup — batched over the
# whole page rather than taken per row. The number is the request re-measured,
# and what still does the work is the equality below: a facet that started
# costing a query per row moves this count with the size of the folder.
# 42 → 43, measured. The page's ancestor chains no longer ride on the statement
# that reads the page: they are read by `(drive_id, ino)` in one of their own.
# The ltree join they used to be is index-served for the table owner and not for
# `alkera_files_app`, where FORCE row security stops PostgreSQL promoting a
# non-leakproof qual — which `subpath()` and ltree containment are — ahead of the
# policy's own, leaving `drive_id` as the only handle: every node of the drive,
# once per item of the page. Integer and uuid equality are leakproof, so the ino
# spelling keeps its unique index under the role. One statement, and the
# equality below is what still does the work.
LIST_STATEMENTS = 43
# 38 → 40 and 40 → 42, measured: the same object-facet render the listing above
# gained, each number the whole request re-measured. Then one more each: the
# detail and the resolve read their ancestors through the same page loader as
# the listing, so the ino-keyed chain read above is theirs too.
DETAIL_STATEMENTS = 41
RESOLVE_STATEMENTS = 43
DRIVE_STATEMENTS = 22
# 48 → 50, measured. The prologue above says a request pays for "the
# idempotency record", and until the key was actually spent that record was
# never written: the claim and the stored answer are +1. The lease fence the
# library now takes on the parent a child lands in — so an unfenced create into
# somebody else's mount is refused before the node exists — is the other.
# 50 → 52, measured. A create is new usage, so it now pays the safety-mode
# read (the drive's usage plus the caller's own, one round trip) and the one
# statement that resolves the ceilings it is checked against (the org's
# override / plan and the caller's own limits, read as the application on a
# session of its own the first time a write asks).
CREATE_STATEMENTS = 52
# 48, re-measured on this tree and unchanged: the rename is exactly at its
# budget, not under it. It pays the idempotency claim and the stored answer the
# create pays, and the window that steps out of the tenant role to decide is one
# statement each way whichever way it leaves — so a refusal costs what this
# success costs, which is what the no-oracle contract pins next door in
# ``test_files_statement_counts.py``.
# 48 → 50, measured. A rename takes the folder gate — the drive row and the
# parent row, in the one fixed order — before it writes its own row, because
# without it two renames in one folder each end up holding the sibling the
# other's folding-flag recompute still has to write, and the detector answers
# an ordinary rename with 40P01.
# 50 → 51, measured: the renamed row is rendered through `with_object_facets`,
# which now also asks `working_folder_nodes` where an object-backed folder's files
# live. One statement, batched over whatever the response renders.
RENAME_STATEMENTS = 51
# 56 → 63, measured. The destination of a move is now decided (its node row, its
# ACL rows and its decision) instead of being taken from the body unread, and it
# is fenced against the lease that holds it. There is no cheaper way to decide a
# node than to read it, and both reads are what stop a writer grafting a subtree
# into any folder in the org whose id they happen to know.
# 63 → 67, measured. The move now runs inside its idempotency claim like the
# create does: the claim's INSERT and the stored answer's UPDATE, and the
# SAVEPOINT / RELEASE pair the joined transaction wraps them in. The create
# row paid the same price when its key was first spent.
# 66 → 67 measured, ceiling unchanged. The folder gate a move takes now
# covers the folder the node leaves as well as the one it lands in — one
# more `FOR UPDATE` — because a move re-derives the folding flags of both,
# and a gate over only the destination leaves the two folders free to be
# taken in opposite orders by the move coming the other way.
# 67 → 72, measured. A move invalidates the subtree's cached ACLs: it re-reads
# the node at its new path, marks the subtree `acl_rewriting` in one statement
# and queues the repair, inside the savepoint pair the joined transaction wraps
# them in. Five statements, and the price of not leaking access — without them
# the moved subtree keeps the interned permission body of the ancestors it has
# left, so moving something out of a shared folder would not unshare it.
# 72 → 73, measured: the one ceilings-facts read above, taken when the move's
# bytes may be entering a folder the caller is limited in.
# 73 → 75, measured: the same `working_folder_nodes` lookup the rename pays, over
# the renders a move's answer makes. Batched per render, never per row — a move
# of ten thousand nodes costs what a move of one costs, which is the claim this
# row exists to make and the one a per-node facet lookup would break.
MOVE_STATEMENTS = 75


async def _drive(client: AsyncClient) -> tuple[str, str]:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    body = response.json()
    # Writes go under the caller's home: the drive root is a signpost that takes no direct write.
    return str(body["id"]), str(body["homeId"])


def _etag(item: dict[str, Any]) -> dict[str, str]:
    return {"If-Match": str(item["etag"])}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def _root_node(fx: FilesFixtures, session: AsyncSession) -> tuple[Any, Any]:
    """The drive row and its root node, refreshed: the drive handle the fixture
    cached was read before the skeleton allocated its inos."""
    from alkera_core.models.files.tree import FileNode

    drive = await fx.drive()
    await session.refresh(drive)
    assert drive.root_node_id is not None
    root = await session.get(FileNode, drive.root_node_id)
    assert root is not None
    return drive, root


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def test_list_one_page_of_a_hundred_thousand_children(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: one page of 500 out of 100,000 children, ordered by name, in 150 ms.

    The equality against the small folder is the anti-N+1 claim: a keyset page
    costs the same number of statements whether the folder holds two hundred
    rows or a hundred thousand.
    """
    sizes = perf_sizes()
    drive_row, _root = await _root_node(fx, real_session)
    big = await fx.node(b"big", kind="folder")
    small = await fx.node(b"small", kind="folder")
    await bulk_children(
        real_session,
        big,
        drive=drive_row,
        org_team_id=fx.org_team_id,
        count=sizes.children,
    )
    await bulk_children(
        real_session, small, drive=drive_row, org_team_id=fx.org_team_id, count=200, prefix="s"
    )
    drive_id, _ = await _drive(files_client)

    def page(folder_id: uuid.UUID) -> Any:
        url = f"{BASE}/drives/{drive_id}/items/{folder_id}/children?orderBy=name&limit=500"

        async def call(_iteration: int) -> Response:
            return await files_client.get(url)

        return call

    sample = await bench.measure("list page of 100k children", page(big.id))
    first = await files_client.get(
        f"{BASE}/drives/{drive_id}/items/{big.id}/children?orderBy=name&limit=500"
    )
    body = first.json()
    assert len(body["value"]) == 500
    assert body["value"][0]["name"] == "n0000000"
    assert body["nextMarker"]

    # Only the control's statement count is read, by the equality below.
    control = await bench.measure("list page of 200 children", page(small.id))
    assert control.statements == sample.statements, (
        "the page cost more statements over 100k children than over 200 — a "
        f"per-row query pattern: {sample.statements} vs {control.statements}"
    )
    bench.report(sample, claim=claim, budget_ms=150, statements=LIST_STATEMENTS)


async def test_the_page_costs_the_same_when_the_revocation_answer_has_gone_stale(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
) -> None:
    """The equality above is about the folder, so the clock must not enter it.

    A request carries one statement nothing about the request decides: the
    session-revocation probe, whose "not revoked" answer lives in process for
    five seconds. The row above pays it on the large folder and not on the
    small one for no reason but elapsed time — seeding and paging a hundred
    thousand children outlasts the cache — and the equality then reads a slow
    run as a per-row query pattern.

    This pins the property at a size that runs in seconds, by dropping the
    cached answer instead of waiting out its lifetime: the same page, over the
    same folder, must cost the same number of statements whether the answer is
    fresh or has to be re-checked.
    """
    # Imported here rather than at module scope, like the type-only import at
    # the top: ``_files_kit`` is reachable only through this directory's path.
    from _files_kit import counting
    from alkera_core.auth import revocation

    drive_row, _root = await _root_node(fx, real_session)
    folder = await fx.node(b"ttl", kind="folder")
    await bulk_children(
        real_session, folder, drive=drive_row, org_team_id=fx.org_team_id, count=3_000
    )
    drive_id, _ = await _drive(files_client)
    url = f"{BASE}/drives/{drive_id}/items/{folder.id}/children?orderBy=name&limit=500"

    # One untimed call so the answer is cached and the route's own one-time
    # costs are paid, exactly as the benchmark harness warms up.
    assert (await files_client.get(url)).status_code == 200

    with counting() as while_fresh:
        assert (await files_client.get(url)).status_code == 200

    # Five seconds of a slow run, without spending five seconds: the process
    # forgets what it knows about this token, so the next request re-checks it
    # against Postgres — which is exactly what the hundred-thousand-child row
    # was observing.
    revocation._cache.reset()
    with counting() as while_stale:
        assert (await files_client.get(url)).status_code == 200

    assert len(while_stale) == len(while_fresh), (
        "the same page cost a different number of statements once the "
        "revocation answer went stale, so the budget above measures how long "
        f"the run took: {len(while_stale)} vs {len(while_fresh)}"
    )


async def test_resolve_a_path_two_hundred_and_fifty_six_deep(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: a depth-256 path resolves in 20 ms, in a bounded number of
    statements — a query per segment would be 256 of them."""
    sizes = perf_sizes()
    drive_row, root = await _root_node(fx, real_session)
    names, deepest = await deep_chain(
        real_session, root, drive=drive_row, org_team_id=fx.org_team_id, depth=sizes.depth
    )
    drive_id, _ = await _drive(files_client)
    url = f"{BASE}/drives/{drive_id}/root:/" + "/".join(names)

    async def call(_iteration: int) -> Response:
        return await files_client.get(url)

    sample = await bench.measure(f"resolve a depth-{sizes.depth} path", call)
    assert (await files_client.get(url)).json()["id"] == str(deepest)
    assert sample.statements < sizes.depth, (
        f"resolution cost {sample.statements} statements for {sizes.depth} segments"
    )
    bench.report(sample, claim=claim, budget_ms=20, statements=RESOLVE_STATEMENTS)


async def test_the_deep_chain_this_module_builds_does_not_outlive_it(
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    purge_deep_paths: Callable[[], Awaitable[int]],
    deepest_indexable_path: int,
) -> None:
    """The chain the row above builds must be gone before the next module runs.

    Nothing rolls this suite's commits back and one worker shares one database,
    so a 256-deep ``path_ids`` is left lying where every other module can find
    it — and the index ``0107``'s ``downgrade()`` rebuilds keys the *whole*
    path, so it cannot be built at all while one exists. That is what killed a
    migration test half way down its trip and left its worker's Files role
    without the grants 0107 hands it.

    Asserted on real rows rather than left to the directory's teardown, which
    pytest runs after the last assertion a test can make: the chain is built,
    seen to be deeper than the index can hold, and then the same cleanup the
    autouse fixture runs is asked to clear it.
    """
    deepest = text("SELECT coalesce(max(nlevel(path_ids)), 0) FROM file_nodes")
    drive_row, root = await _root_node(fx, real_session)
    await deep_chain(
        real_session, root, drive=drive_row, org_team_id=fx.org_team_id, depth=perf_sizes().depth
    )
    before = int((await real_session.execute(deepest)).scalar_one())
    await real_session.rollback()
    assert before > deepest_indexable_path, (
        f"the chain this directory builds is only {before} labels deep, so this "
        "test is no longer standing over the residue it was written for"
    )

    assert await purge_deep_paths() <= deepest_indexable_path, (
        "the cleanup left a path deeper than the pre-0107 index can hold"
    )


async def test_item_detail_with_capabilities(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: one item with its capabilities in 30 ms."""
    node = await fx.node(b"report.txt")
    drive_id, _ = await _drive(files_client)
    url = f"{BASE}/drives/{drive_id}/items/{node.id}"

    async def call(_iteration: int) -> Response:
        return await files_client.get(url)

    sample = await bench.measure("item detail with capabilities", call)
    assert (await files_client.get(url)).json()["capabilities"]["can_read"] is True
    bench.report(sample, claim=claim, budget_ms=30, statements=DETAIL_STATEMENTS)


async def test_quota_check_reads_the_drive_row(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: the quota a session open checks resolves in 10 ms from indexed
    reads, never a subtree walk — so the count must not move when the drive
    grows by ten thousand nodes."""
    drive_row, _root = await _root_node(fx, real_session)
    drive_id, _ = await _drive(files_client)

    async def call(_iteration: int) -> Response:
        return await files_client.get(f"{BASE}/drives")

    # Only the control's statement count is read, by the equality below.
    empty = await bench.measure("quota check (empty drive)", call)
    folder = await fx.node(b"bulk", kind="folder")
    await bulk_children(
        real_session, folder, drive=drive_row, org_team_id=fx.org_team_id, count=10_000, prefix="q"
    )
    sample = await bench.measure("quota check", call)
    assert sample.statements == empty.statements, (
        "the quota read grew with the tree — it is walking the subtree"
    )
    assert (await files_client.get(f"{BASE}/drives")).json()["id"] == drive_id
    bench.report(sample, claim=claim, budget_ms=10, statements=DRIVE_STATEMENTS)


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def test_create_a_child(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: create in 25 ms, one statement each plus history and outbox, and
    never an ancestor update."""
    drive_id, root_id = await _drive(files_client)
    url = f"{BASE}/drives/{drive_id}/items/{root_id}/children"

    async def call(iteration: int) -> Response:
        return await files_client.post(
            url, json={"name": f"made-{iteration:04d}", "kind": "folder"}, headers=_idem()
        )

    sample = await bench.measure("create a child", call, expect=201)
    bench.report(sample, claim=claim, budget_ms=25, statements=CREATE_STATEMENTS)


async def test_rename_a_node(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: rename in 25 ms. Each repetition renames its own freshly created
    node, so twenty samples are twenty renames rather than one rename and
    nineteen etag mismatches."""
    drive_id, root_id = await _drive(files_client)
    created: list[dict[str, Any]] = []
    for index in range(0, 32):
        response = await files_client.post(
            f"{BASE}/drives/{drive_id}/items/{root_id}/children",
            json={"name": f"before-{index:04d}", "kind": "folder"},
            headers=_idem(),
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    async def call(iteration: int) -> Response:
        item = created[iteration]
        return await files_client.patch(
            f"{BASE}/drives/{drive_id}/items/{item['id']}",
            json={"name": f"after-{iteration:04d}"},
            headers={**_idem(), **_etag(item)},
        )

    sample = await bench.measure("rename a node", call)
    bench.report(sample, claim=claim, budget_ms=25, statements=RENAME_STATEMENTS)


async def test_move_a_folder_of_ten_thousand_nodes(
    files_client: AsyncClient,
    files_on: None,
    fx: FilesFixtures,
    real_session: AsyncSession,
    bench: Bench,
    claim: str,
) -> None:
    """Budget: a move of ≤ 10,000 nodes in 500 ms, one ``UPDATE`` of ``path_ids``
    by subpath replacement, not a row per descendant, so the statement count
    stays flat while ten thousand rows change."""
    sizes = perf_sizes()
    drive_row, _root = await _root_node(fx, real_session)
    drive_id, root_id = await _drive(files_client)
    homes: list[dict[str, Any]] = []
    for index in range(0, 32):
        response = await files_client.post(
            f"{BASE}/drives/{drive_id}/items/{root_id}/children",
            json={"name": f"home-{index:04d}", "kind": "folder"},
            headers=_idem(),
        )
        assert response.status_code == 201, response.text
        homes.append(response.json())

    # Seeded in the mover's own home, beside the folders it moves between: a
    # folder the admin reaches only through the org-admin floor may not be
    # moved into a home, where the move would make them its owner.
    subject = await fx.node(b"movable", kind="folder", parent=await fx.folder(uuid.UUID(root_id)))
    await bulk_children(
        real_session,
        subject,
        drive=drive_row,
        org_team_id=fx.org_team_id,
        count=sizes.move - 1,
        prefix="m",
    )

    state: dict[str, Any] = {"etag": None}

    async def call(iteration: int) -> Response:
        url = f"{BASE}/drives/{drive_id}/items/{subject.id}"
        if state["etag"] is None:
            fetched = await files_client.get(url)
            state["etag"] = fetched.json()["etag"]
        response = await files_client.patch(
            url,
            json={"parentId": homes[iteration]["id"]},
            headers={**_idem(), "If-Match": str(state["etag"])},
        )
        if response.status_code == 200:
            state["etag"] = response.json()["etag"]
        return response

    sample = await bench.measure(f"move a folder of {sizes.move} nodes", call)
    bench.report(sample, claim=claim, budget_ms=500, statements=MOVE_STATEMENTS)
