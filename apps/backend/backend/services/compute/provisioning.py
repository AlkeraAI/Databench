"""Machines the platform starts from the admin console: provision, drain,
undrain, terminate.

A provisioned machine is ONE ``compute_allocations`` row from the click to the
release — ``workspace`` lifecycle, ``provisioned`` origin, a platform tenancy —
and every state it passes through is written by
:func:`alkera_core.compute.transitions.transition`, so its history is on
``compute_allocation_events``.

Provision commits the row (``provisioning``) and the machine credential it will
claim with BEFORE the provider is asked to start anything, so a crash mid-launch
leaves a row the reconcile can time out rather than a machine nobody knows
about. The one provider call made first is storing the node's secret: a
provider that refuses THAT has acted on nothing, so the attempt rolls back and
leaves no row at all — a ``failed`` machine the provider never saw is history
of nothing. (A crash between that store and the commit can leave an inert
secret behind, readable by no instance, since only the bind names one.) The
credential is pre-bound to the row: the box's claim lands on this row and moves
it to ``ready``. It is the only thing the node boots with — the box acts as the
machine on every call it makes, never as the admin who provisioned it — and
:mod:`backend.services.compute.machine_credential` is where it is issued.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from alkera_core.authz import ActingContext
from alkera_core.compute.bootstrap import boot_profile
from alkera_core.compute.handoff import (
    Departure,
    count_bound_chats,
    drop_dedicated_assignments,
    leave_placement,
)
from alkera_core.compute.launch import render_node_script
from alkera_core.compute.machines import WORKSPACE
from alkera_core.compute.node_reconcile import revoke_node_credentials, settle_release
from alkera_core.compute.nodes import NodeLaunch, NodeProvider, node_provider_for_kind
from alkera_core.compute.power import (
    NotSleepableError,
    OrgMachinesNotReleasedError,
    release_for_org_deletion,
    sleep_allocation,
)
from alkera_core.compute.power_lock import stop_after_sleep
from alkera_core.compute.provider import ComputeProviderError, node_placement
from alkera_core.compute.reconcile import pod_name_for
from alkera_core.compute.transitions import transition
from alkera_core.compute.wake import WakeResult, wake_allocation
from alkera_core.config import Settings, settings
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import actor_system
from alkera_core.logging import get_logger
from alkera_core.models import OrgComputeAssignment, User
from alkera_core.models.compute import (
    ASLEEP,
    COMPUTE_TERMINAL_STATES,
    DEDICATED_TENANCY,
    DRAINING,
    FAILED,
    POOL_TENANCY,
    PROVISIONED,
    PROVISIONING,
    READY,
    RELEASED,
    RELEASING,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.objects import chat_end
from alkera_core.observability.redaction import REDACTED, scrub_text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import org_audit as org_audit_service
from backend.services.compute import placement
from backend.services.compute.machine_credential import node_credential_for
from backend.services.org import teams as team_service

log = get_logger(__name__)

#: The org audit action written for the org whose dedicated assignment a
#: release dropped: its chats place by the ordinary rules again.
DEDICATED_RELEASED_ACTION = "compute.dedicated_machine_released"

#: The reason a start a message caused is recorded under.
CHAT_WAKE_REASON = "a message woke the machine"


@dataclass(frozen=True, slots=True)
class ReleasedOrg:
    """An org returned to ordinary placement by a release."""

    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class Released:
    """What a terminate did: the row as it now stands, the orgs whose
    dedicated assignment named it, and where its chats went — moved to another
    box by placement, or left stranded on it to wait."""

    machine: ComputeAllocation
    released_orgs: list[ReleasedOrg] = field(default_factory=list)
    chats_moved: int = 0
    chats_stranded: int = 0


class ProvisionError(Exception):
    """A request the plane refuses; ``status`` is the HTTP answer."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


#: What an admin may call a machine. The name reaches the node's boot script
#: and its environment file, so it is held to a plain charset here even though
#: the script quotes it too: letters, digits, spaces, dots, dashes and
#: underscores, starting with a letter or digit, at most 64 characters.
MACHINE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-])?")


def check_machine_name(name: str | None) -> str:
    """The name to store, or :class:`ProvisionError` (400) for one refused.
    Empty means "name it for me"."""
    cleaned = (name or "").strip()
    if cleaned and not MACHINE_NAME_RE.fullmatch(cleaned):
        raise ProvisionError(
            400,
            "bad_machine_name",
            "A machine name is up to 64 letters, digits, spaces, dots, dashes or "
            "underscores, starting with a letter or digit",
        )
    return cleaned


