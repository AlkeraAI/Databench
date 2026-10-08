"""The net under every Files hand-off, from the worker's side.

A route nudges the workflow that runs its queued operation the moment the row
is durable, but that nudge is best-effort inside a two-second budget: a briefly
unreachable orchestrator, or a process killed after the 202, leaves a durable
``queued`` row nobody was ever told about. The watchdog does not look at those
rows — it only judges ``running`` ones by their heartbeat — so without this
pass a bulk trash, a copy or an oversized move is never run, never failed, and
polled forever.

What is pinned here is the worker layer's own half: which rows the pass hands
to which workflow, that an inline deployment runs them itself instead, that a
second pass does not run an operation twice, and the production wiring (queue,
policy, schedule) without which none of it fires. The statement underneath —
the staleness predicate, the attempt counter, the compare-and-swap — is pinned
against real rows in
``packages/api-core/tests/files/ops/test_files_queued_recovery.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.files.ids import OperationId
from alkera_core.files.ops import (
    QUEUED_STALE_AFTER,
    RUNNER_NUDGED,
    RUNNER_PRESENT,
    QueuedRecovery,
    RecoveredQueued,
)
from alkera_core.temporal import (
    QUEUE_FOR,
    RECOVER_SETTLE_ACTIVITY,
    TaskQueue,
    WorkflowType,
    keyed_workflow_id,
)
from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus
from worker import schedules
from worker.activities import files as activities
from worker.tasks import files as tasks
from worker.temporal import queues
from worker.workflows.files import FilesRecoverQueued


async def _ran_fine(op: OperationId, org_team_id: uuid.UUID) -> str:
    """An inline core that finishes. What these cases are about is the pass
    around it, never the library work itself."""
    return "done"


ORG = uuid.UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class _Tenant:
    """One org's abandoned rows, and what the pass did with them."""

    def __init__(self, rows: list[RecoveredQueued], failed: tuple[OperationId, ...] = ()) -> None:
        self.rows = rows
        self.failed = failed
        self.recovered_at: list[datetime] = []
        self.attempts_asked: list[int] = []
        self.limits_asked: list[int] = []
        self.settled: list[dict[Any, Any]] = []

    def deps(self, session: Any, org_team_id: uuid.UUID) -> tasks.FilesJobDeps:
        return tasks.FilesJobDeps(repo=org_team_id, ctx=org_team_id)  # type: ignore[arg-type]

    async def abandoned_queued(self, now: datetime, *, limit: int) -> tuple[RecoveredQueued, ...]:
        self.recovered_at.append(now)
        self.limits_asked.append(limit)
        taken, self.rows = self.rows[:limit], self.rows[limit:]
        return tuple(taken)

    async def settle_recovery(self, outcomes: Any, *, max_attempts: int) -> QueuedRecovery:
        self.settled.append(dict(outcomes))
        self.attempts_asked.append(max_attempts)
        return QueuedRecovery(failed=self.failed)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The pass runs with no real session and no Postgres."""

    @asynccontextmanager
    async def _session() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(tasks, "_session", _session)
    yield None
    tasks.reset_deps_factory()


def _plant(monkeypatch: pytest.MonkeyPatch, tenant: _Tenant, orgs: list[uuid.UUID]) -> None:
    async def _orgs(session: Any, cutoff: datetime, limit: int) -> list[uuid.UUID]:
        # Stands in for the statement; its predicate is pinned against real
        # rows in the library suite. What this lets the cases below see is the
        # cutoff the loop computed, which is the one thing the worker owns.
        tenant.cutoff = cutoff  # type: ignore[attr-defined]
        return orgs

    monkeypatch.setattr(tasks, "_orgs_with_abandoned", _orgs)
    monkeypatch.setattr(tasks, "_deps_factory", tenant.deps)

    class _Operations:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

        async def abandoned_queued(
            self, now: datetime, *, limit: int
        ) -> tuple[RecoveredQueued, ...]:
            return await tenant.abandoned_queued(now, limit=limit)

        async def settle_recovery(self, outcomes: Any, *, max_attempts: int) -> QueuedRecovery:
            return await tenant.settle_recovery(outcomes, max_attempts=max_attempts)

    monkeypatch.setattr(tasks, "Operations", _Operations)


# ---- which runner each abandoned row is handed to ---------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        pytest.param("bulk", WorkflowType.FILES_BULK, id="bulk"),
        pytest.param("copy", WorkflowType.FILES_COPY, id="copy"),
        pytest.param("move", WorkflowType.FILES_LARGE_MOVE, id="large-move"),
        # An upload whose promote nudge was lost: the session is on the row,
        # so the workflow restarts it from (op_id, org) like any other kind.
        pytest.param("upload", WorkflowType.FILES_PROMOTE, id="upload-promote"),
    ],
)
@pytest.mark.asyncio
async def test_an_abandoned_row_is_handed_to_the_workflow_that_finishes_its_kind(
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected: WorkflowType,
) -> None:
    """The hand-off names a runner, or the row is stranded exactly as before.

    The row's own vocabulary, not a route's: an oversized move is a ``move``
    row finished by ``files.large_move``.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind=kind, attempts=1)])
    _plant(monkeypatch, tenant, [ORG])

    page = await tasks.recover_queued(NOW)

    assert [(h.workflow, h.op_id, h.org_team_id) for h in page.handoffs] == [
        (expected, uuid.UUID(str(op_id)), ORG)
    ]
    assert page.ran == ()


