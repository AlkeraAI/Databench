"""The activities behind the workspace jobs: thin wrappers around the cores in
``worker.tasks.workspaces``. The reconcile pass runs under its advisory lock;
the deletion drain needs none, because its pass skips the rows another holds."""

from __future__ import annotations

from functools import partial

from alkera_core.schemas.temporal import DrainInput, DrainOutcome, SweepInput
from alkera_core.temporal import WorkflowType
from temporalio import activity

from worker.activities._sweep import clock, locked_sweep
from worker.tasks.workspaces import run_finish_workspace_deletions, run_reconcile_workspaces
from worker.temporal.interceptors import safe_heartbeat

LOCK = "workspace_reconcile"


@activity.defn(name=WorkflowType.RECONCILE_WORKSPACES.value)
async def reconcile_workspaces(input: SweepInput | None = None) -> int:
    """Every fifteen minutes: adopt chats with no workspace and retire
    workspaces of one whose chat is gone. Returns how many rows changed; ``0``
    when another pass held the lock."""
    changed = await locked_sweep(
        LOCK,
        partial(run_reconcile_workspaces, clock(input)),
        skipped_event="workspace.reconcile.skipped_locked",
    )
    if changed is None:
        return 0
    return changed


@activity.defn(name=WorkflowType.FINISH_WORKSPACE_DELETIONS.value)
async def finish_workspace_deletions(input: DrainInput | None = None) -> DrainOutcome:
    """One pass of the deletion drain, heartbeating once per chat. Every step
    is idempotent, so a retried attempt finishes what the failed one did not."""
    drain = input or DrainInput()
    done = await run_finish_workspace_deletions(limit=drain.limit, heartbeat=safe_heartbeat)
    return DrainOutcome(processed=done.chats + done.failed, applied=done.chats, failed=done.failed)
