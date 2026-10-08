"""The notebook run sweep workflow.

One thin workflow that executes the identically named activity with the
policy the retry table declares for it: transient retry, since the sweep is
idempotent (each batch ends only rows still open, and commits on its own).
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from alkera_core.temporal import WorkflowType

    from worker.activities.notebooks import sweep_runs
    from worker.temporal.retry import policy_for

_POLICY = policy_for(WorkflowType.SWEEP_NOTEBOOK_RUNS.value)


@workflow.defn(name=WorkflowType.SWEEP_NOTEBOOK_RUNS.value)
class SweepNotebookRuns:
    """Every minute: the notebook run sweep. Returns how many runs ended."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity(
            sweep_runs,
            start_to_close_timeout=_POLICY.start_to_close,
            heartbeat_timeout=_POLICY.heartbeat_timeout,
            retry_policy=_POLICY.retry,
        )
