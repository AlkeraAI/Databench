"""The notebook run sweep: no run stays queued or running for good.

The backend follows each run from the engine's events and ends the ones it
can see end (a refused answer, a box that never answered within the request's
window). Some endings nobody is there to see: the backend replica that waited
on the answer restarted, the kernel stopped with runs still on it, the box
went away mid-run, or a run the engine asked to confirm was never confirmed
(a confirmed run is a new request). This sweep ends those rows past their
deadlines, through the one ending owner (``alkera_core.notebooks.runs``), so
each ending is also announced on the notebook's channel. It reads and writes
every org's runs, so it runs through the cross-tenant write seam, which
refuses a login whose answers row security would filter.

Every open status reaches an ending: ``queued`` and ``needs_confirmation`` by
their own deadlines, ``running`` when its kernel stops, and all of them by
:data:`RUN_DEADLINE_AFTER` whatever else holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.logging import get_logger
from alkera_core.notebooks.models import NotebookKernel, NotebookRun
from alkera_core.notebooks.runs import (
    KERNEL_STOPPED,
    MACHINE_SILENT,
    NOT_CONFIRMED,
    RUN_DEADLINE,
    end_runs,
)
from alkera_core.notebooks.schemas import RUN_FINAL_STATUSES, RUN_STATUSES
from sqlalchemy import ColumnElement, and_, exists, select

log = get_logger(__name__)

#: A run the box has not spoken of this long after it was asked for (well
#: past the backend's own answer window) ends ``machine_silent``.
UNANSWERED_AFTER: Final = timedelta(minutes=2)
#: A run waiting for a confirmation nobody gave ends after this long.
CONFIRM_WINDOW: Final = timedelta(minutes=30)
#: A stopped kernel's last events land within this long of its stop; its
#: runs that are still open after it end ``kernel_restarted``.
STOPPED_GRACE: Final = timedelta(seconds=30)
#: No run stays open longer than this, whatever else holds.
RUN_DEADLINE_AFTER: Final = timedelta(hours=24)
#: Rows ended per transaction.
BATCH: Final = 500

#: The statuses a run can still leave.
OPEN_STATUSES: Final = tuple(s for s in RUN_STATUSES if s not in RUN_FINAL_STATUSES)


def _kernel_stopped(now: datetime) -> ColumnElement[bool]:
    return and_(
        NotebookRun.status.in_(OPEN_STATUSES),
        NotebookRun.kernel_id.is_not(None),
        exists().where(
            NotebookKernel.kernel_id == NotebookRun.kernel_id,
            NotebookKernel.stopped_at.is_not(None),
            NotebookKernel.stopped_at < now - STOPPED_GRACE,
        ),
    )


def _unanswered(now: datetime) -> ColumnElement[bool]:
    """Never started, and no live kernel of its notebook heard since it was
    asked for: nothing on the box holds it."""
    return and_(
        NotebookRun.status == "queued",
        NotebookRun.started_at.is_(None),
        NotebookRun.created_at < now - UNANSWERED_AFTER,
        ~exists().where(
            NotebookKernel.org_id == NotebookRun.org_id,
            NotebookKernel.item_id == NotebookRun.item_id,
            NotebookKernel.stopped_at.is_(None),
            NotebookKernel.state != "absent",
            NotebookKernel.updated_at >= NotebookRun.created_at,
        ),
    )


def _unconfirmed(now: datetime) -> ColumnElement[bool]:
    return and_(
        NotebookRun.status == "needs_confirmation",
        NotebookRun.created_at < now - CONFIRM_WINDOW,
    )


def _past_deadline(now: datetime) -> ColumnElement[bool]:
    return and_(
        NotebookRun.status.in_(OPEN_STATUSES),
        NotebookRun.created_at < now - RUN_DEADLINE_AFTER,
    )


async def _end_where(where: ColumnElement[bool], *, status: str, reason: str, now: datetime) -> int:
    """End every open run ``where`` holds, a batch per transaction."""
    total = 0
    while True:
        async with (
            AsyncSessionLocal() as session,
            cross_tenant_write(session, reason="notebooks.sweep_runs") as db,
        ):
            rows = list(
                (
                    await db.execute(
                        select(NotebookRun)
                        .where(where)
                        .order_by(NotebookRun.created_at)
                        .limit(BATCH)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            ended = await end_runs(db, rows, status=status, reason=reason, at=now)
            await db.commit()
        total += len(ended)
        if len(rows) < BATCH:
            return total


async def sweep_notebook_runs(now: datetime | None = None) -> int:
    """End every run no engine will end, past its deadline; how many ended."""
    at = now or datetime.now(UTC)
    endings = (
        (_kernel_stopped(at), "kernel_restarted", KERNEL_STOPPED),
        (_unanswered(at), "refused", MACHINE_SILENT),
        (_unconfirmed(at), "refused", NOT_CONFIRMED),
        (_past_deadline(at), "interrupted", RUN_DEADLINE),
    )
    total = 0
    for where, status, reason in endings:
        ended = await _end_where(where, status=status, reason=reason, now=at)
        if ended:
            log.info("notebooks.runs_ended", reason=reason, count=ended)
        total += ended
    return total


__all__ = [
    "CONFIRM_WINDOW",
    "OPEN_STATUSES",
    "RUN_DEADLINE_AFTER",
    "STOPPED_GRACE",
    "UNANSWERED_AFTER",
    "sweep_notebook_runs",
]
