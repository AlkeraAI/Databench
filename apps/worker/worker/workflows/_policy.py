"""Schedule an activity under the policy the retry table declares for it.

Every workflow that runs one of the historical tasks goes through this helper,
so the timeouts and the retry policy an activity runs with are read from
``ACTIVITY_POLICIES`` in exactly one place and never restated per workflow.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from temporalio import workflow

# Workflow code runs in the SDK's sandbox, which re-imports a workflow module in
# isolation; app modules are passed through so the sandbox does not try to
# re-execute them (they read the environment at import).
with workflow.unsafe.imports_passed_through():
    from alkera_core.temporal import WorkflowType

    from worker.temporal.retry import policy_for

T = TypeVar("T")


async def execute_under_policy(
    workflow_type: WorkflowType | str, fn: Callable[..., Awaitable[T]], *args: Any
) -> T:
    """Run activity ``fn`` — registered under the same name as ``workflow_type``
    — with that name's timeouts (start-to-close, heartbeat and, where the table
    declares one, schedule-to-close) and retry policy, passing ``args`` through
    unchanged.

    A plain string for the few activities that are not a workflow's namesake:
    a second activity a workflow needs still has a policy in the same table,
    and looking it up by name is how it gets one rather than a default."""
    policy = policy_for(workflow_type if isinstance(workflow_type, str) else workflow_type.value)
    return await workflow.execute_activity(
        fn,
        args=list(args),
        start_to_close_timeout=policy.start_to_close,
        schedule_to_close_timeout=policy.schedule_to_close,
        heartbeat_timeout=policy.heartbeat_timeout,
        retry_policy=policy.retry,
    )
