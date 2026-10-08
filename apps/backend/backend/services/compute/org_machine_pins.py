"""Where an org machine is pinned, and the org pool: which org machine a
workspace's chats run on, and which pool machine serves an org's unpinned
chats. Read by placement (:mod:`backend.services.compute.placement`)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from alkera_core.compute.machines import (
    ASLEEP,
    READY,
    STARTING,
    MachineState,
    machine_state,
    stands_behind_its_org,
)
from alkera_core.compute.org_machines import (
    audience_of,
    in_audience,
    may_use,
    org_machine_state,
    team_ids_of,
)
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import (
    ComputeAllocation,
    WorkspaceObject,
)
from alkera_core.models.compute import (
    ASLEEP as ASLEEP_STATE,
)
from alkera_core.models.compute import (
    COMPUTE_ACTIVE_STATES,
)
from alkera_core.models.compute import (
    DRAINING as DRAINING_STATE,
)
from alkera_core.models.org_machines import OrgComputeSettings, OrgMachine
from alkera_core.org_entitlements import org_entitlements
from alkera_core.schemas.org_machines import OrgMachineState
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import chats as chat_domain

PinAction = Literal["bind", "wake", "hold"]
"""What a pinned chat does with its machine (:data:`PIN_ACTIONS`)."""


#: The use mode of an org machine that serves the org's regular chats.
POOL_USE_MODE = "pool"


#: Why a stopped org pool machine may be woken by a regular chat: it stopped
#: when idle, or was slept with no reason recorded on the org machine.
WAKEABLE_STOP_REASONS: tuple[str, ...] = ("", "idle")


#: What a chat whose workspace is pinned does with the pinned machine, by the
#: machine's state: ``bind`` (it answers, or will once it is up), ``wake``
#: (bind, and the message starts it), ``hold`` (bind, and the chat shows why it
#: waits: no hardware, or it could not start). ``None``: the pin counts for
#: nothing. Total over the org machine states, so a state added there fails
#: here at once rather than reaching a chat as a guess.
PIN_ACTIONS: dict[OrgMachineState, PinAction | None] = {
    "running": "bind",
    "unreachable": "bind",
    "starting": "bind",
    "stopping": "wake",
    "stopped": "wake",
    "waiting_for_hardware": "hold",
    "failed": "hold",
    "deleted": None,
}


def pin_action(state: OrgMachineState) -> PinAction | None:
    """What a pinned chat does with its machine in ``state``."""
    return PIN_ACTIONS[state]


def choose_org_pool(
    machines: Sequence[ComputeAllocation],
    *,
    prefer: UUID | None = None,
    now: datetime | None = None,
) -> ComputeAllocation | None:
    """The org pool machine a regular chat goes to, of ``machines`` (the org's
    pool-mode org machines' allocations, oldest first): the one already
    holding the chat while it runs, else the least loaded running one, else
    one that is starting (it will answer soon, and waking another would bill
    a second machine for the same chat), else a stopped one, which the
    message wakes. ``None`` when none of them can take a chat."""
    by_state: dict[MachineState, list[ComputeAllocation]] = {}
    for alloc in machines:
        by_state.setdefault(machine_state(alloc, now=now), []).append(alloc)
    running = by_state.get(READY, [])
    for alloc in running:
        if alloc.id == prefer:
            return alloc
    if running:
        return min(running, key=lambda alloc: alloc.chats_served)
    for state in (STARTING, ASLEEP):
        if by_state.get(state):
            return by_state[state][0]
    return None


async def org_pool_applies(db: AsyncSession, *, org_id: UUID) -> bool:
    """Whether the org's regular chats run on its org pool: an Enterprise plan
    or a self-hosted deployment, read now, so a downgrade takes the pool out
    of placement without touching a machine."""
    return await org_entitlements().enterprise_features(db, org_id)


async def shared_pool_fallback(db: AsyncSession, *, org_id: UUID) -> bool:
    """Whether the org's regular chats may run on the shared pool while no
    org pool machine can take them: the org's setting, ``True`` without one."""
    row = await db.get(OrgComputeSettings, org_id)
    return True if row is None else bool(row.shared_pool_fallback)


def org_machines_of(org_id: UUID) -> Any:
    """The org's live org machines joined to the allocation backing each now.
    Both sides name the org: the machine's ``org_team_id`` and the
    allocation's immutable ``tenant_org_id``, so no row of another org is ever
    read, whatever id a caller hands in."""
    return (
        select(OrgMachine, ComputeAllocation)
        .join(ComputeAllocation, ComputeAllocation.id == OrgMachine.current_allocation_id)
        .where(
            OrgMachine.org_team_id == org_id,
            ComputeAllocation.tenant_org_id == org_id,
            OrgMachine.deleted_at.is_(None),
        )
    )


async def org_pool_machines(db: AsyncSession, *, org_id: UUID) -> list[ComputeAllocation]:
    """The allocations of the org's pool-mode org machines a chat may be placed
    on: live, not draining, stood behind by a live credential. A stopped one
    counts only when it stopped on its own (idle, or a sleep nobody asked to
    keep): one a manager stopped, or that ran out of credit or its cap, stays
    off until someone starts it, and a regular chat goes elsewhere."""
    stmt = (
        org_machines_of(org_id)
        .where(
            OrgMachine.use_mode == POOL_USE_MODE,
            ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
            ComputeAllocation.state != DRAINING_STATE,
            or_(
                ComputeAllocation.state != ASLEEP_STATE,
                OrgMachine.stop_reason.in_(WAKEABLE_STOP_REASONS),
            ),
            stands_behind_its_org(),
        )
        .order_by(ComputeAllocation.created_at.asc())
    )
    return [alloc for _, alloc in (await db.execute(stmt)).all()]


async def pinned_machine(
    db: AsyncSession,
    *,
    org_id: UUID,
    pin: UUID,
    owner_user_id: UUID | None,
    now: datetime | None = None,
) -> tuple[OrgMachine, ComputeAllocation, PinAction] | None:
    """The machine a pin binds a chat of ``owner_user_id``'s to, with what the
    chat does there, or ``None`` when the pin counts for nothing: no live org
    machine of this org by that id, one with no allocation yet, or an owner
    outside its audience. A chat with no owner to check never uses a pin."""
    if owner_user_id is None:
        return None
    found = (await db.execute(org_machines_of(org_id).where(OrgMachine.id == pin).limit(1))).first()
    if found is None:
        return None
    machine, alloc = found
    action = pin_action(org_machine_state(machine, alloc, now=now or datetime.now(UTC))[0])
    if action is None or not await may_use(db, machine, user_id=owner_user_id):
        return None
    return machine, alloc, action


async def workspace_pin(
    db: AsyncSession, workspace_id: str | UUID | None, *, org_id: UUID
) -> UUID | None:
    """The org machine ``workspace_id`` is pinned to, or ``None``: no
    workspace, one of another org, or no pin. A workspace of any layout may be
    pinned (Main included); the id is only a claim until
    :func:`pinned_machine` finds it in the org."""
    if not workspace_id:
        return None
    try:
        key = workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
    except ValueError:
        return None
    raw = (
        await db.execute(
            select(WorkspaceObject.spec["machine_pin"].astext).where(
                WorkspaceObject.id == key,
                WorkspaceObject.org_team_id == org_id,
                WorkspaceObject.type == WORKSPACE_TYPE,
                WorkspaceObject.deleted_at == 0,
            )
        )
    ).scalar_one_or_none()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


async def pins_of(db: AsyncSession, chats: Sequence[WorkspaceObject]) -> dict[UUID, OrgMachine]:
    """The org machine each chat's workspace is pinned to, for the chats whose
    pin holds: a live org machine of the chat's own org whose audience holds
    the chat's owner. A chat absent from the answer has no pin that counts.

    Bounded by the chats asked about, never by an org's history: one read of
    their workspaces, one of the machines named, then two small reads per
    distinct machine and owner for the audience."""
    wanted: dict[UUID, list[WorkspaceObject]] = {}
    for chat in chats:
        workspace_id = chat_domain.chat_spec_of(chat).workspace_id
        try:
            key = UUID(str(workspace_id)) if workspace_id else None
        except ValueError:
            key = None
        if key is not None:
            wanted.setdefault(key, []).append(chat)
    if not wanted:
        return {}
    rows = await db.execute(
        select(
            WorkspaceObject.id,
            WorkspaceObject.org_team_id,
            WorkspaceObject.spec["machine_pin"].astext,
        ).where(
            WorkspaceObject.id.in_(list(wanted)),
            WorkspaceObject.type == WORKSPACE_TYPE,
            WorkspaceObject.deleted_at == 0,
        )
    )
    pinned_chats: dict[UUID, list[WorkspaceObject]] = {}
    for workspace_id, workspace_org, raw in rows.all():
        try:
            pin = UUID(raw) if raw else None
        except ValueError:
            pin = None
        if pin is None:
            continue
        for chat in wanted[workspace_id]:
            # A workspace and its chats share an org; a chat claiming another
            # org's workspace is no claim at all.
            if chat.org_team_id == workspace_org:
                pinned_chats.setdefault(pin, []).append(chat)
    if not pinned_chats:
        return {}
    machines = (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.id.in_(list(pinned_chats)), OrgMachine.deleted_at.is_(None)
            )
        )
    ).scalars()
    found: dict[UUID, OrgMachine] = {}
    teams: dict[tuple[UUID, UUID], set[UUID]] = {}
    for machine in machines:
        grants = await audience_of(db, machine)
        for chat in pinned_chats[machine.id]:
            if chat.org_team_id != machine.org_team_id:
                continue
            seat = (chat.owner_user_id, machine.org_team_id)
            if seat not in teams:
                teams[seat] = await team_ids_of(
                    db, user_id=chat.owner_user_id, org_id=machine.org_team_id
                )
            if in_audience(machine, grants, user_id=chat.owner_user_id, user_team_ids=teams[seat]):
                found[chat.id] = machine
    return found


__all__ = [
    "PIN_ACTIONS",
    "POOL_USE_MODE",
    "WAKEABLE_STOP_REASONS",
    "PinAction",
    "choose_org_pool",
    "org_machines_of",
    "org_pool_applies",
    "org_pool_machines",
    "pin_action",
    "pinned_machine",
    "pins_of",
    "shared_pool_fallback",
    "workspace_pin",
]
