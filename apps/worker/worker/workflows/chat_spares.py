"""The chat-spare sweep's workflow: one thin workflow around the activity of the
same name, under the policy the retry table declares (no retry — the minute
schedule is the retry). A run started with no input uses the wall clock;
``SweepInput.now`` pins it for a test."""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from alkera_core.schemas.temporal import SweepInput
    from alkera_core.temporal import WorkflowType

    from worker.activities import chat_spares as activities
    from worker.workflows._policy import execute_under_policy


@workflow.defn(name=WorkflowType.REAP_CHAT_SPARES.value)
class ReapChatSpares:
    """Every minute: reap every warmed-ahead chat whose owner has left the
    page or which has outlived its maximum age. Returns how many were reaped."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.REAP_CHAT_SPARES, activities.reap_chat_spares, input
        )
