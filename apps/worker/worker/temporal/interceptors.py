"""The activity interceptor every worker runs, and the recorder seam behind it.

Three jobs, all on the activity's inbound path:

1. **Failure classification.** A transient failure (a dropped DB connection, a
   Stripe rate limit, a registered API client's 5xx — ``is_transient_error``) is
   re-raised as is, so Temporal retries it per the activity's policy. Anything else is a
   deterministic failure: it is reported to Sentry and re-raised as a
   non-retryable ``ApplicationError`` carrying the original class name, so the
   retry budget is never burned on a poison call and the workflow fails with a
   readable cause. An ``ApplicationError`` an activity raises deliberately (a
   probe's ``RowNotYetVisible``) passes through untouched.
2. **Log context.** Every log line an activity emits carries the workflow and
   activity identity, bound through structlog's context vars.
3. **The recorder seam.** ``ActivityRecorder`` sees every attempt start and
   finish, with the ``ToolCallActivityInput`` when an activity carries one.
   Nothing adopts it yet; ``NoopRecorder`` is the default. It is the interface
   a future replay / audit store plugs into without touching the activities.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal, Protocol

import structlog
from alkera_core.logging import get_logger
from alkera_core.observability.sentry import capture_exception
from alkera_core.schemas.temporal import ToolCallActivityInput
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from worker.tasks._hardening import is_transient_error

__all__ = [
    "ActivityOutcome",
    "ActivityRecorder",
    "AlkeraWorkerInterceptor",
    "NoopRecorder",
    "ToolCallActivityInput",
    "safe_heartbeat",
]

log = get_logger(__name__)

ActivityOutcome = Literal["ok", "transient_failure", "permanent_failure", "cancelled"]


class ActivityRecorder(Protocol):
    """Observes every activity attempt. Implementations must not raise: a
    recorder failure must never fail the work it observes."""

    def record_start(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        tool_call: ToolCallActivityInput | None,
    ) -> None: ...

    def record_result(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        outcome: ActivityOutcome,
        duration_ms: int,
    ) -> None: ...


class NoopRecorder:
    """The default: records nothing."""

    def record_start(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        tool_call: ToolCallActivityInput | None,
    ) -> None:
        return None

    def record_result(
        self,
        *,
        activity_type: str,
        workflow_id: str,
        attempt: int,
        outcome: ActivityOutcome,
        duration_ms: int,
    ) -> None:
        return None


class AlkeraWorkerInterceptor(Interceptor):
    """Install on every ``Worker`` (the runner and the test harness both do)."""

    def __init__(self, recorder: ActivityRecorder | None = None) -> None:
        self._recorder: ActivityRecorder = recorder if recorder is not None else NoopRecorder()

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _ActivityInbound(next, self._recorder)


class _ActivityInbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, recorder: ActivityRecorder) -> None:
        super().__init__(next)
        self._recorder = recorder

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        # The SDK types the id as optional (a local activity's info may lack one);
        # every activity here is a remote one started by a workflow.
        workflow_id = info.workflow_id or ""
        tool_call = next((a for a in input.args if isinstance(a, ToolCallActivityInput)), None)
        started = time.monotonic()

        def finish(outcome: ActivityOutcome) -> None:
            self._recorder.record_result(
                activity_type=info.activity_type,
                workflow_id=workflow_id,
                attempt=info.attempt,
                outcome=outcome,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        self._recorder.record_start(
            activity_type=info.activity_type,
            workflow_id=workflow_id,
            attempt=info.attempt,
            tool_call=tool_call,
        )
        with structlog.contextvars.bound_contextvars(
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            workflow_type=info.workflow_type,
            activity_type=info.activity_type,
            activity_id=info.activity_id,
            attempt=info.attempt,
            task_queue=info.task_queue,
        ):
            try:
                result = await self.next.execute_activity(input)
            except asyncio.CancelledError:
                finish("cancelled")
                raise
            except ApplicationError as exc:
                # The activity classified itself; honour it.
                finish("permanent_failure" if exc.non_retryable else "transient_failure")
                raise
            except Exception as exc:
                if is_transient_error(exc):
                    log.warning(
                        "activity.transient_failure",
                        error=str(exc) or type(exc).__name__,
                        error_type=type(exc).__name__,
                    )
                    finish("transient_failure")
                    raise
                log.error(
                    "activity.permanent_failure",
                    error=str(exc) or type(exc).__name__,
                    error_type=type(exc).__name__,
                )
                capture_exception(exc, activity_type=info.activity_type, workflow_id=workflow_id)
                finish("permanent_failure")
                raise ApplicationError(
                    str(exc) or type(exc).__name__,
                    type=type(exc).__name__,
                    non_retryable=True,
                ) from exc
            finish("ok")
            return result


def safe_heartbeat(*details: Any) -> None:
    """Heartbeat when running inside an activity; a no-op when the same core is
    driven directly by a test or a CLI, where there is no activity context."""
    if activity.in_activity():
        activity.heartbeat(*details)
