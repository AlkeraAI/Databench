"""What an org machine reads as: the list, the platform console's list, one
machine's detail and timeline, and the money fields only its managers see.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.authz import ActingContext, Action
from alkera_core.authz.policies import org_machine as policy
from alkera_core.compute.meter import compute_metering, machine_runway
from alkera_core.compute.org_machines import (
    drain_stops_at,
    machine_card,
    org_machine_state,
)
from alkera_core.compute.org_reconcile import RetryWindow, allocation_history, capacity_wait
from alkera_core.compute.stock import no_hardware
from alkera_core.config import settings
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import (
    ComputeAllocation,
    ComputeAllocationEvent,
    Team,
    User,
    WorkspaceObject,
)
from alkera_core.models.compute import (
    ASLEEP,
    COMPUTE_TERMINAL_STATES,
)
from alkera_core.models.org_machines import (
    OrgMachine,
    OrgMachineAudience,
)
from alkera_core.schemas.org_machines import (
    AcquisitionLiteral,
    AudienceEntry,
    CreditState,
    DiskGrowRead,
    MachineCard,
    MachineTimelineEntry,
    MachineWaitRead,
    OrgMachineDetail,
    OrgMachineRead,
    WorkspaceRef,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute.org_admission import next_start_rates
from backend.services.compute.org_compute_settings import default_machine_id
from backend.services.compute.org_machine_access import (
    MachineRow,
    OrgMachineError,
    Viewer,
    allowed,
    audiences_of,
    load_rows,
    machine_attrs,
    moves_to_new_hardware,
)
from backend.services.compute.org_machine_disk import grow_offer
from backend.services.compute.ssh_machines import endpoint_read

# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #


async def _labels(
    db: AsyncSession,
    *,
    org_id: UUID,
    grants: Iterable[OrgMachineAudience],
    team_ids: Iterable[UUID],
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """Team names and people's names for every grant and owner team, in two
    reads."""
    wanted_teams = set(team_ids)
    wanted_users: set[UUID] = set()
    for grant in grants:
        if grant.team_id is not None:
            wanted_teams.add(grant.team_id)
        if grant.user_id is not None:
            wanted_users.add(grant.user_id)
    teams: dict[UUID, str] = {}
    users: dict[UUID, str] = {}
    if wanted_teams:
        rows = await db.execute(select(Team.id, Team.name).where(Team.id.in_(wanted_teams)))
        teams = {team_id: name for team_id, name in rows.all()}
    if wanted_users:
        rows = await db.execute(
            select(User.id, User.first_name, User.last_name, User.email).where(
                User.id.in_(wanted_users)
            )
        )
        users = {
            user_id: f"{first} {last}".strip() or email
            for user_id, first, last, email in rows.all()
        }
    return teams, users


def _entry(
    grant: OrgMachineAudience, teams: Mapping[UUID, str], users: Mapping[UUID, str]
) -> AudienceEntry:
    if grant.grantee_kind == "team" and grant.team_id is not None:
        return AudienceEntry(
            kind="team", team_id=str(grant.team_id), label=teams.get(grant.team_id, "")
        )
    if grant.grantee_kind == "user" and grant.user_id is not None:
        return AudienceEntry(
            kind="user", user_id=str(grant.user_id), label=users.get(grant.user_id, "")
        )
    return AudienceEntry(kind="org", label="Everyone in the organization")


async def spend_this_cycle(
    db: AsyncSession, *, org_id: UUID, machine_ids: Sequence[UUID], now: datetime | None = None
) -> dict[UUID, int]:
    """What each machine has been billed since the org's cycle began: compute
    and storage minutes, less refunds, as the compute biller recorded them."""
    return await compute_metering().spend_this_cycle(
        db, org_id=org_id, machine_ids=machine_ids, now=now
    )


async def runway(db: AsyncSession, machine: OrgMachine) -> tuple[int | None, CreditState]:
    """How long the credit behind the machine lasts at what the org's running
    machines burn, and the word for it, as the meter acts on it."""
    return await machine_runway(db, machine)


async def read_models(
    db: AsyncSession, viewer: Viewer, rows: Sequence[MachineRow], *, now: datetime | None = None
) -> list[OrgMachineRead]:
    """The read model of each row the caller may read, in order. Rows the
    caller may not read are left out: the policy decides each one, with no
    decision row per row (the list itself is on record)."""
    moment = now or datetime.now(UTC)
    grants = await audiences_of(db, org_id=viewer.org_id, machine_ids=[r.machine.id for r in rows])
    visible: list[tuple[MachineRow, bool, bool]] = []
    for row in rows:
        machine = row.machine
        mine = grants[machine.id]
        read = await machine_attrs(viewer, machine, mine, purpose=policy.READ_PURPOSE)
        if not allowed(viewer, Action.READ, machine, read):
            continue
        manage = allowed(
            viewer,
            Action.WRITE,
            machine,
            {
                **read,
                "operation": policy.MANAGE,
                "sets_pool": False,
                "org_allows_pool": False,
            },
        )
        use = allowed(viewer, Action.READ, machine, {**read, "purpose": policy.USE_PURPOSE})
        visible.append((row, manage, use))
    teams, users = await _labels(
        db,
        org_id=viewer.org_id,
        grants=[grant for row, _, _ in visible for grant in grants[row.machine.id]],
        team_ids=[row.machine.owner_team_id for row, _, _ in visible],
    )
    managed = [row.machine.id for row, manage, _ in visible if manage]
    org_default = await default_machine_id(db, org_id=viewer.org_id)
    spend = await spend_this_cycle(db, org_id=viewer.org_id, machine_ids=managed, now=moment)
    out: list[OrgMachineRead] = []
    for row, manage, use in visible:
        minutes: int | None = None
        state: CreditState = "ok"
        if manage:
            minutes, state = await runway(db, row.machine)
        out.append(
            _read_model(
                row,
                grants[row.machine.id],
                teams,
                users,
                manage=manage,
                use=use,
                spend=spend.get(row.machine.id) if manage else None,
                runway_minutes=minutes if manage else None,
                credit_state=state,
                drain_stops_at=await drain_stops_at(db, row.machine),
                card=await org_machine_card(
                    db, ctx=viewer.ctx, row=row, now=moment, include_rate=True
                ),
                org_default=row.machine.id == org_default,
                disk_grow=_disk_grow(row, now=moment) if manage else None,
            )
        )
    return out


def _disk_grow(row: MachineRow, *, now: datetime) -> DiskGrowRead | None:
    offer = grow_offer(row, now=now)
    return None if isinstance(offer, OrgMachineError) else offer.read()


async def org_machine_card(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    row: MachineRow,
    now: datetime,
    include_rate: bool,
) -> MachineCard:
    """An org machine's card for a reader acting as ``ctx``. While no session
    runs (stopped, or nothing behind it), its rate is the one the next start
    or wake pins, read through admission's own rule
    (:func:`~backend.services.compute.org_admission.next_start_rates`), so a
    negotiated grant shows and the card cannot disagree with the wake. A
    machine waiting for hardware says why and when the server asks again
    (:func:`~alkera_core.compute.org_reconcile.capacity_wait`, the schedule
    the reconcile acts on)."""
    alloc = row.allocation
    idle = alloc is None or alloc.state in COMPUTE_TERMINAL_STATES or alloc.state == ASLEEP
    next_rates = (
        await next_start_rates(
            db,
            ctx=ctx,
            org_machine=row.machine,
            offering=row.offering,
            machine_type=row.machine_type,
            now=now,
        )
        if include_rate and idle
        else None
    )
    return machine_card(
        row.machine,
        alloc,
        row.offering,
        row.machine_type,
        now=now,
        include_rate=include_rate,
        next_rates=next_rates,
        wait=await machine_wait(db, row, now=now),
    )


async def machine_wait(
    db: AsyncSession, row: MachineRow, *, now: datetime
) -> MachineWaitRead | None:
    """Why ``row`` waits for hardware and its retry schedule, or ``None``."""
    state, _ = org_machine_state(row.machine, row.allocation, now=now)
    if state != "waiting_for_hardware":
        return None
    history = await allocation_history(db, row.machine)
    wait = capacity_wait(row.allocation, history, window=RetryWindow.from_settings(settings))
    if wait is None:
        return None
    mt = row.machine_type
    return MachineWaitRead(
        reason=no_hardware(
            mt.provider,
            vcpu=mt.vcpu,
            gpu_name=mt.gpu_name if mt.gpu_count > 0 else "",
        ),
        next_try_at=wait.next_try_at,
        gives_up_at=wait.gives_up_at,
    )


def _acquisition(value: str) -> AcquisitionLiteral:
    if value == "granted":
        return "granted"
    if value == "added":
        return "added"
    return "purchased"


def _read_model(
    row: MachineRow,
    grants: Sequence[OrgMachineAudience],
    teams: Mapping[UUID, str],
    users: Mapping[UUID, str],
    *,
    manage: bool,
    use: bool,
    spend: int | None,
    runway_minutes: int | None,
    credit_state: CreditState,
    drain_stops_at: datetime | None,
    card: MachineCard,
    org_default: bool = False,
    disk_grow: DiskGrowRead | None = None,
) -> OrgMachineRead:
    """One org machine's read model. Every surface that lists org machines
    builds it here, so the card, the audience words and the money fields
    read the same everywhere."""
    machine = row.machine
    return OrgMachineRead(
        id=str(machine.id),
        name=machine.name,
        card=card,
        use_mode="pool" if machine.use_mode == "pool" else "assigned",
        acquisition=_acquisition(machine.acquisition),
        free_until=machine.free_until,
        audience=[_entry(grant, teams, users) for grant in grants],
        idle_stop_minutes=machine.idle_stop_minutes,
        monthly_cap_nanos=machine.monthly_cap_nanos,
        owner_team_id=str(machine.owner_team_id),
        owner_team_name=teams.get(machine.owner_team_id, ""),
        version=machine.version,
        can_manage=manage,
        can_use=use,
        can_replace=manage and moves_to_new_hardware(row),
        org_default=org_default,
        spend_this_cycle_nanos=spend,
        credit_state=credit_state,
        runway_minutes=runway_minutes,
        drain_stops_at=drain_stops_at,
        created_at=machine.created_at,
        disk_grow=disk_grow,
    )


async def platform_read_models(
    db: AsyncSession, *, org_id: UUID, machine_ids: Iterable[UUID] | None = None
) -> list[OrgMachineRead]:
    """The org's live machines as the platform console reads them: the
    customer's rate on the card, never what a machine costs us, and staff
    neither manage nor use them. ``machine_ids`` narrows the list."""
    moment = datetime.now(UTC)
    rows = await load_rows(db, org_id=org_id)
    if machine_ids is not None:
        wanted = set(machine_ids)
        rows = [row for row in rows if row.machine.id in wanted]
    grants = await audiences_of(db, org_id=org_id, machine_ids=[r.machine.id for r in rows])
    teams, users = await _labels(
        db,
        org_id=org_id,
        grants=[grant for row in rows for grant in grants[row.machine.id]],
        team_ids=[row.machine.owner_team_id for row in rows],
    )
    out: list[OrgMachineRead] = []
    for row in rows:
        _, state = await runway(db, row.machine)
        out.append(
            _read_model(
                row,
                grants[row.machine.id],
                teams,
                users,
                manage=False,
                use=False,
                spend=None,
                runway_minutes=None,
                credit_state=state,
                drain_stops_at=await drain_stops_at(db, row.machine),
                # The console reads the offering's rate for a stopped machine:
                # a negotiated grant is resolved for the org's own people.
                card=machine_card(
                    row.machine,
                    row.allocation,
                    row.offering,
                    row.machine_type,
                    now=moment,
                    include_rate=True,
                ),
            )
        )
    return out


def timeline_words(
    from_state: str, to_state: str, reason: str, *, idle_stop_minutes: int | None
) -> str | None:
    """What one allocation edge means to the people who hold the machine, or
    ``None`` for an edge that says nothing they act on."""
    said = (reason or "").lower()
    if to_state == "provisioning" and from_state == "pending":
        return "Starting"
    if to_state == "ready" and from_state in ("bootstrapping", "asleep", "provisioning"):
        return "Started"
    if to_state == ASLEEP:
        if "idle" in said:
            if idle_stop_minutes:
                return f"Stopped: idle for {idle_stop_minutes} minutes"
            return "Stopped: idle"
        if "credit" in said:
            return "Stopped: out of credits"
        if "cap" in said:
            return "Stopped: monthly cap reached"
        if "free_expired" in said:
            return "Stopped: free period ended"
        if "move" in said:
            return "Stopped: workspace moved"
        return "Stopped"
    if to_state == "failed":
        if "capacity" in said:
            return "Couldn't start: no hardware available"
        if "boot" in said:
            return "Couldn't start: it didn't finish starting"
        # The launch records the provider's own words as the edge's reason.
        return (
            f"Couldn't start: {reason.strip()}" if reason and reason.strip() else "Couldn't start"
        )
    if to_state == "released":
        if "replaced" in said:
            return "Replaced with new hardware"
        return "Released"
    if to_state == "lost":
        return "Lost by the provider"
    return None


async def detail(
    db: AsyncSession, viewer: Viewer, row: MachineRow, *, now: datetime | None = None
) -> OrgMachineDetail:
    """A machine's read model, the workspaces pinned to it and its timeline."""
    [read] = await read_models(db, viewer, [row], now=now)
    machine = row.machine
    pin = WorkspaceObject.spec["machine_pin"].astext
    workspaces = (
        await db.execute(
            select(WorkspaceObject.id, WorkspaceObject.title)
            .where(
                WorkspaceObject.org_team_id == machine.org_team_id,
                WorkspaceObject.type == WORKSPACE_TYPE,
                WorkspaceObject.deleted_at == 0,
                pin == str(machine.id),
            )
            .order_by(WorkspaceObject.title)
        )
    ).all()
    events = (
        await db.execute(
            select(ComputeAllocationEvent)
            .join(ComputeAllocation, ComputeAllocation.id == ComputeAllocationEvent.allocation_id)
            .where(
                ComputeAllocation.org_machine_id == machine.id,
                ComputeAllocation.tenant_org_id == machine.org_team_id,
            )
            .order_by(ComputeAllocationEvent.at.asc())
        )
    ).scalars()
    timeline = [
        MachineTimelineEntry(at=event.at, words=words)
        for event in events
        if (
            words := timeline_words(
                event.from_state,
                event.to_state,
                event.reason,
                idle_stop_minutes=machine.idle_stop_minutes,
            )
        )
        is not None
    ]
    return OrgMachineDetail(
        **read.model_dump(),
        workspaces=[WorkspaceRef(id=str(ws_id), name=title) for ws_id, title in workspaces],
        timeline=timeline,
        ssh=await endpoint_read(db, machine) if read.can_manage else None,
    )


__all__ = [
    "detail",
    "platform_read_models",
    "read_models",
    "runway",
    "spend_this_cycle",
    "timeline_words",
]
