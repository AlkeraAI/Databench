"""The deployment-health activity: probe what a self-hosted operator cares
about and full-replace the snapshot the Health tab reads.

A thin ``@activity.defn`` wrapper around the core in ``worker.tasks.deployment_health``.
The SaaS self-skip and the advisory lock stay inside the activity, so the
scheduled run on a self-host is exactly the run the Health tab's liveness
check expects: it stamps ``last_scheduled_at`` every time it completes. The
activity type is the historical task name.
"""

from __future__ import annotations

from typing import Any

from alkera_core.config import settings
from alkera_core.logging import get_logger
from temporalio import activity

from worker.tasks._hardening import run_locked
from worker.tasks.deployment_health import run_health_checks

log = get_logger(__name__)


@activity.defn(name="deployment_health.run")
async def run_deployment_health() -> dict[str, Any]:
    """Every five minutes: probe and persist the health snapshot. Self-hosted only;
    returns the per-status counts, or which guard skipped the run."""
    if not settings.is_self_hosted:
        return {"skipped": "saas"}
    result = await run_locked("deployment_health", run_health_checks)
    if result is None:
        return {"skipped": "locked"}
    log.info("deployment_health.run", **result)
    return result
