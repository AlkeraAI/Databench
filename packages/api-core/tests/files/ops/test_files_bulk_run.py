"""Running a queued batch: what the tree holds afterwards, and what a kill costs.

Every assertion here reads the tree (or the operation row) back independently
of the code that wrote it: the folder exists under the parent it named, the
trashed node carries a ``trashed_at``, the star row is there. A result row that
merely echoed what the runner decided would prove nothing, so the results are
only ever checked *against* those reads.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import bulk as bulk_core
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, OperationId
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: The tree every batch here works over.
TREE = "src/ src/a.txt src/b.txt dst/"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _queue(
    repo: FilesRepo,
    ctx: ActingContext,
    drive: FileDrive,
    plan: bulk_core.BulkPlan,
) -> OperationId:
    """Queue ``plan`` exactly as the route does: the row and its plan in one txn."""
    ops = Operations(repo, ctx, FakeClock(now=EPOCH))
    async with repo.transaction():
        state = await ops.start(
            bulk_core.BULK_KIND, drive_id=DriveId(drive.id), total=len(plan.items)
        )
        await bulk_core.store_plan(repo, state.id, plan)
    return state.id


def _rows(plan: bulk_core.BulkPlan) -> dict[str, dict[str, Any]]:
    return {str(one["id"]): dict(one) for one in plan.results}


async def _live_children(session: AsyncSession, parent_id: uuid.UUID) -> set[str]:
    """The live child names under ``parent``, read from the rows themselves."""
    found = (
        await session.execute(
            text(
                "SELECT encode(name, 'escape') FROM file_nodes "
                "WHERE parent_id = :parent AND trashed_at IS NULL"
            ),
            {"parent": parent_id},
        )
    ).scalars()
    return {str(one) for one in found}


async def _trashed(session: AsyncSession, node_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT trashed_at FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).first()
    assert row is not None
    return row[0] is not None


async def _starred(session: AsyncSession, node_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT count(*) FROM file_stars WHERE node_id = :id"), {"id": node_id}
        )
    ).first()
    assert row is not None
    return int(row[0]) > 0


async def test_a_mixed_batch_leaves_every_verbs_effect_in_the_tree(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A create, a move, a trash and a star queued together each land, and the
    row the runner writes for each one agrees with what the tree now says."""
    drive = await files_factory.drive()
    made = await files_factory.tree(TREE, drive=drive)
    ctx = _ctx(files_org)
    moved_id, trashed_id, starred_id = made["src/a.txt"].id, made["src/b.txt"].id, made["src"].id
    destination_id = made["dst"].id
    plan = bulk_core.plan(
        [
            {
                "id": "create",
                "op": "createFolder",
                "parent_id": str(made["dst"].id),
                "name": "reports",
            },
            {
                "id": "move",
                "op": "move",
                "item_id": str(moved_id),
                "parent_id": str(made["dst"].id),
                "if_match": made["src/a.txt"].etag,
            },
            {
                "id": "trash",
                "op": "trash",
                "item_id": str(trashed_id),
                "if_match": made["src/b.txt"].etag,
            },
            {"id": "star", "op": "star", "item_id": str(starred_id)},
        ]
    )
    op_id = await _queue(repo, ctx, drive, plan)

    await bulk_core.run(repo, ctx, op_id, batch=2)

    files_session.expire_all()
    assert {"reports", "a.txt"} <= await _live_children(files_session, destination_id)
    assert await _trashed(files_session, trashed_id)
    assert await _starred(files_session, starred_id)

    finished = await bulk_core.load_bulk_plan(repo, op_id)
    rows = _rows(finished)
    assert [rows[key]["status"] for key in ("create", "move", "trash", "star")] == [
        201,
        200,
        204,
        200,
    ]
    assert (await Operations(repo, ctx, FakeClock(now=EPOCH)).get(op_id)).state == "done"


