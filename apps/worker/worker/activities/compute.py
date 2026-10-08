"""The compute plane's activities: the metering tick and the reachability sweep.

Thin ``@activity.defn`` wrappers around the cores in ``worker.tasks.compute``.
Each runs under its advisory lock — a tick that finds another one running
steps aside and reports zero — and heartbeats so a worker that dies mid-pass is
noticed in minutes. The activity types are the workflow type names.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial

from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import WorkflowType
from temporalio import activity

from worker.activities._sweep import clock, locked_sweep
from worker.tasks.compute import (
    run_meter,
    run_org_reconcile,
    run_reconcile,
    run_refresh_catalog,
    run_sweep,
)
from worker.temporal.retry import policy_for

METER_LOCK = "compute_meter"
SWEEP_LOCK = "compute_sweep"
RECONCILE_LOCK = "compute_reconcile"
CATALOG_LOCK = "compute_catalog"
ORG_RECONCILE_LOCK = "compute_org_machine_reconcile"

#: The longest one org-machine reconcile pass runs before it is stopped and
#: its claim released: a minute inside the activity's own timeout, so the pass
#: that overran is the one that says so, and the next pass thirty seconds later
#: is not turned away by a claim nothing is using.
ORG_RECONCILE_BUDGET = policy_for(WorkflowType.ORG_MACHINE_RECONCILE.value).start_to_close - (
    timedelta(minutes=1)
)


@activity.defn(name=WorkflowType.COMPUTE_METER.value)
async def compute_meter(input: SweepInput | None = None) -> int:
    """Every minute: verify, bill and cut off every live allocation. Returns the
    number of allocations metered; ``0`` when another tick held the lock."""
    result = await locked_sweep(
        METER_LOCK, partial(run_meter, clock(input)), skipped_event="compute.meter.skipped_locked"
    )
    if result is None:
        return 0
    return result["metered"]


@activity.defn(name=WorkflowType.COMPUTE_SWEEP.value)
async def compute_sweep(input: SweepInput | None = None) -> int:
    """Every five minutes: announce every workspace machine whose reachability
    changed. Returns how many frames were emitted; ``0`` when another sweep held
    the lock."""
    changed = await locked_sweep(
        SWEEP_LOCK, partial(run_sweep, clock(input)), skipped_event="compute.sweep.skipped_locked"
    )
    if changed is None:
        return 0
    return changed


@activity.defn(name=WorkflowType.COMPUTE_RECONCILE.value)
async def compute_reconcile(input: SweepInput | None = None) -> int:
    """Compare this deployment's pods at the provider with the rows that own
    them. Returns how many pods the pass acted on — adopted plus terminated
    plus written off; ``0`` when another pass held the lock."""
    result = await locked_sweep(
        RECONCILE_LOCK,
        partial(run_reconcile, clock(input)),
        skipped_event="compute.reconcile.skipped_locked",
    )
    if result is None:
        return 0
    return result["adopted"] + result["terminated"] + result["written_off"]


@activity.defn(name=WorkflowType.COMPUTE_CATALOG.value)
async def compute_catalog(input: SweepInput | None = None) -> int:
    """Refresh each configured provider's live price + stock onto its catalog
    rows. Returns how many rows the refresh touched — updated plus reappeared
    plus marked-unavailable; ``0`` when another refresh held the lock."""
    result = await locked_sweep(
        CATALOG_LOCK,
        partial(run_refresh_catalog, clock(input)),
        skipped_event="compute.catalog_refresh.skipped_locked",
    )
    if result is None:
        return 0
    return result["updated"] + result["reappeared"] + result["marked_unavailable"]


@activity.defn(name=WorkflowType.ORG_MACHINE_RECONCILE.value)
async def org_machine_reconcile(input: SweepInput | None = None) -> int:
    """Every 30 seconds: converge every org machine on what its org asked for.
    Returns how many machines the pass acted on; ``0`` when another pass held
    the lock."""
    result = await locked_sweep(
        ORG_RECONCILE_LOCK,
        partial(run_org_reconcile, clock(input)),
        skipped_event="compute.org_machine.reconcile_skipped_locked",
        budget=ORG_RECONCILE_BUDGET,
    )
    if result is None:
        return 0
    return result["acted"]


__all__ = [
    "CATALOG_LOCK",
    "METER_LOCK",
    "ORG_RECONCILE_BUDGET",
    "ORG_RECONCILE_LOCK",
    "RECONCILE_LOCK",
    "SWEEP_LOCK",
    "compute_catalog",
    "compute_meter",
    "compute_reconcile",
    "compute_sweep",
    "org_machine_reconcile",
]
