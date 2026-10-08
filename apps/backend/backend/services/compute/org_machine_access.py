"""Who may do what to an org machine, and the rows every decision reads.

Every org machine service is handed facts the route already decided on through
the ``org_machine`` policy; the services resolve those facts
(:func:`machine_attrs`, :func:`~backend.services.compute.org_machine_buying.plan_purchase`)
and then perform the write. Nothing here calls a provider: power changes are
intent on the org machine (``desired_power``) and the allocation, converged by
the reconcile, which a write nudges.

Tenant safety is by construction. The org always comes from the acting
context, never from a body; every read names it in SQL (an org machine's
``org_team_id`` and its allocation's immutable ``tenant_org_id``), so an id
from another org finds nothing, and a missing row and a foreign one answer the
same not-found.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.authz import ActingContext, Action, Resource, ResourceType, authorize
from alkera_core.authz.roles import RoleResolver, TeamRoles
from alkera_core.compute.org_machines import announce as announce_org_machine
from alkera_core.compute.org_machines import (
    in_audience,
)
from alkera_core.compute.provider import provider_traits
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.models import (
    ComputeAllocation,
    ComputeMachineType,
    User,
)
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import (
    NAME_TAKEN_CODE,
    NAME_TAKEN_MESSAGE,
    OrgMachine,
    OrgMachineAudience,
)
from alkera_core.verification import is_verified
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute import placement

#: The org machine states a replace is offered in: the old disk is gone or
#: was never made, so a fresh machine of the same offering is the way forward.
REPLACEABLE_STATES = frozenset({"failed", "waiting_for_hardware", "stopped"})

#: The audit actions this service writes.
PURCHASED = "machine.purchased"
RENAMED = "machine.renamed"
SETTINGS_CHANGED = "machine.settings_changed"
AUDIENCE_CHANGED = "machine.audience_changed"
STARTED = "machine.started"
STOPPED = "machine.stopped"
REPLACED = "machine.replaced"
DISK_GROWN = "machine.disk_grown"
DELETED = "machine.deleted"
OWNER_MOVED = "machine.owner_moved"
COMPUTE_SETTINGS_CHANGED = "org.compute_settings_changed"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class OrgMachineError(Exception):
    """A refusal the route answers with ``status`` and ``{code, message}``."""

    status = 409

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if status is not None:
            self.status = status


def name_taken() -> OrgMachineError:
    return OrgMachineError(NAME_TAKEN_CODE, NAME_TAKEN_MESSAGE)


def offering_unavailable() -> OrgMachineError:
    return OrgMachineError("offering_unavailable", "This machine can't be bought right now.")


# --------------------------------------------------------------------------- #
# who is asking
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Viewer:
    """The caller as every decision about an org machine sees them: their
    roles (resolved once per team), their teams in the org, whether they are
    an org admin and whether their address is proven."""

    ctx: ActingContext
    user: User
    resolver: RoleResolver
    team_ids: set[UUID]
    is_org_admin: bool
    email_verified: bool
    _roles: dict[UUID, TeamRoles] = field(default_factory=dict)

    @property
    def org_id(self) -> UUID:
        return self.ctx.org_id

    async def roles_on(self, team_id: UUID) -> TeamRoles:
        if team_id not in self._roles:
            self._roles[team_id] = await self.resolver.for_team(team_id)
        return self._roles[team_id]


async def viewer_for(
    db: AsyncSession, *, ctx: ActingContext, user: User, resolver: RoleResolver
) -> Viewer:
    org = await resolver.for_team(ctx.org_id)
    return Viewer(
        ctx=ctx,
        user=user,
        resolver=resolver,
        team_ids=await placement.team_ids_of(db, user_id=user.id, org_id=ctx.org_id),
        is_org_admin=org.in_org and org.is_admin,
        email_verified=is_verified(user),
        _roles={ctx.org_id: org},
    )


async def machine_attrs(
    viewer: Viewer,
    machine: OrgMachine,
    grants: Sequence[OrgMachineAudience],
    **question: object,
) -> dict[str, object]:
    """The facts the ``org_machine`` policy decides about ``machine`` from,
    plus the question (``purpose`` or ``operation`` and what it needs)."""
    owner = await viewer.roles_on(machine.owner_team_id)
    return {
        "in_org": owner.in_org and machine.org_team_id == viewer.org_id,
        "roles": owner.roles,
        "is_org_admin": viewer.is_org_admin,
        "in_audience": in_audience(
            machine, grants, user_id=viewer.user.id, user_team_ids=viewer.team_ids
        ),
        "use_mode": machine.use_mode,
        "email_verified": viewer.email_verified,
        **question,
    }


def resource_for(machine: OrgMachine | None, *, viewer: Viewer, id: str) -> Resource:
    """The resource a decision is about: the machine in its own org, or for an
    id that found nothing, the id in the caller's org (a not-found either way,
    and on record)."""
    if machine is None:
        return Resource(ResourceType.ORG_MACHINE, id=id or "unknown", org_id=viewer.org_id)
    return Resource(
        ResourceType.ORG_MACHINE,
        id=str(machine.id),
        org_id=machine.org_team_id,
        team_id=machine.owner_team_id,
    )


def allowed(
    viewer: Viewer, action: Action, machine: OrgMachine, attrs: Mapping[str, object]
) -> bool:
    """The policy's answer without a decision row: for the per-row flags of a
    read the route already decided on (``can_manage``, ``can_use``, spend)."""
    return authorize(viewer.ctx, action, resource_for(machine, viewer=viewer, id=""), attrs).allowed


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MachineRow:
    machine: OrgMachine
    allocation: ComputeAllocation | None
    offering: ComputeOffering
    machine_type: ComputeMachineType


def moves_to_new_hardware(row: MachineRow) -> bool:
    """Whether a replace can put the machine on fresh hardware: its provider
    starts machines from the catalog. A host the org attached has no other
    hardware to move to."""
    return provider_traits(row.machine_type.provider).catalog_provisioned


def _rows_stmt(org_id: UUID) -> Any:
    return (
        select(OrgMachine, ComputeAllocation, ComputeOffering, ComputeMachineType)
        .join(ComputeOffering, ComputeOffering.id == OrgMachine.offering_id)
        .join(ComputeMachineType, ComputeMachineType.id == ComputeOffering.machine_type_id)
        .outerjoin(
            ComputeAllocation,
            (ComputeAllocation.id == OrgMachine.current_allocation_id)
            & (ComputeAllocation.tenant_org_id == OrgMachine.org_team_id),
        )
        .where(OrgMachine.org_team_id == org_id, OrgMachine.deleted_at.is_(None))
    )


async def load_rows(db: AsyncSession, *, org_id: UUID) -> list[MachineRow]:
    """Every live org machine of the org with what it runs on, in one read."""
    rows = await db.execute(_rows_stmt(org_id).order_by(OrgMachine.created_at.asc()))
    return [MachineRow(*row) for row in rows.all()]


async def load_row(
    db: AsyncSession, *, org_id: UUID, machine_id: UUID | str, lock: bool = False
) -> MachineRow | None:
    """One live org machine of the org, or ``None`` (missing, deleted or
    another org's: the same answer). ``lock`` takes the machine's row lock
    for a write."""
    try:
        key = machine_id if isinstance(machine_id, UUID) else UUID(str(machine_id))
    except ValueError:
        return None
    stmt = _rows_stmt(org_id).where(OrgMachine.id == key)
    if lock:
        row = (await lock_rows(db, LockRank.ORG_MACHINE, stmt, of=OrgMachine)).first()
    else:
        row = (await db.execute(stmt)).first()
    return MachineRow(*row) if row is not None else None


async def audiences_of(
    db: AsyncSession, *, org_id: UUID, machine_ids: Iterable[UUID]
) -> dict[UUID, list[OrgMachineAudience]]:
    ids = list(machine_ids)
    found: dict[UUID, list[OrgMachineAudience]] = {machine_id: [] for machine_id in ids}
    if not ids:
        return found
    rows = await db.execute(
        select(OrgMachineAudience).where(
            OrgMachineAudience.org_team_id == org_id,
            OrgMachineAudience.org_machine_id.in_(ids),
        )
    )
    for grant in rows.scalars():
        found[grant.org_machine_id].append(grant)
    return found


def uuid_or_none(raw: str | None) -> UUID | None:
    """``raw`` as a UUID, or ``None`` for nothing or a malformed id."""
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def bump_version(machine: OrgMachine) -> None:
    """A change other readers must see: the version their If-Match names moves."""
    machine.version += 1
    machine.updated_at = datetime.now(UTC)


# --------------------------------------------------------------------------- #
# telling the world
# --------------------------------------------------------------------------- #


async def announce(
    db: AsyncSession, machine: OrgMachine, *, actor: Mapping[str, Any] | None
) -> None:
    """The org machine's frame: its state, the starting step and why it is
    off. Never a price."""
    alloc = (
        await db.get(ComputeAllocation, machine.current_allocation_id)
        if machine.current_allocation_id is not None
        else None
    )
    await announce_org_machine(db, machine, alloc, actor=actor)


__all__ = [
    "AUDIENCE_CHANGED",
    "COMPUTE_SETTINGS_CHANGED",
    "DELETED",
    "OWNER_MOVED",
    "PURCHASED",
    "RENAMED",
    "REPLACEABLE_STATES",
    "REPLACED",
    "SETTINGS_CHANGED",
    "STARTED",
    "STOPPED",
    "MachineRow",
    "OrgMachineError",
    "Viewer",
    "allowed",
    "announce",
    "audiences_of",
    "bump_version",
    "load_row",
    "load_rows",
    "machine_attrs",
    "name_taken",
    "offering_unavailable",
    "resource_for",
    "uuid_or_none",
    "viewer_for",
]