@pytest.mark.asyncio
async def test_the_pass_reads_the_staleness_threshold_the_rest_of_the_system_uses(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One value decides when a queued row counts as abandoned.

    A second threshold here would make a row the home read re-hands and a row
    this pass re-hands two different ages, which is the kind of drift nobody
    finds until an operation is either double-run or stranded.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    tenant = _Tenant([])
    _plant(monkeypatch, tenant, [])

    await tasks.recover_queued(NOW)

    assert tenant.cutoff == NOW - QUEUED_STALE_AFTER  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_row_of_a_kind_nothing_can_restart_is_reported_not_guessed_at(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kind with no runner (an ACL rewrite is driven by its own subtree mark,
    not by a hand-off) is reported, never handed to some other workflow, which
    would run the wrong work against somebody's drive."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind="acl_rewrite", attempts=1)])
    _plant(monkeypatch, tenant, [ORG])

    page = await tasks.recover_queued(NOW)

    assert page.handoffs == ()
    assert page.unrecoverable == (uuid.UUID(str(op_id)),)
    assert "acl_rewrite" not in tasks.RUNNER_FOR_KIND


@pytest.mark.asyncio
async def test_a_lost_promote_is_run_in_process_on_an_inline_deployment(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no orchestrator the pass drives the upload's promote itself, through
    the same core the workflow calls, from the row's id and org alone."""
    monkeypatch.setattr(settings, "files_inline_operations", True)
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind="upload", attempts=1)])
    _plant(monkeypatch, tenant, [ORG])
    promoted: list[tuple[OperationId, uuid.UUID]] = []

    async def _promote(op: OperationId, org_team_id: uuid.UUID) -> str:
        promoted.append((op, org_team_id))
        return "version"

    monkeypatch.setitem(tasks._INLINE_RUNNER, "upload", _promote)

    page = await tasks.recover_queued(NOW)

    assert promoted == [(op_id, ORG)]
    assert page.ran == (uuid.UUID(str(op_id)),)
    assert page.unrecoverable == ()


@pytest.mark.asyncio
async def test_one_tenants_refusal_does_not_cost_the_rest_their_recovery(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick is fleet-wide; an org whose read raises must not strand every
    other org's abandoned operations until somebody notices."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    other = uuid.UUID("44444444-4444-4444-8444-444444444444")
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind="bulk", attempts=1)])
    calls: list[uuid.UUID] = []

    async def _find(now: datetime, *, limit: int) -> tuple[RecoveredQueued, ...]:
        calls.append(ORG if len(calls) == 0 else other)
        if len(calls) == 1:
            raise RuntimeError("this org refuses")
        return (RecoveredQueued(id=op_id, kind="bulk", attempts=0),)

    tenant.abandoned_queued = _find  # type: ignore[method-assign]
    _plant(monkeypatch, tenant, [ORG, other])

    page = await tasks.recover_queued(NOW)

    assert len(calls) == 2
    assert [h.op_id for h in page.handoffs] == [uuid.UUID(str(op_id))]