def make_node_provider(kind: str, config: Settings) -> NodeProvider:
    """The node provider for ``kind`` — module-level so a test substitutes one."""
    return node_provider_for_kind(kind, config)


def admin_actor(user: User) -> dict[str, Any]:
    return {"kind": "admin", "email": user.email, "name": user.display_name or None}


SYSTEM_ACTOR: dict[str, Any] = {"kind": "system", "email": None, "name": "reconcile"}


async def provision(
    db: AsyncSession,
    *,
    caller: User,
    acting_org_id: UUID,
    provider_kind: str,
    machine_type_code: str,
    storage_gb: int,
    tenancy: str,
    org_id: UUID | None,
    name: str | None,
    config: Settings = settings,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Start one machine. Returns the row in ``provisioning`` with the
    provider's machine id; raises :class:`ProvisionError` for a refusal (the
    row, if one was written, is ``failed`` with the reason).

    The row is filed under ``acting_org_id``, the org of the credential the
    admin provisioned with. ``org_id`` is the org a dedicated machine serves."""
    moment = now or datetime.now(UTC)
    name = check_machine_name(name)
    machine_type = (
        await db.execute(
            select(ComputeMachineType).where(
                ComputeMachineType.provider == provider_kind,
                ComputeMachineType.provider_type_id == machine_type_code,
            )
        )
    ).scalar_one_or_none()
    if machine_type is None or not machine_type.active or not machine_type.available_for_new:
        raise ProvisionError(404, "machine_type_not_found", "Machine type not found")
    provider = make_node_provider(provider_kind, config)
    if not provider.configured():
        raise ProvisionError(
            409, "provider_not_configured", f"{provider_kind} is not configured here"
        )
    if tenancy == DEDICATED_TENANCY:
        if org_id is None:
            raise ProvisionError(400, "org_required", "A dedicated machine needs an org")
        held = await db.get(OrgComputeAssignment, org_id)
        if held is not None:
            box = await db.get(ComputeAllocation, held.machine_id)
            if box is not None and box.state not in COMPUTE_TERMINAL_STATES:
                raise ProvisionError(
                    409, "org_has_machine", "This organization already has a dedicated machine"
                )
            # The assigned box is finished — it never booted, or it was lost
            # and released. The org has been waiting on it ever since, and the
            # replacement is how that wait ends, so it takes the assignment.
            await db.delete(held)
            await db.flush()
    elif tenancy != POOL_TENANCY:
        raise ProvisionError(400, "bad_tenancy", "A machine is pool or dedicated")

    alloc = ComputeAllocation(
        user_id=caller.id,
        org_team_id=acting_org_id,
        machine_type_id=machine_type.id,
        lifecycle=WORKSPACE,
        origin=PROVISIONED,
        tenancy=tenancy,
        name=name,
        storage_gb=storage_gb,
        state="pending",
        created_at=moment,
        price_per_minute_nanos=0,
        true_cost_per_minute_nanos=machine_type.provider_price_per_minute_nanos,
    )
    db.add(alloc)
    await db.flush()
    if not alloc.name:
        alloc.name = f"{provider_kind}-{alloc.id.hex[:8]}"
    profile = boot_profile(provider_kind)
    region = node_placement(provider_kind).region if profile.needs_region else ""
    issued = await node_credential_for(db, alloc, created_by=caller.id, region=region)
    if tenancy == DEDICATED_TENANCY and org_id is not None:
        claim_dedicated(alloc, org_id)
        db.add(
            OrgComputeAssignment(
                org_team_id=org_id, machine_id=alloc.id, assigned_by=caller.id, assigned_at=moment
            )
        )
    secrets = issued.secrets
    actor = admin_actor(caller)
    allocation_id = alloc.id

    try:
        script = await render_node_script(alloc, machine_type, config=config, region=region)
        await provider.store_credential(allocation_id, secrets)
    except (ComputeProviderError, ValueError) as exc:
        # Nothing was started and nothing of ours is held at the provider: the
        # row, its credential and a dedicated org's assignment were never
        # committed, so rolling back leaves the plane exactly as it was.
        await db.rollback()
        await _forget_secret(provider, allocation_id)
        raise ProvisionError(
            502, "provider_error", provider_reason("The provider refused", exc, secrets)
        ) from exc
    transition(db, alloc, PROVISIONING, reason="provision requested", actor=actor, now=moment)
    try:
        await db.commit()
    except Exception:
        await _forget_secret(provider, allocation_id)
        raise

    try:
        machine_id = await provider.run(
            NodeLaunch(
                allocation_id=alloc.id,
                name=pod_name_for(alloc.id),
                type_code=machine_type.provider_type_id,
                storage_gb=storage_gb,
                script=script,
                secrets=secrets,
                tags={
                    "alkera:managed": "true",
                    "alkera:allocation_id": str(alloc.id),
                    "alkera:env": str(config.app_env),
                },
                vcpu=machine_type.vcpu,
                compute_class=machine_type.compute_class,
            )
        )
    except (ComputeProviderError, ValueError) as exc:
        # The provider retried and looked for the machine by its tags before
        # answering with this, so nothing is running for the row. Should it be
        # wrong, the row is finished and the EC2 orphan sweep terminates
        # whatever carries its allocation tag.
        await _fail_provision(db, alloc, provider, actor=actor, error=exc, secrets=secrets)
        raise ProvisionError(
            502, "provider_error", provider_reason("The provider refused", exc, secrets)
        ) from exc
    # The id is on the row before anything else can fail, so every later path
    # — this one's own failure below, the reconcile, a terminate — can reach it.
    alloc.provider_machine_id = machine_id
    await db.commit()
    try:
        # The node reads its secret only once the secret names the node; a
        # node whose secret never did can never claim, so it is ended now
        # rather than left billed until the boot timeout.
        await provider.bind_credential(alloc.id, machine_id)
    except ComputeProviderError as exc:
        try:
            await provider.terminate(machine_id)
        except ComputeProviderError as stop_exc:
            log.warning(
                "compute.provision.terminate_deferred",
                allocation_id=str(alloc.id),
                error=str(stop_exc),
            )
        await _fail_provision(db, alloc, provider, actor=actor, error=exc, secrets=secrets)
        raise ProvisionError(
            502,
            "provider_error",
            provider_reason("The machine's credential could not be bound", exc, secrets),
        ) from exc
    return alloc


def provider_reason(lead: str, error: BaseException, secrets: dict[str, str] | None = None) -> str:
    """``"<lead>: <the provider's words>"``, fit to show the admin who asked.

    The provider's own sentence is the reason a machine did not start, and
    the admin needs it (an expired key, a capacity shortage, a quota). What it
    may never carry is a credential: the node secrets this attempt minted are
    struck out by value, and whatever else looks like one (a bearer token, an
    AWS key id, a JWT) by the shared scrubber."""
    text = str(error)
    for value in (secrets or {}).values():
        if value:
            text = text.replace(value, REDACTED)
    return f"{lead}: {scrub_text(text)}"


async def _forget_secret(provider: NodeProvider, allocation_id: UUID) -> None:
    """Take back a node secret the provider may hold for an attempt that
    left no row. Best-effort: a secret no instance can read is inert."""
    try:
        await provider.delete_credential(allocation_id)
    except ComputeProviderError:
        log.warning("compute.provision.secret_leftover", allocation_id=str(allocation_id))


async def _fail_provision(
    db: AsyncSession,
    alloc: ComputeAllocation,
    provider: NodeProvider,
    *,
    actor: dict[str, Any],
    error: Exception,
    secrets: dict[str, str],
) -> None:
    alloc.error = provider_reason("provider refused", error, secrets)[:1024]
    transition(db, alloc, FAILED, reason=alloc.error[:500], actor=actor)
    await revoke_node_credentials(db, alloc, now=datetime.now(UTC))
    # A dedicated box that never came up must not hold its org: the
    # assignment provision wrote a moment ago goes with the failed row, and
    # the org places by the ordinary rules until a replacement is provisioned.
    await leave_placement(db, alloc, actor=actor_system("compute"))
    await db.commit()
    try:
        await provider.delete_credential(alloc.id)
    except ComputeProviderError:
        log.warning("compute.provision.secret_leftover", allocation_id=str(alloc.id))


async def drain(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    caller: User | None = None,
    actor: dict[str, Any] | None = None,
    reason: str,
    auto_terminate: bool,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Take the machine out of placement; its chats leave as they sleep.

    Said by an admin (``caller``) or by something outside the plane that is
    not a person — a provider's shutdown notice — under its own ``actor``;
    one of the two names who asked, and the history records it."""
    moment = now or datetime.now(UTC)
    if actor is None:
        if caller is None:
            raise ValueError("a drain names who asked for it")
        actor = admin_actor(caller)
    if alloc.state != READY:
        raise ProvisionError(409, "not_ready", f"A {alloc.state} machine cannot be drained")
    transition(db, alloc, DRAINING, reason=reason or "drain", actor=actor, now=moment)
    alloc.drain_requested_at = moment
    alloc.drain_reason = reason
    alloc.drain_kind = None
    alloc.auto_terminate = auto_terminate
    await db.commit()
    return alloc


