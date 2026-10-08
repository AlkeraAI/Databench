"""Compute-plane request/response shapes — the tenant-visible surface.

In-flight HTTP shapes only: plain ``BaseModel`` (nothing here is persisted).
Money is integer nano-USD; ids are UUID strings.

Every figure here is what the CUSTOMER is billed. What a machine costs us —
the provider's price on the catalog row, the pinned true cost on an
allocation, the true cost on a ledger row — has no field on any model in this
module, by construction rather than by omission: a cost-leak guard test walks
every tenant-reachable schema and fails on a cost, margin or provider-price
field. Keep it that way; a platform-staff read model is a separate schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from alkera_core.schemas.compute_machines import MachineResources
from alkera_core.schemas.org_machines import GpuSpec
from alkera_core.status import StatusFact

MachineStatusLiteral = Literal[
    "starting", "ready", "draining", "restarting", "unreachable", "asleep"
]
MachineStateLiteral = Literal[
    "starting", "ready", "draining", "restarting", "unreachable", "asleep", "pool", "none"
]
#: The two-state sandbox mode a box reports: gVisor around the agent, or none.
SandboxModeLiteral = Literal["gvisor", "none"]


class MachineTypeInfo(BaseModel):
    """One provisionable machine type from the catalog, priced for the caller."""

    id: str
    provider: str = "runpod"
    provider_type_id: str
    display_name: str
    compute_class: str = "cpu"
    gpu_count: int = 0
    vcpu: int = 0
    memory_gb: int = 0
    active: bool = True
    # Live-catalog stock: NONE|LOW|MEDIUM|HIGH|unknown. Informational for the picker.
    availability: str = "unknown"
    # Whether NEW machines of this type may be provisioned (out-of-stock types are
    # retained + shown but blocked for new allocations; running ones are unaffected).
    available_for_new: bool = True
    # What the CALLER is billed per minute for this type under their compute
    # grant; None when no grant admits it (a start would be refused).
    rate_per_minute_nanos: int | None = None


class ComputeAllocationInfo(BaseModel):
    """A user's provisioned (or terminated) compute allocation."""

    id: str
    machine_type: MachineTypeInfo
    lifecycle: str = "session"
    name: str = ""
    state: str  # pending|provisioning|ready|releasing|released|failed
    provider_machine_id: str = ""
    public_ip: str = ""
    ssh_port: int = 0
    ssh_user: str = "root"
    created_at: datetime
    ready_at: datetime | None = None
    released_at: datetime | None = None
    minutes_elapsed: int = 0
    # Estimated/actual billed spend in nano-USD: once the meter has run it is the
    # actually-debited ``billed_nanos``; before that, the minutes x pinned-rate
    # estimate. Always populated.
    spend_nanos: int = 0
    minutes_billed: int = 0
    billed_nanos: int = 0
    terminated_reason: str = ""
    low_credit: bool = False
    # Self-imposed lease ceiling (total minutes) of a session machine; None = no
    # time cap (credit exhaustion is the only automatic kill). Never set on a
    # workspace machine.
    max_lease_minutes: int | None = None
    # A human sentence the agent + UI show.
    status_message: str = ""
    error: str = ""
    project_path: str = ""
    session_id: str = ""
    # A workspace machine's reachability (derived from its heartbeat); None for a
    # session machine.
    machine_status: MachineStatusLiteral | None = None
    last_heartbeat_at: datetime | None = None


class ComputeAllocationCreateRequest(BaseModel):
    machine_type_id: str
    project_path: str = ""
    session_id: str = ""
    # Optional self-imposed lease cap (total minutes); omit/None for NO time limit
    # (credits are the safety net). Renewable via the renew route.
    max_minutes: int | None = Field(default=None, ge=1)


class ComputeRenewRequest(BaseModel):
    # The NEW total-minutes lease ceiling for a running allocation; None clears the
    # cap (unlimited — credit exhaustion becomes the only automatic kill).
    max_minutes: int | None = Field(default=None, ge=1)