async def test_only_the_items_the_request_allowed_are_executed(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The plan is the capability. An item the request refused keeps its
    recorded row and its node is untouched, while the item beside it applies —
    so a denial costs exactly one item, not the batch."""
    drive = await files_factory.drive()
    made = await files_factory.tree(TREE, drive=drive)
    ctx = _ctx(files_org)
    refused_id, allowed_id = made["src/a.txt"].id, made["src/b.txt"].id
    plan = bulk_core.plan(
        [
            {
                "id": "refused",
                "op": "trash",
                "item_id": str(refused_id),
                "if_match": made["src/a.txt"].etag,
            },
            {"id": "allowed", "op": "star", "item_id": str(allowed_id)},
        ]
    ).decide({"refused": {"status": 403, "body": {"code": "files.denied"}}})
    op_id = await _queue(repo, ctx, drive, plan)

    await bulk_core.run(repo, ctx, op_id)

    files_session.expire_all()
    assert not await _trashed(files_session, refused_id), "a refused item must not run"
    assert await _starred(files_session, allowed_id)
    rows = _rows(await bulk_core.load_bulk_plan(repo, op_id))
    assert rows["refused"] == {"id": "refused", "status": 403, "body": {"code": "files.denied"}}
    assert rows["allowed"]["status"] == 200


async def test_one_items_failure_is_that_items_row_and_the_batch_goes_on(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """A stale ``If-Match`` on one item is that item's 412 inside its own
    savepoint: the tree keeps the node it addressed exactly as it was, and the
    item after it still applies."""
    drive = await files_factory.drive()
    made = await files_factory.tree(TREE, drive=drive)
    ctx = _ctx(files_org)
    stale_id, allowed_id = made["src/a.txt"].id, made["src/b.txt"].id
    plan = bulk_core.plan(
        [
            {
                "id": "stale",
                "op": "trash",
                "item_id": str(stale_id),
                "if_match": made["src/a.txt"].etag + 99,
            },
            {"id": "after", "op": "star", "item_id": str(allowed_id)},
        ]
    )
    op_id = await _queue(repo, ctx, drive, plan)

    await bulk_core.run(repo, ctx, op_id)

    files_session.expire_all()
    assert not await _trashed(files_session, stale_id)
    assert await _starred(files_session, allowed_id)
    rows = _rows(await bulk_core.load_bulk_plan(repo, op_id))
    assert rows["stale"]["status"] == 412
    assert rows["after"]["status"] == 200
    assert (await Operations(repo, ctx, FakeClock(now=EPOCH)).get(op_id)).state == "done"


async def test_a_killed_batch_resumes_at_the_boundary_and_repeats_nothing(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    """The cursor and the results commit with the changes they describe, so a
    runner killed after the first window continues at item two — and the folder
    the first window created exists exactly once afterwards."""
    drive = await files_factory.drive()
    made = await files_factory.tree(TREE, drive=drive)
    ctx = _ctx(files_org)
    destination_id = made["dst"].id
    plan = bulk_core.plan(
        [
            {"id": "one", "op": "createFolder", "parent_id": str(destination_id), "name": "alpha"},
            {"id": "two", "op": "createFolder", "parent_id": str(destination_id), "name": "beta"},
            {
                "id": "three",
                "op": "createFolder",
                "parent_id": str(destination_id),
                "name": "gamma",
            },
        ]
    )
    op_id = await _queue(repo, ctx, drive, plan)

    killed = PausingCheckpoints()
    killed.kill("bulk.after_batch")
    with pytest.raises(CheckpointKilled):
        await bulk_core.run(repo, ctx, op_id, batch=1, checkpoints=killed)

    files_session.expire_all()
    partial = await bulk_core.load_bulk_plan(repo, op_id)
    assert partial.cursor == 1
    assert await _live_children(files_session, destination_id) == {"alpha"}

    assert await bulk_core.resume(repo, ctx, op_id, batch=1) == "done"

    files_session.expire_all()
    # Exactly one of each name: a resume that re-applied the first window would
    # have made "alpha (1)" beside it, because the conflict behaviour is `fail`.
    assert await _live_children(files_session, destination_id) == {"alpha", "beta", "gamma"}
    finished = await bulk_core.load_bulk_plan(repo, op_id)
    assert finished.cursor == 3
    assert [str(one["id"]) for one in finished.results] == ["one", "two", "three"]


async def test_a_batch_can_never_be_run_by_two_runners_at_once(
    files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """The claim is a compare-and-swap on ``queued``, so the second call over a
    finished batch reports the state rather than applying it a second time."""
    drive = await files_factory.drive()
    made = await files_factory.tree(TREE, drive=drive)
    ctx = _ctx(files_org)
    plan = bulk_core.plan(
        [{"id": "one", "op": "createFolder", "parent_id": str(made["dst"].id), "name": "alpha"}]
    )
    op_id = await _queue(repo, ctx, drive, plan)

    await bulk_core.run(repo, ctx, op_id)
    assert await bulk_core.resume(repo, ctx, op_id) == "done"
    assert (await bulk_core.load_bulk_plan(repo, op_id)).cursor == 1


async def test_a_batch_never_reaches_another_orgs_operation(
    files_org_factory: Any, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """The plan is read under the org scope, so another tenant's repo cannot
    load — let alone run — this batch: it is simply not there."""
    drive = await files_factory.drive()
    made = await files_factory.tree(TREE, drive=drive)
    ctx = _ctx(files_org)
    plan = bulk_core.plan(
        [{"id": "one", "op": "createFolder", "parent_id": str(made["dst"].id), "name": "alpha"}]
    )
    op_id = await _queue(repo, ctx, drive, plan)

    stranger = await files_org_factory()
    foreign = FilesRepo(repo.session, stranger.scope)
    from alkera_core.files.errors import NotFound

    with pytest.raises(NotFound):
        await bulk_core.load_bulk_plan(foreign, op_id)


def test_a_refused_item_survives_the_row_it_is_written_onto() -> None:
    """The decision travels on the row, so a runner in another process reads
    back the same refusal the request recorded — a plan that dropped it would
    silently execute an item the policy denied."""
    built = bulk_core.plan(
        [
            {"id": "no", "op": "trash", "item_id": str(uuid.uuid4()), "if_match": 3},
            {"id": "yes", "op": "star", "item_id": str(uuid.uuid4())},
        ]
    ).decide({"no": {"status": 404, "body": {"code": "not_found"}}})

    read_back = bulk_core.BulkPlan.load(json.loads(json.dumps(built.dump())))

    assert read_back.items[0].allowed is False
    assert read_back.items[0].denial == {"status": 404, "body": {"code": "not_found"}}
    assert read_back.items[1].allowed is True
    assert read_back.items[1].denial is None
