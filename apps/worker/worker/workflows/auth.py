"""Auth maintenance workflows: one thin workflow per prune.

Each ``run`` executes the identically named activity with the policy the retry
table declares for it — no retry, because a predicate ``DELETE`` that failed
gains nothing from a second attempt seconds later; tomorrow's schedule is the
recovery. Nothing else happens in workflow code.
"""

from __future__ import annotations

from temporalio import workflow

# The sandbox re-imports this module in isolation; app modules are passed
# through so their import-time side effects run once, in the worker process.
with workflow.unsafe.imports_passed_through():
    from alkera_core.temporal import WorkflowType

    from worker.activities.auth import (
        prune_expired_device_codes,
        prune_expired_tokens,
        prune_login_lockouts,
    )
    from worker.temporal.retry import policy_for

_TOKENS = policy_for(WorkflowType.PRUNE_EXPIRED_TOKENS.value)
_DEVICE_CODES = policy_for(WorkflowType.PRUNE_EXPIRED_DEVICE_CODES.value)
_LOGIN_LOCKOUTS = policy_for(WorkflowType.PRUNE_LOGIN_LOCKOUTS.value)


@workflow.defn(name=WorkflowType.PRUNE_EXPIRED_TOKENS.value)
class PruneExpiredTokens:
    """Daily: delete expired ``auth_tokens`` rows. Returns the number removed."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity(
            prune_expired_tokens,
            start_to_close_timeout=_TOKENS.start_to_close,
            heartbeat_timeout=_TOKENS.heartbeat_timeout,
            retry_policy=_TOKENS.retry,
        )


@workflow.defn(name=WorkflowType.PRUNE_EXPIRED_DEVICE_CODES.value)
class PruneExpiredDeviceCodes:
    """Daily: delete expired ``device_authorizations`` rows. Returns the number removed."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity(
            prune_expired_device_codes,
            start_to_close_timeout=_DEVICE_CODES.start_to_close,
            heartbeat_timeout=_DEVICE_CODES.heartbeat_timeout,
            retry_policy=_DEVICE_CODES.retry,
        )


@workflow.defn(name=WorkflowType.PRUNE_LOGIN_LOCKOUTS.value)
class PruneLoginLockouts:
    """Daily: delete spent ``login_lockouts`` counters. Returns the number removed."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity(
            prune_login_lockouts,
            start_to_close_timeout=_LOGIN_LOCKOUTS.start_to_close,
            heartbeat_timeout=_LOGIN_LOCKOUTS.heartbeat_timeout,
            retry_policy=_LOGIN_LOCKOUTS.retry,
        )
