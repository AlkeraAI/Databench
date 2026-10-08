"""Deployment health: probe everything the self-hosted operator cares about
every 5 minutes and full-replace the snapshot the org-admin Health tab reads.

The activity in ``worker.activities.deployment_health`` runs the core below
under the SaaS self-skip and the advisory lock, so a slow run never overlaps
its successor and every completed scheduled run stamps the liveness marker
the Health tab reads.
"""

from __future__ import annotations

from typing import Any

from alkera_core.deployment_health import run_and_persist


async def run_health_checks() -> dict[str, Any]:
    results = await run_and_persist(trigger="scheduled")
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts
