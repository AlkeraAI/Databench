"""A workspace whose own machine is gone: the one place that says so, and the
one place that moves it to the default placement.

A workspace pinned to an org machine runs there and nowhere else. When that
machine is deleted (``WorkspaceSpec.lost_machine_id``, written by the delete)
or its audience no longer holds the person whose chat would run there, the pin
can serve nothing. Placement never quietly lands such a workspace somewhere
else: a person opening it is asked where to run it, with the default placement
offered first when it serves the org; any other waker (a message, an agent, a
schedule, Slack, a new chat) moves it to the default placement and the move is
recorded on the workspace (``fell_back_at``) so its machine row says so. When
nothing serves the org by default, nothing moves and the chats wait for a
choice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from alkera_core.compute.org_machines import may_use
from alkera_core.events import Entity, EventType, emit
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import WorkspaceObject
from alkera_core.models.org_machines import OrgMachine
from alkera_core.objects.workspaces import workspace_spec_of
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import chats as chat_domain
from backend.services.compute.org_machine_pins import pins_of

LostReason = Literal["deleted", "no_access"]
"""Why a workspace's own machine can no longer serve it."""


@dataclass(frozen=True, slots=True)
class LostMachine:
    """The machine a workspace lost, why, and whether a waker already moved
    the workspace to the default placement."""

    workspace_id: UUID
    org_machine_id: UUID
    name: str
    reason: LostReason
    fell_back: bool

    @property
    def awaiting_choice(self) -> bool:
        return not self.fell_back


def _uuid(raw: object) -> UUID | None:
    if not raw:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except ValueError:
        return None


async def lost_machine(
    db: AsyncSession, workspace: WorkspaceObject, *, owner_user_id: UUID | None
) -> LostMachine | None:
    """The machine ``workspace`` lost, or ``None`` while its pin (or the default
    placement it never left) still holds. ``owner_user_id`` is whose use of the
    pinned machine is the question: the chat's owner for a wake, the reader for
    the workspace's machine row. Read with the workspace's org in the SQL."""
    spec = workspace_spec_of(workspace.spec)
    lost = _uuid(spec.lost_machine_id)
    pin = _uuid(spec.machine_pin)
    named = lost or pin
    if named is None:
        return None
    machine = (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.id == named, OrgMachine.org_team_id == workspace.org_team_id
            )
        )
    ).scalar_one_or_none()
    name = machine.name if machine is not None else ""
    if machine is None or machine.deleted_at is not None:
        reason: LostReason = "deleted"
    elif lost is not None:
        reason = "no_access"
    elif owner_user_id is not None and not await may_use(db, machine, user_id=owner_user_id):
        reason = "no_access"
    else:
        return None
    return LostMachine(
        workspace_id=workspace.id,
        org_machine_id=named,
        name=name,
        reason=reason,
        fell_back=spec.fell_back_at is not None,
    )


async def _workspace_of(db: AsyncSession, chat: WorkspaceObject) -> WorkspaceObject | None:
    key = _uuid(chat_domain.chat_spec_of(chat).workspace_id)
    if key is None:
        return None
    return (
        await db.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.id == key,
                WorkspaceObject.org_team_id == chat.org_team_id,
                WorkspaceObject.type == WORKSPACE_TYPE,
                WorkspaceObject.deleted_at == 0,
            )
        )
    ).scalar_one_or_none()


async def lost_machine_of_chat(db: AsyncSession, chat: WorkspaceObject) -> LostMachine | None:
    """:func:`lost_machine` for the workspace ``chat`` is in, asked for the
    chat's owner (whose place in the audience a pin needs, never the sender's)."""
    workspace = await _workspace_of(db, chat)
    if workspace is None:
        return None
    return await lost_machine(db, workspace, owner_user_id=chat.owner_user_id)


