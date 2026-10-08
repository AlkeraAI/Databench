"""An oversized move as an operation: moving flags, batches, resume, cancel.

What makes a move an operation is that its subtree is bigger than the request
path will rewrite inline, and what makes it interesting is that it then takes
more than one batch. Both of those are ceilings, so this module brings the
ceilings down to the corpus rather than growing the corpus past the production
ones: ``MAX_INLINE_MOVE_NODES`` is patched to :data:`INLINE_CEILING` and every
run is given :data:`BATCH`. Seeding twenty thousand rows per test proved
exactly the same things and spent minutes of every suite run doing it.

Every tree here is still seeded with a real bulk INSERT rather than through
``files_factory.tree``: the rows are what the batch statements select on, and
what is under test is the batching, not the seeding.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors, large_move
from alkera_core.files import namespace as namespace_module
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId, OperationId
from alkera_core.files.large_move import resume_large_move, run_large_move
from alkera_core.files.namespace import Namespace
from alkera_core.files.ops import DEFAULT_TICK_EVERY, Operations, OperationState
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Rows one batch rewrites here. At least the operations layer's publish
#: interval, because ``Progress.tick`` writes ``done`` onto the operation row
#: only once that many items have accumulated: a smaller batch would leave the
#: published count at zero however many rows had really been rewritten, and the
#: resume proof reads exactly that count.
BATCH = DEFAULT_TICK_EVERY

#: The inline ceiling for this module, monkeypatched over the production
#: ``MAX_INLINE_MOVE_NODES`` by :func:`_small_inline_ceiling`. Under
#: :data:`SUBTREE_NODES`, so the seeded move is unambiguously an operation, and
#: not a multiple of :data:`BATCH`, so a batch and a ceiling cannot be confused
#: for each other.
INLINE_CEILING = 30

#: Descendants under the moved root: past the ceiling, so the move routes to an
#: operation, and three batches' worth, so the cursor has somewhere to stop, a
#: resume has rows left to take, and a partial rewrite is visibly partial.
SUBTREE_NODES = 3 * BATCH

#: How long an armed checkpoint waits. Generous against a loaded machine — a
#: test that timed out waiting for work that was still happening would be a
#: flake, not a finding — but well inside the suite's per-test budget, so a real
#: deadlock is reported as the checkpoint that was never reached rather than as
#: a test killed by the clock.
CHECKPOINT_TIMEOUT = 30.0

#: A cursor past every real id: continuing from it would take no rows at all,
#: which is the loudest shape a corrupt cursor has.
MAX_UUID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


@pytest.fixture(autouse=True)
def _small_inline_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the inline ceiling under the seeded subtree, for every test here.

    The routing decision is ``children > MAX_INLINE_MOVE_NODES``; nothing below
    it cares whether the number crossed was ten thousand or thirty. Lowering it
    is what lets the same proofs run over a corpus a test can seed in one
    statement, and it is the seam the rest of the Files suite already moves this
    ceiling with.
    """
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", INLINE_CEILING)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _seed_big_subtree(
    session: AsyncSession, drive: FileDrive, folders: FilesFactory, *, count: int
) -> tuple[FileNode, FileNode]:
    """``src/big/`` with ``count`` leaves under it, plus an empty ``dst/``.

    Returns the subtree root and the destination folder.
    """
    made = await folders.tree("src/ src/big/ dst/", drive=drive)
    big, destination = made["src/big"], made["dst"]
    first = drive.next_ino
    inos = list(range(first, first + count))
    await session.execute(
        text(
            "INSERT INTO file_nodes (id, ino, drive_id, org_team_id, parent_id, kind, name, "
            "name_display, name_key, flags_names, xattrs, metadata, path_ids, depth, mode, "
            "uid, gid, nlink, rdev, size, atime_ns, mtime_ns, ctime_ns, birthtime_ns, etag, "
            "flags, state, trust, traversal_only) "
            "SELECT gen_random_uuid(), t.ino, :drive, :org, :parent, 'file', "
            "  ('leaf-' || t.ino)::bytea, 'leaf-' || t.ino, 'leaf-' || t.ino, "
            "  '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, "
            "  CAST(:parent_path || '.' || t.label AS ltree), "
            "  :depth, 420, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 'live', 'own', false "
            "FROM unnest(CAST(:inos AS bigint[]), CAST(:labels AS text[])) AS t(ino, label)"
        ),
        {
            "inos": inos,
            "labels": [ino_label(ino) for ino in inos],
            "drive": drive.id,
            "org": drive.org_team_id,
            "parent": big.id,
            "parent_path": big.path_ids,
            "depth": big.depth + 1,
        },
    )
    drive.next_ino = first + count
    await session.commit()
    return big, destination


