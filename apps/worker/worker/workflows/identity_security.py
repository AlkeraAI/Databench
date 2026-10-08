"""The identity security log's retention prune as a workflow: one thin workflow
around the activity of the same name, under the policy the retry table
declares for it. A run started with no input uses the wall clock;
``SweepInput.now`` pins it for a test."""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from alkera_core.schemas.temporal import SweepInput
    from alkera_core.temporal import WorkflowType

    from worker.activities import identity_security as activities
    from worker.workflows._policy import execute_under_policy


@workflow.defn(name=WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS.value)
class PruneIdentitySecurityEvents:
    """Daily: delete identity security events older than the retention window.
    Returns how many were deleted."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.PRUNE_IDENTITY_SECURITY_EVENTS,
            activities.prune_identity_security_events,
            input,
        )