async def fall_back(
    db: AsyncSession, lost: LostMachine, *, org_id: UUID, actor: Mapping[str, Any] | None
) -> bool:
    """Move the workspace off the machine it lost onto the default placement:
    the pin is cleared, the lost machine and the moment kept for the machine
    row to show, and the workspace announced. One conditional write, so two
    wakers racing record it once. Returns whether this call recorded it."""
    if lost.fell_back:
        return False
    rows = (
        await db.execute(
            text(
                "UPDATE workspace_objects "
                "SET spec = spec || jsonb_build_object("
                "      'machine_pin', NULL, 'lost_machine_id', CAST(:lost AS text), "
                "      'fell_back_at', CAST(:at AS text)), "
                "    version = version + 1, updated_at = now() "
                "WHERE id = :id AND org_team_id = :org AND type = 'workspace' "
                "  AND deleted_at = 0 AND spec->>'fell_back_at' IS NULL "
                "  AND (spec->>'lost_machine_id' = :lost OR spec->>'machine_pin' = :lost) "
                "RETURNING version"
            ),
            {
                "id": lost.workspace_id,
                "org": org_id,
                "lost": str(lost.org_machine_id),
                "at": datetime.now(UTC).isoformat(),
            },
        )
    ).all()
    if not rows:
        return False
    version = int(rows[0][0])
    await emit(
        db,
        org_id=org_id,
        type=EventType.WORKSPACE_OBJECT_CHANGED,
        entity=Entity.WORKSPACE_OBJECT,
        entity_id=str(lost.workspace_id),
        version=version,
        payload={"type": WORKSPACE_TYPE, "version": version},
        actor=dict(actor) if actor else None,
    )
    return True


async def fall_back_if_lost(
    db: AsyncSession,
    workspace: WorkspaceObject | None,
    *,
    owner_user_id: UUID | None,
    actor: Mapping[str, Any] | None,
) -> bool:
    """Record that ``workspace`` moved to the default placement, when it is
    waiting on a choice after losing its machine. Called by a waker that is
    not a person opening the workspace, once placement bound a chat of it
    with no pin."""
    if workspace is None:
        return False
    lost = await lost_machine(db, workspace, owner_user_id=owner_user_id)
    if lost is None or not lost.awaiting_choice:
        return False
    return await fall_back(db, lost, org_id=workspace.org_team_id, actor=actor)


async def chat_fell_back_if_lost(
    db: AsyncSession, chat: WorkspaceObject, *, actor: Mapping[str, Any] | None
) -> bool:
    """:func:`fall_back_if_lost` for the workspace ``chat`` is in."""
    workspace = await _workspace_of(db, chat)
    return await fall_back_if_lost(db, workspace, owner_user_id=chat.owner_user_id, actor=actor)


async def waiting_on_choice(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> set[UUID]:
    """The chats among ``chats`` whose workspace lost its machine to a delete
    and has not been moved since: a box coming up does not take them, since
    nobody chose where they run. One read of their workspaces."""
    wanted: dict[UUID, list[WorkspaceObject]] = {}
    for chat in chats:
        key = _uuid(chat_domain.chat_spec_of(chat).workspace_id)
        if key is not None:
            wanted.setdefault(key, []).append(chat)
    if not wanted:
        return set()
    rows = await db.execute(
        select(WorkspaceObject.id, WorkspaceObject.org_team_id).where(
            WorkspaceObject.id.in_(list(wanted)),
            WorkspaceObject.type == WORKSPACE_TYPE,
            WorkspaceObject.deleted_at == 0,
            WorkspaceObject.spec["lost_machine_id"].astext.is_not(None),
            WorkspaceObject.spec["fell_back_at"].astext.is_(None),
        )
    )
    # A chat claiming another org's workspace is no claim at all.
    return {
        chat.id
        for workspace_id, org_id in rows.all()
        for chat in wanted[workspace_id]
        if chat.org_team_id == org_id
    }


async def held_back(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> set[UUID]:
    """The chats among ``chats`` a box coming up must not take: those whose
    workspace's pin holds (they wait for their own machine) and those waiting
    on a choice after their workspace lost its machine."""
    return set(await pins_of(db, chats)) | await waiting_on_choice(db, chats)


async def not_waiting(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> list[WorkspaceObject]:
    """``chats`` without those waiting on a choice of machine."""
    waiting = await waiting_on_choice(db, chats)
    return [chat for chat in chats if chat.id not in waiting]


__all__ = [
    "LostMachine",
    "LostReason",
    "chat_fell_back_if_lost",
    "fall_back",
    "fall_back_if_lost",
    "held_back",
    "lost_machine",
    "lost_machine_of_chat",
    "not_waiting",
    "waiting_on_choice",
]