async def _fresh_repo(org: FilesOrg, engine: AsyncEngine) -> tuple[FilesRepo, AsyncSession]:
    """A repo on its own connection, so a second actor is a second backend.

    The connection comes from the suite's pool. The runner these open is paused
    mid-transaction at a checkpoint for as long as the test looks at it, so a
    second caller is served a second connection rather than the runner's.
    """
    session = AsyncSession(bind=engine, expire_on_commit=False)
    return FilesRepo(session, org.scope), session


async def _paths(session: AsyncSession, drive: FileDrive) -> dict[uuid.UUID, tuple[str, int, str]]:
    rows = (
        await session.execute(
            text(
                "SELECT id, path_ids::text, depth, state, parent_id FROM file_nodes "
                "WHERE drive_id = :drive"
            ),
            {"drive": drive.id},
        )
    ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


async def _assert_tree_consistent(session: AsyncSession, drive: FileDrive) -> None:
    """Every node's ``path_ids`` equals its parent's plus its own ino label.

    Recomputed from the adjacency list — the truth — rather than compared with
    a value the move wrote, so a rewrite that missed a batch is visible.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, parent_id, ino, path_ids::text, depth FROM file_nodes "
                "WHERE drive_id = :drive"
            ),
            {"drive": drive.id},
        )
    ).all()
    by_id = {row[0]: row for row in rows}
    for node_id, parent_id, ino, path, depth in rows:
        if parent_id is None:
            assert path == ino_label(ino)
            assert depth == 0
            continue
        parent = by_id[parent_id]
        assert path == f"{parent[3]}.{ino_label(ino)}", f"node {node_id} path disagrees"
        assert depth == parent[4] + 1, f"node {node_id} depth disagrees"


async def _states(session: AsyncSession, root: FileNode) -> set[str]:
    """The distinct states of the subtree, found at the root's *current* path.

    Read back from the row rather than from the seeded instance: after the move
    the ORM's ``path_ids`` names a subtree that no longer has any rows, and an
    empty answer would pass a "nothing is moving" assertion for free.
    """
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT n.state FROM file_nodes AS n, file_nodes AS r "
                "WHERE r.id = :root AND n.org_team_id = r.org_team_id "
                "AND n.path_ids <@ r.path_ids"
            ),
            {"root": root.id},
        )
    ).all()
    return {row[0] for row in rows}


async def _outbox_for(session: AsyncSession, node_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE entity_id = :id AND type = 'file_node.changed'"
                ),
                {"id": str(node_id)},
            )
        ).scalar_one()
    )


@pytest.fixture
async def big_move(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> AsyncIterator[dict[str, Any]]:
    """A seeded subtree past the inline ceiling, plus a queued move for it."""
    drive = await files_factory.drive()
    big, destination = await _seed_big_subtree(
        files_session, drive, files_factory, count=SUBTREE_NODES
    )
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        routed = await namespace.move(NodeId(big.id), NodeId(destination.id), if_match=big.etag)
    assert isinstance(routed, OperationState)
    yield {
        "drive": drive,
        "root": big,
        "destination": destination,
        "op": routed,
        "before": await _paths(files_session, drive),
    }


# ---- routing --------------------------------------------------------------


async def test_an_oversized_move_becomes_a_queued_operation_and_moves_nothing(
    big_move: dict[str, Any], files_session: AsyncSession
) -> None:
    op: OperationState = big_move["op"]
    assert (op.kind, op.state) == ("move", "queued")
    assert op.total == SUBTREE_NODES + 1
    assert op.result_node_id == big_move["root"].id
    assert await _paths(files_session, big_move["drive"]) == big_move["before"]


async def test_an_inline_sized_move_still_returns_the_node(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The negative twin of the routing: under the threshold, nothing changes."""
    drive = await files_factory.drive()
    made = await files_factory.tree("src/ src/small/ dst/", drive=drive)
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        moved = await namespace.move(
            NodeId(made["src/small"].id), NodeId(made["dst"].id), if_match=made["src/small"].etag
        )
    assert isinstance(moved, FileNode)
    assert moved.parent_id == made["dst"].id


# ---- the run --------------------------------------------------------------


async def test_a_run_moves_the_whole_subtree_and_emits_one_root_event(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    root: FileNode = big_move["root"]
    before = await _outbox_for(files_session, root.id)
    await run_large_move(repo, _ctx(files_org), big_move["op"].id, batch=BATCH, clock=clock)

    await _assert_tree_consistent(files_session, big_move["drive"])
    # Nothing is left mid-move; the whole subtree is instead marked for the ACL
    # repair the move queues, because it hangs under different ancestors now and
    # every cached permission body in it describes the chain it has left. The
    # mark is what makes those nodes read through to the chain meanwhile.
    assert await _states(files_session, root) == {"acl_rewriting"}
    row = (
        await files_session.execute(
            text("SELECT parent_id FROM file_nodes WHERE id = :id"), {"id": root.id}
        )
    ).scalar_one()
    assert row == big_move["destination"].id
    # Still ONE event for the root and none for the whole subtree under it:
    # invalidating the subtree must not become an announcement of its own.
    assert await _outbox_for(files_session, root.id) - before == 1
    # And the mark is not permanent: the repair that lifts it is queued, so the
    # subtree reads from the chain until a worker re-interns it rather than for
    # ever — and the fsck sweeper leaves a flag with a live op alone.
    queued = (
        await files_session.execute(
            text(
                "SELECT count(*) FROM file_ops "
                "WHERE kind = 'acl_rewrite' AND result_node_id = :root AND state = 'queued'"
            ),
            {"root": root.id},
        )
    ).scalar_one()
    assert queued == 1
    assert (await Operations(repo, _ctx(files_org), clock).get(big_move["op"].id)).state == "done"


@pytest.mark.parametrize(
    "write",
    [
        pytest.param("create", id="create"),
        pytest.param("rename", id="rename"),
        pytest.param("move", id="move"),
    ],
)
async def test_a_write_into_a_moving_subtree_is_refused(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
    write: str,
    files_engine: AsyncEngine,
) -> None:
    """Every request-path mutation shares one refusal while the flag is up."""
    root: FileNode = big_move["root"]
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.pause("large_move.after_mark")
    runner, runner_session = await _fresh_repo(files_org, files_engine)
    task = asyncio.create_task(
        run_large_move(
            runner, _ctx(files_org), big_move["op"].id, clock=clock, checkpoints=checkpoints
        )
    )
    try:
        await checkpoints.wait_paused("large_move.after_mark")
        assert await _states(files_session, root) == {"moving"}

        writer, writer_session = await _fresh_repo(files_org, files_engine)
        namespace = Namespace(writer, _ctx(files_org), clock, None)
        moving = (
            await files_session.execute(
                text(
                    "SELECT id, etag FROM file_nodes WHERE parent_id = :parent ORDER BY ino LIMIT 1"
                ),
                {"parent": root.id},
            )
        ).first()
        assert moving is not None
        with pytest.raises(errors.Conflict) as raised:
            async with writer.transaction():
                if write == "create":
                    await namespace.create(
                        DriveId(root.drive_id), NodeId(root.id), "file", b"intruder"
                    )
                elif write == "rename":
                    await namespace.rename(NodeId(moving[0]), b"renamed", if_match=moving[1])
                else:
                    await namespace.move(NodeId(moving[0]), NodeId(root.id), if_match=moving[1])
        assert raised.value.code == "files.moving"
        await writer_session.close()
    finally:
        checkpoints.release("large_move.after_mark")
        await task
        await runner_session.close()


async def test_partial_progress_is_visible_from_another_session_between_batches(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    clock: FakeClock,
    files_engine: AsyncEngine,
) -> None:
    root: FileNode = big_move["root"]
    destination: FileNode = big_move["destination"]
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.pause("large_move.after_batch")
    runner, runner_session = await _fresh_repo(files_org, files_engine)
    task = asyncio.create_task(
        run_large_move(
            runner,
            _ctx(files_org),
            big_move["op"].id,
            batch=BATCH,
            clock=clock,
            checkpoints=checkpoints,
        )
    )
    try:
        await checkpoints.wait_paused("large_move.after_batch")
        new_prefix = f"{destination.path_ids}.{ino_label(root.ino)}"
        rewritten = int(
            (
                await files_session.execute(
                    text(
                        "SELECT count(*) FROM file_nodes WHERE org_team_id = :org "
                        "AND path_ids <@ CAST(:path AS ltree) AND id <> :root"
                    ),
                    {"org": root.org_team_id, "path": new_prefix, "root": root.id},
                )
            ).scalar_one()
        )
        # One batch, committed, with two more still owed: incremental.
        assert rewritten == BATCH
        cursor = large_move.LargeMovePlan(
            dict(
                (
                    await files_session.execute(
                        text("SELECT result FROM file_ops WHERE id = :id"),
                        {"id": big_move["op"].id},
                    )
                ).scalar_one()
            )
        )
        assert cursor.cursor is not None
    finally:
        checkpoints.release("large_move.after_batch")
        await task
        await runner_session.close()
    await _assert_tree_consistent(files_session, big_move["drive"])


async def test_a_second_overlapping_move_is_refused_while_the_first_runs(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
    files_engine: AsyncEngine,
) -> None:
    """An ancestor's move overlaps too, so the clash is tested both ways."""
    root: FileNode = big_move["root"]
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.pause("large_move.after_mark")
    runner, runner_session = await _fresh_repo(files_org, files_engine)
    task = asyncio.create_task(
        run_large_move(
            runner, _ctx(files_org), big_move["op"].id, clock=clock, checkpoints=checkpoints
        )
    )
    try:
        await checkpoints.wait_paused("large_move.after_mark")
        second = await Operations(repo, _ctx(files_org), clock).start(
            "move", drive_id=DriveId(root.drive_id)
        )
        async with repo.transaction():
            await repo.session.execute(
                text("UPDATE file_ops SET result = CAST(:body AS jsonb) WHERE id = :id"),
                {
                    "id": second.id,
                    "body": __import__("json").dumps(
                        large_move.plan_body(
                            NodeId(root.id),
                            NodeId(big_move["destination"].id),
                            if_match=root.etag,
                            old_path=root.path_ids,
                        )
                    ),
                },
            )
        with pytest.raises(errors.Conflict) as raised:
            await run_large_move(repo, _ctx(files_org), second.id, clock=clock)
        assert raised.value.code == "files.moving"
    finally:
        checkpoints.release("large_move.after_mark")
        await task
        await runner_session.close()


# ---- crash and resume -----------------------------------------------------


async def test_a_crash_between_batches_resumes_from_the_cursor(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The resumed move ends where an uninterrupted one would, from the cursor.

    The crash is a ``CheckpointKilled`` raised inside the loop — the same shape
    a killed process leaves behind, since every batch was already committed —
    and the resume must not rewrite the rows the first run did: it starts at
    the cursor, which is what the batch count proves.
    """
    op_id: OperationId = big_move["op"].id
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.kill("large_move.after_batch")
    with pytest.raises(CheckpointKilled):
        await run_large_move(
            repo, _ctx(files_org), op_id, batch=BATCH, clock=clock, checkpoints=checkpoints
        )
    parked = await large_move.load_plan(repo, op_id)
    assert parked.cursor is not None and parked.root_moved

    await resume_large_move(repo, _ctx(files_org), op_id, batch=BATCH, clock=clock)

    await _assert_tree_consistent(files_session, big_move["drive"])
    # Nothing is left moving; the subtree wears the ACL-repair mark the finish
    # puts on it, because it now hangs under different ancestors.
    assert await _states(files_session, big_move["root"]) == {"acl_rewriting"}
    done = await Operations(repo, _ctx(files_org), clock).get(op_id)
    assert done.state == "done"
    # The resumed run counted only what was left: it continued from the cursor.
    # A restart would have counted every descendant again.
    assert done.done == SUBTREE_NODES - BATCH


async def test_resuming_a_finished_move_is_refused(
    big_move: dict[str, Any], files_org: FilesOrg, repo: FilesRepo, clock: FakeClock
) -> None:
    op_id: OperationId = big_move["op"].id
    await run_large_move(repo, _ctx(files_org), op_id, clock=clock)
    with pytest.raises(errors.PreconditionFailed):
        await resume_large_move(repo, _ctx(files_org), op_id, clock=clock)


# ---- cancel ---------------------------------------------------------------


async def test_a_cancel_at_a_batch_boundary_leaves_a_consistent_tree(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
    files_engine: AsyncEngine,
) -> None:
    """Cancelled after the root moved: the rewrite drains, nothing is left moving."""
    op_id: OperationId = big_move["op"].id
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.pause("large_move.after_batch")
    runner, runner_session = await _fresh_repo(files_org, files_engine)
    task = asyncio.create_task(
        run_large_move(
            runner,
            _ctx(files_org),
            op_id,
            batch=BATCH,
            clock=clock,
            checkpoints=checkpoints,
        )
    )
    try:
        await checkpoints.wait_paused("large_move.after_batch")
        await Operations(repo, _ctx(files_org), clock).cancel(op_id)
    finally:
        checkpoints.release("large_move.after_batch")
        await task
        await runner_session.close()

    assert (await Operations(repo, _ctx(files_org), clock).get(op_id)).state == "cancelled"
    await _assert_tree_consistent(files_session, big_move["drive"])
    # A cancel after the root moved still finishes the tree, so it also invalidates
    # the caches the new ancestors made wrong: nothing is left moving, and the
    # subtree is marked for the repair rather than trusting a body it has outgrown.
    assert await _states(files_session, big_move["root"]) == {"acl_rewriting"}


async def test_a_cancel_before_the_root_moves_stops_and_leaves_the_old_tree(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The negative twin: cancelled while queued, no runner ever claims it."""
    op_id: OperationId = big_move["op"].id
    await Operations(repo, _ctx(files_org), clock).cancel(op_id)
    with pytest.raises(errors.PreconditionFailed):
        await run_large_move(repo, _ctx(files_org), op_id, clock=clock)

    assert (await Operations(repo, _ctx(files_org), clock).get(op_id)).state == "cancelled"
    assert await _paths(files_session, big_move["drive"]) == big_move["before"]
    assert await _states(files_session, big_move["root"]) == {"live"}


# ---- an unresumable move ---------------------------------------------------


async def test_a_cursor_that_names_no_row_is_swept_and_checked_before_the_flag_comes_off(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """A corrupt cursor cannot be continued from — so the resume checks instead.

    Continuing from a cursor that names nothing would take only the rows
    ordered after it and report a finished move over a subtree whose paths
    still disagree with the adjacency list. The resume therefore sweeps from
    the start of the id order and only clears ``moving`` once every committed
    batch has been checked against its parent.
    """
    op_id: OperationId = big_move["op"].id
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.kill("large_move.after_batch")
    with pytest.raises(CheckpointKilled):
        await run_large_move(
            repo, _ctx(files_org), op_id, batch=BATCH, clock=clock, checkpoints=checkpoints
        )
    parked = await large_move.load_plan(repo, op_id)
    assert parked.cursor is not None and parked.root_moved

    # Corrupt the cursor the way a torn write or a stale replica would: an id
    # that is well-formed, in nobody's subtree, and past every real row — so a
    # runner that trusted it would take no rows and call the move finished.
    body = parked.dump()
    body["cursor"] = MAX_UUID
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET result = CAST(:body AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {"id": op_id, "org": files_org.org_team_id, "body": json.dumps(body)},
        )

    assert await large_move.cursor_matches_rows(repo, await large_move.load_plan(repo, op_id)) is (
        False
    )
    await resume_large_move(repo, _ctx(files_org), op_id, batch=BATCH, clock=clock)

    # The check found nothing but lag, so the sweep finished the rewrite: the
    # tree is whole, the moving flag is off, and the operation is done. What the
    # subtree now wears is the ACL-repair mark the finish puts on every moved tree.
    await _assert_tree_consistent(files_session, big_move["drive"])
    assert await _states(files_session, big_move["root"]) == {"acl_rewriting"}
    finished = await large_move.load_plan(repo, op_id)
    assert await large_move.inconsistent_rows(repo, finished) == []
    done = await Operations(repo, _ctx(files_org), clock).get(op_id)
    assert done.state == "done"


async def test_an_inconsistent_subtree_keeps_the_flag_and_names_the_rows(
    big_move: dict[str, Any],
    files_session: AsyncSession,
    files_org: FilesOrg,
    repo: FilesRepo,
    clock: FakeClock,
) -> None:
    """The negative twin: a row no rewrite can repair stops the flag coming off.

    One descendant is given a path its parent does not have — the damage a
    half-applied batch would leave — and the batch loop cannot fix it, because
    the row no longer matches the old prefix it selects on. The operation must
    fail with that row named and the subtree must stay ``moving``.
    """
    op_id: OperationId = big_move["op"].id
    checkpoints = PausingCheckpoints(timeout=CHECKPOINT_TIMEOUT)
    checkpoints.kill("large_move.after_batch")
    with pytest.raises(CheckpointKilled):
        await run_large_move(
            repo, _ctx(files_org), op_id, batch=BATCH, clock=clock, checkpoints=checkpoints
        )
    parked = await large_move.load_plan(repo, op_id)
    assert parked.new_path is not None

    async with repo.transaction():
        victim = (
            await repo.session.execute(
                text(
                    "SELECT id FROM file_nodes WHERE org_team_id = :org "
                    "AND path_ids <@ CAST(:new AS ltree) AND id <> :root "
                    "ORDER BY id LIMIT 1"
                ),
                {"org": files_org.org_team_id, "new": parked.new_path, "root": parked.node_id},
            )
        ).one()[0]
        await repo.session.execute(
            text("UPDATE file_nodes SET depth = depth + 3 WHERE id = :id AND org_team_id = :org"),
            {"id": victim, "org": files_org.org_team_id},
        )

    body = parked.dump()
    body["cursor"] = MAX_UUID
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET result = CAST(:body AS jsonb) "
                "WHERE id = :id AND org_team_id = :org"
            ),
            {"id": op_id, "org": files_org.org_team_id, "body": json.dumps(body)},
        )

    await resume_large_move(repo, _ctx(files_org), op_id, batch=BATCH, clock=clock)

    failed = await Operations(repo, _ctx(files_org), clock).get(op_id)
    assert failed.state == "failed"
    assert any(str(victim) in str(one) for one in failed.errors), failed.errors
    # The flag is what protects the damaged tree from a writer, so it stays.
    assert "moving" in await _states(files_session, big_move["root"])
