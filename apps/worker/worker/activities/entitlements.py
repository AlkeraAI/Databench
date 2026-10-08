"""The entitlements watchdog activity: a daily status line so an expiring
grant stays loud.

A thin ``@activity.defn`` wrapper around the core in ``worker.tasks.entitlements``;
the body is synchronous and pure (it reads the cached grant and logs), so it is
called inline. The activity type is the historical task name.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from worker.tasks.entitlements import run_watchdog


@activity.defn(name="entitlements.watchdog")
async def watchdog() -> dict[str, Any]:
    """Daily: re-emit the entitlement status line (grace/expiry stay visible)."""
    return run_watchdog()
