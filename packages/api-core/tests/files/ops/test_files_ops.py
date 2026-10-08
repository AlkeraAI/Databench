"""Operations, their inverses and undo, against real Postgres.

The property at the centre is the one the product promises: *whatever you did,
undoing it in reverse gives you back the tree you had*. It is checked over
generated sequences rather than a hand-picked one, comparing every row's name,
parent, path, attributes and trashed state — a snapshot equality, not a spot
check, because the bugs in this area are the columns nobody thought to compare.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors, ops
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.namespace import Namespace
from alkera_core.files.ops import Operations, OperationState, Progress
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import Trash
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _ops(repo: FilesRepo, org: FilesOrg, clock: FakeClock, **kwargs: Any) -> Operations:
    return Operations(repo, _ctx(org), clock, None, **kwargs)


async def _snapshot(session: AsyncSession, org: FilesOrg) -> list[tuple[Any, ...]]:
    """Every column of the tenant's tree that an inverse has to restore.

    Read as raw columns rather than through the ORM: the mutations run as
    ``text()`` statements, so an identity-mapped instance would answer with the
    values it was loaded with and hide exactly the drift this asserts against.
    """
    rows = await session.execute(
        text(
            "SELECT id, name, parent_id, path_ids::text, depth, mode, uid, gid, "
            "mtime_ns, trashed_at IS NOT NULL FROM file_nodes "
            "WHERE org_team_id = :org ORDER BY id"
        ),
        {"org": org.org_team_id},
    )
    return [tuple(row) for row in rows.fetchall()]


async def _node(repo: FilesRepo, node_id: NodeId) -> Any:
    async with repo.transaction():
        node = await repo.node(node_id)
        assert node is not None
        await repo.session.refresh(node)
    return node


async def _seed(files_factory: FilesFactory) -> tuple[DriveId, dict[str, NodeId]]:
    drive = await files_factory.drive()
    made = await files_factory.tree("one/ two/ a.txt b.txt", drive=drive)
    return DriveId(drive.id), {name: NodeId(node.id) for name, node in made.items()}


# ---- the property ---------------------------------------------------------


def op_sequences() -> st.SearchStrategy[list[tuple[str, str, str]]]:
    """Sequences over the undoable mutations, biased to interact.

    The names and the two folders are fixed so the sequence can *collide* — a
    rename onto a name a sibling may take, a move back and forth — which is
    where an inverse that merely "looks right" stops restoring the exact prior
    tree.
    """
    step = st.one_of(
        st.tuples(st.just("rename"), st.sampled_from(["a.txt", "b.txt"]), st.just("z.txt")),
        st.tuples(st.just("move"), st.sampled_from(["a.txt", "b.txt"]), st.just("two")),
        st.tuples(st.just("attrs"), st.sampled_from(["a.txt", "b.txt"]), st.just("")),
        st.tuples(st.just("trash"), st.sampled_from(["a.txt", "b.txt"]), st.just("")),
    )
    return st.lists(step, min_size=1, max_size=4)


#: The generated step names mapped onto the kinds ``file_ops`` admits: the
#: table's CHECK names one kind per *bulk* operation, so a rename rides ``move``
#: (both are namespace edits) and an attribute edit rides ``acl_rewrite``.
STEP_KINDS = {"rename": "move", "move": "move", "attrs": "acl_rewrite", "trash": "trash"}


async def _apply_step(
    step: tuple[str, str, str],
    *,
    repo: FilesRepo,
    org: FilesOrg,
    clock: FakeClock,
    drive_id: DriveId,
    nodes: dict[str, NodeId],
    operations: Operations,
) -> OperationState | None:
    """Run one generated step as its own operation, or skip it if it cannot run.

    A step whose target an earlier step already trashed is skipped rather than
    forced: the property under test is about inverses, and a forward call the
    tree refuses would be testing the mutation's refusals instead.
    """
    kind, target, argument = step
    node_id = nodes[target]
    node = await _node(repo, node_id)
    if node.trashed_at is not None:
        return None
    try:
        async with operations.perform(STEP_KINDS[kind], drive_id=drive_id) as state:
            async with repo.transaction():
                if kind == "rename":
                    await Namespace(repo, _ctx(org), clock, None).rename(
                        node_id, argument.encode(), if_match=node.etag, op=operations
                    )
                elif kind == "move":
                    await Namespace(repo, _ctx(org), clock, None).move(
                        node_id, nodes[argument], if_match=node.etag, op=operations
                    )
                elif kind == "attrs":
                    await ops.set_attrs(
                        repo, _ctx(org), node_id, {"mode": 0o600, "uid": 7}, op=operations
                    )
                else:
                    await Trash(repo, _ctx(org), clock, None).trash(
                        node_id, if_match=node.etag, op=operations
                    )
    except errors.FilesError:
        return None
    return state


@settings(
    max_examples=8,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(sequence=op_sequences())
async def test_undoing_a_sequence_in_reverse_restores_the_exact_prior_tree(
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
    files_session: AsyncSession,
    sequence: list[tuple[str, str, str]],
) -> None:
    """Undo in reverse is the identity on the tree, column by column.

    Each example seeds its own tenant: an org holds exactly one org drive, and
    a shared one would let one example's leftovers decide the next one's result.
    """
    files_org = await files_org_factory()
    repo = FilesRepo(files_session, files_org.scope)
    files_factory = FilesFactory(files_session, files_org)
    drive_id, nodes = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    before = await _snapshot(files_session, files_org)

    performed: list[OperationState] = []
    for step in sequence:
        state = await _apply_step(
            step,
            repo=repo,
            org=files_org,
            clock=clock,
            drive_id=drive_id,
            nodes=nodes,
            operations=operations,
        )
        if state is not None:
            performed.append(state)

    for state in reversed(performed):
        await operations.undo(state.id)

    assert await _snapshot(files_session, files_org) == before


async def test_undo_of_an_undo_is_a_redo(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """The second undo puts back exactly what the first one took away."""
    drive_id, nodes = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    node = await _node(repo, nodes["a.txt"])
    original_parent = node.parent_id

    async with operations.perform("move", drive_id=drive_id) as moved:
        async with repo.transaction():
            await Namespace(repo, _ctx(files_org), clock, None).move(
                nodes["a.txt"], nodes["one"], if_match=node.etag, op=operations
            )
    after_forward = await _snapshot(files_session, files_org)

    undone, _ = await operations.undo(moved.id)
    assert (await _node(repo, nodes["a.txt"])).parent_id == original_parent

    await operations.undo(undone.id)
    assert (await _node(repo, nodes["a.txt"])).parent_id == nodes["one"]
    assert await _snapshot(files_session, files_org) == after_forward


# ---- the state machine ----------------------------------------------------


async def test_progress_is_observable_from_another_session_mid_run(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    checkpoints: PausingCheckpoints,
    files_engine: AsyncEngine,
) -> None:
    """A reader on its own connection sees `running` and the count while the body waits.

    The writer is paused mid-transaction at the checkpoint, so the reader is on
    a second connection from the pool and reads what the writer has committed
    rather than what it is still holding.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock, checkpoints=checkpoints)
    started = await operations.start("copy", drive_id=drive_id, total=4)

    other = AsyncSession(bind=files_engine, expire_on_commit=False)
    watcher = Operations(FilesRepo(other, files_org.scope), _ctx(files_org), clock, None)

    async def body(progress: Progress) -> int:
        await progress.tick(2)
        return progress.done

    checkpoints.pause("ops.after_tick")
    try:
        run = asyncio.create_task(
            operations.run(
                started.id,
                lambda _: body(Progress(operations, started.id, every=1)),
            )
        )
        await checkpoints.wait_paused("ops.after_tick")
        seen = await watcher.get(started.id)
        assert (seen.state, seen.done, seen.total) == ("running", 2, 4)
        checkpoints.release("ops.after_tick")
        assert await run == 2
    finally:
        await other.close()

    assert (await operations.get(started.id)).state == "done"