class MachineRegisterRequest(BaseModel):
    """Register a running pod as the org's workspace machine (idempotent by
    ``provider_pod_id``: registering the same pod twice returns the same row)."""

    provider: str = Field(default="runpod", min_length=1, max_length=32)
    provider_pod_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    # The catalog's provider type id for the pod's flavor (e.g. ``cpu3c``).
    machine_type_code: str = Field(min_length=1, max_length=128)
    # A fresh id the daemon process generated for itself. The heartbeat carries
    # it, and a beat from any process but the one that registered last is
    # refused: an exiting process cannot drain its successor's row.
    daemon_instance_id: str | None = Field(default=None, min_length=1, max_length=64)


class MachineClaimRequest(BaseModel):
    """A platform box claims the machine its credential was minted for. The
    kind, size and tenancy come from the credential, never from the box; the
    box says only which instance it is and what it can hold."""

    provider_pod_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    capacity: int = Field(default=6, ge=1, le=1000)
    daemon_version: str = Field(default="", max_length=32)
    # A fresh id the daemon process generated for itself. The heartbeat carries
    # it, and a beat from any process but the one that registered last is
    # refused: an exiting process cannot drain its successor's row.
    daemon_instance_id: str | None = Field(default=None, min_length=1, max_length=64)
    # The sandbox mode the box enforces. The placement guard reads it: a "none"
    # box never joins the shared pool. Fail closed: an older daemon that says
    # nothing reads "none" and stays off the pool.
    sandbox: SandboxModeLiteral = "none"


class MachineIsolationReport(BaseModel):
    """How far apart the box can keep its orgs, as its probe found. The
    profile placement reads is the ``org_isolation`` capability; this is the
    detail beside it. Names this side does not know are kept out on read."""

    profile: str = Field(max_length=32)
    mechanisms: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=16
    )


class MachineFaultReport(BaseModel):
    """Why the box serves none of the orgs it was asked to: a
    ``WorkerFault`` code (an unknown one reads as ``other``) and the failure's
    own words, scrubbed on the box and again here."""

    code: str = Field(min_length=1, max_length=64)
    summary: str = Field(default="", max_length=300)


class MachineHeartbeatRequest(BaseModel):
    """What a box says with each heartbeat, all optional: how many chats it
    can hold and holds now (how a pool is spread), the daemon it runs, and
    whether it has been told to stop and is finishing what it holds."""

    capacity: int | None = Field(default=None, ge=1, le=1000)
    chats_served: int | None = Field(default=None, ge=0)
    daemon_version: str | None = Field(default=None, max_length=32)
    # A box drains once and does not come back: the deploy that told it to stop
    # is going to replace the process. ``None`` (an older daemon, or one that
    # simply has nothing to say) leaves the row as it stands rather than
    # un-draining a box mid-shutdown.
    draining: bool | None = None
    # The box's supervisor is restarting this daemon in place: the row leaves
    # placement like a drain but keeps its chats, and reads ``restarting``
    # until the new process registers. A plain ``draining`` outranks it.
    restarting: bool | None = None
    # The process speaking, as it registered. ``None`` (an older daemon) is
    # not checked.
    daemon_instance_id: str | None = Field(default=None, min_length=1, max_length=64)
    # What the box is using now (cpu over the beat interval, memory against its
    # cgroup limit, the work volume's disk). ``None`` from an older daemon
    # leaves the last sample standing.
    resources: MachineResources | None = None
    # The sandbox mode the box enforces, re-reported on every beat so a box that
    # was reconfigured and restarted updates its row. ``None`` from an older
    # daemon leaves the last reported mode standing.
    sandbox: SandboxModeLiteral | None = None
    # What this build of the daemon can do that an older one cannot, restated
    # whole on every beat (``workspaces``: it runs a workspace's chats in one
    # sandbox under one lease; ``model_switch_v1``: a chat's model switch runs on
    # the next turn). ``None`` (an older daemon) is read as able to do nothing
    # new, and clears what the row held.
    capabilities: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=32
    )
    # How far apart the box keeps its orgs, restated on every beat. ``None``
    # (an older daemon) clears what the row held.
    isolation: MachineIsolationReport | None = None
    # Set while no worker of the box can serve any org it was asked to; a beat
    # without it says the box serves (or has nothing to serve). Its running
    # time is not billed while it is set.
    fault: MachineFaultReport | None = None
    # The last moment any chat turn, kernel or sandbox process on the box did
    # work. What an idle stop is measured from. ``None`` (an older daemon, or a
    # box that has done nothing since it started) leaves the last stamp standing.
    last_activity_at: datetime | None = None


