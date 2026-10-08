"""Starting one provider machine for a pending allocation, the way the platform
starts a box: its own machine credential, a rendered bootstrap, the provider's
``run``, with the retry policy around the create.

The org-machine reconcile calls :func:`launch_allocation` for an org machine's
``pending`` allocation. Like the admin console's provision, it:

- mints the machine credential the box will claim with, bound to the row, in
  the org the machine serves (a box speaks for one org);
- renders the bootstrap from the provider's boot profile, so a node never boots
  configured for a sandbox its provider cannot give;
- stores the node's secret at the provider, then commits the row
  ``provisioning`` BEFORE asking for a machine, so a worker that dies mid-create
  leaves a row the node reconcile times out, and the pod's name (derived from
  the allocation id) is the handle that finds a create whose answer was lost;
- asks the provider through :func:`provision_with_policy`: a throttle is tried
  again on the same target, no capacity walks the machine type's fallbacks.
  Before trying again after a failure it asks the provider whether the create
  landed anyway, so a lost answer never buys a second machine;
- records every attempt in ``provision_attempts`` and, when the policy gives
  up, ends the row ``failed`` with the provider's failure kind
  (``terminated_reason='provider_capacity'`` for no hardware) and takes back
  everything the box was handed.

It never writes an ``org_compute_assignments`` row: an org machine reaches
placement through ``org_machines``, not through the dedicated-box assignment.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.auth.machine_token import looks_like_machine_token, mint_machine_token
from alkera_core.compute.bootstrap import (
    BootstrapError,
    BootstrapSpec,
    boot_profile,
    render_bootstrap,
)
from alkera_core.compute.box_builds import plan_for
from alkera_core.compute.daemon_source import daemon_source
from alkera_core.compute.handoff import leave_placement
from alkera_core.compute.node_reconcile import revoke_node_credentials
from alkera_core.compute.nodes import NodeProvider
from alkera_core.compute.provider import (
    CAPACITY_FAILURE,
    INVALID_FAILURE,
    ComputeProviderError,
    NodeLaunch,
    node_placement,
)
from alkera_core.compute.provision_policy import (
    Attempt,
    ProvisionExhaustedError,
    fallbacks_of,
    provision_with_policy,
)
from alkera_core.compute.reconcile import pod_name_for
from alkera_core.compute.transitions import transition
from alkera_core.config import Settings
from alkera_core.events import actor_system
from alkera_core.logging import get_logger
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    FAILED,
    PENDING,
    PROVISIONING,
    TERMINATED_PROVIDER_CAPACITY,
    ComputeAllocation,
    ComputeMachineType,
)
from alkera_core.models.machine_credential import MachineCredential
from alkera_core.observability.redaction import REDACTED, scrub_text

log = get_logger(__name__)

#: The one key a node's secret carries; the bootstrap reads it into the box's
#: environment and the daemon presents it on every call.
NODE_CREDENTIAL_KEY = "ALKERA_MACHINE_CREDENTIAL"

LAUNCH_ACTOR: dict[str, Any] = {"kind": "system", "email": None, "name": "reconcile"}

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class LaunchOutcome:
    """What a launch did: whether a machine was started, how many creates it
    took, and the failure kind when it gave up (``""`` when it did not)."""

    started: bool
    attempts: int
    failure_kind: str = ""


async def mint_node_credential(
    db: AsyncSession,
    alloc: ComputeAllocation,
    machine_type: ComputeMachineType,
    *,
    org_id: UUID,
    created_by: UUID | None,
    region: str,
    now: datetime,
) -> dict[str, str]:
    """Mint the credential ``alloc``'s box boots with, bound to the row, and
    return the node's secrets. Any live credential the row held before is
    revoked first, so a node holds one secret, never two. Flushes; the
    caller's transaction owns the row."""
    await db.execute(
        update(MachineCredential)
        .where(MachineCredential.machine_id == alloc.id, MachineCredential.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    raw, token_hash = mint_machine_token()
    if not looks_like_machine_token(raw):
        # Re-checked where the secret leaves the plane: a node must never boot
        # with something the resolver would route to a person.
        raise ValueError("the minted secret is not a machine credential")
    db.add(
        MachineCredential(
            token_hash=token_hash,
            label=alloc.name or f"node-{alloc.id.hex[:8]}",
            org_team_id=org_id,
            machine_type_id=machine_type.id,
            region=region,
            tenancy=DEDICATED_TENANCY,
            machine_id=alloc.id,
            created_by=created_by,
        )
    )
    await db.flush()
    return {NODE_CREDENTIAL_KEY: raw}


async def render_node_script(
    alloc: ComputeAllocation,
    machine_type: ComputeMachineType,
    *,
    config: Settings,
    region: str | None = None,
) -> str:
    """The bootstrap for ``alloc``'s box, from its provider's boot profile, its
    tenancy and the build the box will install. Every path that starts a box
    renders it here, so a box started again gets the script a new one would."""
    profile = boot_profile(machine_type.provider)
    if region is None:
        region = node_placement(machine_type.provider).region if profile.needs_region else ""
    plan = await plan_for(profile, config)
    return render_bootstrap(
        BootstrapSpec(
            provider=machine_type.provider,
            allocation_id=str(alloc.id),
            machine_name=alloc.name or f"node-{alloc.id.hex[:8]}",
            type_code=machine_type.provider_type_id,
            tenancy=alloc.tenancy,
            api_url=config.node_api_url,
            gateway_url=config.node_gateway_url,
            # What its provider can enforce: a RunPod pod cannot nest a kernel
            # and runs "none"; an EC2 node runs what the deployment requires.
            sandbox_mode=profile.sandbox_mode(config.alkera_node_sandbox_required),
            release_base_url=daemon_source().release_base(config.alkera_node_release_base_url),
            credential_secret=f"{config.node_secret_prefix}{alloc.id}",
            region=region,
            drain_ceiling_seconds=profile.drain_ceiling(config.compute_drain_ceiling_seconds),
            chat_idle_minutes=config.compute_chat_idle_minutes,
            chat_memory_pressure_percent=config.compute_chat_memory_pressure_percent,
            log_group=(
                node_placement(machine_type.provider).log_group if profile.needs_region else ""
            ),
        ),
        plan,
    )


def _reason(error: BaseException, secrets: Mapping[str, str]) -> str:
    """The provider's words, fit to keep on the row: the node's secrets struck
    out by value and anything else that looks like a credential scrubbed."""
    text = str(error)
    for value in secrets.values():
        if value:
            text = text.replace(value, REDACTED)
    return scrub_text(text)[:1024]


async def _give_up(
    db: AsyncSession,
    alloc: ComputeAllocation,
    provider: NodeProvider,
    *,
    kind: str,
    reason: str,
    now: datetime,
) -> None:
    """End the row ``failed`` with ``kind`` and take back what the box held."""
    alloc.failure_kind = kind
    alloc.error = reason
    alloc.terminated_reason = alloc.terminated_reason or (
        TERMINATED_PROVIDER_CAPACITY if kind == CAPACITY_FAILURE else "provider_error"
    )
    transition(db, alloc, FAILED, reason=reason[:500], actor=LAUNCH_ACTOR, now=now)
    await revoke_node_credentials(db, alloc, now=now)
    await leave_placement(db, alloc, actor=actor_system("compute"))
    await db.commit()
    try:
        await provider.delete_credential(alloc.id)
    except ComputeProviderError:
        log.warning("compute.launch.secret_leftover", allocation_id=str(alloc.id))


async def launch_allocation(
    db: AsyncSession,
    alloc: ComputeAllocation,
    machine_type: ComputeMachineType,
    provider: NodeProvider,
    *,
    org_id: UUID,
    created_by: UUID | None,
    config: Settings,
    sleep: Sleep,
    now: datetime | None = None,
) -> LaunchOutcome:
    """Start a provider machine for the ``pending`` ``alloc`` (module
    docstring). Commits at each step; returns what happened.

    No row lock is held across a provider call: the node's credential is
    minted and the row moved to ``provisioning`` in the caller's transaction,
    which commits before the secret is stored or the machine created, so
    whatever the caller locked to decide on the launch is released first and
    nothing reads the row as pending meanwhile. A launch that fails after that
    commit ends the row ``failed`` (the node reconcile ends one left
    ``provisioning`` by a crash)."""
    moment = now or datetime.now(UTC)
    if alloc.state != PENDING:
        return LaunchOutcome(started=False, attempts=0)
    try:
        profile = boot_profile(machine_type.provider)
    except BootstrapError as exc:
        await _give_up(db, alloc, provider, kind=INVALID_FAILURE, reason=str(exc), now=moment)
        return LaunchOutcome(started=False, attempts=0, failure_kind=INVALID_FAILURE)
    region = node_placement(machine_type.provider).region if profile.needs_region else ""
    secrets = await mint_node_credential(
        db, alloc, machine_type, org_id=org_id, created_by=created_by, region=region, now=moment
    )
    try:
        script = await render_node_script(alloc, machine_type, config=config, region=region)
    except ValueError as exc:
        await _give_up(
            db, alloc, provider, kind=INVALID_FAILURE, reason=_reason(exc, secrets), now=moment
        )
        return LaunchOutcome(started=False, attempts=0, failure_kind=INVALID_FAILURE)
    transition(
        db,
        alloc,
        PROVISIONING,
        reason="the org machine is starting",
        actor=LAUNCH_ACTOR,
        now=moment,
    )
    alloc.failure_kind = ""
    await db.commit()
    try:
        await provider.store_credential(alloc.id, secrets)
    except ComputeProviderError as exc:
        await _give_up(db, alloc, provider, kind=exc.kind, reason=_reason(exc, secrets), now=moment)
        return LaunchOutcome(started=False, attempts=0, failure_kind=exc.kind)

    name = pod_name_for(alloc.id)
    tags = {
        "alkera:managed": "true",
        "alkera:allocation_id": str(alloc.id),
        "alkera:env": str(config.app_env),
    }

    async def create(attempt: Attempt) -> str:
        alloc.provision_attempts = (alloc.provision_attempts or 0) + 1
        if attempt.number > 1:
            # The last create may have been taken even though its answer never
            # came back: adopt that machine rather than buy a second.
            found = await provider.find(alloc.id)
            if found is not None:
                return found.machine_id
        return await provider.run(
            NodeLaunch(
                allocation_id=alloc.id,
                name=name,
                type_code=machine_type.provider_type_id,
                storage_gb=alloc.storage_gb,
                script=script,
                secrets=secrets,
                tags=tags,
                vcpu=machine_type.vcpu,
                compute_class=machine_type.compute_class,
                gpu_count=machine_type.gpu_count,
                overrides=attempt.overrides,
            )
        )

    try:
        outcome = await provision_with_policy(
            create, fallbacks=fallbacks_of(machine_type.provider_config), sleep=sleep
        )
    except ProvisionExhaustedError as exc:
        if exc.kind == CAPACITY_FAILURE:
            machine_type.capacity_refused_at = datetime.now(UTC)
        await _give_up(
            db, alloc, provider, kind=exc.kind, reason=_reason(exc, secrets), now=datetime.now(UTC)
        )
        return LaunchOutcome(started=False, attempts=exc.attempts, failure_kind=exc.kind)
    alloc.provider_machine_id = outcome.result
    # The provider had the hardware: an earlier refusal no longer says much.
    machine_type.capacity_refused_at = None
    await db.commit()
    try:
        # The node reads its secret only once the secret names the node; a
        # node whose secret never did can never claim, so it is ended now
        # rather than left billed until the boot timeout.
        await provider.bind_credential(alloc.id, outcome.result)
    except ComputeProviderError as exc:
        try:
            await provider.terminate(outcome.result)
        except ComputeProviderError:
            log.warning("compute.launch.terminate_deferred", allocation_id=str(alloc.id))
        await _give_up(
            db, alloc, provider, kind=exc.kind, reason=_reason(exc, secrets), now=datetime.now(UTC)
        )
        return LaunchOutcome(started=False, attempts=outcome.attempts, failure_kind=exc.kind)
    log.info(
        "compute.launch.started",
        allocation_id=str(alloc.id),
        attempts=outcome.attempts,
        target=outcome.target,
    )
    return LaunchOutcome(started=True, attempts=outcome.attempts)


__all__ = [
    "NODE_CREDENTIAL_KEY",
    "LaunchOutcome",
    "launch_allocation",
    "mint_node_credential",
    "render_node_script",
]
