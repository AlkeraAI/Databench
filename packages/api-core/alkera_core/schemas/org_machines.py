"""Wire shapes of machines an org holds, the catalog it buys from, and moving a
workspace between machines.

In-flight HTTP shapes only: plain ``BaseModel``. Money is integer nano-USD; ids
are UUID strings.

Every figure on a tenant shape is the CUSTOMER'S rate or spend. What a machine
costs us (the provider's price, the markup, a true cost) appears only on the
platform shapes named in :data:`PLATFORM_ONLY_SHAPES`, which only ``/admin``
routes answer with or accept; the cost-leak guard test walks every other model
here and fails on a cost, margin or provider-price field.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from alkera_core.status import StatusFact
from alkera_core.validation.non_blank import non_blank_name

#: What a person sees of an org machine. ``starting`` carries a
#: :data:`StartingStep`.
OrgMachineState = Literal[
    "starting",
    "running",
    # Up and answering, but no worker of it can serve: its running time is
    # not billed (``alkera_core.compute.unservable``).
    "unhealthy",
    "unreachable",
    "stopping",
    "stopped",
    "waiting_for_hardware",
    "failed",
    "deleted",
]
#: Where a starting machine is: the provider is finding hardware
#: (``reserving``), the machine is booting (``booting``), Alkera is being
#: installed on it (``installing``), or it is installed and has not checked in
#: yet (``connecting``).
StartingStep = Literal["reserving", "booting", "installing", "connecting"]
UseModeLiteral = Literal["pool", "assigned"]
AcquisitionLiteral = Literal["purchased", "granted", "added"]
GranteeKindLiteral = Literal["org", "team", "user"]
MachineCardKind = Literal["org_machine", "shared", "personal", "org_box"]
MoveStateLiteral = Literal[
    "requested", "draining", "switching", "waking", "done", "failed", "canceled"
]
PricingModeLiteral = Literal["pass_through", "fixed"]
OfferingAudienceLiteral = Literal["all", "enterprise", "listed"]
#: How the funding behind a machine stands: enough (``ok``), running low
#: (``low``), about to run out (``urgent``), being stopped for it
#: (``draining``), or stopped for it (``stopped``).
CreditState = Literal["ok", "low", "urgent", "draining", "stopped"]

ORG_MACHINE_NAME_MAX = 64
OFFERING_NAME_MAX = 128

#: A machine's or an offering's name as a request carries it: stripped of
#: surrounding whitespace, and refused when nothing is left.
MachineName = Annotated[str, non_blank_name(ORG_MACHINE_NAME_MAX)]
OfferingName = Annotated[str, non_blank_name(OFFERING_NAME_MAX)]
IDEMPOTENCY_KEY_MIN = 8
IDEMPOTENCY_KEY_MAX = 64


class GpuSpec(BaseModel):
    name: str
    count: int = Field(ge=0)
    memory_gb: int = Field(ge=0)


class MachineSpec(BaseModel):
    """The hardware and the customer's rates of one machine."""

    offering_name: str
    provider: str
    region: str
    gpu: GpuSpec | None = None
    vcpu: int = Field(ge=0)
    memory_gb: int = Field(ge=0)
    disk_gb: int = Field(ge=0)
    # None when the reader may not see rates.
    rate_per_minute_nanos: int | None = None
    storage_rate_per_minute_nanos: int | None = None


class MachineFaultRead(BaseModel):
    """Why a box that answers runs no chat: the server's code and message,
    and, for platform staff only, the box's own scrubbed words and when it began."""

    code: str
    message: str
    summary: str = ""
    since: datetime | None = None


class MachineWaitRead(BaseModel):
    """Why a machine waits for hardware and when the server asks again, as
    the reconcile will act on it."""

    reason: str
    #: When the provider is asked again; ``None`` while it is asked every
    #: half minute.
    next_try_at: datetime | None = None
    #: When the server stops asking and the machine reads "couldn't start".
    gives_up_at: datetime


