"""Adding a machine the org runs by its SSH details, and testing one first.

The route decides through ``org_machine.access`` (``WRITE``, operation
``add``) with the facts :func:`add_attrs` resolves; this module then does the
work. A test reads the host's key without signing in, signs in pinned to that
key, and reads what a node needs. An add does the same against the
fingerprint the admin confirmed, refuses when the host presents another key or
lacks a prerequisite, and writes the org machine, its first allocation and the
endpoint with the credential sealed. The reconcile installs the node.

An added machine needs a catalog row and an offering for its foreign keys:
one inactive ``ssh`` machine type per hardware shape and one unlisted,
unpurchasable offering for it at a rate of zero, made on first use. Neither
appears in any buy list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from alkera_core.authz import Action, authorize
from alkera_core.authz.policies import org_machine as policy
from alkera_core.compute.node_bundle import install_refusal
from alkera_core.compute.node_reach import (
    NODE_API_URL_SETTING,
    NODE_GATEWAY_URL_SETTING,
    loopback_callback_refusal,
)
from alkera_core.compute.org_machines import (
    AdmittedRate,
    create_org_machine,
    unlisted_offering_for,
)
from alkera_core.compute.provider import kinds_with, provider_for_kind
from alkera_core.compute.ssh import (
    AsyncsshTransport,
    HostFacts,
    HostKeyMismatchError,
    ProbeResult,
    SshAuth,
    SshError,
    SshTransport,
    host_key_type,
    probe_host,
    seal_credential,
    vet_host,
)
from alkera_core.config import settings
from alkera_core.models import ComputeAllocation, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import ADDED, OrgMachine
from alkera_core.models.ssh_machines import SshMachineEndpoint
from alkera_core.schemas.org_machines import SshEndpointRead
from alkera_core.schemas.ssh_machines import SshMachineAdd, SshMachineTarget, SshMachineTestRead
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.audit import record_org_audit
from backend.services.compute import placement
from backend.services.compute.org_machine_access import (
    OrgMachineError,
    Viewer,
    announce,
    name_taken,
    resource_for,
)
from backend.services.compute.org_machine_buying import name_is_taken, validate_audience

#: The audit action an add writes.
ADDED_AUDIT = "machine.added"


def default_transport() -> SshTransport:
    """The transport routes use; a test overrides the dependency that calls it."""
    return AsyncsshTransport()


def adding_enabled() -> bool:
    """Whether this deployment lets an org attach a host: the setting is on and
    a configured provider attaches hosts."""
    if not settings.ssh_machines_on:
        return False
    return any(
        provider_for_kind(kind, settings).configured()
        for kind in kinds_with(lambda traits: traits.attaches_hosts)
    )


async def add_attrs(db: AsyncSession, viewer: Viewer, *, sets_pool: bool) -> dict[str, object]:
    """The facts an add (or a test) is decided on."""
    org = await viewer.roles_on(viewer.org_id)
    return {
        "in_org": org.in_org,
        "is_org_admin": viewer.is_org_admin,
        "email_verified": viewer.email_verified,
        "operation": policy.ADD,
        "adding_enabled": adding_enabled(),
        "sets_pool": sets_pool,
        "org_allows_pool": await placement.org_pool_applies(db, org_id=viewer.org_id),
    }


async def can_add(db: AsyncSession, viewer: Viewer) -> bool:
    """The verdict the Machines page reads to offer "Add machine"."""
    attrs = await add_attrs(db, viewer, sets_pool=False)
    return authorize(
        viewer.ctx, Action.WRITE, resource_for(None, viewer=viewer, id="new"), attrs
    ).allowed


def _auth(body: SshMachineTarget) -> SshAuth:
    if body.auth_kind == "password":
        assert body.password is not None
        return SshAuth(kind="password", secret=body.password.get_secret_value())
    assert body.private_key is not None
    return SshAuth(
        kind="private_key",
        secret=body.private_key.get_secret_value().strip() + "\n",
        passphrase=body.passphrase.get_secret_value() if body.passphrase else "",
    )


def _refused(exc: SshError) -> OrgMachineError:
    status = 409 if isinstance(exc, HostKeyMismatchError) else 422
    return OrgMachineError(exc.code, str(exc), status=status)


#: The code a test and an add answer when the host could not reach this
#: deployment back at the address its node would be given.
CALLBACK_UNREACHABLE = "callback_unreachable"
#: The code a test and an add answer when this deployment holds no node
#: bundle for the host's architecture.
NO_NODE_BUNDLE = "no_node_bundle"


def _bundle_refusal(facts: HostFacts) -> str | None:
    return install_refusal(settings.node_bundle_dir, facts.arch)


def node_callbacks() -> dict[str, str]:
    """Every address an attached host's node calls this deployment back on,
    by the setting that names it: the API and the model gateway."""
    return {
        NODE_API_URL_SETTING: settings.node_api_url,
        NODE_GATEWAY_URL_SETTING: settings.node_gateway_url,
    }


async def _probe(
    body: SshMachineTarget, transport: SshTransport, *, expected_fingerprint: str | None
) -> tuple[ProbeResult, str | None]:
    """The host's key and facts, and why its node could never reach this
    deployment back (``None`` when it can). A loopback address is refused
    without asking the host; any other is fetched from the host itself."""
    target = await vet_host(body.host, body.port, allow_private=settings.ssh_machines_private_ok)
    callbacks = node_callbacks()
    refused = loopback_callback_refusal(callbacks)
    found = await probe_host(
        transport,
        target,
        username=body.username,
        auth=_auth(body),
        expected_fingerprint=expected_fingerprint,
        callbacks={} if refused is not None else callbacks,
    )
    return found, refused or found.callback_error


async def test_connection(body: SshMachineTarget, transport: SshTransport) -> SshMachineTestRead:
    """Connect, and say what was found. A host this deployment does not
    connect to is refused (422); one that cannot be reached or refuses the
    credential answers ``reachable`` false with the reason."""
    try:
        found, callback_error = await _probe(body, transport, expected_fingerprint=None)
    except SshError as exc:
        if exc.code == "address_not_allowed":
            raise _refused(exc) from exc
        return SshMachineTestRead(reachable=False, error_code=exc.code, message=str(exc))
    facts = found.facts
    bundle_refusal = _bundle_refusal(facts)
    error_code = None
    if bundle_refusal is not None:
        error_code = NO_NODE_BUNDLE
    elif callback_error is not None:
        error_code = CALLBACK_UNREACHABLE
    return SshMachineTestRead(
        reachable=True,
        host_key_fingerprint=found.host_key.fingerprint,
        host_key_type=found.host_key.key_type,
        os=facts.os,
        arch=facts.arch,
        vcpu=facts.vcpu,
        memory_gb=facts.memory_gb,
        disk_gb=facts.disk_gb,
        gpu_count=facts.gpu_count,
        prerequisites_met=not facts.missing and error_code is None,
        missing=facts.missing,
        error_code=error_code,
        message=bundle_refusal or callback_error,
    )


def _shape_code(facts: HostFacts) -> str:
    arch = "".join(ch for ch in facts.arch.lower() if ch.isalnum() or ch == "_") or "unknown"
    return f"host-{arch}-{facts.vcpu}c-{facts.memory_gb}g-{facts.gpu_count}gpu"


async def _catalog_for(
    db: AsyncSession, facts: HostFacts
) -> tuple[ComputeOffering, ComputeMachineType]:
    """The inactive machine type and unlisted offering an added host of this
    shape is recorded under, made on first use."""
    code = _shape_code(facts)
    wanted = (ComputeMachineType.provider == "ssh") & (ComputeMachineType.provider_type_id == code)
    await db.execute(
        pg_insert(ComputeMachineType)
        .values(
            id=uuid4(),
            provider="ssh",
            provider_type_id=code,
            display_name=f"Your machine ({facts.vcpu} vCPU, {facts.memory_gb} GB)",
            compute_class="gpu" if facts.gpu_count > 0 else "cpu",
            gpu_count=facts.gpu_count,
            vcpu=facts.vcpu,
            memory_gb=facts.memory_gb,
            disk_gb=facts.disk_gb,
            provider_price_per_minute_nanos=0,
            active=False,
            available_for_new=False,
        )
        .on_conflict_do_nothing(constraint="uq_compute_machine_types_provider_type")
    )
    machine_type = (await db.execute(select(ComputeMachineType).where(wanted))).scalar_one()
    return await unlisted_offering_for(db, machine_type), machine_type


async def add(
    db: AsyncSession, viewer: Viewer, body: SshMachineAdd, transport: SshTransport
) -> OrgMachine:
    """Add the host as an org machine, once the route decided on it. Refused,
    before anything is written: a host whose key is not the one confirmed
    (409 ``host_key_mismatch``), one that cannot be reached or refuses the
    credential (422), one missing a prerequisite (422 ``host_not_ready``), one
    whose architecture this deployment holds no node bundle for (422
    ``no_node_bundle``), one whose node could never reach this deployment back (422
    ``callback_unreachable``), an audience outside the caller's scope (422)
    and a name in use (409)."""
    try:
        found, callback_error = await _probe(
            body, transport, expected_fingerprint=body.host_key_fingerprint
        )
    except SshError as exc:
        raise _refused(exc) from exc
    facts = found.facts
    if facts.missing:
        raise OrgMachineError(
            "host_not_ready",
            "The host needs " + ", ".join(facts.missing) + ".",
            status=422,
        )
    bundle_refusal = _bundle_refusal(facts)
    if bundle_refusal is not None:
        raise OrgMachineError(NO_NODE_BUNDLE, bundle_refusal, status=422)
    if callback_error is not None:
        raise OrgMachineError(CALLBACK_UNREACHABLE, callback_error, status=422)
    audience = await validate_audience(
        db, viewer, owner_team_id=viewer.org_id, audience=body.audience
    )
    if await name_is_taken(db, org_id=viewer.org_id, name=body.name):
        raise name_taken()
    offering, machine_type = await _catalog_for(db, facts)
    auth = _auth(body)
    # A name taken between the check above and this insert is refused by
    # NAME_LIVE_INDEX, which org_machines registers as name_taken, so the loser
    # of that race gets the same 409 as a sequential duplicate.
    machine = await create_org_machine(
        db,
        org_id=viewer.org_id,
        owner_team_id=viewer.org_id,
        offering=offering,
        machine_type=machine_type,
        name=body.name,
        acquisition=ADDED,
        free_until=None,
        use_mode=body.use_mode,
        storage_gb=max(facts.disk_gb, 1),
        audience=audience,
        idle_stop_minutes=body.idle_stop_minutes,
        monthly_cap_nanos=None,
        admitted=AdmittedRate(rate_per_minute_nanos=0, billing_account_id=None),
        created_by=viewer.user.id,
    )
    db.add(
        SshMachineEndpoint(
            org_team_id=viewer.org_id,
            org_machine_id=machine.id,
            host=body.host.strip(),
            port=body.port,
            username=body.username,
            auth_kind=auth.kind,
            secret_sealed=seal_credential(auth),
            host_key=found.host_key.public_key,
            host_key_fingerprint=found.host_key.fingerprint,
            os=facts.os,
            arch=facts.arch,
        )
    )
    await db.flush()
    await record_org_audit(
        db,
        org_id=viewer.org_id,
        actor=viewer.user,
        action=ADDED_AUDIT,
        target=str(machine.id),
        detail={
            "name": machine.name,
            "host": body.host.strip(),
            "port": body.port,
            "username": body.username,
            "auth_kind": auth.kind,
            "host_key_fingerprint": found.host_key.fingerprint,
            "use_mode": machine.use_mode,
        },
        acting=viewer.ctx,
    )
    await announce(db, machine, actor=viewer.ctx.audit_dict())
    return machine


async def endpoint_read(db: AsyncSession, machine: OrgMachine) -> SshEndpointRead | None:
    """Where an added machine is reached, never its credential."""
    row = (
        await db.execute(
            select(SshMachineEndpoint).where(
                SshMachineEndpoint.org_machine_id == machine.id,
                SshMachineEndpoint.org_team_id == machine.org_team_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return SshEndpointRead(
        host=row.host,
        port=row.port,
        username=row.username,
        auth_kind="private_key" if row.auth_kind == "private_key" else "password",
        host_key_fingerprint=row.host_key_fingerprint,
        host_key_type=host_key_type(row.host_key),
    )


async def forget_unlaunched(db: AsyncSession, machine: OrgMachine) -> None:
    """On removal, forget the credential at once when no host was ever
    started for the machine: the reconcile has nothing to uninstall, so no
    later step would forget it. A machine that was launched keeps it until
    the uninstall has run (``SshProvider.terminate``)."""
    if machine.acquisition != ADDED:
        return
    alloc = (
        await db.get(ComputeAllocation, machine.current_allocation_id)
        if machine.current_allocation_id is not None
        else None
    )
    if alloc is not None and alloc.provider_machine_id:
        return
    endpoint = (
        await db.execute(
            select(SshMachineEndpoint).where(
                SshMachineEndpoint.org_machine_id == machine.id,
                SshMachineEndpoint.org_team_id == machine.org_team_id,
            )
        )
    ).scalar_one_or_none()
    if endpoint is not None:
        endpoint.secret_sealed = ""
        endpoint.updated_at = datetime.now(UTC)
        await db.flush()


__all__ = [
    "ADDED_AUDIT",
    "CALLBACK_UNREACHABLE",
    "add",
    "add_attrs",
    "adding_enabled",
    "can_add",
    "default_transport",
    "endpoint_read",
    "forget_unlaunched",
    "node_callbacks",
    "test_connection",
]
