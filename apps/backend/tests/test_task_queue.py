"""The backend→worker nudges: best-effort starts through Temporal, with the
shapes and names the worker serves.

The backend starts workflows by NAME across a process boundary, so no import
ties the two sides together and a one-sided rename would drop every nudge with
both suites green. This suite pins the whole contract once: each nudge's
workflow type, queue and id shape (a drain is a signal-with-start on the
type-name id; per-entity work attaches to the run in flight), the budget and
the never-raise rule, and — across the package boundary — that every nudged
type is a workflow the worker actually registers on that queue.

The last section is the other half of "best-effort": what happens to a nudge
the orchestrator did NOT take inside that budget. A loaded machine made every
nudge time out while the orchestrator and the worker were both healthy, and a
probe request — which no schedule re-runs — was then stranded pending while its
dialog blamed the worker. A nudge nobody took is now recovered from the
verification record itself, which any replica can read — see the
product's connections job that recovers verifications.
and records how the hand-off went, so the budget still bounds the request and
the delay stops being a loss.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.temporal import (
    MORE_WORK_SIGNAL,
    QUEUE_FOR,
    FilesOperationInput,
    WorkflowType,
    drain_workflow_id,
    keyed_workflow_id,
)
from backend.services.files import operations_runner
from backend.services.infra import task_queue
from temporalio.common import WorkflowIDConflictPolicy

_RUN = UUID(int=7)
_ORG = UUID(int=9)


@dataclass(frozen=True)
class _Nudge:
    call: Callable[[], Awaitable[bool]]
    workflow: WorkflowType
    key: str | None
    fail_log_event: str
    #: The workflow's positional arguments when they are more than the key
    #: alone; ``None`` means the key is the one argument.
    args: tuple[str, ...] | None = None

    @property
    def signal(self) -> bool:
        """A drain (no key) is a signal-with-start; keyed work attaches."""
        return self.key is None

    @property
    def expected_args(self) -> list[str]:
        assert self.key is not None
        return [self.key] if self.args is None else list(self.args)

    @property
    def workflow_id(self) -> str:
        if self.key is None:
            return drain_workflow_id(self.workflow)
        return keyed_workflow_id(self.workflow, self.key)


NUDGES = [
    pytest.param(
        _Nudge(
            task_queue.nudge_org_machine_reconcile,
            WorkflowType.ORG_MACHINE_RECONCILE,
            None,
            "compute.org_machine.nudge_failed",
        ),
        id="org-machine-reconcile",
    ),
    pytest.param(
        _Nudge(
            lambda: task_queue.start_workspace_machine_move(_RUN, [str(_RUN), str(_ORG), ""]),
            WorkflowType.WORKSPACE_MACHINE_MOVE,
            str(_RUN),
            "workspace.machine_move.start_failed",
            args=(str(_RUN), str(_ORG), ""),
        ),
        id="workspace-machine-move",
    ),
    *[
        pytest.param(
            _Nudge(
                (lambda w=workflow: task_queue.nudge_files_operation(w, _RUN, _ORG)),
                workflow,
                str(_RUN),
                f"{workflow.value}.nudge_failed",
                args=(str(_RUN), str(_ORG)),
            ),
            id=workflow.value.replace(".", "-").replace("_", "-"),
        )
        # Every kind a Files route can queue: the hand-off is the only thing
        # that starts one, so a kind missing here is a 202 nobody ever runs.
        for workflow in sorted(operations_runner.WORKER_WORKFLOW.values(), key=lambda w: w.value)
    ],
]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Every test that takes a ``nudge`` runs over the ``NUDGES`` of the module
    it is collected in, so a family's own module runs the same contract over
    its nudges."""
    if "nudge" in metafunc.fixturenames:
        metafunc.parametrize("nudge", metafunc.module.NUDGES)


PUBLIC_NUDGES = (
    task_queue.nudge_files_operation,
    task_queue.nudge_org_machine_reconcile,
    task_queue.start_workspace_machine_move,
)