class MachineCard(BaseModel):
    """One machine as every surface draws it: an org machine, the shared
    machines, a person's own box, or a box a member registered for the org."""

    kind: MachineCardKind
    org_machine_id: str | None = None
    name: str
    # None for shared machines: which box placement picks is not the reader's.
    spec: MachineSpec | None = None
    state: OrgMachineState | Literal["shared"]
    step: StartingStep | None = None
    step_started_at: datetime | None = None
    step_expected_seconds: int | None = None
    stop_reason: str = ""
    # When a machine draining because its credit (or its cap) ran out stops.
    drain_stops_at: datetime | None = None
    # Why an ``unhealthy`` machine runs no chat, in the server's words.
    fault: MachineFaultRead | None = None
    # A running machine whose volume is almost full
    # (``alkera_core.compute.disk.disk_nearly_full``).
    disk_full: bool = False
    # Where the machine stands as every surface draws it, words and all.
    status: StatusFact | None = None
    # Why a ``waiting_for_hardware`` machine waits, and its retry schedule.
    wait: MachineWaitRead | None = None


class AudienceGrant(BaseModel):
    """Who may use an assigned machine: the whole org, a team (and its
    sub-teams), or one person."""

    kind: GranteeKindLiteral
    team_id: str | None = None
    user_id: str | None = None


class AudienceEntry(AudienceGrant):
    label: str


class WorkspaceRef(BaseModel):
    id: str
    name: str


class MachineTimelineEntry(BaseModel):
    at: datetime
    words: str


class DiskGrowRead(BaseModel):
    """How far a manager may grow a machine's disk now, as the server decided
    it. ``None`` on the machine where it cannot grow."""

    min_gb: int
    max_gb: int
    #: The machine stops to grow it (its running chats finish first).
    restarts: bool


class OrgMachineRead(BaseModel):
    id: str
    name: str
    card: MachineCard
    use_mode: UseModeLiteral
    acquisition: AcquisitionLiteral
    free_until: datetime | None = None
    audience: list[AudienceEntry]
    idle_stop_minutes: int | None = None
    monthly_cap_nanos: int | None = None
    owner_team_id: str
    owner_team_name: str
    version: int
    can_manage: bool
    can_use: bool
    # Whether a manager may put it on new hardware (a host the org attached
    # has none); offered only in the states a replace applies to.
    can_replace: bool = False
    # Whether new workspaces run on it unless their creator names another.
    org_default: bool = False
    # Managers only; None for everyone else.
    spend_this_cycle_nanos: int | None = None
    credit_state: CreditState
    # Managers only; None for everyone else, and for a machine with no end.
    runway_minutes: int | None = None
    # When a machine draining because its credit (or its cap) ran out stops;
    # None unless such a drain is under way.
    drain_stops_at: datetime | None = None
    created_at: datetime
    # Managers only: how far its disk may grow now; None where it cannot.
    disk_grow: DiskGrowRead | None = None


class MachineBuyingRead(BaseModel):
    """Whether the caller's org may buy another machine now, without saying
    which plan it is on: ``reason`` is ``plan`` when its plan buys none and
    ``quota`` when it holds as many as it may."""

    can_buy: bool
    reason: Literal["plan", "quota"] | None = None
    quota: int
    used: int
    # Whether the caller may add a machine the org runs by its SSH details.
    can_add: bool = False


class SshEndpointRead(BaseModel):
    """Where an added machine is reached. Never the credential."""

    host: str
    port: int
    username: str
    auth_kind: Literal["password", "private_key"]
    host_key_fingerprint: str
    #: The OpenSSH name of the pinned key's type (``ED25519``, ``ECDSA``,
    #: ``RSA``); empty when the recorded key names no known type.
    host_key_type: str = ""


