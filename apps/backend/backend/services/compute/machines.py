"""The org's workspace machine: register a running pod, heartbeat it, read it.

A workspace machine is the long-lived box a customer's chats bind to. It is
registered by the daemon that runs on it (an agent acting for the customer's
user), under the same admission as any other start — the grant's ceiling
counts it, its billed rate and true cost are pinned from the grant and the
catalog — but with no lease ceiling and with a heartbeat that decides its
reachability. The plane did not create the box, so the row is ``registered``
rather than ``provisioned``: the provider is never asked whether it lives (it
never made the pod and cannot know), and the meter keeps it on the plane while
its heartbeat is fresh and reaps it once the heartbeat lapses past the ready
window. Register is idempotent by provider pod id: the daemon coming back
after a restart gets the same row, not a second machine — and so does the box
whose row was reaped while it was quiet, which takes that row back (re-admitted
and re-priced) rather than leaving every chat bound to it with nowhere to run.

Every reachability transition is a ``compute_machine.changed`` frame:
``starting`` at register, ``ready`` on the first heartbeat, ``unreachable`` from
the sweep when heartbeats stop, ``ready`` again when they resume.

A box coming up is also a placement moment. A chat opened while the org had no
machine holds a question nobody will answer — no box lists a chat bound to
nothing, and placement otherwise runs only on the chat's next message — so the
register and the heartbeat that makes a box ready both bind the org's stranded
chats to it (:func:`placement.bind_stranded_chats`), inside the same commit as
the frame.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.box_contract import (
    BOX_TOO_OLD,
    BOX_TOO_OLD_MESSAGE,
    BoxCapability,
    admit_claim,
)
from alkera_core.compute.events import announce_machine
from alkera_core.compute.machines import (
    ASLEEP,
    NONE,
    READY,
    WORKSPACE,
    machine_state,
    machine_status,
)
from alkera_core.compute.org_machines import free_machine_name, unlisted_offering_for
from alkera_core.compute.provider import provider_traits
from alkera_core.compute.transitions import is_legal, revive, transition
from alkera_core.compute.unservable import (
    is_unhealthy,
    record_fault,
    record_isolation,
    record_silence,
)
from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.models import MachineCredential, User, WorkspaceObject
from alkera_core.models.compute import (
    BOOTSTRAPPING,
    COMPUTE_ACTIVE_STATES,
    DRAIN_RESTART,
    DRAINING,
    NO_SANDBOX_MODE,
    ORG_TENANCY,
    PERSONAL_TENANCY,
    POOL_TENANCY,
    PROVISIONED,
    PROVISIONING,
    REGISTERED,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.compute import READY as READY_STATE
from alkera_core.models.org_machines import (
    ADDED,
    POWER_ON,
    STOP_NONE,
    USE_POOL,
    OrgMachine,
)
from alkera_core.schemas.compute import (
    BoxMachineCard,
    MachineRead,
    MachineStateRead,
)
from alkera_core.schemas.org_machines import GpuSpec
from alkera_core.status import placement_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute import grants, placement, provisioning
from backend.services.compute.service import machine_type_info
from backend.services.credentials import machine_credentials as machine_credential_service


async def find_registered(
    db: AsyncSession,
    *,
    org_id: UUID,
    provider: str,
    provider_pod_id: str,
    tenancy: str | None = None,
) -> ComputeAllocation | None:
    """The org's workspace row for this pod, live or not, if it was registered
    before — the live one when the pod has one, the most recent otherwise.

    The dead rows are deliberately in scope. The pod id is the box's identity,
    and a box only asks to register again because the row it had stopped
    answering for it: the meter reaped a pod the provider no longer knows, an
    operator released it. Matching only the live rows opens a SECOND row for
    the same box every time that happens, and every chat bound to the old id is
    stranded — no daemon serves a chat bound to a machine it did not register
    as.

    ``tenancy`` narrows to one tenancy. The org's own register passes
    ``org``: a platform box's row is claimed with its machine credential and
    never through that door, or a box whose credential was revoked could take
    its row — and every chat bound to it — back by registering its pod id.
    """
    stmt = (
        select(ComputeAllocation)
        .join(ComputeMachineType, ComputeMachineType.id == ComputeAllocation.machine_type_id)
        .where(
            ComputeAllocation.org_team_id == org_id,
            ComputeAllocation.lifecycle == WORKSPACE,
            ComputeAllocation.provider_machine_id == provider_pod_id,
            ComputeMachineType.provider == provider,
        )
    )
    if tenancy is not None:
        stmt = stmt.where(ComputeAllocation.tenancy == tenancy)
    return (
        await db.execute(
            stmt.order_by(
                ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES).desc(),
                ComputeAllocation.created_at.desc(),
            ).limit(1)
        )
    ).scalar_one_or_none()


async def register_machine(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    machine_type: ComputeMachineType,
    provider_pod_id: str,
    name: str,
    daemon_instance_id: str | None = None,
    now: datetime | None = None,
) -> tuple[ComputeAllocation, bool]:
    """Register a running pod as the org's workspace machine. Returns the row
    and whether it was created by this call (``False`` = the pod was already
    registered and the existing row is returned unchanged).

    A new registration goes through :func:`grants.admit` like every other
    start — a ``ComputeRefusedError`` (402/429, already on record) is raised when
    the grant refuses — and pins the billed rate and the true cost. The row is
    born ``ready`` (the pod is live) with the meter anchored at registration and
    no heartbeat yet, so its reachability reads ``starting`` until the daemon's
    first heartbeat. Commits.
    """
    moment = now or datetime.now(UTC)
    org_id = ctx.org_id
    await advisory_xact_lock(db, advisory_key("compute-register", org_id))
    existing = await find_registered(
        db,
        org_id=org_id,
        provider=machine_type.provider,
        provider_pod_id=provider_pod_id,
        tenancy=ORG_TENANCY,
    )
    credential = ctx.credential_id or ""
    if existing is not None and existing.state in COMPUTE_ACTIVE_STATES:
        _pin_credential(existing, user=user, credential=credential)
        _adopt_instance(existing, daemon_instance_id)
        await hold_as_org_machine(db, existing, machine_type, user=user)
        await _resume_from_drain(db, existing, ctx=ctx)
        await db.commit()
        return existing, False
    grant = await grants.admit(
        db, ctx=ctx, user=user, org_team_id=org_id, machine_type=machine_type, at=moment
    )
    if existing is not None:
        await _revive(db, existing, machine_type=machine_type, grant=grant, moment=moment)
        await hold_as_org_machine(db, existing, machine_type, user=user)
        _pin_credential(existing, user=user, credential=credential)
        _adopt_instance(existing, daemon_instance_id)
        await announce_machine(db, existing, status="starting", reason=None, actor=ctx.audit_dict())
        await _bind_stranded(db, existing, ctx=ctx)
        await db.commit()
        return existing, False
    alloc = ComputeAllocation(
        user_id=user.id,
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        lifecycle=WORKSPACE,
        origin=REGISTERED,
        name=name,
        tenancy=ORG_TENANCY,
        state="ready",
        provider_machine_id=provider_pod_id,
        created_at=moment,
        ready_at=moment,
        last_metered_at=moment,
        grant_id=grant.source_grant_id,
        price_per_minute_nanos=grant.rate_per_minute_nanos,
        true_cost_per_minute_nanos=machine_type.provider_price_per_minute_nanos,
        registered_jti=credential,
        daemon_instance_id=daemon_instance_id,
    )
    db.add(alloc)
    await db.flush()
    await compute_funding().fund_allocation(db, alloc.id, grant.funding_account_id)
    await hold_as_org_machine(db, alloc, machine_type, user=user)
    await announce_machine(db, alloc, status="starting", reason=None, actor=ctx.audit_dict())
    await _bind_stranded(db, alloc, ctx=ctx)
    await db.commit()
    return alloc, True


async def register_platform_machine(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User | None,
    credential: MachineCredential,
    machine_type: ComputeMachineType,
    provider_pod_id: str,
    name: str,
    capacity: int,
    daemon_version: str,
    daemon_instance_id: str | None = None,
    sandbox: str = NO_SANDBOX_MODE,
    now: datetime | None = None,
) -> tuple[ComputeAllocation, bool]:
    """A platform box claims the machine its credential was minted for.

    What the box IS — its kind, size, tenancy and so its true cost — comes from
    the credential, never from the box; the box says which instance it is and
    what it holds. No grant admits it and nothing is billed: the platform runs
    it for its tenants, at the catalog's true cost on the row. Idempotent by
    pod id under the credential's operator org; a credential already claimed
    by a DIFFERENT pod is refused (``ValueError``) — a box cannot take over
    another box's standing by presenting its secret. Commits.

    ``user`` is the box user when the box signed in as a person; ``None`` when
    it claims on its own credential. The row's ``user_id`` then keeps whoever
    provisioning wrote (the admin who clicked Provision), or the admin who
    minted the credential for a row that does not exist yet — a record of who
    stood the box up, conferring nothing on the machine's own path.
    """
    moment = now or datetime.now(UTC)
    org_id = credential.org_team_id
    actor_email = user.email if user is not None else None
    await advisory_xact_lock(db, advisory_key("compute-register", org_id))
    existing = await find_registered(
        db, org_id=org_id, provider=machine_type.provider, provider_pod_id=provider_pod_id
    )
    if existing is None and credential.machine_id is not None:
        # A machine the plane provisioned: its credential was bound to its row
        # before the machine existed, so the first claim may arrive before the
        # row learned the provider's id for it.
        bound = await db.get(ComputeAllocation, credential.machine_id)
        if (
            bound is not None
            and bound.origin == PROVISIONED
            and bound.state in (PROVISIONING, BOOTSTRAPPING)
            and bound.provider_machine_id in ("", provider_pod_id)
        ):
            bound.provider_machine_id = provider_pod_id
            existing = bound
    if credential.machine_id is not None and (
        existing is None or credential.machine_id != existing.id
    ):
        raise ValueError("this machine credential was claimed by another box")
    admission = admit_claim(daemon_version)
    if not admission.admitted:
        await _end_box_too_old(db, existing)
        raise BoxTooOldError(daemon_version)
    jti = ctx.credential_id or ""
    if existing is not None and existing.state in (PROVISIONING, BOOTSTRAPPING):
        # The daemon on a machine the plane started has come up: the claim is
        # what makes it ready, whatever the provider said meanwhile.
        existing.registered_jti = jti
        existing.user_id = _stood_up_by(existing, user, credential)
        existing.capacity = capacity
        existing.daemon_version = daemon_version
        _adopt_instance(existing, daemon_instance_id)
        existing.sandbox = sandbox
        existing.last_metered_at = moment
        await machine_credential_service.claim(db, credential, existing)
        transition(
            db,
            existing,
            READY_STATE,
            reason="the daemon claimed the machine",
            actor={"kind": "box", "email": actor_email, "name": name or existing.name},
            now=moment,
        )
        await announce_machine(db, existing, status="starting", reason=None, actor=ctx.audit_dict())
        await db.commit()
        return existing, False
    if existing is not None and existing.state in COMPUTE_ACTIVE_STATES:
        existing.registered_jti = jti
        existing.user_id = _stood_up_by(existing, user, credential)
        existing.tenancy = credential.tenancy
        existing.capacity = capacity
        existing.daemon_version = daemon_version
        _adopt_instance(existing, daemon_instance_id)
        await machine_credential_service.claim(db, credential, existing)
        await _resume_from_drain(db, existing, ctx=ctx)
        await db.commit()
        return existing, False
    if existing is not None:
        if existing.origin == PROVISIONED:
            # A machine the plane started is finished once its row is: the
            # reconcile has terminated it or is terminating it, and its secret
            # and credential go with it. A box still holding that credential
            # must not bring the row back.
            raise ValueError("this machine was released and cannot be claimed again")
        existing.machine_type_id = machine_type.id
        existing.origin = REGISTERED
        revive(
            db,
            existing,
            reason="the daemon registered again",
            actor={"kind": "box", "email": actor_email, "name": name or existing.name},
            now=moment,
        )
        existing.error = ""
        existing.terminated_reason = ""
        existing.last_metered_at = moment
        existing.last_heartbeat_at = None
        existing.price_per_minute_nanos = 0
        existing.true_cost_per_minute_nanos = machine_type.provider_price_per_minute_nanos
        existing.registered_jti = jti
        existing.user_id = _stood_up_by(existing, user, credential)
        existing.tenancy = credential.tenancy
        existing.capacity = capacity
        existing.daemon_version = daemon_version
        _adopt_instance(existing, daemon_instance_id)
        existing.drain_kind = None
        await machine_credential_service.claim(db, credential, existing)
        await announce_machine(db, existing, status="starting", reason=None, actor=ctx.audit_dict())
        await _bind_stranded(db, existing, ctx=ctx)
        await db.commit()
        return existing, False
    alloc = ComputeAllocation(
        user_id=_stood_up_by(None, user, credential),
        org_team_id=org_id,
        machine_type_id=machine_type.id,
        lifecycle=WORKSPACE,
        origin=REGISTERED,
        name=name,
        tenancy=credential.tenancy,
        sandbox=sandbox,
        capacity=capacity,
        daemon_version=daemon_version,
        daemon_instance_id=daemon_instance_id,
        state="ready",
        provider_machine_id=provider_pod_id,
        created_at=moment,
        ready_at=moment,
        last_metered_at=moment,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=machine_type.provider_price_per_minute_nanos,
        registered_jti=jti,
    )
    db.add(alloc)
    await db.flush()
    await machine_credential_service.claim(db, credential, alloc)
    await announce_machine(db, alloc, status="starting", reason=None, actor=ctx.audit_dict())
    await _bind_stranded(db, alloc, ctx=ctx)
    await db.commit()
    return alloc, True


def _stood_up_by(
    existing: ComputeAllocation | None, user: User | None, credential: MachineCredential
) -> UUID:
    """Who stood the box up, for the row's ``user_id``: the box user when a
    person signed the box in, else whoever the row already names, else the
    admin who minted the credential. A credential whose minter is gone and
    whose row does not exist yet cannot be claimed on the credential alone.

    A personal box is its owner's, always: the row names the person the
    credential was minted for, whoever signed the box in and whatever the row
    said before, because that person is the only one whose chats it holds."""
    if credential.tenancy == PERSONAL_TENANCY:
        if credential.created_by is None:
            raise ValueError("this personal box credential names nobody it serves")
        return credential.created_by
    if user is not None:
        return user.id
    if existing is not None:
        return existing.user_id
    if credential.created_by is None:
        raise ValueError("this machine credential names nobody who stood the box up")
    return credential.created_by


async def _bind_stranded(db: AsyncSession, alloc: ComputeAllocation, *, ctx: ActingContext) -> None:
    """The chats that nothing serves go onto the box that just came up: the
    org's own for an org box, every org's that places here for a platform box."""
    await placement.bind_stranded_chats_platform(db, alloc=alloc, actor=ctx.audit_dict())


def _pin_credential(alloc: ComputeAllocation, *, user: User, credential: str) -> None:
    """The box that registers again with a fresh device token (a daemon
    restarted after a new ``alkera login``) is still the box: the row follows
    its operator's current credential. Only the operator's own registration
    moves it — a colleague naming the same pod id neither becomes the box nor
    unseats it."""
    if alloc.user_id == user.id:
        alloc.registered_jti = credential


async def _revive(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    machine_type: ComputeMachineType,
    grant: grants.Grant,
    moment: datetime,
) -> None:
    """Put a reaped row back on the plane as the box that just registered.

    Everything a fresh registration pins is re-pinned: the stretch the box was
    gone is not compute anyone used, so the meter is re-anchored at the return
    rather than left at the first boot (it bills from ``last_metered_at``), and
    the rate, the true cost and the funding account come from the admission
    this call just passed, never from the lapsed one the row was carrying. The
    heartbeat is cleared so the row reads ``starting`` until the daemon's first
    beat, exactly as a new registration does.
    """
    alloc.machine_type_id = machine_type.id
    alloc.origin = REGISTERED
    revive(db, alloc, reason="the daemon registered again", now=moment)
    alloc.error = ""
    alloc.terminated_reason = ""
    alloc.last_metered_at = moment
    alloc.last_heartbeat_at = None
    alloc.grant_id = grant.source_grant_id
    alloc.price_per_minute_nanos = grant.rate_per_minute_nanos
    alloc.true_cost_per_minute_nanos = machine_type.provider_price_per_minute_nanos
    alloc.drain_kind = None
    await compute_funding().fund_allocation(db, alloc.id, grant.funding_account_id)


async def _resume_from_drain(
    db: AsyncSession, alloc: ComputeAllocation, *, ctx: ActingContext
) -> None:
    """A registration on a draining row is a NEW process on the box: the one
    that drained has exited and this one is here to serve. Registration is the
    only thing that clears a drain — a heartbeat cannot, or a box could re-admit
    itself to placement on its way out — so the row goes back to placeable and
    the org's stranded chats are offered it again, the same as any box coming
    up. Nothing to do for a row that is not draining."""
    if alloc.state != DRAINING or alloc.drain_requested_at is not None:
        # Not draining, or drained by an operator: only undrain lifts that.
        return
    transition(db, alloc, READY_STATE, reason="the daemon registered again", now=None)
    alloc.drain_kind = None
    await announce_machine(
        db, alloc, status=machine_status(alloc), reason=None, actor=ctx.audit_dict()
    )
    await _bind_stranded(db, alloc, ctx=ctx)


def _adopt_instance(alloc: ComputeAllocation, daemon_instance_id: str | None) -> None:
    """Record the process that registered. A different process (or one that
    names none) has said nothing yet about what it can do: what the last one
    said is forgotten until its first beat restates it, so a box rolled back
    to an older build is never read as able to do what the newer one did."""
    if daemon_instance_id is None or daemon_instance_id != alloc.daemon_instance_id:
        alloc.capabilities_json = None
    alloc.daemon_instance_id = daemon_instance_id


async def hold_as_org_machine(
    db: AsyncSession, alloc: ComputeAllocation, machine_type: ComputeMachineType, *, user: User
) -> OrgMachine | None:
    """A box that registers itself is its org's machine: the org added it, so
    it is an org machine like a host attached by SSH (acquisition ``added``,
    nothing charged, in the org's pool) and the org's Machines page and the
    admin console show it under its org. Made on the box's first registration
    and kept on every later one; a box of any other kind is left as it is.

    Fails closed: an allocation already tied to another org is never adopted.
    Flushes, never commits."""
    if not provider_traits(machine_type.provider).self_registers:
        return None
    org_id = alloc.org_team_id
    if alloc.tenant_org_id is not None and alloc.tenant_org_id != org_id:
        return None
    # Nothing is charged for a machine the org runs itself, whatever the grant
    # that admitted it says.
    alloc.price_per_minute_nanos = 0
    await compute_funding().fund_allocation(db, alloc.id, None)
    if alloc.org_machine_id is not None:
        return await db.get(OrgMachine, alloc.org_machine_id)
    offering = await unlisted_offering_for(db, machine_type)
    machine = OrgMachine(
        org_team_id=org_id,
        owner_team_id=org_id,
        offering_id=offering.id,
        name=await free_machine_name(db, org_id=org_id, wanted=alloc.name.strip() or "Machine"),
        acquisition=ADDED,
        free_until=None,
        use_mode=USE_POOL,
        storage_gb=max(alloc.storage_gb, 1),
        idle_stop_minutes=None,
        monthly_cap_nanos=None,
        desired_power=POWER_ON,
        stop_reason=STOP_NONE,
        created_by=user.id,
    )
    db.add(machine)
    await db.flush()
    alloc.tenant_org_id = org_id
    alloc.org_machine_id = machine.id
    machine.current_allocation_id = alloc.id
    await db.flush()
    return machine


async def heartbeat(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    ctx: ActingContext,
    now: datetime | None = None,
    capacity: int | None = None,
    chats_served: int | None = None,
    daemon_version: str | None = None,
    draining: bool | None = None,
    restarting: bool | None = None,
    daemon_instance_id: str | None = None,
    resources: dict[str, Any] | None = None,
    sandbox: str | None = None,
    capabilities: Sequence[str] | None = None,
    last_activity_at: datetime | None = None,
    isolation: Mapping[str, object] | None = None,
    fault: Mapping[str, object] | None = None,
) -> ComputeAllocation:
    """Stamp the machine's heartbeat and announce a reachability transition
    (``starting`` -> ``ready``, ``unreachable`` -> ``ready``, anything ->
    ``draining``); a heartbeat that changes nothing emits nothing. A transition
    to ``ready`` also binds the org's stranded chats to this box. What the box
    says about its load is written as it comes — it is how a pool is spread.
    Commits.

    ``draining`` is one-way for the life of the row. A box drains because it
    was told to stop, and the process is going away; a later beat saying
    otherwise would be a box re-admitting itself to placement on its way out,
    which is how a deploy hands a chat to the very process that is exiting.
    The box that comes back registers again, and registration is what clears
    it. ``restarting`` is the same one-way drain, recorded as a restart
    (``drain_kind``) so the row reads ``restarting`` and its chats stay bound;
    a later plain ``draining`` beat turns it into an ordinary drain.

    Only the process that registered last may move the row: a beat naming
    another ``daemon_instance_id`` raises :class:`StaleInstanceError` and
    changes nothing — not even the stamp. That is the exiting process of a
    restart, whose last ``draining`` beat would otherwise land after its
    successor registered and drain the row it is serving from. A beat that
    names no instance (an older daemon) and a row that recorded none are not
    checked.

    ``isolation`` and ``fault`` are restated whole on every beat
    (:mod:`alkera_core.compute.unservable`): a beat without a fault ends the
    span its running time went unbilled for."""
    if (
        daemon_instance_id is not None
        and alloc.daemon_instance_id is not None
        and daemon_instance_id != alloc.daemon_instance_id
    ):
        raise StaleInstanceError(alloc.id)
    if not admit_claim(daemon_version).admitted:
        # Not even the stamp: a box this backend cannot serve is not kept
        # alive by its beats, so its row goes unreachable and its chats move.
        raise BoxTooOldError(daemon_version or "")
    moment = now or datetime.now(UTC)
    record_silence(alloc, now=moment)
    alloc.last_heartbeat_at = moment
    if capacity is not None:
        alloc.capacity = capacity
    if chats_served is not None:
        alloc.chats_served = chats_served
    if daemon_version is not None:
        alloc.daemon_version = daemon_version
    if resources is not None:
        alloc.resources_json = with_previous_gpus(resources, alloc.resources_json)
    if last_activity_at is not None:
        # What an idle stop is measured from. A box's clock ahead of ours
        # cannot keep its machine awake into the future: a stamp is never
        # later than the beat that carried it, and never moves backwards.
        stamp = min(last_activity_at, moment)
        if alloc.last_activity_at is None or stamp > alloc.last_activity_at:
            alloc.last_activity_at = stamp
    if sandbox is not None:
        alloc.sandbox = sandbox
    # Restated whole on every beat, and a beat that names none says the box
    # can do nothing new: a box rolled back to an older build sends no list,
    # and keeping the last one would bind it chats it cannot serve.
    alloc.capabilities_json = sorted(set(capabilities)) if capabilities is not None else None
    record_isolation(alloc, isolation)
    was_unhealthy = is_unhealthy(alloc)
    record_fault(alloc, fault, now=moment)
    if alloc.ran_org_workers_at is None and BoxCapability.ORG_WORKERS in (capabilities or ()):
        # Remembered for the life of the row: a later build that serves every
        # org from one process must not regain what this one gave up.
        alloc.ran_org_workers_at = moment
    if draining and not restarting:
        # A stop that hands the chats on, including one that began as a
        # restart and became a real stop. Only a serving box drains: a beat
        # that lands on a row already on its way off the plane changes nothing.
        if is_legal(alloc.state, DRAINING):
            transition(db, alloc, DRAINING, reason="the daemon is stopping", now=moment)
        if alloc.state == DRAINING:
            alloc.drain_kind = None
    elif restarting and is_legal(alloc.state, DRAINING):
        transition(db, alloc, DRAINING, reason="the daemon is restarting", now=moment)
        alloc.drain_kind = DRAIN_RESTART
    status = machine_status(alloc, now=moment)
    if status != alloc.last_reported_status:
        await announce_machine(db, alloc, status=status, reason=None, actor=ctx.audit_dict())
        if status == READY:
            await _bind_stranded(db, alloc, ctx=ctx)
    elif was_unhealthy != is_unhealthy(alloc):
        # Whether its worker can serve is part of what every surface shows.
        await announce_machine(
            db, alloc, status=status, reason=alloc.fault_code, actor=ctx.audit_dict()
        )
    await db.commit()
    return alloc


def with_previous_gpus(sample: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """A beat's resource sample, keeping the GPUs the row already holds when
    the sample says nothing about them: a daemon too old to sample GPUs (and
    one with none, whose sample carries no ``gpus`` key) must not erase what
    an earlier beat reported. The readings from before a sleep are dropped by
    the wake itself, so this never carries a stopped machine's GPUs forward."""
    if "gpus" in sample or not previous or not previous.get("gpus"):
        return sample
    return {**sample, "gpus": previous["gpus"]}


class BoxTooOldError(Exception):
    """A box whose build is older than the oldest this backend serves
    (``MIN_SUPPORTED_BOX``)."""

    code = BOX_TOO_OLD

    def __init__(self, reported_version: str) -> None:
        super().__init__(BOX_TOO_OLD_MESSAGE)
        self.reported_version = reported_version


async def _end_box_too_old(db: AsyncSession, alloc: ComputeAllocation | None) -> None:
    """End a machine the plane started whose box came up on a build too old to
    serve: it can never claim, so it fails now rather than after the boot
    timeout, with the words the console shows and the provider machine
    released. A row that already served (a box rolled back under a running
    machine) is left to its liveness; only its claim is refused."""
    if (
        alloc is None
        or alloc.origin != PROVISIONED
        or alloc.state
        not in (
            PROVISIONING,
            BOOTSTRAPPING,
        )
    ):
        return
    alloc.error = BOX_TOO_OLD_MESSAGE
    alloc.terminated_reason = BOX_TOO_OLD
    await provisioning.terminate(db, alloc, caller=None, force=True, reason=BOX_TOO_OLD_MESSAGE)


class StaleInstanceError(Exception):
    """A heartbeat from a daemon process that is no longer the one registered
    on the row — the process a restart replaced."""

    code = "machine_stale_instance"

    def __init__(self, machine_id: UUID) -> None:
        super().__init__("This daemon process is no longer the one registered for this machine.")
        self.machine_id = machine_id


async def get_workspace_machine(
    db: AsyncSession, *, machine_id: UUID, org_id: UUID
) -> ComputeAllocation | None:
    """Org-scoped fetch of a workspace row; a foreign or session row reads as missing."""
    return (
        await db.execute(
            select(ComputeAllocation).where(
                ComputeAllocation.id == machine_id,
                ComputeAllocation.org_team_id == org_id,
                ComputeAllocation.lifecycle == WORKSPACE,
            )
        )
    ).scalar_one_or_none()


def machine_read(
    alloc: ComputeAllocation, machine_type: ComputeMachineType, *, now: datetime | None = None
) -> MachineRead:
    return MachineRead(
        id=str(alloc.id),
        name=alloc.name,
        provider=machine_type.provider,
        provider_pod_id=alloc.provider_machine_id,
        machine_type=machine_type_info(
            machine_type, rate_per_minute_nanos=alloc.price_per_minute_nanos
        ),
        lifecycle=alloc.lifecycle,
        status=machine_status(alloc, now=now),
        last_heartbeat_at=alloc.last_heartbeat_at,
        created_at=alloc.created_at,
    )


async def box_card_for(
    db: AsyncSession, alloc: ComputeAllocation, *, org_id: UUID
) -> BoxMachineCard | None:
    """What the box backing an org machine is told about the machine it runs
    on, or ``None`` for a box that backs none (a shared-pool box, an org's own
    registered box).

    ``org_id`` is the org of the credential the box spoke with. The card is
    answered only for an org machine of that org, read in the allocation's own
    tenant (the database ties the two by a composite key), so a box never
    learns another org's machine. The card never carries a price, only whether
    minutes are billed."""
    if alloc.org_machine_id is None or alloc.tenant_org_id != org_id:
        return None
    om = (
        await db.execute(
            select(OrgMachine).where(
                OrgMachine.id == alloc.org_machine_id,
                OrgMachine.org_team_id == alloc.tenant_org_id,
                OrgMachine.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if om is None:
        return None
    machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
    if machine_type is None:
        return None
    gpu = (
        GpuSpec(
            name=machine_type.gpu_name or machine_type.display_name,
            count=machine_type.gpu_count,
            memory_gb=machine_type.gpu_memory_gb,
        )
        if machine_type.gpu_count > 0
        else None
    )
    return BoxMachineCard(
        name=om.name,
        gpu=gpu,
        vcpu=max(0, machine_type.vcpu),
        memory_gb=max(0, machine_type.memory_gb),
        disk_gb=max(0, om.storage_gb),
        billed_per_minute=alloc.price_per_minute_nanos > 0,
        idle_stop_minutes=om.idle_stop_minutes,
    )


#: Shown instead of the provider's error when a machine failed to come up.
FAILED_MACHINE_REASON = "The workspace could not be started."


def _banner_reason(state: str, alloc: ComputeAllocation) -> str:
    """A recorded provider error is written for whoever operates the machine —
    an API message carrying a URL and a machine id — so the banner never shows
    it. A machine that did not come up gets a sentence instead; the raw text
    stays on the allocation for the admin machines page."""
    if state == "ready" or not alloc.error:
        return ""
    return FAILED_MACHINE_REASON


async def machine_state_read(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    org_id: UUID,
    workspace_pin: UUID | None = None,
    owner_user_id: UUID | None = None,
    now: datetime | None = None,
) -> MachineStateRead:
    """What the banner shows before a chat is open: the machine the org's next
    chat would be placed on, ``pool`` when that is a shared pool box, or
    ``none`` when nothing would take it.

    Answered by placement itself (:func:`placement.resolve_machine_for`, for a
    new chat, which is writable until its mode says otherwise), so the page and
    the create it gates cannot disagree: a pool-served org is told it can
    compose because the create WILL place its chat, and an org nothing serves
    is told so before it types. ``workspace_pin`` asks it for a chat of a
    workspace pinned to an org machine, which places on that machine (and
    wakes it) wherever the org's other chats go. A pool box is named only as ``pool`` — which
    shared box takes the chat is placement's choice at create time, and the
    box's name and id belong to the platform, not to the tenant reading this.
    """
    binding = await placement.resolve_machine_for(
        db,
        ctx=ctx,
        org_team_id=org_id,
        purpose="chat",
        workspace_pin=workspace_pin,
        owner_user_id=owner_user_id,
    )
    alloc = await db.get(ComputeAllocation, binding.machine_id) if binding is not None else None
    if alloc is not None and alloc.tenancy == POOL_TENANCY:
        return MachineStateRead(machine_id=None, status="pool", name="", last_heartbeat_at=None)
    state = machine_state(alloc, now=now)
    if alloc is None or state == "none":
        return MachineStateRead(
            machine_id=None,
            status="none",
            name="",
            last_heartbeat_at=None,
            status_fact=placement_status("none"),
        )
    return MachineStateRead(
        machine_id=str(alloc.id),
        status=state,
        name=alloc.name,
        status_fact=placement_status(state, machine_name=alloc.name),
        # What the machine itself recorded about a state that is not ready; a
        # ready one has nothing to explain. A provisioning failure records the
        # provider's own error — a URL and an id, written for whoever operates
        # the machine, not for the person waiting on an answer — so that state
        # gets a sentence instead. The raw text stays on the allocation, where
        # the admin machines page reads it.
        reason=_banner_reason(state, alloc),
        last_heartbeat_at=alloc.last_heartbeat_at,
    )


async def bound_chat_counts(db: AsyncSession, machine_ids: Iterable[UUID]) -> dict[UUID, int]:
    """How many live chats are bound to each machine, counted from the chats.

    The number an operator reads beside a machine. The row's ``chats_served`` is
    the box's own last report (a heartbeat's, or a placement's increment), so it
    lags a box that went quiet and drifts when a placement never reached it; the
    chats themselves say where they are bound. A machine nothing is bound to is
    absent from the answer (read it as zero)."""
    async with cross_tenant_write(db, reason="compute.machine_chats.bound_counts"):
        wanted = [str(machine_id) for machine_id in machine_ids]
        if not wanted:
            return {}
        bound = WorkspaceObject.spec["machine_id"].astext
        counted = await db.execute(
            select(bound, func.count())
            .where(
                WorkspaceObject.type == "chat",
                WorkspaceObject.deleted_at == 0,
                bound.in_(wanted),
            )
            .group_by(bound)
        )
        return {UUID(str(machine)): int(n) for machine, n in counted.all()}


@dataclass(frozen=True, slots=True)
class ChatLoad:
    """The chats bound to one machine, split by what the machine can do for
    them. The three add up to the bound count."""

    #: Live load: bound to a box that is serving — starting, ready, draining,
    #: restarting, or silent but still on the plane — and not parked by it.
    served: int = 0
    #: Bound to a box that has left the plane; waiting for the next box that
    #: serves the org, or for their next message.
    stranded: int = 0
    #: Parked: on a sleeping box, or on a ready box that closed the chat's
    #: session. A wake (or the chat's next message) resumes them.
    asleep: int = 0


async def chat_load(
    db: AsyncSession, machine_ids: Iterable[UUID], *, now: datetime | None = None
) -> dict[UUID, ChatLoad]:
    """The load an operator reads beside each machine, split the way each
    chat's own row reads it (:func:`placement.chat_machine_status`).

    The bound count is taken from the chats (the durable fact: which box last
    had each one); the split from the machine's state first, never from the
    word a chat's spec recorded at binding — counting "chats that say ready"
    once showed a released box carrying its whole history as load ("67 / 10"
    on a box that was gone). A box off the plane has stranded chats and no
    load; a sleeping box has parked chats and no load. A box in any serving
    state carries its bound chats as load, except the ones a ready box has
    itself reported closing (``mirror_state`` ``asleep``): those are parked,
    as the machine's detail page and the chat page already say. The box's
    heartbeat figure counts every session it mirrors, parked ones included,
    so it is not the number shown here. A machine nothing is bound to is
    absent (read it as zeros)."""
    async with cross_tenant_write(db, reason="compute.machine_chats.load"):
        wanted = [str(machine_id) for machine_id in machine_ids]
        if not wanted:
            return {}
        bound = WorkspaceObject.spec["machine_id"].astext
        parked = WorkspaceObject.spec["mirror_state"].astext == "asleep"
        counted = (
            await db.execute(
                select(bound, func.count(), func.count().filter(parked))
                .where(
                    WorkspaceObject.type == "chat",
                    WorkspaceObject.deleted_at == 0,
                    bound.in_(wanted),
                )
                .group_by(bound)
            )
        ).all()
        if not counted:
            return {}
        split = {
            UUID(str(machine)): (int(total) - int(asleep), int(asleep))
            for machine, total, asleep in counted
        }
        rows = (
            (
                await db.execute(
                    select(ComputeAllocation).where(ComputeAllocation.id.in_(list(split)))
                )
            )
            .scalars()
            .all()
        )
        machines = {alloc.id: alloc for alloc in rows}
        load: dict[UUID, ChatLoad] = {}
        for machine_id, (awake, asleep) in split.items():
            state = machine_state(machines.get(machine_id), now=now)
            if state == NONE:
                load[machine_id] = ChatLoad(stranded=awake + asleep)
            elif state == ASLEEP:
                load[machine_id] = ChatLoad(asleep=awake + asleep)
            elif state == READY:
                load[machine_id] = ChatLoad(served=awake, asleep=asleep)
            else:
                load[machine_id] = ChatLoad(served=awake + asleep)
        return load


__all__ = [
    "BoxTooOldError",
    "ChatLoad",
    "StaleInstanceError",
    "bound_chat_counts",
    "chat_load",
    "find_registered",
    "get_workspace_machine",
    "heartbeat",
    "machine_read",
    "machine_state_read",
    "register_machine",
    "register_platform_machine",
]
