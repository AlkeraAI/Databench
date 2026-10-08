"""An org machine's life once bought: changing it, starting, stopping,
replacing and deleting it, waking it for a chat, and what happens to it when
people, teams and orgs go away.

Reads live in :mod:`~backend.services.compute.org_machine_reads`, buying in
:mod:`~backend.services.compute.org_machine_buying`, and who may do what in
:mod:`~backend.services.compute.org_machine_access`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute.org_machines import (
    new_allocation_for,
    org_machine_state,
    request_delete,
    request_power,
)
from alkera_core.config import settings
from alkera_core.events.actor import actor_system
from alkera_core.logging import get_logger
from alkera_core.models import (
    ComputeAllocation,
)
from alkera_core.models.compute import (
    COMPUTE_TERMINAL_STATES,
    DRAIN_USER,
)
from alkera_core.models.org_machines import (
    OrgMachine,
    OrgMachineAudience,
)
from alkera_core.schemas.org_machines import (
    AudienceGrant,
    MachineQuote,
    OrgMachineUpdate,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import record_org_audit
from backend.services.compute import grants, org_admission, provisioning
from backend.services.compute.org_machine_access import (
    AUDIENCE_CHANGED,
    DELETED,
    DISK_GROWN,
    OWNER_MOVED,
    RENAMED,
    REPLACEABLE_STATES,
    REPLACED,
    SETTINGS_CHANGED,
    STARTED,
    STOPPED,
    MachineRow,
    OrgMachineError,
    Viewer,
    announce,
    audiences_of,
    bump_version,
    load_row,
    moves_to_new_hardware,
    name_taken,
    uuid_or_none,
)
from backend.services.compute.org_machine_buying import (
    admission_quote,
    admit_start,
    name_is_taken,
    validate_audience,
)
from backend.services.compute.org_machine_disk import check_grow
from backend.services.compute.ssh_machines import forget_unlaunched
from backend.services.org import (
    MemberLeft,
    OrgDeleted,
    TeamDeleted,
    on_member_left,
    on_org_deleted,
    on_team_deleted,
)

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# changing
# --------------------------------------------------------------------------- #


async def update(
    db: AsyncSession, viewer: Viewer, machine: OrgMachine, body: OrgMachineUpdate
) -> OrgMachine:
    """Rename, idle stop, monthly cap and use mode; fields not sent are left
    alone. Each kind of change is its own audit entry."""
    sent = body.model_fields_set
    changed: dict[str, Any] = {}
    if "name" in sent and body.name is not None and body.name != machine.name:
        name = body.name
        if await name_is_taken(db, org_id=machine.org_team_id, name=name, except_id=machine.id):
            raise name_taken()
        previous = machine.name
        machine.name = name
        await record_org_audit(
            db,
            org_id=machine.org_team_id,
            actor=viewer.user,
            action=RENAMED,
            target=str(machine.id),
            detail={"from": previous, "to": name},
            acting=viewer.ctx,
        )
    if "idle_stop_minutes" in sent and body.idle_stop_minutes != machine.idle_stop_minutes:
        if body.idle_stop_minutes is not None and body.idle_stop_minutes < 5:
            raise OrgMachineError(
                "idle_stop_too_short", "Stop when idle takes 5 minutes or more.", status=422
            )
        changed["idle_stop_minutes"] = body.idle_stop_minutes
        machine.idle_stop_minutes = body.idle_stop_minutes
    if "monthly_cap_nanos" in sent and body.monthly_cap_nanos != machine.monthly_cap_nanos:
        if body.monthly_cap_nanos is not None and body.monthly_cap_nanos < 0:
            raise OrgMachineError("cap_negative", "A monthly cap can't be negative.", status=422)
        changed["monthly_cap_nanos"] = body.monthly_cap_nanos
        machine.monthly_cap_nanos = body.monthly_cap_nanos
    if "use_mode" in sent and body.use_mode is not None and body.use_mode != machine.use_mode:
        changed["use_mode"] = body.use_mode
        machine.use_mode = body.use_mode
    if changed:
        await record_org_audit(
            db,
            org_id=machine.org_team_id,
            actor=viewer.user,
            action=SETTINGS_CHANGED,
            target=str(machine.id),
            detail=changed,
            acting=viewer.ctx,
        )
    if changed or "name" in sent:
        bump_version(machine)
        await db.flush()
        await announce(db, machine, actor=viewer.ctx.audit_dict())
    return machine


async def replace_audience(
    db: AsyncSession, viewer: Viewer, machine: OrgMachine, audience: Sequence[AudienceGrant]
) -> OrgMachine:
    """Replace who may use the machine. Someone taken out of it keeps any
    workspace already pinned here: placement stops honouring the pin for
    them, and the workspace says so."""
    grants = await validate_audience(
        db, viewer, owner_team_id=machine.owner_team_id, audience=audience
    )
    existing = (await audiences_of(db, org_id=machine.org_team_id, machine_ids=[machine.id]))[
        machine.id
    ]
    for row in existing:
        await db.delete(row)
    await db.flush()
    for grant in grants:
        db.add(
            OrgMachineAudience(
                org_team_id=machine.org_team_id,
                org_machine_id=machine.id,
                grantee_kind=grant.kind,
                team_id=uuid_or_none(grant.team_id),
                user_id=uuid_or_none(grant.user_id),
                created_by=viewer.user.id,
            )
        )
    bump_version(machine)
    await db.flush()
    await record_org_audit(
        db,
        org_id=machine.org_team_id,
        actor=viewer.user,
        action=AUDIENCE_CHANGED,
        target=str(machine.id),
        detail={"audience": [grant.model_dump() for grant in grants]},
        acting=viewer.ctx,
    )
    await announce(db, machine, actor=viewer.ctx.audit_dict())
    return machine


async def start(db: AsyncSession, viewer: Viewer, row: MachineRow) -> OrgMachine:
    """Turn the machine on, once admission passes: a stopped one wakes, one
    whose provider machine failed or left gets a fresh one of the same
    offering. The reconcile does the starting."""
    machine = row.machine
    admission = await admit_start(db, ctx=viewer.ctx, row=row)
    alloc = row.allocation
    if alloc is None or alloc.state in COMPUTE_TERMINAL_STATES:
        await new_allocation_for(
            db,
            machine,
            offering=row.offering,
            machine_type=row.machine_type,
            user_id=viewer.user.id,
            admitted=admission.admitted,
        )
    await request_power(
        db,
        machine,
        desired="on",
        reason="user",
        drain_kind=None,
        deadline=None,
        actor=viewer.ctx.audit_dict(),
    )
    await record_org_audit(
        db,
        org_id=machine.org_team_id,
        actor=viewer.user,
        action=STARTED,
        target=str(machine.id),
        detail={},
        acting=viewer.ctx,
    )
    return machine


async def stop(
    db: AsyncSession, viewer: Viewer, machine: OrgMachine, *, now_: bool = False
) -> OrgMachine:
    """Turn the machine off, keeping its disk. Running turns get the move
    grace to finish unless ``now_``."""
    moment = datetime.now(UTC)
    deadline = moment if now_ else moment + timedelta(seconds=settings.move_turn_grace_seconds)
    await request_power(
        db,
        machine,
        desired="off",
        reason="user",
        drain_kind=DRAIN_USER,
        deadline=deadline,
        actor=viewer.ctx.audit_dict(),
    )
    await record_org_audit(
        db,
        org_id=machine.org_team_id,
        actor=viewer.user,
        action=STOPPED,
        target=str(machine.id),
        detail={"reason": "user", "now": now_},
        acting=viewer.ctx,
    )
    return machine


async def replace(db: AsyncSession, viewer: Viewer, row: MachineRow) -> OrgMachine:
    """Put the machine on fresh hardware of the same offering: its id, name,
    audience and pins are kept, its old disk is not. Offered only while it is
    failed, waiting for hardware or stopped."""
    machine = row.machine
    if not moves_to_new_hardware(row):
        raise OrgMachineError(
            "machine_not_replaceable", "This machine runs on your own host; it has no new hardware."
        )
    state, _ = org_machine_state(machine, row.allocation, now=datetime.now(UTC))
    if state not in REPLACEABLE_STATES:
        raise OrgMachineError(
            "machine_not_replaceable",
            "Only a stopped machine, or one that couldn't start, can move to new hardware.",
        )
    admission = await admit_start(db, ctx=viewer.ctx, row=row)
    # The old allocation is retired as the fresh one becomes current
    # (``new_allocation_for``).
    old = row.allocation
    await new_allocation_for(
        db,
        machine,
        offering=row.offering,
        machine_type=row.machine_type,
        user_id=viewer.user.id,
        admitted=admission.admitted,
    )
    await request_power(
        db,
        machine,
        desired="on",
        reason="user",
        drain_kind=None,
        deadline=None,
        actor=viewer.ctx.audit_dict(),
    )
    await record_org_audit(
        db,
        org_id=machine.org_team_id,
        actor=viewer.user,
        action=REPLACED,
        target=str(machine.id),
        detail={"previous_allocation_id": str(old.id) if old is not None else None},
        acting=viewer.ctx,
    )
    return machine


async def delete(db: AsyncSession, viewer: Viewer, machine: OrgMachine) -> None:
    """Delete the machine: it drains, its disk is destroyed by the reconcile,
    pinned workspaces return to the org default. An added machine that was
    never started forgets its credential now."""
    await forget_unlaunched(db, machine)
    await request_delete(db, machine, actor=viewer.ctx.audit_dict())
    await record_org_audit(
        db,
        org_id=machine.org_team_id,
        actor=viewer.user,
        action=DELETED,
        target=str(machine.id),
        detail={"name": machine.name},
        acting=viewer.ctx,
    )


# --------------------------------------------------------------------------- #
# a message wakes a stopped machine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChatMachineWake:
    """What a chat's wake did to its stopped box: whether the box was started,
    and the admission refusal that left it stopped, if one did."""

    started: bool
    refused: grants.ComputeRefusedError | None = None


async def wake_for_chat(
    db: AsyncSession, machine_id: UUID, *, ctx: ActingContext
) -> ChatMachineWake:
    """A message reached a chat bound to a stopped box: start it. For an org
    machine, admission first (a refusal leaves it stopped, and the chat reads
    the machine's state) and the org's intent turned on, so the reconcile does
    not stop it again; then the box is started like any sleeping box."""
    alloc = await db.get(ComputeAllocation, machine_id)
    if alloc is not None and alloc.org_machine_id is not None and alloc.tenant_org_id is not None:
        # The org machine first, then its allocation: the order the reconcile
        # and the stop route take them in, so a wake never holds the
        # allocation while it waits for the machine they hold.
        row = await load_row(
            db, org_id=alloc.tenant_org_id, machine_id=alloc.org_machine_id, lock=True
        )
        if row is None or row.allocation is None or row.allocation.id != alloc.id:
            return ChatMachineWake(started=False)
        try:
            await admit_start(db, ctx=ctx, row=row)
        except grants.ComputeRefusedError as refused:
            log.info(
                "compute.org_machine.wake_refused",
                org_machine_id=str(row.machine.id),
                code=refused.code,
            )
            return ChatMachineWake(started=False, refused=refused)
        if row.machine.desired_power != "on":
            await request_power(
                db,
                row.machine,
                desired="on",
                reason="chat",
                drain_kind=None,
                deadline=None,
                actor=ctx.audit_dict(),
            )
    return ChatMachineWake(started=await provisioning.wake_for_chat(db, machine_id, ctx=ctx))


# --------------------------------------------------------------------------- #
# people, teams and orgs going away
# --------------------------------------------------------------------------- #


async def forget_member(db: AsyncSession, *, org_id: UUID, user_id: UUID) -> int:
    """Someone left the org (removed or deactivated): every grant naming them
    on the org's machines goes. Their pinned workspaces keep the pin, which
    placement no longer honours for them. Returns how many grants went."""
    rows = (
        (
            await db.execute(
                select(OrgMachineAudience).where(
                    OrgMachineAudience.org_team_id == org_id,
                    OrgMachineAudience.grantee_kind == "user",
                    OrgMachineAudience.user_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await db.delete(row)
    if rows:
        await db.flush()
    return len(rows)


async def hand_machines_up(
    db: AsyncSession, *, org_id: UUID, team_id: UUID, parent_team_id: UUID
) -> list[OrgMachine]:
    """A team is being deleted: the machines it holds move to its parent, so
    their managers become that team's admins. Each move is on the org's
    record. Grants naming the team go with it (the foreign key cascades)."""
    machines = (
        (
            await db.execute(
                select(OrgMachine).where(
                    OrgMachine.org_team_id == org_id,
                    OrgMachine.owner_team_id == team_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for machine in machines:
        machine.owner_team_id = parent_team_id
        bump_version(machine)
        await record_org_audit(
            db,
            org_id=org_id,
            actor=None,
            action=OWNER_MOVED,
            target=str(machine.id),
            detail={"from_team_id": str(team_id), "to_team_id": str(parent_team_id)},
        )
    if machines:
        await db.flush()
    return list(machines)


async def delete_all_for_org(
    db: AsyncSession, *, org_id: UUID, actor: Mapping[str, Any] | None = None
) -> list[OrgMachine]:
    """The org is being deleted: every machine it holds is deleted first, so
    the reconcile releases each provider machine and its disk."""
    machines = (
        (
            await db.execute(
                select(OrgMachine).where(
                    OrgMachine.org_team_id == org_id, OrgMachine.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    who = {
        **(dict(actor) if actor else actor_system("org.delete")),
        "terminated_reason": "org_deleted",
    }
    for machine in machines:
        await request_delete(db, machine, actor=who)
    if machines:
        await db.flush()
    return list(machines)


@on_member_left("org_machines.audience")
async def _member_left(db: AsyncSession, event: MemberLeft) -> None:
    await forget_member(db, org_id=event.org_id, user_id=event.user_id)


@on_team_deleted("org_machines.owner")
async def _team_deleted(db: AsyncSession, event: TeamDeleted) -> None:
    await hand_machines_up(
        db, org_id=event.org_id, team_id=event.team_id, parent_team_id=event.parent_team_id
    )


@on_org_deleted("org_machines.machines")
async def _org_deleted(db: AsyncSession, event: OrgDeleted) -> None:
    """Terminate every provider machine holding the org's data before its rows
    cascade away (a reconcile that would have released them later finds no
    rows), then delete the machines on record. A provider that does not
    confirm a machine gone raises, and the org is not purged."""
    await provisioning.release_org_machines(db, event.org_id)
    await delete_all_for_org(db, org_id=event.org_id, actor=event.actor)


async def grow_disk(
    db: AsyncSession, viewer: Viewer, row: MachineRow, volume_gb: int
) -> OrgMachine:
    """Ask for a bigger disk. Only larger, only to a size the offering sells,
    only where the provider grows a volume, and only once admission passes at
    the new size (the disk's price is part of what a start must carry). The
    reconcile does the growing: in place where the provider and the box grow
    a volume online (EC2); else a running machine is drained (its turns
    finish), stopped, grown and started again, and a stopped one stays
    stopped (RunPod)."""
    machine = row.machine
    check_grow(row, volume_gb, now=datetime.now(UTC))
    await org_admission.admit_org_machine(
        db,
        ctx=viewer.ctx,
        org_id=machine.org_team_id,
        owner_team_id=machine.owner_team_id,
        offering=row.offering,
        machine_type=row.machine_type,
        storage_gb=volume_gb,
        org_machine=machine,
    )
    grown_from = machine.storage_gb
    machine.storage_gb = volume_gb
    bump_version(machine)
    await record_org_audit(
        db,
        org_id=machine.org_team_id,
        actor=viewer.user,
        action=DISK_GROWN,
        target=str(machine.id),
        detail={"from_gb": grown_from, "to_gb": volume_gb},
        acting=viewer.ctx,
    )
    await announce(db, machine, actor=viewer.ctx.audit_dict())
    return machine


async def quote_disk_grow(
    db: AsyncSession, viewer: Viewer, row: MachineRow, volume_gb: int
) -> MachineQuote:
    """What the machine would cost with its disk at ``volume_gb``, and whether
    the grow would be admitted now: the grow's own refusals first, then
    admission's verdict at the new size. Nothing changes."""
    check_grow(row, volume_gb, now=datetime.now(UTC))
    return await admission_quote(
        db,
        viewer.ctx,
        org_id=row.machine.org_team_id,
        owner_team_id=row.machine.owner_team_id,
        offering=row.offering,
        machine_type=row.machine_type,
        storage_gb=volume_gb,
        org_machine=row.machine,
    )


__all__ = [
    "ChatMachineWake",
    "delete",
    "delete_all_for_org",
    "forget_member",
    "grow_disk",
    "hand_machines_up",
    "quote_disk_grow",
    "replace",
    "replace_audience",
    "start",
    "stop",
    "update",
    "wake_for_chat",
]