class OrgMachineDetail(OrgMachineRead):
    workspaces: list[WorkspaceRef]
    timeline: list[MachineTimelineEntry]
    # Managers of an added machine only; None otherwise.
    ssh: SshEndpointRead | None = None


class OrgMachinePurchase(BaseModel):
    offering_id: str
    name: MachineName
    storage_gb: int = Field(gt=0)
    use_mode: UseModeLiteral = "assigned"
    audience: list[AudienceGrant]
    # Required, and null means never stop when idle: a buyer states it.
    idle_stop_minutes: int | None = Field(ge=5)
    monthly_cap_nanos: int | None = Field(default=None, ge=0)
    # Default: the org root.
    owner_team_id: str | None = None
    idempotency_key: str = Field(min_length=IDEMPOTENCY_KEY_MIN, max_length=IDEMPOTENCY_KEY_MAX)


class OrgMachineUpdate(BaseModel):
    """Fields not sent are left as they are (read ``model_fields_set``); a
    field sent as ``null`` clears it where clearing means something
    (``idle_stop_minutes``: never stop; ``monthly_cap_nanos``: no cap)."""

    name: MachineName | None = None
    idle_stop_minutes: int | None = Field(default=None, ge=5)
    monthly_cap_nanos: int | None = Field(default=None, ge=0)
    use_mode: UseModeLiteral | None = None


class OrgMachineAudienceUpdate(BaseModel):
    audience: list[AudienceGrant]


class OrgComputeSettingsRead(BaseModel):
    shared_pool_fallback: bool
    min_awake_pool: int
    # The org machine a new workspace runs on when its creator names none and
    # may use it; None when unset or the machine is gone.
    default_org_machine_id: str | None = None
    version: int


class OrgComputeSettingsUpdate(BaseModel):
    shared_pool_fallback: bool | None = None
    min_awake_pool: int | None = Field(default=None, ge=0)
    # Sent as null, clears the default; left out, keeps it.
    default_org_machine_id: str | None = None


class DiskChoicesRead(BaseModel):
    """What a buyer may choose for an offering's disks, as the server decided
    it (``alkera_core.compute.offering_disks``). Clients render these and
    never derive one."""

    volume_min_gb: int
    volume_max_gb: int
    volume_default_gb: int
    #: The container disk the machine gets; 0 where the provider sizes it.
    container_gb: int
    #: The provider bills the volume while the machine is stopped.
    volume_billed_while_stopped: bool
    #: How a volume grows: in place, with a restart, or not at all.
    grow: Literal["online", "restart", "never"]


class DiskGrowRequest(BaseModel):
    """The bigger disk asked for, in GB."""

    volume_gb: int = Field(gt=0, le=1_000_000)


class MachineQuoteRequest(BaseModel):
    """A size to quote an offering at, before buying it."""

    offering_id: str = Field(min_length=1, max_length=64)
    storage_gb: int = Field(gt=0, le=1_000_000)


class MachineQuote(BaseModel):
    """What buying an offering at a size would cost the caller's org, and
    whether it would be admitted now: the purchase's own rules, decided by the
    server."""

    offering_id: str
    storage_gb: int
    rate_per_minute_nanos: int
    storage_rate_per_minute_nanos: int
    #: The volume's cost over a 30-day month.
    storage_per_month_nanos: int
    volume_billed_while_stopped: bool
    #: What the paying account must hold to start it.
    start_runway_nanos: int
    verdict: Literal["ok", "refused"]
    #: The refusal's code (``insufficient_credit``, ``machine_quota``, ...).
    code: str | None = None
    message: str = ""
    #: Whether a biller charges for machines here. False, the rates are not
    #: billed and a client shows no price.
    priced: bool = False


class StockRead(BaseModel):
    """Whether an offering can be bought now, as the server decided it
    (``alkera_core.compute.stock.offer_verdict``): the stock of the size it
    sells, and a sentence saying why not when it cannot. Clients render it."""

    state: Literal["in_stock", "limited", "out_of_stock", "unknown"]
    can_buy: bool
    reason: str = ""


