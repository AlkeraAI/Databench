"""Session allocation lifecycle: admit, provision, poll, renew, release.

Admission is :func:`backend.services.compute.grants.admit` — the only count of
machines on the plane. Prices are pinned at allocate from the grant (the billed
rate) and the catalog (the provider's true cost) so nothing re-rates a running
machine. The SSH private key is sealed before the row is written and opened
only through :func:`open_private_key_for`, a library seam for the operator
tooling that provisions a box — no route returns it.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute import meter
from alkera_core.compute.billing_port import compute_funding
from alkera_core.compute.events import announce_machine
from alkera_core.compute.keys import generate_ssh_keypair, open_private_key, seal_private_key
from alkera_core.compute.machines import machine_status
from alkera_core.compute.provider import (
    ComputeProvider,
    ComputeProviderError,
    ComputeProviderUnavailableError,
)
from alkera_core.compute.reconcile import pod_name_for
from alkera_core.config import settings
from alkera_core.db.locking import advisory_key, advisory_xact_lock
from alkera_core.logging import get_logger
from alkera_core.models import User
from alkera_core.models.compute import (
    COMPUTE_ACTIVE_STATES,
    COMPUTE_TERMINAL_STATES,
    FAILED,
    PENDING,
    PROVISIONING,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.money import nanos_to_credits, usd_label
from alkera_core.schemas.compute import ComputeAllocationInfo, MachineTypeInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.compute import grants

log = get_logger(__name__)


class MachineTypeUnavailableError(Exception):
    """The machine type is out of stock / retired for new machines (-> 409). A
    running machine of this type is unaffected — it bills on its pinned prices."""


# -- catalog ---------------------------------------------------------------


async def list_machine_types(
    db: AsyncSession, *, include_inactive: bool = False
) -> list[ComputeMachineType]:
    stmt = select(ComputeMachineType).order_by(
        ComputeMachineType.compute_class, ComputeMachineType.provider_price_per_minute_nanos
    )
    if not include_inactive:
        stmt = stmt.where(ComputeMachineType.active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def get_machine_type(db: AsyncSession, machine_type_id: UUID) -> ComputeMachineType | None:
    return (
        await db.execute(select(ComputeMachineType).where(ComputeMachineType.id == machine_type_id))
    ).scalar_one_or_none()


async def get_machine_type_by_code(
    db: AsyncSession, *, provider: str, code: str
) -> ComputeMachineType | None:
    return (
        await db.execute(
            select(ComputeMachineType).where(
                ComputeMachineType.provider == provider,
                ComputeMachineType.provider_type_id == code,
            )
        )
    ).scalar_one_or_none()


# -- allocations -----------------------------------------------------------


async def create_allocation(
    db: AsyncSession,
    *,
    ctx: ActingContext,
    user: User,
    machine_type: ComputeMachineType,
    project_path: str,
    session_id: str,
    provider: ComputeProvider,
    max_lease_minutes: int | None = None,
) -> ComputeAllocation:
    """Admit, mint the ephemeral keypair, provision via the provider.

    Provisioning a machine outlives the client's own control timeout, so the
    retry that timeout invites must not buy a second one: a named session that
    already has a live machine of this type gets that machine back, and two
    retries in flight are serialized on an advisory lock so neither can miss the
    other's row.

    The row is committed ``provisioning``, under the pod name it derives from
    its own id, BEFORE the provider is asked for anything — so the machine we
    are about to pay for is named in the database first, and a create that is
    cancelled or never answered leaves something the reconciler can resolve.

    Raises a ``grants.ComputeRefusedError`` (402/429, already on record),
    :class:`MachineTypeUnavailableError` (409) or ``ComputeProviderError``
    (502). A provider that REFUSED leaves the row ``failed``; a provider that
    never answered leaves it ``provisioning`` for the reconciler, because a
    create we did not hear the end of may well have built a machine.
    """
    # The machine is the org's the request is acting in: the credential's, never
    # one read off the person, who may belong to several.
    org_team_id = ctx.org_id
    if session_id:
        # Held until this transaction ends, so the whole look-then-provision runs
        # once per session slot. Keyed per (user, org, session, type): a different
        # session never waits on this one.
        await advisory_xact_lock(
            db,
            advisory_key("compute-session-slot", user.id, org_team_id, session_id, machine_type.id),
        )
        existing = await live_allocation_for_session(
            db,
            user_id=user.id,
            org_team_id=org_team_id,
            session_id=session_id,
            machine_type_id=machine_type.id,
        )
        if existing is not None:
            # A repeat of a request already answered — the same machine, not a second
            # one. Availability and admission below are about NEW machines only.
            return existing
    # Gate NEW machines on live availability — a running machine of a now-unavailable
    # type is untouched, but we cannot provision a fresh one.
    if not machine_type.active or not machine_type.available_for_new:
        raise MachineTypeUnavailableError(
            f"machine type {machine_type.display_name!r} is not currently available "
            "for new machines (out of stock or retired)"
        )
    grant = await grants.admit(
        db, ctx=ctx, user=user, org_team_id=org_team_id, machine_type=machine_type
    )
    private_pem, public_key = generate_ssh_keypair()
    alloc = ComputeAllocation(
        user_id=user.id,
        org_team_id=org_team_id,
        machine_type_id=machine_type.id,
        lifecycle="session",
        state=PROVISIONING,
        ssh_public_key=public_key,
        ssh_private_key_enc=seal_private_key(private_pem),
        project_path=project_path,
        session_id=session_id,
        grant_id=grant.source_grant_id,
        # Pin BOTH prices now: the billed rate from the grant and the provider's
        # true cost from the catalog. Neither can be re-rated mid-lease.
        price_per_minute_nanos=grant.rate_per_minute_nanos,
        true_cost_per_minute_nanos=machine_type.provider_price_per_minute_nanos,
        max_lease_minutes=max_lease_minutes,
    )
    db.add(alloc)
    await db.flush()
    await compute_funding().fund_allocation(db, alloc.id, grant.funding_account_id)
    # The INTENT is made durable before a cent is spent. The row carries the
    # name the pod will be given, derived from its own id, so from here on every
    # pod this deployment holds can be traced back to the row that asked for it
    # — by name, without the provider's id ever having reached us. Committing
    # first also means a cancel that lands anywhere in the provider call leaves
    # a row the reconciler can resolve, instead of a rolled-back intent and a
    # pod nobody in the database has ever heard of.
    pod_name = pod_name_for(alloc.id)
    await db.commit()
    # The whole create-and-record section is shielded, not just the commit. A
    # CancelledError delivered INSIDE create_pod — a client disconnect, a
    # shutdown — used to unwind with no pod id in hand while the provider went
    # on building a machine we then paid for and never found; the retry that the
    # client's own control timeout invites bought a second one. Shielded, the
    # section runs to the end and the caller's cancellation is re-raised after.
    record = asyncio.ensure_future(
        _create_and_record(db, alloc, provider=provider, machine_type=machine_type, name=pod_name)
    )
    try:
        await asyncio.shield(record)
    except BaseException:
        with contextlib.suppress(BaseException):
            await asyncio.wait([record])
        raise
    return alloc


async def _create_and_record(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    provider: ComputeProvider,
    machine_type: ComputeMachineType,
    name: str,
) -> None:
    """Ask the provider for the machine and record what came back. Commits.

    Three outcomes, and the difference between the last two is money:

    - the provider answers with an id: it is committed, and the meter can find
      the pod from the next tick;
    - the provider REFUSES — it answered with a status, or it is a provider this
      deployment cannot act through at all, which never reached anything: no
      machine was made, so the row is ``failed`` and the grant slot goes back;
    - we never heard (a timeout, a dropped connection, an answer with no id):
      the provider may well have built the machine. The row STAYS
      ``provisioning`` under its pod name, because filing it ``failed`` is a
      claim we cannot make — it closes the row, frees the slot and leaves a pod
      billing us that nothing will ever look for again. The reconciler resolves
      it either way: it adopts the pod if one of that name exists, and writes
      the row off once it is old enough to be sure none does.
    """
    try:
        pod_id = await provider.create_pod(
            name=name, machine_type=machine_type, ssh_public_key=alloc.ssh_public_key
        )
    except ComputeProviderError as exc:
        alloc.error = str(exc)[:1024]
        if exc.status_code is not None or isinstance(exc, ComputeProviderUnavailableError):
            # Nothing was created: either the provider answered and refused, or
            # this deployment cannot act through it and never reached it.
            alloc.state = FAILED
        else:
            log.warning(
                "compute.create.unconfirmed",
                allocation_id=str(alloc.id),
                pod_name=name,
                error=str(exc),
            )
        await db.commit()
        raise
    alloc.provider_machine_id = pod_id
    alloc.state = PROVISIONING
    await db.commit()


async def refresh_allocation(
    db: AsyncSession, alloc: ComputeAllocation, *, provider: ComputeProvider
) -> ComputeAllocation:
    """Pull live provider state for an in-flight allocation. Transitions
    provisioning -> ready when the pod is SSH-connectable. Provider hiccups
    during a poll are swallowed — the caller sees the last-known state."""
    if alloc.state not in ("pending", "provisioning") or not alloc.provider_machine_id:
        return alloc
    try:
        status = await provider.pod_status(alloc.provider_machine_id)
    except ComputeProviderError:
        return alloc
    if status.ssh_ready:
        # Lock the row (after the network poll, not during it): the meter ALSO drives
        # provisioning->ready and anchors last_metered_at, so an unlocked stale-snapshot
        # write here could rewind the anchor the meter already advanced.
        await db.refresh(alloc, with_for_update=True)
        if alloc.state not in ("pending", "provisioning"):
            return alloc  # a meter tick readied it first under the lock
        now = datetime.now(UTC)
        alloc.state = "ready"
        alloc.ready_at = now
        alloc.public_ip = status.public_ip
        alloc.ssh_port = status.ssh_port
        if alloc.last_metered_at is None:
            alloc.last_metered_at = now
        await db.commit()
    return alloc


async def renew_allocation(
    db: AsyncSession, alloc: ComputeAllocation, *, max_lease_minutes: int | None
) -> ComputeAllocation:
    """Set (renew/extend or clear) a session allocation's self-imposed lease
    ceiling. ``None`` clears it (credit exhaustion becomes the only automatic
    kill). No billing state is touched. A workspace machine never has a lease;
    renewing one is a no-op."""
    if alloc.lifecycle == "workspace":
        return alloc
    alloc.max_lease_minutes = max_lease_minutes
    await db.commit()
    return alloc


async def terminate_allocation(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    provider: ComputeProvider,
    actor: dict[str, object] | None = None,
) -> ComputeAllocation:
    """Idempotent release: released/failed rows are a no-op; the provider's 404
    counts as success. Settles the running interval first so a release between
    meter ticks is not free compute, and announces the machine gone."""
    if alloc.state in COMPUTE_TERMINAL_STATES:
        return alloc
    # Serialize against the meter (and a concurrent release) on the row lock the
    # meter itself takes; re-check the terminal state under it so a racing
    # double-click release settles exactly once.
    await db.refresh(alloc, with_for_update=True)
    if alloc.state in COMPUTE_TERMINAL_STATES:
        return alloc
    now = datetime.now(UTC)
    with contextlib.suppress(Exception):
        # A SAVEPOINT so a settle error rolls back JUST this nested block: the
        # terminate + commit below must still succeed (the pod is actually killed)
        # rather than fail on a poisoned transaction. Best-effort by design.
        async with db.begin_nested():
            await meter.settle_final(
                db, alloc, now=now, max_minutes=settings.compute_max_lease_minutes
            )
    if alloc.provider_machine_id:
        await provider.terminate_pod(alloc.provider_machine_id)
    alloc.state = "released"
    alloc.terminated_reason = "user_released"
    alloc.released_at = now
    await announce_machine(db, alloc, status="none", reason="user_released", actor=actor)
    await db.commit()
    return alloc


async def list_allocations(
    db: AsyncSession, *, user_id: UUID, org_team_id: UUID, active_only: bool = False
) -> list[ComputeAllocation]:
    """The person's allocations in one org: a member of two orgs lists each
    org's machines from that org's session only."""
    stmt = (
        select(ComputeAllocation)
        .where(ComputeAllocation.user_id == user_id, ComputeAllocation.org_team_id == org_team_id)
        .order_by(ComputeAllocation.created_at.desc())
    )
    if active_only:
        stmt = stmt.where(ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES))
    return list((await db.execute(stmt)).scalars().all())


async def live_allocation_for_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_team_id: UUID,
    session_id: str,
    machine_type_id: UUID,
) -> ComputeAllocation | None:
    """The machine this session already has of this type, if any — the machine a
    repeated provision request is answered with. Released and failed rows are
    not it: a session that gave its machine back may ask for another."""
    if not session_id:
        return None
    return (
        await db.execute(
            select(ComputeAllocation)
            .where(
                ComputeAllocation.user_id == user_id,
                ComputeAllocation.org_team_id == org_team_id,
                ComputeAllocation.session_id == session_id,
                ComputeAllocation.machine_type_id == machine_type_id,
                ComputeAllocation.lifecycle == "session",
                ComputeAllocation.state.in_(COMPUTE_ACTIVE_STATES),
            )
            .order_by(ComputeAllocation.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_allocation(
    db: AsyncSession, *, allocation_id: UUID, org_team_id: UUID
) -> ComputeAllocation | None:
    """Org-scoped fetch — a foreign allocation is indistinguishable from a
    nonexistent one. Ownership is decided by the policy, not here."""
    return (
        await db.execute(
            select(ComputeAllocation).where(
                ComputeAllocation.id == allocation_id,
                ComputeAllocation.org_team_id == org_team_id,
            )
        )
    ).scalar_one_or_none()


def open_private_key_for(alloc: ComputeAllocation) -> str:
    """The allocation's SSH private key, for the operator tooling that stages a
    box. A library seam on purpose: no HTTP route returns a private key."""
    return open_private_key(alloc.ssh_private_key_enc)


# -- serialization ---------------------------------------------------------


def machine_type_info(
    mt: ComputeMachineType, *, rate_per_minute_nanos: int | None = None
) -> MachineTypeInfo:
    """The tenant view of a catalog row: the type, its stock, and what the
    caller would be billed — never what it costs us."""
    return MachineTypeInfo(
        id=str(mt.id),
        provider=mt.provider,
        provider_type_id=mt.provider_type_id,
        display_name=mt.display_name,
        compute_class=mt.compute_class,
        # Defensive defaults: an un-flushed in-memory row has these as None (the
        # column defaults only apply on INSERT), and the picker should never crash.
        gpu_count=mt.gpu_count or 0,
        vcpu=mt.vcpu or 0,
        memory_gb=mt.memory_gb or 0,
        active=mt.active if mt.active is not None else True,
        availability=mt.availability or "unknown",
        available_for_new=mt.available_for_new if mt.available_for_new is not None else True,
        rate_per_minute_nanos=rate_per_minute_nanos,
    )


_TERMINATED_REASON_PHRASE: dict[str, str] = {
    "credits_exhausted": "credits exhausted",
    "grant_expired": "the compute grant expired",
    "provider_gone": "the machine disappeared upstream",
    "provider_error": "an upstream provider error",
    "heartbeat_lost": "the machine stopped answering",
    "user_released": "released by you",
    "max_minutes": "the maximum lease was reached",
}


def _add_credit(org_team_id: UUID) -> str:
    """The sentence pointing at where to add credit for the org the machine
    bills, or nothing when the deployment bills no compute."""
    url = meter.compute_metering().add_credit_url(org_team_id)
    return f"Add credit at {url}" if url is not None else ""


def _status_message(alloc: ComputeAllocation, *, spend_nanos: int) -> str:
    """A single human sentence the agent + UI show — state, why it stopped (if it
    did), and the billed spend so far."""
    spend = f"{nanos_to_credits(spend_nanos):,} credits ({usd_label(spend_nanos)})"
    minutes = alloc.minutes_billed
    if alloc.state in COMPUTE_TERMINAL_STATES:
        phrase = _TERMINATED_REASON_PHRASE.get(alloc.terminated_reason)
        if phrase is None:
            phrase = "released" if alloc.state == "released" else "stopped"
        msg = f"Terminated: {phrase} after {minutes} min ({spend} billed)"
        if alloc.terminated_reason == "credits_exhausted" and (
            add := _add_credit(alloc.org_team_id)
        ):
            msg += f". {add}"
        return msg
    if alloc.state == "ready":
        if alloc.low_credit:
            low = f"Low credit — will stop soon; {minutes} min, {spend} billed."
            add = _add_credit(alloc.org_team_id)
            return f"{low} {add}" if add else low
        return f"Running, {minutes} min, {spend} billed"
    if alloc.state in (PENDING, PROVISIONING):
        if alloc.state == PROVISIONING and not alloc.provider_machine_id:
            # The provider never answered the create, so we do not know that
            # there is a machine coming. Saying "Provisioning…" here would be a
            # claim we cannot make, and the wait can run to the
            # unconfirmed-create window before the reconciler settles it either
            # way. Name the actual state so the reader knows a retry is not
            # what is missing.
            return "Waiting for the provider to confirm this machine…"
        return "Provisioning…"
    return f"{alloc.state}, {minutes} min, {spend} billed"


def allocation_info(
    alloc: ComputeAllocation,
    machine_type: ComputeMachineType,
    *,
    now: datetime | None = None,
) -> ComputeAllocationInfo:
    moment = now or datetime.now(UTC)
    end = alloc.released_at or moment
    minutes = max(0, int((end - alloc.created_at).total_seconds() // 60))
    # Once the meter has run (last_metered_at set), report what was ACTUALLY
    # billed; before that, the wall-clock minutes x pinned-rate estimate.
    metered = alloc.last_metered_at is not None
    spend_nanos = alloc.billed_nanos if metered else minutes * alloc.price_per_minute_nanos
    workspace = alloc.lifecycle == "workspace"
    return ComputeAllocationInfo(
        id=str(alloc.id),
        machine_type=machine_type_info(
            machine_type, rate_per_minute_nanos=alloc.price_per_minute_nanos
        ),
        lifecycle=alloc.lifecycle,
        name=alloc.name,
        state=alloc.state,
        provider_machine_id=alloc.provider_machine_id,
        public_ip=alloc.public_ip,
        ssh_port=alloc.ssh_port,
        ssh_user=alloc.ssh_user,
        created_at=alloc.created_at,
        ready_at=alloc.ready_at,
        released_at=alloc.released_at,
        minutes_elapsed=minutes,
        spend_nanos=spend_nanos,
        minutes_billed=alloc.minutes_billed,
        billed_nanos=alloc.billed_nanos,
        terminated_reason=alloc.terminated_reason,
        low_credit=alloc.low_credit,
        max_lease_minutes=alloc.max_lease_minutes,
        status_message=_status_message(alloc, spend_nanos=spend_nanos),
        error=alloc.error,
        project_path=alloc.project_path,
        session_id=alloc.session_id,
        machine_status=(
            machine_status(alloc, now=moment)
            if workspace and alloc.state not in COMPUTE_TERMINAL_STATES
            else None
        ),
        last_heartbeat_at=alloc.last_heartbeat_at,
    )
