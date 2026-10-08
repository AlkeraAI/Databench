"""Platform-staff shapes for the machines page: every box the platform runs,
provisioned by the backend or registered by hand, with its lifecycle, cost and
load. ``MachineRow`` keeps every field of the credential row the dedicated-box
picker already reads (``credential_id`` … ``assigned_org_name``) so one list
serves both."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from alkera_core.models.compute import ComputeTenancy
from alkera_core.schemas.org_machines import MachineFaultRead
from alkera_core.status import StatusFact

#: An org id as a box reports it (a UUID string, kept a string so the sample
#: is stored as the box sent it).
_OrgId = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]

MachineStateLiteral = Literal[
    "pending",
    "provisioning",
    "bootstrapping",
    "ready",
    "draining",
    "asleep",
    "releasing",
    "released",
    "failed",
    "lost",
]
LivenessLiteral = Literal[
    "starting", "ready", "draining", "restarting", "unreachable", "asleep", "none"
]
#: Every tenancy a machine may stand under, a person's own box included: the
#: machines page lists each one, so its rows accept the model's whole vocabulary.
TenancyLiteral = ComputeTenancy
#: The boundary a box reports it runs chats under. ``none`` is also what a
#: machine that has not reported (or has no allocation yet) reads: the same
#: fail-closed default the placement guard applies.
SandboxLiteral = Literal["gvisor", "none"]
ProvisionProvider = Literal["ec2", "localdev", "runpod"]


class OrgRef(BaseModel):
    id: str
    name: str


class MachineDrain(BaseModel):
    requested_at: datetime
    reason: str = ""


class MachineCost(BaseModel):
    price_per_minute_nanos: int = 0
    minutes_billed: int = 0
    true_cost_nanos: int = 0
    currency: Literal["USD"] = "USD"


class GpuSample(BaseModel):
    """One GPU as the box sampled it on its last beat."""

    index: int = Field(ge=0)
    name: str = Field(default="", max_length=128)
    memory_used_bytes: int = Field(default=0, ge=0)
    memory_total_bytes: int = Field(default=0, ge=0)
    utilization_percent: float = Field(default=0.0, ge=0, le=100)


class MachineResources(BaseModel):
    cpu_percent: float = Field(default=0.0, ge=0)
    memory_used_bytes: int = Field(default=0, ge=0)
    memory_limit_bytes: int = Field(default=0, ge=0)
    disk_used_bytes: int = Field(default=0, ge=0)
    disk_total_bytes: int = Field(default=0, ge=0)
    # A box that runs a worker per org: how many it runs, how many orgs its
    # memory holds a worker for at once (placement sends no new org past it;
    # 0 = a box with no per-org workers, not gated), and the workers' memory.
    org_workers: int = Field(default=0, ge=0)
    org_worker_capacity: int = Field(default=0, ge=0)
    org_worker_memory_bytes: int = Field(default=0, ge=0)
    # How many of its orgs' workers keep failing soon after they start (a
    # crash loop): the box is up, but those orgs' chats are not served.
    org_workers_failing: int = Field(default=0, ge=0)
    # Which orgs those are: placement serves none of their chats on the box
    # (``alkera_core.compute.machines.failing_orgs``). Platform staff only.
    org_workers_failing_ids: list[_OrgId] = Field(
        default_factory=list, max_length=4096, exclude_if=lambda ids: not ids
    )
    # Every GPU the box has; empty from a box with none, or an older daemon.
    # Left out of the dump when empty, so a sample from a daemon that reports
    # no GPUs is stored and read back exactly as that daemon sent it.
    gpus: list[GpuSample] = Field(
        default_factory=list, max_length=64, exclude_if=lambda gpus: not gpus
    )
    # The worker slots a new org can still take on the box right now. At 0,
    # placement sends no org the box does not already hold a chat for; ``None``
    # (a box that does not report it) is not gated.
    org_slots_free: int | None = Field(default=None, ge=0)


class MachineIsolationRead(BaseModel):
    """How far apart the box can keep the orgs it serves, as its probe found
    (``alkera_core.compute.box_isolation``)."""

    profile: Literal["org_namespaces", "single_org"] = "single_org"
    mechanisms: list[str] = Field(default_factory=list)


class MachineRow(BaseModel):
    id: str
    name: str = ""
    provider: str = ""
    provider_machine_id: str = ""
    machine_type_code: str = ""
    region: str = ""
    state: MachineStateLiteral = "pending"
    liveness: LivenessLiteral = "none"
    tenancy: TenancyLiteral = "pool"
    sandbox: SandboxLiteral = "none"
    dedicated_org: OrgRef | None = None
    capacity: int = 0
    # The chats bound to the machine, split by what the machine can do for
    # them. Decided from the MACHINE's state, never from a chat's own word.
    # ``chats_served`` is live load: chats on a box that is serving (starting,
    # ready, draining, restarting, or silent but still on the plane).
    chats_served: int = 0
    # Chats bound to a box that is off the plane (released, failed, lost) and
    # that nothing could take: they wait for the next box, or their next message.
    chats_stranded: int = 0
    # Chats parked on a sleeping box; a wake resumes them.
    chats_asleep: int = 0
    storage_gb: int = 0
    heartbeat_at: datetime | None = None
    daemon_version: str = ""
    created_at: datetime
    state_changed_at: datetime | None = None
    drain: MachineDrain | None = None
    # When a wake the provider refused (the machine was still stopping) was
    # asked for; the machine starts on its own once the provider allows. NULL
    # when no wake is waiting.
    wake_requested_at: datetime | None = None
    cost: MachineCost = Field(default_factory=MachineCost)
    resources: MachineResources | None = None
    # ``None`` for a box that has not reported one (an older build), which
    # placement reads as ``single_org``.
    isolation: MachineIsolationRead | None = None
    # Set while the box answers but no worker of it can serve.
    fault: MachineFaultRead | None = None
    origin: Literal["registered", "provisioned"] = "registered"
    # -- the credential row the dedicated-box picker reads ------------------
    credential_id: str | None = None
    label: str = ""
    instance_type: str = ""
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    machine_id: str | None = None
    machine_name: str = ""
    status: LivenessLiteral = "none"
    last_heartbeat_at: datetime | None = None
    true_cost_per_minute_nanos: int = 0
    assigned_org_id: str | None = None
    assigned_org_name: str = ""
    #: Where the machine stands as the fleet draws it, words and all. A
    #: revoked credential with nothing behind it reads revoked, not starting.
    status_fact: StatusFact | None = None


class MachineList(BaseModel):
    items: list[MachineRow]


class MachineChat(BaseModel):
    id: str
    title: str = ""
    org: OrgRef
    user: dict[str, str | None]
    machine_status: str = ""
    last_activity_at: datetime | None = None


class EventActor(BaseModel):
    # ``member`` is a person in the org the machine serves whose message woke
    # it — the one edge a tenant, rather than an admin or the plane, causes.
    kind: Literal["admin", "system", "box", "member"] = "system"
    email: str | None = None
    name: str | None = None


class MachineEvent(BaseModel):
    at: datetime
    from_state: str
    to_state: str
    reason: str = ""
    actor: EventActor


class MachineDetail(MachineRow):
    chats: list[MachineChat] = Field(default_factory=list)
    events: list[MachineEvent] = Field(default_factory=list)


class MachineReleased(MachineRow):
    """What a terminate answers: the row as it now stands, plus what the
    release did to placement — the orgs whose dedicated assignment named this
    machine and who now place by the ordinary rules again, how many of its
    chats another box took, and how many wait stranded on it."""

    released_orgs: list[OrgRef] = Field(default_factory=list)
    chats_moved: int = 0


class MachineTypeStorage(BaseModel):
    min_gb: int
    max_gb: int
    kind: str


class MachineTypeAvailability(BaseModel):
    status: Literal["available", "limited", "unavailable", "unknown"]
    detail: str = ""
    checked_at: datetime


class MachineTypeQuota(BaseModel):
    vcpu_limit: int
    vcpu_in_use: int
    vcpu_available: int


class MachineTypeRow(BaseModel):
    # The catalog row's id: what an offering names its machine type by.
    id: str
    provider: str
    code: str
    vcpu: int
    memory_gb: int
    gpu: int
    storage: MachineTypeStorage
    price_per_minute_nanos: int
    available: bool
    #: Whether an offering may be sold on this type in this deployment: it can
    #: start machines at the type's provider. The offering editor lists only
    #: these; the catalog write refuses any other.
    can_offer: bool
    availability: MachineTypeAvailability
    quota: MachineTypeQuota | None = None


class ProviderStatus(BaseModel):
    """Whether this deployment can start machines at one provider, and, when it
    cannot, the settings it is missing."""

    kind: ProvisionProvider
    configured: bool
    reason: str = ""


class MachineTypeList(BaseModel):
    items: list[MachineTypeRow]
    #: Every provider the console can provision through, configured or not, so
    #: a provider with no catalog rows still reads as refused rather than absent.
    providers: list[ProviderStatus] = Field(default_factory=list)


class UnmanagedMachine(BaseModel):
    """A machine a provider bills this account for that no allocation row owns:
    started by hand, or left behind. Read-only; the console never acts on it."""

    provider: str
    provider_machine_id: str
    name: str = ""
    phase: str = ""
    raw_status: str = ""
    created_at: datetime | None = None


class ProviderNote(BaseModel):
    provider: str
    detail: str


class UnmanagedMachineList(BaseModel):
    items: list[UnmanagedMachine]
    #: Providers that could not be listed this time; their machines are unknown,
    #: not absent.
    unavailable: list[ProviderNote] = Field(default_factory=list)


class ProvisionRequest(BaseModel):
    provider: ProvisionProvider
    machine_type_code: str = Field(min_length=1, max_length=128)
    storage_gb: int = Field(ge=10, le=16384)
    tenancy: Literal["pool", "dedicated"]
    org_id: str | None = None
    name: str | None = Field(default=None, max_length=128)


class DrainRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=512)
    auto_terminate: bool = False


class TerminateRequest(BaseModel):
    force: bool = False


__all__ = [
    "DrainRequest",
    "EventActor",
    "MachineChat",
    "MachineCost",
    "MachineDetail",
    "MachineDrain",
    "MachineEvent",
    "MachineFaultRead",
    "MachineIsolationRead",
    "MachineList",
    "MachineReleased",
    "MachineResources",
    "MachineRow",
    "MachineTypeAvailability",
    "MachineTypeList",
    "MachineTypeQuota",
    "MachineTypeRow",
    "MachineTypeStorage",
    "OrgRef",
    "ProviderNote",
    "ProviderStatus",
    "ProvisionRequest",
    "TerminateRequest",
    "UnmanagedMachine",
    "UnmanagedMachineList",
]