class BoxMachineCard(BaseModel):
    """What a box is told about the machine it runs on, so its chats can say
    what they run on and whether the minutes are billed."""

    name: str
    gpu: GpuSpec | None = None
    vcpu: int = Field(ge=0)
    memory_gb: int = Field(ge=0)
    disk_gb: int = Field(ge=0)
    billed_per_minute: bool
    idle_stop_minutes: int | None = None


class MachineHeartbeatResponse(BaseModel):
    """The answer to a heartbeat that carries a body: the machine card, or
    ``None`` for a box that backs no org machine."""

    card: BoxMachineCard | None = None


class MachineRead(BaseModel):
    """The org's workspace machine as the daemon and the machine banner see it."""

    id: str
    name: str
    provider: str
    provider_pod_id: str
    machine_type: MachineTypeInfo
    lifecycle: str = "workspace"
    status: MachineStatusLiteral
    last_heartbeat_at: datetime | None = None
    created_at: datetime
    # The claim answer carries the machine card (``None`` for a box that backs
    # no org machine).
    card: BoxMachineCard | None = None


class MachineStateRead(BaseModel):
    """What the machine banner shows for the caller's org: the machine its next
    chat would be placed on and that machine's reachability, ``pool`` when a
    shared pool box would take it (no id or name: which box is placement's
    choice at create time), or ``none`` when nothing would.

    ``reason`` is the machine's own account of a state that is not ready — the
    text a failed provision or a refused start recorded. Without it the banner
    can say only that something is wrong, which leaves the reader with nothing
    to act on and an admin with nothing to look for. It is never a credential
    and never a price: it is the same words the ``compute_machine.changed``
    frame carries.
    """

    machine_id: str | None = None
    status: MachineStateLiteral = "none"
    name: str = ""
    reason: str = ""
    last_heartbeat_at: datetime | None = None
    #: What stands between a chat placed there and its first turn, as every
    #: surface draws it; ``None`` when nothing does. (``status`` is the
    #: machine's raw word above, kept for the composer's gating.)
    status_fact: StatusFact | None = None


class PersonalBoxRead(BaseModel):
    """One box on the caller's own hardware, as its person sees it: what it is
    called, the org it serves them in, when it was set up and last spoke, and
    whether its standing was taken away. ``id`` is the credential's id, the
    one the revoke route names. Never the secret."""

    id: str
    label: str
    org_id: str
    machine_id: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class PersonalBoxList(BaseModel):
    items: list[PersonalBoxRead]


__all__ = [
    "BoxMachineCard",
    "ComputeAllocationCreateRequest",
    "ComputeAllocationInfo",
    "ComputeRenewRequest",
    "MachineFaultReport",
    "MachineHeartbeatResponse",
    "MachineIsolationReport",
    "MachineRead",
    "MachineRegisterRequest",
    "MachineStateLiteral",
    "MachineStateRead",
    "MachineStatusLiteral",
    "MachineTypeInfo",
    "PersonalBoxList",
    "PersonalBoxRead",
]