class OfferingRead(BaseModel):
    """An offering as an org's buyer sees it: the customer rate only."""

    id: str
    name: str
    description: str
    provider: str
    region: str
    compute_class: str
    gpu: GpuSpec | None = None
    vcpu: int
    memory_gb: int
    rate_per_minute_nanos: int
    storage_rate_per_gb_month_nanos: int
    storage_gb_default: int
    storage_gb_max: int
    availability: str
    purchasable: bool
    idle_stop_minutes_default: int | None = None
    #: How many minutes of compute and storage the paying account must hold
    #: for a start: the deployment's ``machine_start_runway_minutes``.
    start_runway_minutes: int = 60
    #: The disk sizes a buyer may choose; ``None`` for an offering its
    #: provider cannot make (it is then not purchasable).
    disk: DiskChoicesRead | None = None
    #: Whether it can be bought now, and why not.
    stock: StockRead
    #: Whether a biller charges for machines here. False, the rates are not
    #: billed and a client shows no price.
    priced: bool = False


class OfferingAdminRead(OfferingRead):
    """An offering as a platform admin sees it. Platform only."""

    machine_type_id: str
    pricing_mode: PricingModeLiteral
    markup_bps: int
    fixed_rate_per_minute_nanos: int | None = None
    provider_price_per_minute_nanos: int
    audience: OfferingAudienceLiteral
    org_ids: list[str]
    sort_order: int
    retired_at: datetime | None = None


class OfferingCreate(BaseModel):
    """A new offering. Platform only."""

    machine_type_id: str
    name: OfferingName
    description: str = Field(default="", max_length=512)
    pricing_mode: PricingModeLiteral
    markup_bps: int = Field(default=0, ge=0)
    fixed_rate_per_minute_nanos: int | None = Field(default=None, ge=0)
    storage_gb_default: int = Field(gt=0)
    storage_gb_max: int = Field(gt=0)
    storage_rate_per_gb_month_nanos: int = Field(default=0, ge=0)
    region: str = Field(default="", max_length=32)
    audience: OfferingAudienceLiteral = "all"
    org_ids: list[str] = Field(default_factory=list)
    purchasable: bool = True
    idle_stop_minutes_default: int | None = Field(default=None, ge=5)
    sort_order: int = 0


class OfferingUpdate(BaseModel):
    """A change to an offering; fields not sent are left as they are.
    ``retired`` true retires it, false brings it back. Platform only."""

    machine_type_id: str | None = None
    name: OfferingName | None = None
    description: str | None = Field(default=None, max_length=512)
    pricing_mode: PricingModeLiteral | None = None
    markup_bps: int | None = Field(default=None, ge=0)
    fixed_rate_per_minute_nanos: int | None = Field(default=None, ge=0)
    storage_gb_default: int | None = Field(default=None, gt=0)
    storage_gb_max: int | None = Field(default=None, gt=0)
    storage_rate_per_gb_month_nanos: int | None = Field(default=None, ge=0)
    region: str | None = Field(default=None, max_length=32)
    audience: OfferingAudienceLiteral | None = None
    org_ids: list[str] | None = None
    purchasable: bool | None = None
    idle_stop_minutes_default: int | None = Field(default=None, ge=5)
    sort_order: int | None = None
    retired: bool | None = None


class OrgMachineGrant(BaseModel):
    """A machine Alkera gives an org, free until ``free_until``. Platform only."""

    offering_id: str
    name: MachineName
    free_until: datetime
    use_mode: UseModeLiteral = "pool"
    storage_gb: int = Field(gt=0)
    audience: list[AudienceGrant] = Field(default_factory=list)


class WorkspaceMachineMoveRequest(BaseModel):
    # None moves the workspace to the org's default placement.
    to_org_machine_id: str | None = None
    stop_running: bool = False


