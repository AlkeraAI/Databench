"""The entitlements watchdog workflow.

One thin workflow that executes the identically named activity with the
policy the retry table declares for it — a single attempt, because the job is
a log line: a retry would only repeat it, and tomorrow's schedule re-emits it
anyway. Nothing else happens in workflow code.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow

# The sandbox re-imports this module in isolation; app modules are passed
# through so their import-time side effects run once, in the worker process.
with workflow.unsafe.imports_passed_through():
    from alkera_core.temporal import WorkflowType

    from worker.activities.entitlements import watchdog
    from worker.temporal.retry import policy_for

_POLICY = policy_for(WorkflowType.ENTITLEMENTS_WATCHDOG.value)


@workflow.defn(name=WorkflowType.ENTITLEMENTS_WATCHDOG.value)
class EntitlementsWatchdog:
    """Daily: re-emit the entitlement status line. Returns the status summary."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        return await workflow.execute_activity(
            watchdog,
            start_to_close_timeout=_POLICY.start_to_close,
            heartbeat_timeout=_POLICY.heartbeat_timeout,
            retry_policy=_POLICY.retry,
        )