class _SpyClient:
    """Stands in for the Temporal client: records starts, or fails, or hangs."""

    def __init__(self, *, fail: Exception | None = None, hang: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail
        self._hang = hang

    async def start_workflow(self, workflow: str, **kwargs: Any) -> object:
        self.calls.append((workflow, kwargs))
        if self._fail is not None:
            raise self._fail
        if self._hang:
            await asyncio.sleep(60)
        return object()


class _LogRecorder:
    """Stands in for a module logger: the app configures structlog with
    ``cache_logger_on_first_use``, which makes ``capture_logs`` order-dependent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.calls.append((event, kw))


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    """One recorder behind BOTH loggers a nudge can write to, so "logged once"
    is asserted across the nudge and the shared start primitive."""
    from alkera_core.temporal import client as client_mod

    rec = _LogRecorder()
    monkeypatch.setattr(task_queue, "log", rec)
    monkeypatch.setattr(client_mod, "log", rec)
    return rec


def _provide(
    client: object | None = None, *, fail: Exception | None = None, hang: bool = False
) -> Any:
    async def provider() -> object:
        if fail is not None:
            raise fail
        if hang:
            await asyncio.sleep(60)
        return client

    return provider


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _SpyClient:
    client = _SpyClient()
    monkeypatch.setattr(task_queue, "temporal_client_provider", _provide(client))
    return client


# --- the contract ---------------------------------------------------------------------


def test_every_nudge_is_a_coroutine_function() -> None:
    """The routes await them (and BackgroundTasks awaits coroutine functions
    in-loop); a sync nudge would silently block the event loop on a connect."""
    assert all(inspect.iscoroutinefunction(n) for n in PUBLIC_NUDGES)
    assert len(PUBLIC_NUDGES) == 3


@pytest.mark.real_temporal_client
def test_the_default_provider_is_the_shared_client_with_the_backend_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production default itself is what is asserted, so the root conftest's
    guard (which substitutes a recording provider) has to stand aside."""
    seen: list[dict[str, Any]] = []

    async def fake_shared_client(**kwargs: Any) -> str:
        seen.append(kwargs)
        return "the-client"

    monkeypatch.setattr(task_queue, "shared_client", fake_shared_client)
    assert task_queue.temporal_client_provider is task_queue.backend_client
    assert asyncio.run(task_queue.backend_client()) == "the-client"
    assert seen == [{"component": "backend"}]
    assert task_queue.NUDGE_TIMEOUT_S == 2.0


async def test_each_nudge_starts_its_workflow_on_its_queue_with_its_id(
    nudge: _Nudge, spy: _SpyClient, logs: _LogRecorder
) -> None:
    assert await nudge.call() is True
    [(workflow, kwargs)] = spy.calls
    assert workflow == nudge.workflow.value
    assert kwargs["id"] == nudge.workflow_id
    assert kwargs["task_queue"] == QUEUE_FOR[nudge.workflow].value
    assert kwargs["rpc_timeout"] == timedelta(seconds=2)
    if nudge.signal:
        assert kwargs["start_signal"] == MORE_WORK_SIGNAL
        assert "id_conflict_policy" not in kwargs, "a drain is never started with a conflict policy"
        assert "args" not in kwargs, (
            "a drain runs on its defaults: the wall clock, the default page"
        )
    else:
        assert kwargs["id_conflict_policy"] is WorkflowIDConflictPolicy.USE_EXISTING
        assert "start_signal" not in kwargs, "per-entity work carries no signal"
        assert kwargs["args"] == nudge.expected_args, (
            "per-entity work takes its entity id — and, for a Files operation, its org"
        )
    assert logs.calls == []


async def test_a_provider_that_cannot_connect_is_swallowed_and_logged_once(
    nudge: _Nudge, monkeypatch: pytest.MonkeyPatch, logs: _LogRecorder
) -> None:
    monkeypatch.setattr(
        task_queue, "temporal_client_provider", _provide(fail=ConnectionError("frontend down"))
    )
    assert await nudge.call() is False
    [(event, fields)] = logs.calls
    assert event == nudge.fail_log_event
    assert fields["error"] == "frontend down"
    assert fields["workflow"] == nudge.workflow.value
    assert fields["task_queue"] == QUEUE_FOR[nudge.workflow].value


async def test_a_refused_start_is_swallowed_and_logged_once(
    nudge: _Nudge, monkeypatch: pytest.MonkeyPatch, logs: _LogRecorder
) -> None:
    client = _SpyClient(fail=RuntimeError("namespace not found"))
    monkeypatch.setattr(task_queue, "temporal_client_provider", _provide(client))
    assert await nudge.call() is False
    assert len(client.calls) == 1
    [(event, fields)] = logs.calls
    assert event == nudge.fail_log_event
    assert fields["error"] == "namespace not found"
    assert fields["workflow_id"] == nudge.workflow_id


@pytest.mark.parametrize("where", ["connect", "start"])
async def test_a_hanging_orchestrator_returns_within_the_budget(
    where: str, monkeypatch: pytest.MonkeyPatch, logs: _LogRecorder
) -> None:
    """Whether the connect or the start hangs, the whole nudge is bounded by one
    budget — never the sum of two."""
    monkeypatch.setattr(task_queue, "NUDGE_TIMEOUT_S", 0.2)
    if where == "connect":
        monkeypatch.setattr(task_queue, "temporal_client_provider", _provide(hang=True))
    else:
        monkeypatch.setattr(task_queue, "temporal_client_provider", _provide(_SpyClient(hang=True)))
    started = time.monotonic()
    assert await task_queue.nudge_workspace_deletions() is False
    assert time.monotonic() - started < 1.0
    # Exactly one line, whichever of the nudge's own deadline or the start
    # primitive's fired first (a hanging start arms both with the same budget).
    [(event, fields)] = logs.calls
    assert event == "workspace.deletion.nudge_failed"
    if where == "connect":
        assert fields["error"] == "no answer from the orchestrator within 0.2s"


async def test_a_key_that_cannot_name_a_workflow_is_logged_not_raised(
    spy: _SpyClient, logs: _LogRecorder
) -> None:
    """A per-entity nudge with an empty or unsafe key would alias entities onto
    one id; it is refused inside the never-raise boundary."""
    for key in ("", "a b"):
        took = await task_queue.nudge(
            WorkflowType.FILES_PROMOTE, key=key, signal=None, fail_log_event="files.nudge_failed"
        )
        assert took is False
    assert spy.calls == []
    assert [event for event, _ in logs.calls] == ["files.nudge_failed", "files.nudge_failed"]
    # And nothing is retried: an id that cannot be built now cannot be built in
    # ten seconds either, so a retry would only repeat the same refusal.


async def test_a_cancelled_caller_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only failures are best-effort: a request that is cancelled mid-nudge
    still cancels."""
    monkeypatch.setattr(task_queue, "temporal_client_provider", _provide(hang=True))
    task = asyncio.ensure_future(task_queue.nudge_workspace_deletions())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- the cross-package pin: every nudged type is served by the worker -----------------


def test_the_nudged_workflow_type_is_registered_on_the_queue_the_nudge_targets(
    nudge: _Nudge,
) -> None:
    """The nudge and the worker agree by name only. Pin each nudged type to a
    workflow class the worker registers on exactly the queue the nudge uses."""
    from worker.temporal import queues

    queue = QUEUE_FOR[nudge.workflow]
    served = {queues.workflow_type_name(c) for c in queues.WORKFLOWS_BY_QUEUE[queue]}
    assert nudge.workflow.value in served
    assert nudge.workflow.value in queues.served_workflow_types()
    for other in set(queues.WORKFLOWS_BY_QUEUE) - {queue}:
        others = {queues.workflow_type_name(c) for c in queues.WORKFLOWS_BY_QUEUE[other]}
        assert nudge.workflow.value not in others


# --- the cross-package pin: a Files hand-off binds to the workflow it starts -----------


@pytest.mark.parametrize("kind", sorted(operations_runner.WORKER_WORKFLOW))
async def test_a_files_hand_off_binds_by_name_to_the_workflow_it_starts(
    kind: str, spy: _SpyClient, logs: _LogRecorder
) -> None:
    """The nudge sends positional args across a process boundary, so a wrong
    arity or order is invisible to both suites until the worker's activation
    raises -- which is how every upload's promote sat ``queued`` for good. Bind
    what was sent to the worker's own ``run`` by name: every kind takes exactly
    ``(op_id, org_team_id)``."""
    from worker.temporal import queues

    workflow = operations_runner.WORKER_WORKFLOW[kind]
    assert await task_queue.nudge_files_operation(workflow, _RUN, _ORG)
    [(_, kwargs)] = spy.calls
    (cls,) = [c for c in queues.ALL_WORKFLOWS if queues.workflow_type_name(c) == workflow.value]
    bound = inspect.signature(cls.run).bind(object(), *kwargs["args"])
    assert dict(bound.arguments) == {
        "self": bound.arguments["self"],
        "op_id": str(_RUN),
        "org_team_id": str(_ORG),
    }
    assert (
        list(kwargs["args"]) == FilesOperationInput(op_id=str(_RUN), org_team_id=str(_ORG)).args()
    )


def test_the_shared_files_input_is_every_files_workflow_s_declared_input() -> None:
    """One typed input, built by every starter; its field order must be the
    parameter order of every queued Files workflow's ``run``, or the starters
    and the workflows disagree however carefully each is written."""
    import dataclasses

    from worker.tasks import files as worker_files
    from worker.temporal import queues

    fields = [f.name for f in dataclasses.fields(FilesOperationInput)]
    runners = set(operations_runner.WORKER_WORKFLOW.values()) | set(
        worker_files.RUNNER_FOR_KIND.values()
    )
    for workflow in sorted(runners, key=lambda w: w.value):
        (cls,) = [c for c in queues.ALL_WORKFLOWS if queues.workflow_type_name(c) == workflow.value]
        params = list(inspect.signature(cls.run).parameters)[1:]
        assert params == fields, workflow.value
    probe = FilesOperationInput(op_id="op", org_team_id="org")
    assert probe.args() == [getattr(probe, name) for name in fields]
