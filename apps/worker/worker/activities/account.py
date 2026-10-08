"""Account lifecycle activities: the lifecycle sweep and the re-erasure.

Thin ``@activity.defn`` wrappers around ``worker.tasks.account``; each activity
is registered under its workflow's name.
"""

from __future__ import annotations

from alkera_core.logging import get_logger
from alkera_core.temporal import WorkflowType
from temporalio import activity

from worker.tasks.account import _lifecycle_sweep, _reerase

log = get_logger(__name__)


@activity.defn(name=WorkflowType.ACCOUNT_LIFECYCLE_SWEEP.value)
async def account_lifecycle_sweep() -> dict[str, int]:
    """Run due erasures."""
    report = await _lifecycle_sweep()
    log.info("account.lifecycle_sweep", **report)
    return report


@activity.defn(name=WorkflowType.ACCOUNT_REERASE.value)
async def account_reerase() -> dict[str, int]:
    """Erase again every ledgered identity a restore brought back."""
    report = await _reerase()
    log.info("account.reerase", **report)
    if report["failed"]:
        raise RuntimeError(f"{report['failed']} re-erasure(s) failed; retrying")
    return report
