"""A promoted result the machine never answered gets a terminal state.

Promoting creates the object first and asks the machine for the rows second, so
``pending_upload`` — the page's "Saving — the workspace is uploading this
result." — is a state a reader meets legitimately. It stops being legitimate the
moment there is no machine answering: a box that died between the promote and
the upload never delivers the payload AND never refuses it, so the object waits
for ever with no rows, no reason, and a permanent "Saving" pill on the list.
That is what a reader met, ninety minutes after pressing Save.

So a wait has an end. Past :func:`promote_deadline` the object is failed with a
reason in the reader's words and announced like any other change, which is what
turns a page that never resolves into one that says what happened and offers to
try again.

Deliberately NOT a fallback for a machine that is alive: one that cannot find
the result behind a promote refuses it through ``POST
/objects/{id}/payload/failed``, carrying its own reason, long before this. The
deadline covers the absent machine, which is the only case that can say nothing
at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import settings
from alkera_core.events import Entity, EventType, emit
from alkera_core.logging import get_logger
from alkera_core.models.workspace_object import WorkspaceObject

log = get_logger(__name__)

#: What the object's page says once the wait is over. The reader's words, not
#: the machine's: they pressed Save, nothing came back, and the thing that owed
#: them an answer is their workspace.
PROMOTE_UNANSWERED_REASON = "the workspace did not answer"

#: The status a promote sits in until its payload lands, and the one it moves to
#: when it never does. Spelled here so the sweep and the routes agree.
PENDING_UPLOAD = "pending_upload"
FAILED = "failed"


def promote_deadline() -> timedelta:
    """How long a promote may wait for its payload."""
    return timedelta(seconds=settings.objects_promote_deadline_seconds)


def stalled_promotes(*, now: datetime | None = None) -> Select[tuple[WorkspaceObject]]:
    """Every live promoted result whose wait is over.

    Ordered oldest first so a backlog is worked through in the order the readers
    pressed Save. ``updated_at`` is the row's own cursor and a promote does not
    touch the row again while it waits, so it is the moment the wait began.
    """
    moment = now or datetime.now(UTC)
    return (
        select(WorkspaceObject)
        .where(
            WorkspaceObject.type == "result",
            WorkspaceObject.status == PENDING_UPLOAD,
            WorkspaceObject.deleted_at == 0,
            WorkspaceObject.updated_at < moment - promote_deadline(),
        )
        .order_by(WorkspaceObject.updated_at)
    )


async def expire_stalled_promotes(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Fail every promote whose deadline has passed, and announce each one.

    Returns how many frames were emitted. Commits. Idempotent: a failed object
    no longer matches, so a re-run inside the same window fails nothing twice.
    """
    moment = now or datetime.now(UTC)
    rows = (await db.execute(stalled_promotes(now=moment))).scalars().all()
    expired = 0
    for obj in rows:
        spec = dict(obj.spec or {})
        # Only if the machine has not already said why. A refusal that landed
        # between the read and this write is the better answer, and the version
        # bump below would otherwise overwrite it with a generic one.
        if spec.get("failure_reason"):
            continue
        # Read before the write: `updated_at` is server-defaulted `onupdate`, so
        # the flush below expires it and reading it again would be a lazy load.
        waited_seconds = int((moment - obj.updated_at).total_seconds())
        spec["failure_reason"] = PROMOTE_UNANSWERED_REASON
        obj.spec = spec
        obj.status = FAILED
        obj.version += 1
        obj.content_updated_at = moment.timestamp()
        await db.flush()
        await emit(
            db,
            org_id=obj.org_team_id,
            type=EventType.WORKSPACE_OBJECT_CHANGED,
            entity=Entity.WORKSPACE_OBJECT,
            entity_id=str(obj.id),
            version=obj.version,
            payload={"type": obj.type, "version": obj.version},
            actor=None,
        )
        expired += 1
        log.info(
            "objects.promote.expired",
            object_id=str(obj.id),
            org_id=str(obj.org_team_id),
            waited_seconds=waited_seconds,
        )
    await db.commit()
    return expired


__all__ = [
    "FAILED",
    "PENDING_UPLOAD",
    "PROMOTE_UNANSWERED_REASON",
    "expire_stalled_promotes",
    "promote_deadline",
    "stalled_promotes",
]
