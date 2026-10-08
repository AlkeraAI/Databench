"""The workspace jobs' workflows.

The reconcile pass is one thin workflow around the activity of the same name,
under the policy the retry table declares (no retry, the schedule is the
retry). ``SweepInput.now`` pins the clock for a test.

The deletion drain runs passes until no deleted workspace has anything left to
finish. Started by the delete route's nudge (a signal-with-start on the id
``workspace.finish_deletions``) and by its schedule as the net, so at most one
drain runs at a time and a nudge that lands during a pass queues one more.
"""

from __future__ import annotations

from temporalio import workflow

from worker.workflows._drain import run_drain_loop

with workflow.unsafe.imports_passed_through():
    from alkera_core.schemas.temporal import DrainInput, DrainOutcome, DrainReport, SweepInput
    from alkera_core.temporal import MORE_WORK_SIGNAL, WorkflowType

    from worker.activities import workspaces as activities
    from worker.temporal.retry import policy_for
    from worker.workflows._policy import execute_under_policy


@workflow.defn(name=WorkflowType.RECONCILE_WORKSPACES.value)
class ReconcileWorkspaces:
    """Every fifteen minutes: every chat in a workspace, and no workspace of
    one outliving its chat. Returns how many rows changed."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.RECONCILE_WORKSPACES, activities.reconcile_workspaces, input
        )


@workflow.defn(name=WorkflowType.FINISH_WORKSPACE_DELETIONS.value)
class FinishWorkspaceDeletions:
    """Passes until every deleted workspace's chats are finished and its
    folder is in the trash."""

    def __init__(self) -> None:
        self._more_work = False

    @workflow.signal(name=MORE_WORK_SIGNAL)
    def more_work(self) -> None:
        self._more_work = True

    @workflow.run
    async def run(self, input: DrainInput | None = None) -> DrainReport:
        drain = input or DrainInput()
        policy = policy_for(WorkflowType.FINISH_WORKSPACE_DELETIONS.value)

        async def run_pass() -> DrainOutcome:
            return await workflow.execute_activity(
                activities.finish_workspace_deletions,
                drain,
                start_to_close_timeout=policy.start_to_close,
                schedule_to_close_timeout=policy.schedule_to_close,
                heartbeat_timeout=policy.heartbeat_timeout,
                retry_policy=policy.retry,
            )

        def clear() -> None:
            self._more_work = False

        return await run_drain_loop(
            run_pass=run_pass,
            more_work=lambda: self._more_work,
            clear_more_work=clear,
            input=drain,
        )