class WorkspaceMachineMoveRead(BaseModel):
    id: str
    workspace_id: str
    from_org_machine_id: str | None = None
    to_org_machine_id: str | None = None
    state: MoveStateLiteral
    error_code: str = ""
    error: str = ""
    requested_at: datetime
    finished_at: datetime | None = None
    # Whether the departing box flushed the workspace before the switch; None
    # while unknown, or for a box too old to flush.
    flushed_before_switch: bool | None = None


LostMachineReason = Literal["deleted", "no_access"]


class LostMachineRead(BaseModel):
    """The org machine a workspace ran on that can no longer serve it: deleted,
    or out of reach of the person whose chat would run there. ``fell_back``:
    a waker that is not a person already moved the workspace to the default
    placement, which the workspace's card now shows."""

    org_machine_id: str
    name: str
    reason: LostMachineReason
    fell_back: bool


class MachineUnavailableRead(BaseModel):
    """A wake held because the workspace's machine is gone: what was lost, and
    where the opener may wake it instead. ``choices`` are the targets the
    opener may move the workspace to (empty when they may not move it), the
    default placement first when it serves the org; ``preselect_default`` is
    whether the opener is offered it as the answer."""

    workspace_id: str
    lost: LostMachineRead
    choices: list[MachineCard]
    preselect_default: bool


class WorkspaceMachineRead(BaseModel):
    card: MachineCard
    pin: str | None = None
    # The machine the workspace lost, while that is still news: awaiting a
    # choice, or moved off it automatically and not moved since.
    lost: LostMachineRead | None = None
    active_move: WorkspaceMachineMoveRead | None = None
    # The most recent move that ended (done, failed or canceled): why a move
    # failed, and where it was going, after its run is over.
    last_move: WorkspaceMachineMoveRead | None = None
    can_move: bool
    # What the caller may move the workspace to: the default first, only when
    # something serves the org's regular chats, then the org machines they may use.
    targets: list[MachineCard]


class MachineUsageRow(BaseModel):
    org_machine_id: str
    name: str
    day: date
    running_minutes: int
    stopped_minutes: int
    compute_nanos: int
    storage_nanos: int
    refund_nanos: int


class MachineUsageRead(BaseModel):
    rows: list[MachineUsageRow]


#: The shapes only ``/admin`` routes answer with or accept. They carry what a
#: machine costs us; no tenant route may name them.
PLATFORM_ONLY_SHAPES: frozenset[str] = frozenset(
    {"OfferingAdminRead", "OfferingCreate", "OfferingUpdate", "OrgMachineGrant"}
)


__all__ = [
    "PLATFORM_ONLY_SHAPES",
    "AcquisitionLiteral",
    "AudienceEntry",
    "AudienceGrant",
    "CreditState",
    "GpuSpec",
    "GranteeKindLiteral",
    "LostMachineRead",
    "LostMachineReason",
    "MachineCard",
    "MachineCardKind",
    "MachineFaultRead",
    "MachineSpec",
    "MachineTimelineEntry",
    "MachineUnavailableRead",
    "MachineUsageRead",
    "MachineUsageRow",
    "MoveStateLiteral",
    "OfferingAdminRead",
    "OfferingAudienceLiteral",
    "OfferingCreate",
    "OfferingRead",
    "OfferingUpdate",
    "OrgComputeSettingsRead",
    "OrgComputeSettingsUpdate",
    "OrgMachineAudienceUpdate",
    "OrgMachineDetail",
    "OrgMachineGrant",
    "OrgMachinePurchase",
    "OrgMachineRead",
    "OrgMachineState",
    "OrgMachineUpdate",
    "PricingModeLiteral",
    "SshEndpointRead",
    "StartingStep",
    "UseModeLiteral",
    "WorkspaceMachineMoveRead",
    "WorkspaceMachineMoveRequest",
    "WorkspaceMachineRead",
    "WorkspaceRef",
]
