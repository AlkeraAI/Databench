"""What happens to a machine's chats and to the org that held it when the
machine stops serving. Written in the same transaction as the machine's own
transition, by whichever process makes that transition.

Two facts follow a workspace machine off the plane:

* **Its chats.** A chat bound to a machine that no longer serves it has to say
  so. The chats another box can take are moved by placement (the backend's
  ``rebind_chats_off``); the rest are restated here (``stranded`` when the
  machine left the plane, ``asleep`` when it was put to sleep, ``starting``
  when it is woken), and each one is announced so an open browser re-reads it
  at once rather than on its next poll. A chat keeps the id
  of the box that last had it: that is how the next box that comes up finds
  it (placement reads "bound to a box that is not serving" as stranded), and
  how the console counts what a released box left behind.

* **Its dedicated assignment.** An org's assignment names the one box its
  chats run on. Once that box has left the plane the assignment would block
  placement on a dead box, so the assignment goes with the machine. The backend records the org
  audit event beside it; the worker, which cannot write the org's hash chain,
  drops the row and logs the org.

Both the backend (terminate, a failed provision) and the worker (the reconcile
finishing a release, a provider-confirmed loss) call :func:`leave_placement`,
so the invariant holds whichever side moves the row. Every call is
idempotent: a chat already restated and an assignment already dropped cost
nothing, which is what lets the reconcile settle a release the backend began.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.locking import LockRank, hold_place, lock_rows
from alkera_core.events import BOUND_MACHINE_KEY, Entity, EventType, emit
from alkera_core.logging import get_logger
from alkera_core.models.compute import ComputeAllocation
from alkera_core.models.machine_credential import OrgComputeAssignment
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.objects.publisher_report import clear_refusal
from alkera_core.schemas.objects.specs import MachineStatus as ChatMachineStatus

log = get_logger(__name__)

STRANDED: ChatMachineStatus = "stranded"
"""A chat whose machine left service with nothing to take it."""


@dataclass(frozen=True, slots=True)
class Departure:
    """What a machine's leaving did: the orgs whose dedicated assignment named
    it (returned to ordinary placement) and the chats left stranded on it."""

    released_org_ids: list[UUID] = field(default_factory=list)
    stranded_chat_ids: list[UUID] = field(default_factory=list)


async def restate_bound_chats(
    db: AsyncSession,
    *,
    machine_id: UUID,
    status: ChatMachineStatus,
    actor: Mapping[str, Any] | None,
) -> list[UUID]:
    """Write ``status`` onto every live chat bound to ``machine_id`` whose
    word differs, and announce each one. Returns the chats restated; the
    caller commits.

    The rows are locked in one statement, oldest first, the same lock every
    other writer of a chat's spec takes, so a concurrent edit is serialized
    against this and neither clobbers the other. Every bound chat's document
    is locked before them, in one statement: the order every writer of a chat
    takes (:func:`alkera_core.objects.chat_end.lock_chat_for_write`), and the
    one the endings a sleep runs next need, since an ending stamps the
    document. ``stranded`` also drops the publisher refusal: it was the
    departed box's reason, and a banner about a box that is gone would be a
    second lie over the first.
    """
    async with cross_tenant_write(db, reason="compute.machine_chats.restate"):
        bound_to = WorkspaceObject.spec["machine_id"].astext
        said = WorkspaceObject.spec["machine_status"].astext
        bound = select(WorkspaceObject.org_team_id, cast(WorkspaceObject.id, String)).where(
            WorkspaceObject.type == "chat",
            WorkspaceObject.deleted_at == 0,
            bound_to == str(machine_id),
        )
        await lock_rows(
            db,
            LockRank.REALTIME_DOC,
            select(RealtimeDoc.doc_id)
            .where(
                RealtimeDoc.doc_type == "chat",
                tuple_(RealtimeDoc.org_id, RealtimeDoc.doc_id).in_(bound),
            )
            .order_by(RealtimeDoc.org_id, RealtimeDoc.doc_id),
        )
        # Every bound chat's document step is done, whether or not it had a
        # document to lock: an ending that follows takes each chat alone.
        for chat_id in (
            await db.execute(
                select(WorkspaceObject.id).where(
                    WorkspaceObject.type == "chat",
                    WorkspaceObject.deleted_at == 0,
                    bound_to == str(machine_id),
                )
            )
        ).scalars():
            hold_place(db, LockRank.REALTIME_DOC, chat_id)
        rows = (
            (
                await lock_rows(
                    db,
                    LockRank.WORKSPACE_OBJECT,
                    select(WorkspaceObject)
                    .where(
                        WorkspaceObject.type == "chat",
                        WorkspaceObject.deleted_at == 0,
                        bound_to == str(machine_id),
                        or_(said.is_(None), said != status),
                    )
                    .order_by(WorkspaceObject.created_at, WorkspaceObject.id)
                    .execution_options(populate_existing=True),
                )
            )
            .scalars()
            .all()
        )
        restated: list[UUID] = []
        for chat in rows:
            spec = dict(chat.spec)
            spec["machine_status"] = status
            if status == STRANDED:
                clear_refusal(spec)
            chat.spec = spec
            await emit(
                db,
                org_id=chat.org_team_id,
                type=EventType.CHAT_UPDATED,
                entity=Entity.CHAT,
                entity_id=str(chat.id),
                version=chat.version,
                payload={
                    "team_id": str(chat.team_id) if chat.team_id else None,
                    BOUND_MACHINE_KEY: str(machine_id),
                },
                actor=actor,
                flush=False,
            )
            restated.append(chat.id)
        if restated:
            await db.flush()
        return restated


async def count_bound_chats(db: AsyncSession, *, machine_id: UUID) -> int:
    """How many live chats are bound to ``machine_id`` right now."""
    async with cross_tenant_write(db, reason="compute.machine_chats.count"):
        bound_to = WorkspaceObject.spec["machine_id"].astext
        return int(
            (
                await db.execute(
                    select(func.count()).where(
                        WorkspaceObject.type == "chat",
                        WorkspaceObject.deleted_at == 0,
                        bound_to == str(machine_id),
                    )
                )
            ).scalar_one()
        )


async def drop_dedicated_assignments(db: AsyncSession, *, machine_id: UUID) -> list[UUID]:
    """Remove every org assignment naming ``machine_id`` (the schema allows one
    org per box, so at most one). Returns the orgs released; the caller commits
    and, where it can, records the org audit event for each."""
    rows = await db.execute(
        delete(OrgComputeAssignment)
        .where(OrgComputeAssignment.machine_id == machine_id)
        .returning(OrgComputeAssignment.org_team_id)
    )
    return list(rows.scalars().all())


async def leave_placement(
    db: AsyncSession, alloc: ComputeAllocation, *, actor: Mapping[str, Any] | None
) -> Departure:
    """The machine has left service: its org's dedicated assignment goes with
    it and every chat still bound to it is stranded. Called in the transaction
    that moves the row, after placement has been given its chance to move what
    it can. Idempotent."""
    released = await drop_dedicated_assignments(db, machine_id=alloc.id)
    for org_id in released:
        log.info(
            "compute.dedicated.assignment_released",
            allocation_id=str(alloc.id),
            org_id=str(org_id),
            state=alloc.state,
        )
    stranded = await restate_bound_chats(db, machine_id=alloc.id, status=STRANDED, actor=actor)
    # The chats are no longer served anywhere: each ends through the one
    # transition, so the folder lease the departed box held goes with it rather
    # than waiting out its TTL in front of the next box.
    from alkera_core.objects import chat_end

    await chat_end.end_chats(
        db,
        await chat_end.live_chats_bound_to(db, alloc.id),
        chat_end.ChatEndReason.BOX_LOST,
        actor=actor,
        # Each chat was announced by the restate above; one frame per change.
        announce=False,
    )
    if stranded:
        log.info(
            "compute.placement.chats_stranded",
            allocation_id=str(alloc.id),
            state=alloc.state,
            chats=len(stranded),
        )
    return Departure(released_org_ids=released, stranded_chat_ids=stranded)


__all__ = [
    "STRANDED",
    "Departure",
    "count_bound_chats",
    "drop_dedicated_assignments",
    "leave_placement",
    "restate_bound_chats",
]
