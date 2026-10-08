"""How a notebook run's row moves and how the platform announces an ending.

``notebook_runs`` follows the engine's events (the backend's run follower)
and, where no engine will say a run ended, the platform ends it and says so
on the notebook's channel. Both the backend and the worker end runs, so the
rules for a status change and the announcement live here.

A status only moves forward: an ended run stays ended, and a late
``queued`` never undoes ``running``. The one exception is the platform's own
guess: a run ended ``machine_silent`` because the box did not answer in time
takes the engine's word when the box does speak of it after all.

Every ending the platform makes goes through :func:`end_runs`, which records
it and announces it, so a person never waits on a run that has ended.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.events import EventType, emit
from alkera_core.events.types import Entity
from alkera_core.notebooks.models import NotebookRun
from alkera_core.notebooks.schemas import RUN_FINAL_STATUSES, RUN_STATUSES
from alkera_core.schemas.realtime.machine import NOTEBOOK_FOLDER_NOT_HELD

#: The realtime channel a notebook's kernel events travel on.
NB_CHANNEL_PREFIX: Final = "nb:"
NB_CHANNEL_RE: Final = re.compile(
    r"^nb:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
#: The outbox payload key of events the platform made (no kernel's).
PLATFORM_EVENTS_KEY: Final = "platform_events"
#: Why the platform ended a run no engine will end. The box never answered:
MACHINE_SILENT: Final = "machine_silent"
#: the request never reached the machine channel:
NOT_SENT: Final = "not_sent"
#: nobody confirmed a run the engine asked to confirm (a confirmed run is a
#: new request):
NOT_CONFIRMED: Final = "not_confirmed"
#: the kernel it ran or waited on stopped without ending it:
KERNEL_STOPPED: Final = "kernel_stopped"
#: it outlived the longest a run may take:
RUN_DEADLINE: Final = "run_deadline"
#: the box never held the folder its lease names (its ``folder_not_held``
#: answer):
FOLDER_NOT_HELD: Final = NOTEBOOK_FOLDER_NOT_HELD

_RANK: Final[Mapping[str, int]] = {"queued": 0, "needs_confirmation": 0, "running": 1}


def nb_channel(item_id: uuid.UUID) -> str:
    """The notebook's event channel name."""
    return f"{NB_CHANNEL_PREFIX}{item_id}"


def item_of_channel(raw: str) -> uuid.UUID | None:
    """The notebook a ``nb:<item_id>`` channel names, in its one spelling (a
    lower-case hyphenated UUID); ``None`` for anything else."""
    match = NB_CHANNEL_RE.fullmatch(raw)
    return None if match is None else uuid.UUID(match.group(1))


def _rank(status: str) -> int:
    return 2 if status in RUN_FINAL_STATUSES else _RANK.get(status, 0)


def _silenced(run: NotebookRun) -> bool:
    """Ended only because the box did not answer in time."""
    return run.status == "refused" and run.reason == MACHINE_SILENT


def advance(run: NotebookRun, status: str, reason: str | None = None) -> bool:
    """Move ``run`` to ``status`` when that is forward; whether it moved. An
    unknown status from a newer engine reads as ``error``. A run the platform
    ended ``machine_silent`` moves to whatever the engine says of it."""
    if status not in RUN_STATUSES:
        reason = reason or f"status:{status}"
        status = "error"
    if _silenced(run) and not (status == "refused" and reason == MACHINE_SILENT):
        run.status, run.reason, run.finished_at = "queued", None, None
    if run.status in RUN_FINAL_STATUSES or _rank(status) < _rank(run.status):
        return False
    run.status = status
    if reason:
        run.reason = reason[:64]
    return True


def finished_event(run: NotebookRun) -> dict[str, Any]:
    """The ``run.finished`` event a run the platform ended is announced as,
    under the id the run's requester and the engine know it by."""
    return {
        "type": "run.finished",
        "run_id": run.engine_run_id or str(run.run_id),
        "status": run.status,
        "reason": run.reason,
    }


async def announce_platform_events(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    events: Sequence[Mapping[str, Any]],
) -> None:
    """Announce ``events`` the platform made (no kernel's, so unnumbered) on
    the notebook's channel. The caller commits."""
    if not events:
        return
    await emit(
        db,
        org_id=org_id,
        type=EventType.NOTEBOOK_EVENT,
        entity=Entity.NOTEBOOK,
        entity_id=nb_channel(item_id),
        payload={PLATFORM_EVENTS_KEY: [dict(one) for one in events]},
    )


async def end_runs(
    db: AsyncSession,
    runs: Iterable[NotebookRun],
    *,
    status: str,
    reason: str,
    at: datetime,
) -> list[NotebookRun]:
    """End each of ``runs`` that has not ended (``status`` must be final) and
    announce them on their notebooks' channels; the runs that ended. The
    caller holds the rows and commits."""
    if status not in RUN_FINAL_STATUSES:
        raise ValueError(f"{status} is not a final run status")
    ended = [run for run in runs if run.status not in RUN_FINAL_STATUSES]
    by_notebook: dict[tuple[uuid.UUID, uuid.UUID], list[dict[str, Any]]] = {}
    for run in ended:
        advance(run, status, reason)
        run.finished_at = at
        by_notebook.setdefault((run.org_id, run.item_id), []).append(finished_event(run))
    await db.flush()
    for (org_id, item_id), events in by_notebook.items():
        await announce_platform_events(db, org_id=org_id, item_id=item_id, events=events)
    return ended


__all__ = [
    "KERNEL_STOPPED",
    "MACHINE_SILENT",
    "NB_CHANNEL_PREFIX",
    "NB_CHANNEL_RE",
    "NOT_CONFIRMED",
    "NOT_SENT",
    "PLATFORM_EVENTS_KEY",
    "RUN_DEADLINE",
    "advance",
    "announce_platform_events",
    "end_runs",
    "finished_event",
    "item_of_channel",
    "nb_channel",
]