# ---- the inline deployment shape -------------------------------------------


@pytest.mark.asyncio
async def test_an_inline_deployment_runs_the_operation_instead_of_handing_it_off(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no workflow to nudge where queued work runs in-process, so a
    lost hand-off there would strand the row just as hard. The pass drives the
    very same library core the inline dispatcher calls."""
    monkeypatch.setattr(settings, "files_inline_operations", True)
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind="copy", attempts=2)])
    _plant(monkeypatch, tenant, [ORG])
    ran: list[tuple[OperationId, uuid.UUID]] = []

    async def _copy(op: OperationId, org_team_id: uuid.UUID) -> str:
        ran.append((op, org_team_id))
        return "done"

    monkeypatch.setitem(tasks._INLINE_RUNNER, "copy", _copy)

    page = await tasks.recover_queued(NOW)

    assert ran == [(op_id, ORG)]
    assert page.ran == (uuid.UUID(str(op_id)),)
    assert page.handoffs == (), "an inline deployment has no worker to hand anything to"


@pytest.mark.asyncio
async def test_an_inline_run_that_raises_leaves_the_tick_standing(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure is already on the operation row, and the next tick offers
    the row again — up to its attempts. A raise here would abort the pass and
    take every other abandoned operation down with it."""
    monkeypatch.setattr(settings, "files_inline_operations", True)
    first, second = OperationId(uuid.uuid4()), OperationId(uuid.uuid4())
    tenant = _Tenant(
        [
            RecoveredQueued(id=first, kind="bulk", attempts=1),
            RecoveredQueued(id=second, kind="bulk", attempts=1),
        ]
    )
    _plant(monkeypatch, tenant, [ORG])

    async def _bulk(op: OperationId, org_team_id: uuid.UUID) -> str:
        if op == first:
            raise RuntimeError("the store is down")
        return "done"

    monkeypatch.setitem(tasks._INLINE_RUNNER, "bulk", _bulk)

    page = await tasks.recover_queued(NOW)

    assert page.ran == (uuid.UUID(str(second)),)


@pytest.mark.asyncio
async def test_a_second_tick_finds_nothing_left_to_hand_over(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick takes the rows it finds; the next one looks again.

    What stops a row being offered forever is the attempt count the settlement
    keeps, not the finding — so a second tick over an emptied fleet simply has
    nothing to offer."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind="bulk", attempts=1)])
    _plant(monkeypatch, tenant, [ORG])

    first = await tasks.recover_queued(NOW)
    second = await tasks.recover_queued(NOW + timedelta(seconds=30))

    assert len(first.handoffs) == 1
    assert second.handoffs == ()


@pytest.mark.asyncio
async def test_the_number_of_offers_is_the_deployments_to_set(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How long an abandoned row is offered before it is failed is a setting,
    not a constant buried in a statement — and only a tick that actually
    offered something settles anything at all."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(settings, "files_queued_recovery_attempts", 7)
    tenant = _Tenant([])
    _plant(monkeypatch, tenant, [ORG])

    await tasks.recover_queued(NOW)

    assert tenant.attempts_asked == [], "nothing was offered, so nothing was settled"

    tenant.rows = [RecoveredQueued(id=OperationId(uuid.uuid4()), kind="bulk", attempts=0)]
    monkeypatch.setattr(settings, "files_inline_operations", True)
    monkeypatch.setitem(tasks._INLINE_RUNNER, "bulk", _ran_fine)
    await tasks.recover_queued(NOW)

    assert tenant.attempts_asked == [7]


@pytest.mark.asyncio
async def test_a_failed_row_is_counted_so_an_operator_can_see_the_abandonment(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows that ran out of offers are the signal that hand-offs are being
    lost; a pass that reported only successes would hide it."""
    monkeypatch.setattr(settings, "files_inline_operations", True)
    gone = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=gone, kind="bulk", attempts=3)], failed=(gone,))
    _plant(monkeypatch, tenant, [ORG])
    monkeypatch.setitem(tasks._INLINE_RUNNER, "bulk", _ran_fine)

    page = await tasks.recover_queued(NOW)

    assert page.failed == (uuid.UUID(str(gone)),)
    assert page.ran == (), "a row the settlement abandoned is not then run anyway"


# ---- production wiring ------------------------------------------------------


def test_the_sweep_is_served_on_the_default_queue() -> None:
    """A schedule for a type no worker serves would only pile up runs."""
    assert QUEUE_FOR[WorkflowType.FILES_RECOVER_QUEUED] is TaskQueue.DEFAULT
    served = queues.WORKFLOWS_BY_QUEUE[TaskQueue.DEFAULT]
    assert WorkflowType.FILES_RECOVER_QUEUED.value in {
        workflow._Definition.must_from_class(one).name for one in served
    }
    registered = queues.ACTIVITIES_BY_QUEUE[TaskQueue.DEFAULT]
    assert WorkflowType.FILES_RECOVER_QUEUED.value in {
        activity._Definition.must_from_callable(one).name for one in registered
    }


def test_the_sweep_is_scheduled_tightly_enough_to_be_a_net() -> None:
    """A row has to cross the staleness threshold before the pass takes it, so
    a cadence slower than that threshold would add its own delay to every lost
    hand-off on top of the wait the row already served."""
    entry = next(one for one in schedules.SCHEDULES if one.id == "files-recover-queued")
    assert entry.workflow is WorkflowType.FILES_RECOVER_QUEUED
    assert entry.every is not None
    assert entry.every <= QUEUED_STALE_AFTER
    assert entry.execution_timeout < timedelta(hours=1), (
        "a pass that runs twice a minute cannot hold its id for the default hour"
    )


def test_the_sweep_is_not_created_on_a_deployment_that_does_not_serve_files() -> None:
    """Every workflow is 'registered' everywhere — the registry is a module
    walk — so a Files schedule on a non-Files deployment would fire forever
    against a job that can only refuse."""
    assert WorkflowType.FILES_RECOVER_QUEUED in schedules._FILES_WORKFLOWS


def test_a_pass_is_not_retried_into_spending_an_operations_attempts() -> None:
    """The activity counts an offer against every row it claims, so a retried
    pass would spend two of an operation's attempts on one unlucky minute. The
    thirty-second tick is the recovery."""
    from worker.temporal.retry import ACTIVITY_POLICIES

    policy = ACTIVITY_POLICIES[WorkflowType.FILES_RECOVER_QUEUED.value]
    assert policy.retry.maximum_attempts == 1


# ---- through a real Worker --------------------------------------------------


@pytest.mark.temporal
@pytest.mark.asyncio
async def test_the_workflow_offers_a_runner_and_settles_what_the_server_answered(
    temporal_worker: Any, temporal_client: Any
) -> None:
    """Driven through a real Worker on the local dev server.

    The two halves meet here and nowhere else. The finding activity writes
    nothing, because a row whose runner is merely waiting for a worker slot is
    indistinguishable in the database from one nobody was told about — so the
    evidence has to come from the server: a child start that SUCCEEDS proves
    the runner was absent, and ``WorkflowAlreadyStartedError`` proves one
    exists. This pins that both answers reach the settlement as what they are,
    under the same keyed workflow id the route's own nudge uses.

    The already-running case is made the way it really happens: a runner is
    started for that operation first, under exactly that id.
    """
    absent, present = str(uuid.uuid4()), str(uuid.uuid4())
    org = str(ORG)

    @activity.defn(name=WorkflowType.FILES_RECOVER_QUEUED.value)
    async def _found(input: Any = None) -> activities.RecoveryPage:
        return activities.RecoveryPage(
            handoffs=[
                activities.QueuedHandoff(
                    workflow=WorkflowType.FILES_BULK.value, op_id=absent, org_team_id=org
                ),
                activities.QueuedHandoff(
                    workflow=WorkflowType.FILES_COPY.value, op_id=present, org_team_id=org
                ),
            ]
        )

    settled: list[activities.RecoverySettlement] = []

    @activity.defn(name=RECOVER_SETTLE_ACTIVITY)
    async def _settle(settlement: activities.RecoverySettlement) -> int:
        settled.append(settlement)
        return 0

    # A runner for `present` is already going, exactly as a route's own nudge
    # would have left it: same type, same queue, same keyed id.
    already = await temporal_client.start_workflow(
        WorkflowType.FILES_COPY.value,
        args=[present, org],
        id=keyed_workflow_id(WorkflowType.FILES_COPY, present),
        task_queue=QUEUE_FOR[WorkflowType.FILES_COPY].value,
    )
    try:
        async with temporal_worker(
            workflows=[FilesRecoverQueued], activities=[_found, _settle]
        ) as running:
            handed = await temporal_client.execute_workflow(
                WorkflowType.FILES_RECOVER_QUEUED.value,
                id=f"recover-queued-{uuid.uuid4()}",
                task_queue=running.task_queue,
            )

        assert handed == 1, "only the row whose runner was really gone was handed one"
        assert [entry.outcomes for entry in settled] == [
            {absent: RUNNER_NUDGED, present: RUNNER_PRESENT}
        ]
        assert [entry.org_team_id for entry in settled] == [org]

        # The runner it did start is on the queue that serves it, under the id
        # a late nudge would attach to. No worker here serves that queue on
        # purpose: a stub registered under the real Files workflow names would
        # take tasks belonging to everything else on the dev server.
        handle = temporal_client.get_workflow_handle(
            keyed_workflow_id(WorkflowType.FILES_BULK, absent)
        )
        described = await handle.describe()
        assert described.status is WorkflowExecutionStatus.RUNNING
        assert described.workflow_type == WorkflowType.FILES_BULK.value
        assert described.task_queue == QUEUE_FOR[WorkflowType.FILES_BULK].value
        await handle.terminate("the case has read what it needed")
    finally:
        await already.terminate("the case has read what it needed")


@pytest.mark.temporal
async def test_every_child_the_recovery_starts_binds_to_its_workflow_by_name(
    temporal_worker: Any, temporal_client: Any
) -> None:
    """The recovery pass starts each runner with positional args across a
    process boundary -- the same shape that stranded every upload when a
    starter and a workflow disagreed. So each child it really starts on the dev
    server is read back from its own history and its input bound to the
    child's declared ``run`` by name and position: a swapped pair fails here.
    One child per recoverable kind, the promote included."""
    import inspect

    from alkera_core.temporal import FilesOperationInput

    org = str(ORG)
    by_kind = {kind: str(uuid.uuid4()) for kind in tasks.RUNNER_FOR_KIND}

    @activity.defn(name=WorkflowType.FILES_RECOVER_QUEUED.value)
    async def _found(input: Any = None) -> activities.RecoveryPage:
        return activities.RecoveryPage(
            handoffs=[
                activities.QueuedHandoff(
                    workflow=tasks.RUNNER_FOR_KIND[kind].value, op_id=op_id, org_team_id=org
                )
                for kind, op_id in by_kind.items()
            ]
        )

    @activity.defn(name=RECOVER_SETTLE_ACTIVITY)
    async def _settle(settlement: activities.RecoverySettlement) -> int:
        return 0

    handles = []
    try:
        async with temporal_worker(
            workflows=[FilesRecoverQueued], activities=[_found, _settle]
        ) as running:
            started = await temporal_client.execute_workflow(
                WorkflowType.FILES_RECOVER_QUEUED.value,
                id=f"recover-queued-bind-{uuid.uuid4()}",
                task_queue=running.task_queue,
            )
        assert started == len(by_kind)

        for kind, op_id in by_kind.items():
            runner = tasks.RUNNER_FOR_KIND[kind]
            handle = temporal_client.get_workflow_handle(keyed_workflow_id(runner, op_id))
            handles.append(handle)
            history = await handle.fetch_history()
            first = history.events[0].workflow_execution_started_event_attributes
            sent = await temporal_client.data_converter.decode(first.input.payloads)
            (cls,) = [
                c for c in queues.ALL_WORKFLOWS if queues.workflow_type_name(c) == runner.value
            ]
            bound = inspect.signature(cls.run).bind(object(), *sent)
            assert list(bound.arguments)[1:] == ["op_id", "org_team_id"], kind
            assert bound.arguments["op_id"] == op_id, kind
            assert bound.arguments["org_team_id"] == org, kind
            assert sent == FilesOperationInput(op_id=op_id, org_team_id=org).args(), kind
    finally:
        for handle in handles:
            await handle.terminate("the case has read what it needed")


@pytest.mark.asyncio
async def test_the_budget_is_one_bound_on_the_tick_not_a_bound_per_tenant(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-org cap under an org cap is a tick whose real size is the product
    of two numbers nobody reads together: 200 orgs of 200 rows is 40,000 child
    workflows from one line of settings that says 200.
    """
    monkeypatch.setattr(settings, "files_inline_operations", False)
    monkeypatch.setattr(settings, "files_queued_recovery_budget", 3)
    tenant = _Tenant(
        [RecoveredQueued(id=OperationId(uuid.uuid4()), kind="bulk", attempts=0) for _ in range(9)]
    )
    _plant(monkeypatch, tenant, [ORG, uuid.UUID(int=8), uuid.UUID(int=9)])

    page = await tasks.recover_queued(NOW)

    assert len(page.handoffs) == 3, "the whole tick is bounded, not each tenant's share"
    assert tenant.limits_asked[0] == 3
    assert sum(tenant.limits_asked) <= 3 + 3, "a later tenant is asked only for what is left"


@pytest.mark.asyncio
async def test_a_runner_that_already_exists_is_settled_as_no_attempt(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settlement is where the orchestrator's answer reaches the row, and
    the two answers must not arrive as the same thing: one is an abandonment
    counted against the operation, the other is proof it was told after all."""
    monkeypatch.setattr(settings, "files_inline_operations", False)
    op_id = str(uuid.uuid4())
    tenant = _Tenant([])
    _plant(monkeypatch, tenant, [])

    await tasks.settle_recovery({op_id: RUNNER_PRESENT}, ORG, 3)

    assert tenant.settled == [{OperationId(uuid.UUID(op_id)): RUNNER_PRESENT}]


@pytest.mark.asyncio
async def test_an_inline_deployment_counts_its_dispatch_before_it_runs(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatching IS the hand-off where there is no orchestrator to ask, so
    the attempt is counted first: a core that dies taking the process with it
    must not leave the row looking as though it was never offered."""
    monkeypatch.setattr(settings, "files_inline_operations", True)
    op_id = OperationId(uuid.uuid4())
    tenant = _Tenant([RecoveredQueued(id=op_id, kind="bulk", attempts=0)])
    _plant(monkeypatch, tenant, [ORG])
    order: list[str] = []

    async def _slow(op: OperationId, org_team_id: uuid.UUID) -> str:
        order.append("ran")
        return "done"

    original = tenant.settle_recovery

    async def _recording(outcomes: Any, *, max_attempts: int) -> QueuedRecovery:
        order.append("settled")
        return await original(outcomes, max_attempts=max_attempts)

    tenant.settle_recovery = _recording  # type: ignore[method-assign]
    monkeypatch.setitem(tasks._INLINE_RUNNER, "bulk", _slow)

    await tasks.recover_queued(NOW)

    assert order == ["settled", "ran"]
    assert tenant.settled == [{op_id: RUNNER_NUDGED}]


def test_both_deployment_shapes_can_run_every_recoverable_kind() -> None:
    """A kind one shape can restart and the other cannot is a row the pass
    picks up on every tick and can never finish."""
    assert tasks.RUNNER_FOR_KIND.keys() == tasks._INLINE_RUNNER.keys()


def test_the_settlement_has_a_policy_of_its_own_and_is_retried() -> None:
    """The pass is not retried — a retried pass would offer every row twice —
    but the settlement is the opposite: its writes are what keep a row from
    being offered forever, and losing them loses the count."""
    from worker.temporal.retry import ACTIVITY_POLICIES

    assert ACTIVITY_POLICIES[RECOVER_SETTLE_ACTIVITY].retry.maximum_attempts > 1


def test_the_settlement_is_served_on_the_same_queue_as_the_pass() -> None:
    """The workflow schedules it directly, so a queue that does not register it
    is a tick that finds rows, offers them and never records the offer."""
    registered = {
        activity._Definition.must_from_callable(one).name
        for one in queues.ACTIVITIES_BY_QUEUE[TaskQueue.DEFAULT]
    }
    assert RECOVER_SETTLE_ACTIVITY in registered
