"""The compute plane's workflows: one thin workflow per periodic core.

Each executes the identically named activity under the policy the retry table
declares for it — transient retry for the meter (idempotent: whole minutes
past a high-water mark, under an advisory lock), no retry for the sweep (a
duplicate pass buys nothing; its five-minute schedule is the retry). A run
started with no input — every scheduled run — uses the wall clock;
``SweepInput.now`` pins the clock for a test or a replay.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from alkera_core.schemas.temporal import SweepInput
    from alkera_core.temporal import WorkflowType

    from worker.activities import compute as activities
    from worker.workflows._policy import execute_under_policy


@workflow.defn(name=WorkflowType.COMPUTE_METER.value)
class ComputeMeter:
    """Every minute: verify, bill and cut off every live allocation.
    Returns the number of allocations metered."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.COMPUTE_METER, activities.compute_meter, input
        )


@workflow.defn(name=WorkflowType.COMPUTE_RECONCILE.value)
class ComputeReconcile:
    """Every reconcile period: adopt the pods a row lost, terminate the ones no
    row will ever bill or stop. Returns how many pods the pass acted on."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.COMPUTE_RECONCILE, activities.compute_reconcile, input
        )


@workflow.defn(name=WorkflowType.COMPUTE_SWEEP.value)
class ComputeSweep:
    """Every five minutes: announce every workspace machine whose reachability
    changed. Returns how many frames were emitted."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.COMPUTE_SWEEP, activities.compute_sweep, input
        )


@workflow.defn(name=WorkflowType.COMPUTE_CATALOG.value)
class ComputeCatalog:
    """Every hour: pull each configured provider's live price + stock onto its
    own catalog rows. Returns how many rows the refresh touched."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.COMPUTE_CATALOG, activities.compute_catalog, input
        )


@workflow.defn(name=WorkflowType.ORG_MACHINE_RECONCILE.value)
class OrgMachineReconcile:
    """Every 30 seconds: start, replace, stop, sleep and release org machines
    until each matches what its org asked for. Returns how many the pass acted
    on."""

    @workflow.run
    async def run(self, input: SweepInput | None = None) -> int:
        return await execute_under_policy(
            WorkflowType.ORG_MACHINE_RECONCILE, activities.org_machine_reconcile, input
        )
