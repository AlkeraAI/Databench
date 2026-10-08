"""Entitlements watchdog: a daily status line so an expiring grant stays LOUD.

Boot logs are easy to miss on a long-lived deployment — this daily job re-emits
the single ``entitlements.status`` line, which is INFO while valid, WARNING for
the whole 30-day grace window, and ERROR after. Self-skips when no entitlement
is configured (SaaS and most self-hosted installs), so it costs nothing where
it doesn't apply. Read-only — no lock needed. The activity in
``worker.activities.entitlements`` calls the core below.
"""

from __future__ import annotations

from typing import Any

from alkera_core.entitlements import get_entitlements, log_entitlements_status


def run_watchdog() -> dict[str, Any]:
    """Re-emit the entitlement status line; the status summary is the result."""
    ent = get_entitlements()
    state = ent.state()
    if state == "absent":
        return {"skipped": "no entitlement configured"}
    log_entitlements_status("worker")
    return {
        "state": state,
        "customer": ent.customer,
        "features": ent.feature_names(),
        "expires_on": ent.expires_on.isoformat() if ent.expires_on else None,
    }