async def test_cancel_stops_a_batched_body_at_a_boundary_with_a_consistent_tree(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """The body sees the flag at the next boundary; the batch it ran stays committed."""
    drive_id, nodes = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("move", drive_id=drive_id, total=2)
    renamed: list[bytes] = []

    async def body(progress: Progress) -> None:
        for index, target in enumerate((nodes["a.txt"], nodes["b.txt"])):
            await progress.stop_if_cancelled()
            node = await _node(repo, target)
            async with repo.transaction():
                await Namespace(repo, _ctx(files_org), clock, None).rename(
                    target, f"batch{index}.txt".encode(), if_match=node.etag
                )
            renamed.append(bytes((await _node(repo, target)).name))
            await progress.tick()
            await operations.cancel(started.id)

    await operations.run(started.id, body)

    assert renamed == [b"batch0.txt"]
    assert (await operations.get(started.id)).state == "cancelled"
    rows = await files_session.execute(
        text(
            "SELECT name FROM file_nodes WHERE org_team_id = :org AND kind = 'file' ORDER BY name"
        ),
        {"org": files_org.org_team_id},
    )
    assert [bytes(row[0]) for row in rows.fetchall()] == [b"b.txt", b"batch0.txt"]


async def test_watchdog_fails_a_running_operation_with_a_lost_heartbeat(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Ten minutes without a beat is a dead runner; one second short of it is not."""
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("copy", drive_id=drive_id)
    claimed = await operations._transition(started.id, "queued", "running", heartbeat=True)
    assert claimed
    beat = (await operations.get(started.id)).heartbeat_at
    assert beat is not None

    assert await operations.watchdog(beat + ops.HEARTBEAT_DEADLINE - timedelta(seconds=1)) == []
    assert (await operations.get(started.id)).state == "running"

    assert await operations.watchdog(beat + ops.HEARTBEAT_DEADLINE + timedelta(seconds=1)) == [
        started.id
    ]
    failed = await operations.get(started.id)
    assert failed.state == "failed"
    assert failed.errors == ["heartbeat lost"]


async def test_a_declared_byte_budget_buys_the_operation_its_transfer_time(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A promote carrying a terabyte is not dead for being large.

    The flat ten minutes is what a rename needs. An operation that says how
    many bytes it is moving is given those ten minutes PLUS the time the bytes
    need at the budgeted throughput — so a long landing survives the wait a
    small one is judged on, and is still failed once it has been silent for
    longer than its own bytes could possibly take.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("upload", drive_id=drive_id)
    assert await operations._transition(started.id, "queued", "running", heartbeat=True)
    terabyte = 1_024**4
    await Progress(operations, started.id).declare_bytes(terabyte)
    beat = (await operations.get(started.id)).heartbeat_at
    assert beat is not None
    earned = timedelta(seconds=terabyte / ops.WATCHDOG_BYTES_PER_SECOND)

    assert await operations.watchdog(beat + ops.HEARTBEAT_DEADLINE + timedelta(minutes=1)) == []
    assert (await operations.get(started.id)).state == "running"

    over = beat + ops.HEARTBEAT_DEADLINE + earned + timedelta(seconds=1)
    assert await operations.watchdog(over) == [started.id]
    assert (await operations.get(started.id)).state == "failed"


async def test_the_bytes_already_moved_are_taken_off_the_wait(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The wait covers what is LEFT, so a stall in the last mile is caught.

    An operation that declared a terabyte and has landed all but a gigabyte of
    it is no longer owed a terabyte's worth of silence: the budget is read
    against what it has reported moving, so the runner that dies at 99% is
    failed in the time the last gigabyte would have taken.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("upload", drive_id=drive_id)
    assert await operations._transition(started.id, "queued", "running", heartbeat=True)
    terabyte = 1_024**4
    gigabyte = 1_024**3
    progress = Progress(operations, started.id)
    await progress.declare_bytes(terabyte)
    await progress.bytes_moved(terabyte - gigabyte)
    state = await operations.get(started.id)
    assert state.bytes == terabyte - gigabyte
    beat = state.heartbeat_at
    assert beat is not None

    remaining = timedelta(seconds=gigabyte / ops.WATCHDOG_BYTES_PER_SECOND)
    assert await operations.watchdog(beat + ops.HEARTBEAT_DEADLINE + timedelta(seconds=1)) == []
    over = beat + ops.HEARTBEAT_DEADLINE + remaining + timedelta(seconds=1)
    assert await operations.watchdog(over) == [started.id]


async def test_an_operation_that_declares_no_bytes_is_judged_on_the_floor(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Deriving the wait from bytes may not make a byte-less operation immortal.

    A move moves no bytes and declares none, so nothing is added to the floor
    and it dies at ten minutes exactly as it always did.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("move", drive_id=drive_id)
    assert await operations._transition(started.id, "queued", "running", heartbeat=True)
    beat = (await operations.get(started.id)).heartbeat_at
    assert beat is not None

    assert await operations.watchdog(beat + ops.HEARTBEAT_DEADLINE - timedelta(seconds=1)) == []
    assert await operations.watchdog(beat + ops.HEARTBEAT_DEADLINE + timedelta(seconds=1)) == [
        started.id
    ]


async def test_counting_bytes_publishes_them_and_beats(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A poll on a long write sees a figure that moves, and a live heartbeat.

    Bytes under the publish threshold are counted but not written — a beat per
    chunk would cost a history row per 64 KiB — and the beat that does land
    carries the running total and refreshes the heartbeat the watchdog reads.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("upload", drive_id=drive_id)
    assert await operations._transition(started.id, "queued", "running", heartbeat=True)
    progress = Progress(operations, started.id)
    await progress.declare_bytes(4 * ops.BYTE_TICK_EVERY)
    declared = await operations.get(started.id)
    assert declared.bytes == 0
    assert declared.heartbeat_at is not None

    await progress.bytes_moved(ops.BYTE_TICK_EVERY // 2)
    assert (await operations.get(started.id)).bytes == 0
    assert progress.bytes == ops.BYTE_TICK_EVERY // 2

    await progress.bytes_moved(ops.BYTE_TICK_EVERY)
    published = await operations.get(started.id)
    assert published.bytes == ops.BYTE_TICK_EVERY + ops.BYTE_TICK_EVERY // 2
    assert published.heartbeat_at is not None
    assert published.heartbeat_at >= declared.heartbeat_at


async def test_undo_after_the_window_closes_is_refused(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """The undo window is checked against the injected clock, across the boundary."""
    drive_id, nodes = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    node = await _node(repo, nodes["a.txt"])
    deadline = EPOCH + timedelta(minutes=30)

    async with operations.perform("move", drive_id=drive_id, undoable_until=deadline) as state:
        async with repo.transaction():
            await Namespace(repo, _ctx(files_org), clock, None).rename(
                nodes["a.txt"], b"renamed.txt", if_match=node.etag, op=operations
            )

    clock.move_to(deadline)
    undone, _ = await operations.undo(state.id)
    assert undone.state == "done"
    assert bytes((await _node(repo, nodes["a.txt"])).name) == b"a.txt"

    after = await _snapshot(files_session, files_org)
    clock.move_to(deadline + timedelta(seconds=1))
    with pytest.raises(errors.PreconditionFailed):
        await operations.undo(undone.id)
    assert await _snapshot(files_session, files_org) == after


async def test_a_raw_inverse_is_refused_and_the_tree_is_untouched(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """An inverse a newer writer recorded is refused, never guessed at."""
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("acl_rewrite", drive_id=drive_id)
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_ops SET state = 'done', "
                'inverse = \'{"kind": "teleport", "where": "mars"}\'::jsonb '
                "WHERE id = :id AND org_team_id = :org"
            ),
            {"id": started.id, "org": files_org.org_team_id},
        )
    before = await _snapshot(files_session, files_org)

    state = await operations.get(started.id)
    assert isinstance(state.inverse, ops.RawInverse)
    with pytest.raises(errors.InvalidRequest):
        await operations.undo(started.id)
    assert await _snapshot(files_session, files_org) == before


async def test_an_operation_with_no_inverse_never_undoes(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Permanent deletion records nothing, so nothing can replay it backwards."""
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    async with operations.perform("purge", drive_id=drive_id) as purged:
        pass
    assert (await operations.get(purged.id)).inverse is None
    with pytest.raises(errors.InvalidRequest):
        await operations.undo(purged.id)


async def test_an_unknown_operation_kind_is_refused_before_the_insert(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A kind the table would reject names itself instead of poisoning the transaction."""
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    with pytest.raises(errors.InvalidRequest):
        await operations.start("teleport", drive_id=drive_id)
    assert await _drive_op_count(repo, drive_id) == 0


async def _drive_op_count(repo: FilesRepo, drive_id: DriveId) -> int:
    async with repo.transaction():
        rows = await repo.session.execute(
            text("SELECT count(*) FROM file_ops WHERE drive_id = :drive"),
            {"drive": drive_id},
        )
        return int(rows.scalar_one())


async def test_a_second_runner_cannot_claim_a_finished_operation(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The claim is a compare-and-swap, so the body never runs twice."""
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("copy", drive_id=drive_id)
    runs = 0

    async def body(progress: Progress) -> None:
        nonlocal runs
        runs += 1

    await operations.run(started.id, body)
    with pytest.raises(errors.PreconditionFailed):
        await operations.run(started.id, body)
    assert runs == 1


async def _xattrs(session: AsyncSession, node_id: NodeId) -> dict[str, str]:
    """The stored xattr document, read as a raw column.

    Around the identity map on purpose: ``set_attrs`` writes through a
    ``text()`` statement, so an ORM instance would answer with the document it
    was loaded with and a missing write would be invisible.
    """
    row = await session.execute(
        text("SELECT xattrs FROM file_nodes WHERE id = :id"), {"id": node_id}
    )
    stored: dict[str, str] = row.scalar_one()
    return stored


async def test_set_attrs_round_trips_user_xattrs_and_undo_puts_the_old_set_back(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """F-294/F-187: `set_attrs` refused every `user.*` xattr, so the push's attrs
    PATCH could carry provenance but never store it.

    The stored spelling is the base64 the wire renders, and the inverse is the
    document it replaced — asserted through a real undo rather than by reading
    the recorded inverse, because what matters is the row afterwards."""
    drive_id, nodes = await _seed(files_factory)
    node_id = nodes["a.txt"]
    operations = _ops(repo, files_org, clock)
    first = {"user.alkera.origin": b"github.com/acme/repo", "user.tags": b"Red\x00\xff"}

    async with operations.perform("acl_rewrite", drive_id=drive_id) as state:
        async with repo.transaction():
            await ops.set_attrs(repo, _ctx(files_org), node_id, {"xattrs": first}, op=operations)

    stored = await _xattrs(files_session, node_id)
    assert stored == {
        "user.alkera.origin": base64.b64encode(first["user.alkera.origin"]).decode("ascii"),
        "user.tags": base64.b64encode(first["user.tags"]).decode("ascii"),
    }

    async with operations.perform("acl_rewrite", drive_id=drive_id) as second:
        async with repo.transaction():
            await ops.set_attrs(
                repo,
                _ctx(files_org),
                node_id,
                {"xattrs": {"user.tags": b"Blue"}},
                op=operations,
            )
    # A whole-document replace, not a merge: the name the second write omitted
    # is gone, which is the only shape whose inverse can restore a removal.
    assert await _xattrs(files_session, node_id) == {
        "user.tags": base64.b64encode(b"Blue").decode("ascii")
    }

    await operations.undo(second.id)
    assert await _xattrs(files_session, node_id) == stored
    assert state.id != second.id


@pytest.mark.parametrize(
    ("attrs", "why"),
    [
        pytest.param({"xattrs": {"system.posix_acl_access": b"x"}}, "kernel-owned", id="system"),
        pytest.param({"xattrs": {"security.selinux": b"x"}}, "kernel-owned", id="security"),
        pytest.param({"xattrs": {"trusted.overlay": b"x"}}, "kernel-owned", id="trusted"),
        pytest.param({"xattrs": {"tags": b"x"}}, "no namespace at all", id="bare-name"),
        pytest.param(
            {"xattrs": {"user.big": b"x" * (64 * 1024 + 1)}}, "over the value cap", id="too-large"
        ),
        pytest.param({"ctime_ns": 1}, "server-derived", id="ctime"),
        pytest.param({"name": "x"}, "structural", id="name"),
    ],
)
async def test_an_xattr_a_box_could_not_restore_is_refused_and_nothing_is_written(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    attrs: dict[str, Any],
    why: str,
) -> None:
    """A refusal has to leave the row alone: a partial write would store half a
    document a materializer would then apply. The `user.` restriction is the
    interesting one — the kernel namespaces need privilege a box does not have,
    so accepting them would promise a round trip that cannot happen."""
    _drive_id, nodes = await _seed(files_factory)
    node_id = nodes["a.txt"]
    async with repo.transaction():
        await ops.set_attrs(repo, _ctx(files_org), node_id, {"xattrs": {"user.keep": b"yes"}})
    before = await _xattrs(files_session, node_id)

    async with repo.transaction():
        with pytest.raises(errors.InvalidRequest):
            await ops.set_attrs(repo, _ctx(files_org), node_id, attrs)

    assert await _xattrs(files_session, node_id) == before, why


async def test_undoing_a_trash_hands_back_the_re_parent_it_had_to_queue(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    files_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undo whose restore has to queue its re-parent must say so.

    The inverse of a trash is a restore, and a restore that cannot land the
    subtree back under its old (now trashed) parent moves it — which is a
    queued operation once the subtree is oversized. Nothing inside the library
    starts a runner, so an operation dropped here is a ``file_ops`` row nobody
    ever picks up and a subtree that never arrives where the undo said it had.
    """
    from alkera_core.files import namespace as namespace_module

    drive = await files_factory.drive()
    tree = await files_factory.tree("home/ home/inner/ home/inner/a.txt away/", drive=drive)
    operations = _ops(repo, files_org, clock)
    folder = tree["home/inner"]
    trash = Trash(repo, _ctx(files_org), clock, None)

    async with operations.perform("trash", drive_id=DriveId(drive.id)) as trashed:
        async with repo.transaction():
            await trash.trash(NodeId(folder.id), if_match=int(folder.etag), op=operations)
    # The old parent goes to the trash too, so the restore has to land the
    # subtree somewhere else — which is what makes it a move at all.
    home = tree["home"]
    async with repo.transaction():
        await trash.trash(NodeId(home.id), if_match=int(home.etag))
    # Every subtree is oversized: the threshold, not a hundred thousand rows.
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 0)

    undone, queued = await operations.undo(trashed.id)

    assert queued is not None, "the queued re-parent was swallowed by the undo"
    assert queued.kind == "move"
    assert queued.id != undone.id
    still_queued = (
        await files_session.execute(
            text("SELECT state FROM file_ops WHERE id = :id"), {"id": queued.id}
        )
    ).scalar_one()
    assert still_queued == "queued", "the caller starts the runner, not the library"
    # The restore itself happened; only the re-parent is outstanding.
    restored_row = (
        await files_session.execute(
            text("SELECT trashed_at FROM file_nodes WHERE id = :id"), {"id": folder.id}
        )
    ).scalar_one()
    assert restored_row is None


async def test_a_finished_run_publishes_the_last_short_window_of_work(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Progress is written every Nth item, so the tail of a batch that is not a
    whole window was counted in memory and never reached the row.

    The operation settled ``done`` while its progress still read 100 of 101,
    and a client drawing that figure showed a finished batch one item short for
    ever — which is what the terminal statement now publishes.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("bulk", drive_id=drive_id, total=101)

    async def _body(progress: Progress) -> None:
        await progress.tick(101)

    await operations.run(started.id, _body)

    finished = await operations.get(started.id)
    assert (finished.state, finished.done, finished.total) == ("done", 101, 101)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param("cancelled", "cancelled", id="cancelled"),
        pytest.param("failed", "failed", id="failed"),
    ],
)
async def test_a_run_that_stops_early_publishes_what_it_had_reached(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    outcome: str,
    expected: str,
) -> None:
    """The count a client is left looking at has to be the work that happened.

    A cancel taken at a batch boundary, and a body that raised, both settle
    through the same statement — so the items of the last short window are on
    the row in either case, not only on the happy path. Without it a cancelled
    batch reports fewer items than it really moved, and a retry or an undo is
    reasoned about from the wrong figure.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("bulk", drive_id=drive_id, total=101)

    async def _stops(progress: Progress) -> None:
        await progress.tick(7)
        if outcome == "cancelled":
            raise ops.OperationCancelled
        raise RuntimeError("the store went away")

    if outcome == "cancelled":
        assert await operations.run(started.id, _stops) is None
    else:
        with pytest.raises(RuntimeError):
            await operations.run(started.id, _stops)

    stopped = await operations.get(started.id)
    assert (stopped.state, stopped.done) == (expected, 7)


async def test_a_resumed_run_never_publishes_a_smaller_count_than_the_one_before(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A resumed body counts only the items left to it.

    The run that finishes the tail of a batch has a ``done`` of its own that is
    far smaller than the figure the killed run before it already reached, so
    the terminal publish is a maximum — otherwise finishing a batch would make
    its progress bar jump backwards at the very moment it completed.
    """
    drive_id, _ = await _seed(files_factory)
    operations = _ops(repo, files_org, clock)
    started = await operations.start("bulk", drive_id=drive_id, total=101)

    async def _first(progress: Progress) -> None:
        await progress.tick(100)
        raise RuntimeError("killed after the hundredth item")

    with pytest.raises(RuntimeError):
        await operations.run(started.id, _first)
    assert (await operations.get(started.id)).done == 100

    # The resume: a fresh row-claim and a body that only has the tail to do.
    await repo.session.execute(
        text("UPDATE file_ops SET state = 'queued' WHERE id = :id"), {"id": started.id}
    )
    await repo.session.commit()

    async def _rest(progress: Progress) -> None:
        await progress.tick(1)

    await operations.run(started.id, _rest)

    finished = await operations.get(started.id)
    assert (finished.state, finished.done) == ("done", 100)
