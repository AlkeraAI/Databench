"""The workspace jobs.

* The reconcile pass: every live chat in a workspace, and no workspace of one
  outliving its chat. Everything it does is in
  :mod:`alkera_core.objects.workspace_reconcile`.
* The deletion drain: what a workspace deletion leaves after its request,
  each chat's ending finished and the workspace's folder trashed. Everything
  it does is in :mod:`alkera_core.objects.workspace_end`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.objects import workspace_end, workspace_reconcile

log = get_logger(__name__)


async def run_reconcile_workspaces(now: datetime) -> int:
    """Passes until the backlog is drained or the run's bound is reached, each
    committed on its own. Returns how many rows they changed."""
    done = await workspace_reconcile.drain(AsyncSessionLocal, now=now)
    if done.adopted or done.retired:
        log.info("workspace.reconciled", adopted=done.adopted, retired=done.retired)
    return done.adopted + done.retired


async def run_finish_workspace_deletions(
    *, limit: int, heartbeat: Callable[[], None] | None = None
) -> workspace_end.Finished:
    """One committed pass of the deletion drain: up to ``limit`` chats of
    deleted workspaces finished, then every deleted workspace with none left."""
    async with AsyncSessionLocal() as session:
        done = await workspace_end.finish(session, batch=limit, heartbeat=heartbeat)
        await session.commit()
    if done.chats or done.workspaces or done.failed:
        log.info(
            "workspace.deletions_finished",
            chats=done.chats,
            workspaces=done.workspaces,
            failed=done.failed,
        )
    return done
