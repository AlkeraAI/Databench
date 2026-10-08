"""The deployment-health workflow.

One thin workflow that executes the identically named activity with the
policy the retry table declares for it — transient retry inside a one-minute
timeout, because the run has its own fifteen-second budget and a snapshot is
full-replaced under a transaction lock, so a second attempt never leaves a
half-written one. Nothing else happens in workflow code.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow

# The sandbox re-imports this module in isolation; app modules are passed
# through so their import-time side effects run once, in the worker process.
with workflow.unsafe.imports_passed_through():
    from alkera_core.temporal import WorkflowType

    from worker.activities.deployment_health import run_deployment_health
    from worker.temporal.retry import policy_for

_POLICY = policy_for(WorkflowType.DEPLOYMENT_HEALTH.value)


@workflow.defn(name=WorkflowType.DEPLOYMENT_HEALTH.value)
class DeploymentHealthRun:
    """Every five minutes: probe and persist the health snapshot on a self-host."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        return await workflow.execute_activity(
            run_deployment_health,
            start_to_close_timeout=_POLICY.start_to_close,
            heartbeat_timeout=_POLICY.heartbeat_timeout,
            retry_policy=_POLICY.retry,
        )
