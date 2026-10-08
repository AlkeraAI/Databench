"""The worker's activity interceptor, proven through a real Worker on the dev server.

A transient failure must reach Temporal's retry machinery untouched; a
deterministic one must become a non-retryable failure (one attempt, reported
to Sentry) instead of burning the retry budget; an activity's own
``ApplicationError`` passes through; every attempt is visible to the recorder
seam with the right outcome; and every activity log line carries the workflow
identity. The activities and driver workflow here are stubs; the interceptor is
the production one, installed exactly as the runner installs it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
import structlog
from alkera_core.schemas.temporal import ToolCallActivityInput
from sqlalchemy.exc import OperationalError
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ActivityCancellationType
from worker.temporal import interceptors as interceptors_mod
from worker.temporal.interceptors import (
    ActivityOutcome,
    AlkeraWorkerInterceptor,
    NoopRecorder,
    safe_heartbeat,
)

pytestmark = pytest.mark.temporal

# Per-test scripts, keyed by a test-chosen key. Activities run in the test
# process (outside the workflow sandbox), so they see these real dicts.
_ATTEMPTS: dict[str, int] = {}


@activity.defn(name="stub.flaky")
async def flaky(key: str, fail_times: int, transient: bool) -> dict[str, Any]:
    n = _ATTEMPTS.get(key, 0) + 1
    _ATTEMPTS[key] = n
    if n <= fail_times:
        if transient:
            raise OperationalError("SELECT 1", {}, Exception("connection dropped"))
        raise ValueError(f"poison {key}")
    context = structlog.contextvars.get_contextvars()
    return {"attempt": activity.info().attempt, "context": dict(context)}


@activity.defn(name="stub.self_classified")
async def self_classified(key: str) -> None:
    _ATTEMPTS[key] = _ATTEMPTS.get(key, 0) + 1
    raise ApplicationError("row not yet visible", type="RowNotYetVisible", non_retryable=True)


@activity.defn(name="stub.tool_call")
async def tool_call(call: ToolCallActivityInput) -> str:
    return call.call_id


@activity.defn(name="stub.cancellable")
async def cancellable() -> None:
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.02)


# Unsandboxed: a test module (pytest, the app) is not sandbox-importable; the
# production workflow modules pass their imports through and are validated under
# the default sandbox by the runner's build_worker.
@workflow.defn(name="stub.interceptor_driver", sandboxed=False)
class Driver:
    @workflow.run
    async def run(self, kind: str, key: str, fail_times: int, transient: bool) -> Any:
        fast = RetryPolicy(
            initial_interval=timedelta(milliseconds=10),
            backoff_coefficient=1.0,
            maximum_attempts=3,
        )
        if kind == "flaky":
            return await workflow.execute_activity(
                flaky,
                args=[key, fail_times, transient],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=fast,
            )
        if kind == "self_classified":
            return await workflow.execute_activity(
                self_classified,
                key,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=fast,
            )
        if kind == "tool_call":
            return await workflow.execute_activity(
                tool_call,
                ToolCallActivityInput(
                    tool_name="read",
                    session_id="ses",
                    call_id=key,
                    org_id="org",
                    input_ref="blob:1",
                    idempotency_key=f"ses:{key}",
                ),
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=fast,
            )
        if kind == "cancel":
            handle = workflow.start_activity(
                cancellable,
                start_to_close_timeout=timedelta(seconds=10),
                heartbeat_timeout=timedelta(seconds=2),
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=fast,
            )
            await workflow.sleep(0.2)
            handle.cancel()
            try:
                await handle
            except ActivityError:
                return "cancelled"
            return "not cancelled"
        raise ValueError(kind)


@dataclass
class RecordingRecorder:
    starts: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    def record_start(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        tool_call: ToolCallActivityInput | None,
    ) -> None:
        self.starts.append(
            {
                "activity_type": activity_type,
                "workflow_id": workflow_id,
                "attempt": attempt,
                "tool_call": tool_call,
            }
        )

    def record_result(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        outcome: ActivityOutcome,
        duration_ms: int,
    ) -> None:
        self.results.append(
            {
                "activity_type": activity_type,
                "workflow_id": workflow_id,
                "attempt": attempt,
                "outcome": outcome,
                "duration_ms": duration_ms,
            }
        )


@pytest.fixture
def sentry_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_capture(error: BaseException | None = None, **tags: Any) -> None:
        calls.append({"error": error, **tags})

    monkeypatch.setattr(interceptors_mod, "capture_exception", fake_capture)
    return calls


def _key(request: pytest.FixtureRequest) -> str:
    key = f"{request.node.name}-{id(request)}"
    _ATTEMPTS.pop(key, None)
    return key


async def _run(
    client: Client,
    task_queue: str,
    kind: str,
    key: str,
    *,
    fail_times: int = 0,
    transient: bool = False,
) -> Any:
    return await client.execute_workflow(
        Driver.run,
        args=[kind, key, fail_times, transient],
        id=f"wf-{key}",
        task_queue=task_queue,
        execution_timeout=timedelta(seconds=30),
    )


async def test_a_transient_failure_is_retried_and_the_next_attempt_succeeds(
    temporal_worker: Any,
    temporal_client: Client,
    request: pytest.FixtureRequest,
    sentry_calls: list,
) -> None:
    key = _key(request)
    rec = RecordingRecorder()
    async with temporal_worker(workflows=[Driver], activities=[flaky], recorder=rec) as running:
        result = await _run(
            temporal_client, running.task_queue, "flaky", key, fail_times=1, transient=True
        )
    assert result["attempt"] == 2
    assert _ATTEMPTS[key] == 2
    assert [r["outcome"] for r in rec.results] == ["transient_failure", "ok"]
    assert [s["attempt"] for s in rec.starts] == [1, 2]
    assert sentry_calls == []


async def test_a_permanent_failure_runs_once_and_fails_the_workflow_non_retryably(
    temporal_worker: Any,
    temporal_client: Client,
    request: pytest.FixtureRequest,
    sentry_calls: list,
) -> None:
    key = _key(request)
    rec = RecordingRecorder()
    async with temporal_worker(workflows=[Driver], activities=[flaky], recorder=rec) as running:
        with pytest.raises(WorkflowFailureError) as excinfo:
            await _run(
                temporal_client, running.task_queue, "flaky", key, fail_times=5, transient=False
            )
    activity_error = excinfo.value.cause
    assert isinstance(activity_error, ActivityError)
    app_error = activity_error.cause
    assert isinstance(app_error, ApplicationError)
    assert app_error.non_retryable is True
    assert app_error.type == "ValueError"
    assert f"poison {key}" in str(app_error)
    # The retry policy allowed three attempts; the classification stopped it at one.
    assert _ATTEMPTS[key] == 1
    assert [r["outcome"] for r in rec.results] == ["permanent_failure"]
    assert len(sentry_calls) == 1
    assert isinstance(sentry_calls[0]["error"], ValueError)
    assert sentry_calls[0]["activity_type"] == "stub.flaky"
    assert sentry_calls[0]["workflow_id"] == f"wf-{key}"


async def test_an_activitys_own_application_error_passes_through_untouched(
    temporal_worker: Any,
    temporal_client: Client,
    request: pytest.FixtureRequest,
    sentry_calls: list,
) -> None:
    key = _key(request)
    rec = RecordingRecorder()
    async with temporal_worker(
        workflows=[Driver], activities=[self_classified], recorder=rec
    ) as running:
        with pytest.raises(WorkflowFailureError) as excinfo:
            await _run(temporal_client, running.task_queue, "self_classified", key)
    app_error = excinfo.value.cause.cause  # type: ignore[union-attr]
    assert isinstance(app_error, ApplicationError)
    assert app_error.type == "RowNotYetVisible"
    assert app_error.non_retryable is True
    assert _ATTEMPTS[key] == 1
    assert [r["outcome"] for r in rec.results] == ["permanent_failure"]
    assert sentry_calls == [], "a self-classified failure is not a crash to report"


async def test_activity_log_context_names_the_workflow_and_attempt(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    key = _key(request)
    async with temporal_worker(workflows=[Driver], activities=[flaky]) as running:
        result = await _run(temporal_client, running.task_queue, "flaky", key)
    context = result["context"]
    assert context["workflow_id"] == f"wf-{key}"
    assert context["workflow_type"] == "stub.interceptor_driver"
    assert context["activity_type"] == "stub.flaky"
    assert context["attempt"] == 1
    assert context["task_queue"] == running.task_queue
    assert context["workflow_run_id"] and context["activity_id"]


async def test_the_recorder_sees_the_tool_call_input(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    key = _key(request)
    rec = RecordingRecorder()
    async with temporal_worker(workflows=[Driver], activities=[tool_call], recorder=rec) as running:
        assert await _run(temporal_client, running.task_queue, "tool_call", key) == key
    [start] = rec.starts
    assert isinstance(start["tool_call"], ToolCallActivityInput)
    assert start["tool_call"].call_id == key
    assert start["tool_call"].idempotency_key == f"ses:{key}"
    [result] = rec.results
    assert result["outcome"] == "ok"
    assert result["duration_ms"] >= 0


async def test_a_cancelled_attempt_is_recorded_as_cancelled(
    temporal_worker: Any,
    temporal_client: Client,
    request: pytest.FixtureRequest,
    sentry_calls: list,
) -> None:
    key = _key(request)
    rec = RecordingRecorder()
    async with temporal_worker(
        workflows=[Driver], activities=[cancellable], recorder=rec
    ) as running:
        assert await _run(temporal_client, running.task_queue, "cancel", key) == "cancelled"
    assert [r["outcome"] for r in rec.results] == ["cancelled"]
    assert sentry_calls == []


async def test_a_flaky_activity_without_a_tool_call_records_none(
    temporal_worker: Any, temporal_client: Client, request: pytest.FixtureRequest
) -> None:
    key = _key(request)
    rec = RecordingRecorder()
    async with temporal_worker(workflows=[Driver], activities=[flaky], recorder=rec) as running:
        await _run(temporal_client, running.task_queue, "flaky", key)
    assert rec.starts[0]["tool_call"] is None


def test_noop_recorder_is_the_default_and_accepts_every_call() -> None:
    interceptor = AlkeraWorkerInterceptor()
    assert isinstance(interceptor._recorder, NoopRecorder)
    NoopRecorder().record_start(activity_type="a", workflow_id="w", attempt=1, tool_call=None)
    NoopRecorder().record_result(
        activity_type="a", workflow_id="w", attempt=1, outcome="ok", duration_ms=0
    )


def test_safe_heartbeat_outside_an_activity_is_a_no_op() -> None:
    assert not activity.in_activity()
    safe_heartbeat({"progress": 1})  # no activity context: must not raise


async def test_tool_call_input_round_trips_through_the_worker_converter() -> None:
    call = ToolCallActivityInput(
        tool_name="read",
        session_id="ses",
        call_id="c1",
        org_id="org",
        input_ref="blob:1",
        idempotency_key="ses:c1",
    )
    payloads = await pydantic_data_converter.encode([call])
    [decoded] = await pydantic_data_converter.decode(payloads, [ToolCallActivityInput])
    assert decoded == call
