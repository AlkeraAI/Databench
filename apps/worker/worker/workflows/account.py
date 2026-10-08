"""Account lifecycle workflows: one thin workflow per activity."""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from alkera_core.temporal import WorkflowType

    from worker.activities import account as activities
    from worker.workflows._policy import execute_under_policy


@workflow.defn(name=WorkflowType.ACCOUNT_LIFECYCLE_SWEEP.value)
class AccountLifecycleSweep:
    """Every fifteen minutes, and on a support nudge: the lifecycle pass."""

    @workflow.run
    async def run(self) -> dict[str, int]:
        return await execute_under_policy(
            WorkflowType.ACCOUNT_LIFECYCLE_SWEEP, activities.account_lifecycle_sweep
        )


@workflow.defn(name=WorkflowType.ACCOUNT_REERASE.value)
class AccountReerase:
    """On every worker boot and from the restore runbook: re-erase what a
    restore brought back. A no-op when the ledger finds nobody."""

    @workflow.run
    async def run(self) -> dict[str, int]:
        return await execute_under_policy(WorkflowType.ACCOUNT_REERASE, activities.account_reerase)