async def undrain(
    db: AsyncSession, alloc: ComputeAllocation, *, caller: User, now: datetime | None = None
) -> ComputeAllocation:
    if alloc.state != DRAINING:
        raise ProvisionError(409, "not_draining", f"A {alloc.state} machine is not draining")
    transition(db, alloc, READY, reason="undrain", actor=admin_actor(caller), now=now)
    alloc.drain_requested_at = None
    alloc.drain_reason = ""
    alloc.drain_kind = None
    alloc.auto_terminate = False
    await db.commit()
    return alloc


async def sleep(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    caller: User,
    config: Settings = settings,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Put a running machine to sleep: stopped at the provider (its disk kept)
    so it stops costing compute and stops being metered, its chats handed to the
    boxes that can take them. A later :func:`wake` starts it back to ``ready``.

    The chats nothing else may take — a dedicated org's, with no pool to fall
    back on — stay bound and are restated ``asleep`` in this same transaction,
    each announced so an open browser reads the truth at once; the machine's
    own frame says ``asleep`` too. A wake resumes them: a message on one of
    them is what starts the box back.

    The ``asleep`` row commits first, and the provider is asked to stop after
    it, holding only the machine's power claim
    (:func:`~alkera_core.compute.power_lock.stop_after_sleep`): every wake (the
    admin's, a message's, the reconcile's) takes the same claim, so none can
    start the machine while its stop is with the provider; one that meets the
    stop in flight is kept on the row for the reconcile, which starts the
    machine once the stop has finished. The stop is best-effort here; the node
    reconcile stops a machine that stayed running."""
    chain = _admin_chain(caller, alloc)
    kind = await _kind_of(db, alloc)
    provider = make_node_provider(kind, config) if kind else None

    async def rebind(machine: ComputeAllocation) -> int:
        return await rebind_chats_off(db, machine, actor=chain)

    try:
        slept = await sleep_allocation(
            db,
            alloc,
            actor=admin_actor(caller),
            chain=chain,
            end_reason=chat_end.ChatEndReason.BOX_ASLEEP,
            rebind=rebind,
            now=now,
        )
    except NotSleepableError as exc:
        raise ProvisionError(409, "not_ready", str(exc)) from exc
    await db.commit()
    if provider is not None:
        await stop_after_sleep(db, slept.id, provider)
    return slept


async def wake(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    caller: User,
    config: Settings = settings,
    now: datetime | None = None,
) -> ComputeAllocation:
    """Start a sleeping machine back to ``ready``, at an admin's request.

    The provider is started FIRST: if it refuses, the row stays ``asleep`` and
    nothing is billed, rather than a ``ready`` row for a machine that never came
    back. The refused wake is kept on the row and answered ``409 wake_pending``:
    the node reconcile starts the machine once the provider allows (a sleep's
    stop takes about a minute to finish on EC2). On success the metering
    high-water mark is reset to now, so the sleep window — during which the
    machine was stopped and unbilled — is never billed when metering resumes.
    The machine's row is not held while the provider starts it
    (:func:`~alkera_core.compute.wake.wake_allocation`); commits."""
    if alloc.state != ASLEEP:
        raise ProvisionError(409, "not_asleep", f"A {alloc.state} machine is not asleep")
    result = await _start(
        db,
        alloc,
        actor=admin_actor(caller),
        chain=_admin_chain(caller, alloc),
        reason="wake",
        config=config,
        now=now,
    )
    if result.refused is not None:
        # Most often the machine is still stopping from its sleep, which the
        # provider refuses a start for: the wake is kept on the row and the
        # reconcile starts the machine once the provider allows.
        log.warning(
            "compute.wake.start_refused", allocation_id=str(alloc.id), error=str(result.refused)
        )
        raise ProvisionError(
            409,
            "wake_pending",
            f"The machine cannot start yet ({result.refused}); it starts on its own once "
            "the provider allows",
        ) from result.refused
    if not result.started:
        raise ProvisionError(409, "not_asleep", f"A {result.state} machine is not asleep")
    await db.refresh(alloc)
    return alloc


async def wake_for_chat(
    db: AsyncSession,
    machine_id: UUID,
    *,
    ctx: ActingContext,
    config: Settings = settings,
    now: datetime | None = None,
) -> bool:
    """A message reached a chat bound to a sleeping box: start it.

    The send path calls this after placement handed the chat back to its
    org's asleep dedicated box. Whether the box is still asleep is decided
    under the row's lock, so two messages arriving together start it once and
    an admin's wake that landed in between is seen rather than repeated. A
    provider that refuses the start — most often because the machine is still
    stopping from its sleep — leaves the row asleep with the wake recorded on
    it; the message is recorded regardless, and the node reconcile starts the
    machine once the provider allows.

    Ends the caller's transaction first, and holds no row while the provider
    starts the machine (:func:`~alkera_core.compute.wake.wake_allocation`):
    what the caller wrote so far is committed. Returns whether the box was
    started. Never raises for the provider."""
    alloc = await db.get(ComputeAllocation, machine_id, populate_existing=True)
    if alloc is None or alloc.state != ASLEEP:
        return False
    result = await _start(
        db,
        alloc,
        actor={"kind": "member", "email": ctx.subject.label or None, "name": None},
        chain=ctx.audit_dict(),
        reason=CHAT_WAKE_REASON,
        config=config,
        now=now,
    )
    if result.refused is not None:
        log.warning(
            "compute.wake_for_chat.start_refused",
            allocation_id=str(machine_id),
            error=str(result.refused),
        )
    return result.started


async def _start(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    actor: dict[str, Any],
    chain: dict[str, Any],
    reason: str,
    config: Settings,
    now: datetime | None,
) -> WakeResult:
    """The one wake (:func:`alkera_core.compute.wake.wake_allocation`), with
    the machine's provider. Commits."""
    kind = await _kind_of(db, alloc)
    provider = make_node_provider(kind, config) if kind else None
    return await wake_allocation(
        db, alloc.id, provider, actor=actor, chain=chain, reason=reason, now=now
    )


async def terminate(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    caller: User | None,
    force: bool,
    reason: str = "terminate",
    acting: ActingContext | None = None,
    config: Settings = settings,
    now: datetime | None = None,
) -> Released:
    """Release the machine. Refused while it serves chats unless ``force``.

    Whatever the machine held leaves it in the transaction that takes it out
    of service (``releasing``): the org it was dedicated to gets its
    assignment dropped, with an org audit event, and places by the ordinary
    rules again; placement offers the machine's chats to every box that may
    take them; the chats nothing may take are restated ``stranded`` and
    announced, so no reader is ever shown a ``ready`` chat on a box that is
    gone. The provider call is best-effort here; the reconcile finishes a
    release that did not, and finds nothing left to hand off.

    ``acting`` is the request's resolved context when the route has one: the
    chats' frames and the audit event then carry the whole chain."""
    moment = now or datetime.now(UTC)
    if alloc.state == FAILED:
        return await _release_failed(
            db, alloc, caller=caller, reason=reason, acting=acting, config=config, now=moment
        )
    if alloc.state in COMPUTE_TERMINAL_STATES or alloc.state == RELEASING:
        raise ProvisionError(409, "already_released", f"The machine is already {alloc.state}")
    if alloc.chats_served > 0 and not force:
        raise ProvisionError(
            409,
            "machine_has_chats",
            f"This machine serves {alloc.chats_served} chats; drain it or force the release",
        )
    actor = admin_actor(caller) if caller is not None else SYSTEM_ACTOR
    transition(db, alloc, RELEASING, reason=reason, actor=actor, now=moment)
    alloc.terminated_reason = alloc.terminated_reason or "user_released"
    await revoke_node_credentials(db, alloc, now=moment)
    await db.flush()
    # The chats' own stream records the move under the admin's actor chain,
    # the shape every chat frame carries; the machine's history above keeps
    # its short admin actor.
    chain = _chat_actor(caller, acting, alloc)
    # The assignment goes first: placement then reads the org as it now is —
    # pool-served, or waiting on nothing — when it decides where each chat
    # goes, rather than holding the chats for a box that is leaving.
    released_orgs = await _release_assignment(
        db, alloc, caller=caller, acting=acting, reason=reason
    )
    # What the hand-off did to THIS machine's chats: the pass offers every
    # stranded chat on the plane to the boxes standing, so its own count would
    # include chats other releases left behind.
    held = await count_bound_chats(db, machine_id=alloc.id)
    await rebind_chats_off(db, alloc, actor=chain)
    departure: Departure = await leave_placement(db, alloc, actor=chain)
    await db.commit()
    kind = await _kind_of(db, alloc)
    if alloc.origin == PROVISIONED and kind:
        provider = make_node_provider(kind, config)
        try:
            if alloc.provider_machine_id:
                await provider.terminate(alloc.provider_machine_id)
            # Finished here once the provider confirms the machine gone, so the
            # console reads ``released`` on its next read; the reconcile pass
            # only finishes what this could not.
            await settle_release(db, alloc.id, provider, now=moment)
        except ComputeProviderError as exc:
            log.warning("compute.terminate.deferred", allocation_id=str(alloc.id), error=str(exc))
    stranded = len(departure.stranded_chat_ids)
    return Released(
        machine=alloc,
        released_orgs=released_orgs,
        chats_moved=max(held - stranded, 0),
        chats_stranded=stranded,
    )


def _admin_chain(caller: User, alloc: ComputeAllocation) -> dict[str, Any]:
    """The actor chain an admin's action writes on a machine's chats, in the
    org of the machine the action serves (an admin is never assumed to have
    one org of their own)."""
    return ActingContext.for_user(
        user_id=caller.id, org_id=alloc.org_team_id, email=caller.email
    ).audit_dict()


def _chat_actor(
    caller: User | None, acting: ActingContext | None, alloc: ComputeAllocation
) -> dict[str, Any]:
    """The actor chain a release writes on the chats it moves or strands."""
    if acting is not None:
        return acting.audit_dict()
    if caller is not None:
        return _admin_chain(caller, alloc)
    return actor_system("compute")


async def _release_failed(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    caller: User | None,
    reason: str,
    acting: ActingContext | None,
    config: Settings,
    now: datetime,
) -> Released:
    """Release a machine that failed.

    A failure already took the machine off the plane — its credential
    revoked, its org's assignment dropped, its chats handed off — but a
    machine that failed after the provider started it may still be running
    there, billed and unwatched: nothing reconciles a finished row. So the
    provider is told to terminate it first, and a refusal is the admin's
    answer with the row left ``failed`` for another try. The hand-off steps
    run again (each finds nothing on a machine that failed cleanly) so the
    answer says truthfully what the release did. The row's end time stays
    when it failed; the release is an entry in its history.

    The provider is asked with no row held (its call can take minutes); the
    row is taken again after it and released only if it is still ``failed``."""
    kind = await _kind_of(db, alloc)
    if alloc.provider_machine_id and alloc.origin == PROVISIONED and kind:
        alloc_id, machine_id = alloc.id, alloc.provider_machine_id
        await db.commit()
        try:
            await make_node_provider(kind, config).terminate(machine_id)
        except ComputeProviderError as exc:
            raise ProvisionError(
                502, "provider_error", provider_reason("The provider refused the terminate", exc)
            ) from exc
        relocked = (
            await lock_rows(
                db,
                LockRank.ALLOCATION,
                select(ComputeAllocation)
                .where(ComputeAllocation.id == alloc_id)
                .execution_options(populate_existing=True),
            )
        ).scalar_one_or_none()
        if relocked is None or relocked.state != FAILED:
            raise ProvisionError(
                409, "already_released", "The machine changed while it was being released"
            )
        alloc = relocked
    actor = admin_actor(caller) if caller is not None else SYSTEM_ACTOR
    ended = alloc.released_at
    transition(db, alloc, RELEASED, reason=reason, actor=actor, now=now)
    alloc.released_at = ended or alloc.released_at
    await revoke_node_credentials(db, alloc, now=now)
    released_orgs = await _release_assignment(
        db, alloc, caller=caller, acting=acting, reason=reason
    )
    departure = await leave_placement(db, alloc, actor=_chat_actor(caller, acting, alloc))
    await db.commit()
    return Released(
        machine=alloc,
        released_orgs=released_orgs,
        chats_stranded=len(departure.stranded_chat_ids),
    )


async def _release_assignment(
    db: AsyncSession,
    alloc: ComputeAllocation,
    *,
    caller: User | None,
    acting: ActingContext | None,
    reason: str,
) -> list[ReleasedOrg]:
    """Drop the dedicated assignment naming ``alloc`` and record, on the
    org's own audit chain, that its compute returned to ordinary placement.
    The chain is written here, by the backend, because the worker cannot; a
    loss the worker finds drops the row and logs the org instead."""
    released: list[ReleasedOrg] = []
    for org_id in await drop_dedicated_assignments(db, machine_id=alloc.id):
        org = await team_service.get_by_id(db, org_id)
        released.append(ReleasedOrg(id=org_id, name=org.name if org is not None else ""))
        await org_audit_service.record(
            db,
            org_id=org_id,
            actor=caller,
            action=DEDICATED_RELEASED_ACTION,
            target=str(alloc.id),
            detail={"machine_id": str(alloc.id), "machine_name": alloc.name, "reason": reason},
            acting=acting,
        )
        log.info(
            "compute.dedicated.assignment_released",
            allocation_id=str(alloc.id),
            org_id=str(org_id),
            state=alloc.state,
        )
    return released


async def release_org_machines(
    db: AsyncSession,
    org_id: UUID,
    *,
    config: Settings = settings,
    now: datetime | None = None,
) -> list[UUID]:
    """Terminate every machine holding ``org_id``'s data before the org is
    purged (:func:`alkera_core.compute.power.release_for_org_deletion`), each
    through its own provider. The org-deletion path calls this before its
    rows cascade away; a provider that does not confirm a machine gone is a
    :class:`ProvisionError` (502 ``org_machines_not_released``), so the
    deletion stops and a later attempt still finds the machine. Does not
    commit."""
    try:
        return await release_for_org_deletion(
            db, org_id, providers=lambda kind: make_node_provider(kind, config), now=now
        )
    except OrgMachinesNotReleasedError as exc:
        raise ProvisionError(
            502,
            "org_machines_not_released",
            "The organization's machines could not all be shut down; try again",
        ) from exc


def claim_dedicated(alloc: ComputeAllocation, org_id: UUID) -> None:
    """Record that ``org_id``'s chats may now land on this dedicated box, or
    refuse (409) a box another org has already had.

    A box's disk can hold what its org's chats wrote — a chat still awake, a
    hand-back that never landed, the agent's own caches — and nothing the
    backend can reach proves it empty. So a dedicated box serves one org for
    its whole life, and a different org gets a fresh machine rather than a
    wiped one."""
    if alloc.tenant_org_id is not None and alloc.tenant_org_id != org_id:
        raise ProvisionError(
            409,
            "machine_held_another_org",
            "This machine has served another organization; provision a new one for this "
            "organization",
        )
    alloc.tenant_org_id = org_id


async def rebind_chats_off(
    db: AsyncSession, alloc: ComputeAllocation, *, actor: dict[str, Any]
) -> int:
    """Offer the chats this (now unserving) machine held to every live
    platform box; placement decides which may take which. Returns how many
    chats moved; the ones no box may take are left bound to ``alloc`` for the
    caller to restate."""
    boxes = (
        (
            await db.execute(
                select(ComputeAllocation).where(
                    ComputeAllocation.id != alloc.id,
                    ComputeAllocation.state == READY,
                    ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)),
                )
            )
        )
        .scalars()
        .all()
    )
    moved = 0
    for box in boxes:
        moved += len(await placement.bind_stranded_chats_platform(db, alloc=box, actor=actor))
    return moved


async def _kind_of(db: AsyncSession, alloc: ComputeAllocation) -> str:
    machine_type = await db.get(ComputeMachineType, alloc.machine_type_id)
    return machine_type.provider if machine_type is not None else ""


__all__ = [
    "DEDICATED_RELEASED_ACTION",
    "MACHINE_NAME_RE",
    "SYSTEM_ACTOR",
    "ProvisionError",
    "Released",
    "ReleasedOrg",
    "admin_actor",
    "check_machine_name",
    "claim_dedicated",
    "drain",
    "make_node_provider",
    "provision",
    "rebind_chats_off",
    "release_org_machines",
    "sleep",
    "terminate",
    "undrain",
    "wake",
    "wake_for_chat",
]
