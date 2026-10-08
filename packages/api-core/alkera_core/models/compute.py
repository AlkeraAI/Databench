"""Compute-plane ORM models: the machine-type catalog, grants, allocations.

- ``compute_machine_types`` — the provisionable catalog, seeded from a static
  snapshot of the provider's offering and price-refreshed live. The price a
  row carries is the PROVIDER'S list price — what a machine costs us — never a
  customer rate. It is named ``provider_price_per_minute_nanos`` so a tenant
  response model that reaches for it is caught by the cost-leak guard test.
- ``compute_grants`` — admission control granted to a team (the org root, or a
  team below it): a ceiling on concurrent machines, the rate the grantee is
  billed per minute (0 for a comped customer), and an expiry every grant has.
  ``resolve_compute_grant`` walks the grantee's ancestor chain and picks the
  most specific live row; it is the only admission check on any start path.
- ``compute_allocations`` — one machine per row, scoped to an org and owned by
  a user. Two lifecycles: ``session`` (the ephemeral per-session SSH pod the
  plane was built for, lease-capped) and ``workspace`` (the long-lived machine a
  customer's chats bind to: no lease ceiling, heartbeated, stable). Two
  origins, which decide who vouches for a machine's liveness: ``provisioned``
  (the plane created the pod; the provider's word is final) and ``registered``
  (a daemon announced a box the plane never created; its heartbeat is the only
  word there is, and the provider is never asked). The row
  pins BOTH prices at allocate — the billed rate from the grant and the true
  provider cost from the catalog — so a catalog refresh never re-rates a
  running machine and the account margin stays answerable from the ledger.
  The per-allocation SSH private key is sealed with ``secret_box`` before it is
  stored; the column holds ciphertext only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.compute.provider import COMPUTE_PROVIDER_KINDS
from alkera_core.compute.tenancy import (
    COMPUTE_TENANCIES,
    DEDICATED_TENANCY,
    ORG_TENANCY,
    PERSONAL_TENANCY,
    POOL_TENANCY,
    ComputeTenancy,
)
from alkera_core.db.base import Base

#: Admitted, nothing asked of the provider yet.
PENDING = "pending"
#: The row is durable and the provider has been (or is being) asked for the
#: machine. It carries the pod id once the provider answers with one.
PROVISIONING = "provisioning"
#: The provider reports the machine running; its bootstrap is installing the
#: daemon, which has not claimed the row yet. Costing money, serving nothing.
BOOTSTRAPPING = "bootstrapping"
#: Up and serving.
READY = "ready"

#: A box that was told to stop and is finishing the turns it already holds.
#: Still live in every sense that costs or serves — it is metered, it counts
#: against its grant, and the chats mid-answer on it keep being answered — and
#: never a candidate for a NEW chat. It is a state rather than a flag because
#: everything that reads a machine's life already reads ``state``.
DRAINING = "draining"

#: ``drain_kind`` of a draining row whose daemon is being restarted in place by
#: the box's supervisor: out of placement like any draining row, but its chats
#: stay where they are and the row reads ``restarting``.
DRAIN_RESTART = "restart"

#: ``drain_kind`` of a draining row being stopped on purpose, so its chats are
#: handed on and the row then sleeps or releases: the funding account cannot
#: carry it (``credits``), its monthly cap is spent (``cap``), nothing ran on it
#: for its idle window (``idle``), a workspace is leaving it (``move``), or a
#: person stopped it (``user``).
DRAIN_CREDITS = "credits"
DRAIN_CAP = "cap"
DRAIN_IDLE = "idle"
DRAIN_MOVE = "move"
DRAIN_USER = "user"
#: Every ``drain_kind`` a row may carry.
DRAIN_KINDS: tuple[str, ...] = (
    DRAIN_RESTART,
    DRAIN_CREDITS,
    DRAIN_CAP,
    DRAIN_IDLE,
    DRAIN_MOVE,
    DRAIN_USER,
)

#: The stop is decided and committed (with its reason and whatever tail was
#: just billed); the provider call is the leg left to finish.
RELEASING = "releasing"

#: Given back by its owner.
RELEASED = "released"
#: Stopped by the plane — credit, the grant, the lease, or the provider.
FAILED = "failed"
#: The provider no longer has a machine the plane thought was serving. Nothing
#: runs and nothing is billed; its chats are handed to another box and the row
#: is then released.
LOST = "lost"

#: Stopped on purpose but KEPT: the provider machine is stopped (its disk
#: survives) so it stops costing compute and stops being billed, its chats are
#: handed on, and a later wake starts it back to ``ready``. The power axis's
#: "asleep" — what lets an idle dedicated box stop costing without being
#: destroyed. "on" is ``ready``; "off" is a release (``releasing`` -> terminal).
ASLEEP = "asleep"

#: THE LIFECYCLE, PARTITIONED. Every state a ``compute_allocations`` row can
#: hold belongs to exactly one of the four tuples below, and the union of them
#: is :data:`COMPUTE_ALLOCATION_STATES`. The partition is what makes the
#: lifecycle total: a new state that nobody classifies fails
#: ``packages/api-core/tests/compute/test_state_partition.py`` instead of quietly falling out of
#: the meter, which is how ``draining`` came to run free — the model's own
#: docstring said it was metered while the meter's state list did not name it.
#: Read the money question off the tuple, never off a state literal.

#: Nothing is running yet and nothing can be spent: no provider call has been
#: made, so there is no machine to bill for and nothing to terminate.
COMPUTE_PRE_PROVISION_STATES: tuple[str, ...] = (PENDING,)

#: The states the meter acts on: a machine that is live, coming up, draining,
#: or half-stopped. Every one of them can be costing money at the provider
#: right now, so every one of them gets the whole tick — the provider poll (or
#: the heartbeat reap), the grant re-read, the minutes, the credit cutoff and
#: the lease ceiling.
COMPUTE_METERED_STATES: tuple[str, ...] = (
    PROVISIONING,
    BOOTSTRAPPING,
    READY,
    DRAINING,
    RELEASING,
)

#: Kept but not running: an ``asleep`` box is stopped at the provider, so it is
#: never metered (nothing to bill) and never placed on (nothing serving), yet it
#: is NOT terminal — a wake starts it back to ``ready``, and it still holds its
#: grant slot because the org has not given it back. Its own class because it is
#: neither a money-bearing state nor an end state.
COMPUTE_DORMANT_STATES: tuple[str, ...] = (ASLEEP,)

#: Allocation states past which a row is never metered, counted or reused.
#: ``lost`` is here: the machine is already gone at the provider, so there is
#: nothing to bill and nothing to place on; the row only waits for its chats to
#: be handed on before it is released.
COMPUTE_TERMINAL_STATES: tuple[str, ...] = (RELEASED, FAILED, LOST)

#: The whole vocabulary of the ``state`` column.
COMPUTE_ALLOCATION_STATES: tuple[str, ...] = (
    *COMPUTE_PRE_PROVISION_STATES,
    *COMPUTE_METERED_STATES,
    *COMPUTE_DORMANT_STATES,
    *COMPUTE_TERMINAL_STATES,
)

#: Allocation states that count against a grant's ceiling (the machine is, or
#: is becoming, live, or is kept asleep for its org). Narrower than "not
#: terminal": a ``releasing`` row is on its way off the plane and must not hold a
#: slot the stop has already freed. An ``asleep`` box still holds its slot —
#: sleeping frees compute cost, not the org's claim on the machine.
COMPUTE_ACTIVE_STATES: tuple[str, ...] = (
    PENDING,
    PROVISIONING,
    BOOTSTRAPPING,
    READY,
    DRAINING,
    ASLEEP,
)

#: The states that bill MINUTES — a machine that is up and serving, whether or
#: not it is also finishing its last turns. ``draining`` is here because a
#: draining box is a running box: it answers the chats it holds and the
#: provider charges us for every minute of it.
COMPUTE_BILLABLE_STATES: tuple[str, ...] = (READY, DRAINING)

#: The states that bill STORAGE when the row carries a storage price: the
#: states in which the provider holds the volume of a machine that has been
#: ready. ``asleep`` is here (a stopped machine keeps its disk, and the
#: provider charges for it). ``provisioning`` and ``bootstrapping`` are not: a
#: machine that never came up was never the org's to pay for, so its volume
#: costs us until the claim (``ready``) and the org nothing. ``pending`` has no
#: volume yet and a terminal state none left. A ``releasing`` row bills only if
#: it was ready first (a start cancelled before the claim passes through it).
COMPUTE_STORAGE_METERED_STATES: tuple[str, ...] = (
    READY,
    DRAINING,
    RELEASING,
    ASLEEP,
)

#: Why a machine stopped, beyond the reasons the meter and the reconcile
#: already write (``credits_exhausted`` … ``max_minutes``): the provider had no
#: hardware (``provider_capacity``), the box never came up
#: (``boot_failed``), a stopped machine's funding stayed empty past the
#: retention period (``unfunded_retention``), its org went away
#: (``org_deleted``), or a fresh machine took its place (``replaced``).
TERMINATED_PROVIDER_CAPACITY = "provider_capacity"
TERMINATED_BOOT_FAILED = "boot_failed"
TERMINATED_UNFUNDED_RETENTION = "unfunded_retention"
TERMINATED_ORG_DELETED = "org_deleted"
TERMINATED_REPLACED = "replaced"

#: How a provider refused or failed a call (``ComputeProviderError.kind``,
#: copied to ``compute_allocations.failure_kind``): no hardware right now
#: (``capacity``), an account limit (``quota``), a request the provider will
#: never accept (``invalid``), a credential it refused (``auth``), something
#: worth retrying (``transient``), or nothing it said lets us tell
#: (``unknown``). ``''`` on a row that has not failed.
FAILURE_CAPACITY = "capacity"
FAILURE_QUOTA = "quota"
FAILURE_INVALID = "invalid"
FAILURE_AUTH = "auth"
FAILURE_TRANSIENT = "transient"
FAILURE_UNKNOWN = "unknown"
FAILURE_KINDS: tuple[str, ...] = (
    FAILURE_CAPACITY,
    FAILURE_QUOTA,
    FAILURE_INVALID,
    FAILURE_AUTH,
    FAILURE_TRANSIENT,
    FAILURE_UNKNOWN,
)

#: The ledger ``reason`` of a machine's storage minutes and of a refund of
#: compute it was billed for and did not get. The compute minutes themselves
#: are ``compute``.
LEDGER_REASON_COMPUTE = "compute"
LEDGER_REASON_COMPUTE_STORAGE = "compute_storage"
LEDGER_REASON_COMPUTE_REFUND = "compute_refund"


def compute_ledger_ref(allocation_id: object, minute: int | str) -> str:
    """The ledger ``ref`` of an allocation's ``minute``-th billed compute minute."""
    return f"compute:{allocation_id}:{minute}"


def storage_ledger_ref(allocation_id: object, minute: int | str) -> str:
    """The ledger ``ref`` of an allocation's ``minute``-th billed storage minute."""
    return f"compute-storage:{allocation_id}:{minute}"


def refund_ledger_ref(allocation_id: object) -> str:
    """The ledger ``ref`` of the one refund an allocation may carry."""
    return f"compute-refund:{allocation_id}"


def drain_hold_request_id(allocation_id: object) -> str:
    """The hold request id that reserves a draining machine's last minutes."""
    return f"compute-drain:{allocation_id}"


ComputeLifecycle = Literal["session", "workspace"]
COMPUTE_LIFECYCLES: tuple[str, ...] = ("session", "workspace")

ComputeOrigin = Literal["provisioned", "registered"]
COMPUTE_ORIGINS: tuple[str, ...] = ("provisioned", "registered")
#: The plane asked the provider to create the machine; the provider vouches for it.
PROVISIONED: ComputeOrigin = "provisioned"
#: A daemon on a box the plane did not create told the plane about it; its
#: heartbeat vouches for it.
REGISTERED: ComputeOrigin = "registered"


#: The sandbox mode a box reports it runs chats under. Two states only: ``gvisor`` (the agent runs
#: under gVisor/runsc — the real boundary the shared multi-tenant pool requires)
#: or ``none`` (no boundary — allowed only on a single-tenant node). The
#: placement guard reads it: a ``none`` box is never placed on the pool.
ComputeSandbox = Literal["gvisor", "none"]
COMPUTE_SANDBOXES: tuple[str, ...] = ("gvisor", "none")
#: gVisor: the pool-grade boundary.
GVISOR_SANDBOX: ComputeSandbox = "gvisor"
#: No boundary: a single-tenant dedicated or dev/RunPod box. The fail-closed
#: default for a row that has not reported yet, so an unreported box is kept off
#: the pool until it proves it runs gVisor.
NO_SANDBOX_MODE: ComputeSandbox = "none"

#: How many chats a box serves at once when its daemon has not said.
DEFAULT_MACHINE_CAPACITY = 6

MACHINE_NAME_MAX = 128


class ComputeMachineType(Base):
    __tablename__ = "compute_machine_types"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_type_id", name="uq_compute_machine_types_provider_type"
        ),
        # The catalog may only name a provider the registry can build: a row
        # nothing can resolve is a machine the plane could neither meter nor
        # terminate. The vocabulary is COMPUTE_PROVIDER_KINDS, so widening the
        # registry and widening this constraint are one change.
        CheckConstraint(
            f"provider IN {COMPUTE_PROVIDER_KINDS}", name="ck_compute_machine_types_provider"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="runpod")
    # The provider's own id for the type (a RunPod GPU id or CPU flavor code).
    provider_type_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    compute_class: Mapped[str] = mapped_column(String(16), nullable=False, server_default="cpu")
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vcpu: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    memory_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The GPU model and its memory per card, from the provider feed ("" and 0
    # for a CPU type or one the feed has not described yet).
    gpu_name: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    gpu_memory_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The container (boot) disk the type comes with, in GB; 0 when unknown.
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: When the provider last refused a machine of this type for lack of
    #: hardware: the honest stock signal for a size the provider's own stock
    #: word does not cover (``alkera_core.compute.stock``). Cleared by the
    #: next create it takes.
    capacity_refused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What the PROVIDER charges us for one minute of this type, integer nano-USD.
    # Our cost, never a customer rate: the billed rate comes from the grant.
    provider_price_per_minute_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Live-catalog stock (NONE|LOW|MEDIUM|HIGH|unknown). A running allocation is
    # unaffected by this; it only informs the picker.
    availability: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    # Whether NEW machines of this type may be provisioned. A type that leaves the
    # live feed is retained (never deleted) so running allocations keep their name.
    available_for_new: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Last time the live provider feed confirmed this type (None = seed-only).
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Provider-specific provisioning parameters (image, disk, cloud tier today; a
    # subnet / security group / AMI / user-data / region for an EC2 provider later).
    # Read only by the provider that owns the row; the service never interprets it.
    provider_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ComputeGrant(Base):
    __tablename__ = "compute_grants"
    __table_args__ = (
        CheckConstraint("ceiling >= 0", name="ck_compute_grants_ceiling_nonnegative"),
        CheckConstraint("rate_per_minute_nanos >= 0", name="ck_compute_grants_rate_nonnegative"),
        CheckConstraint(
            "per_user_max IS NULL OR per_user_max >= 1",
            name="ck_compute_grants_per_user_max_positive",
        ),
        Index("ix_compute_grants_org_team_id", "org_team_id"),
        Index("ix_compute_grants_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The grantee: an org root or any team below it. Resolution walks the caller's
    # team up to the root and the most specific live grant wins.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = the grant covers every machine type; a typed grant beats a wildcard
    # at the same level of the chain.
    machine_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("compute_machine_types.id", ondelete="CASCADE"), nullable=True
    )
    # Concurrent machines admitted under this grant. 0 is a legal "nothing".
    ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional per-user share of the ceiling so one person cannot drain a team's.
    per_user_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # What the grantee is BILLED per minute, integer nano-USD. 0 = comped: the
    # meter still records the provider's true cost on every ledger row it writes.
    rate_per_minute_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The account the billed minutes debit is billing's record, not a column here
    # (compute_funding()): none = the default funding order, the user's seat, then
    # the org pool.
    # Every grant expires; a comped grant with no expiry is how a trial becomes
    # permanent free infrastructure.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ComputeAllocation(Base):
    __tablename__ = "compute_allocations"
    __table_args__ = (
        CheckConstraint(
            f"lifecycle IN {COMPUTE_LIFECYCLES}", name="ck_compute_allocations_lifecycle"
        ),
        CheckConstraint(f"origin IN {COMPUTE_ORIGINS}", name="ck_compute_allocations_origin"),
        CheckConstraint(f"tenancy IN {COMPUTE_TENANCIES}", name="ck_compute_allocations_tenancy"),
        CheckConstraint(f"sandbox IN {COMPUTE_SANDBOXES}", name="ck_compute_allocations_sandbox"),
        CheckConstraint("capacity >= 1", name="ck_compute_allocations_capacity_positive"),
        CheckConstraint("chats_served >= 0", name="ck_compute_allocations_chats_nonnegative"),
        CheckConstraint(
            f"state IN {COMPUTE_ALLOCATION_STATES}", name="ck_compute_allocations_state"
        ),
        CheckConstraint("storage_gb >= 0", name="ck_compute_allocations_storage_nonnegative"),
        # Admission counts "this user's active allocations"; state is in the key so
        # released rows fall out of the scan.
        Index("ix_compute_allocations_user_state", "user_id", "state"),
        Index("ix_compute_allocations_org_team_id", "org_team_id"),
        Index("ix_compute_allocations_machine_type_id", "machine_type_id"),
        Index("ix_compute_allocations_grant_id", "grant_id"),
        Index("ix_compute_allocations_provider_machine_id", "provider_machine_id"),
        Index("ix_compute_allocations_org_machine_id", "org_machine_id"),
        # The key an org machine's composite foreign key points at, so the
        # database itself refuses an org machine whose allocation belongs to
        # another org.
        Index("ux_compute_allocations_id_tenant_org", "id", "tenant_org_id", unique=True),
        # An allocation that backs an org machine is that org's: it must carry
        # the tenant, and the pair must name an org machine of the same org.
        CheckConstraint(
            "org_machine_id IS NULL OR tenant_org_id IS NOT NULL",
            name="ck_compute_allocations_org_machine_tenant",
        ),
        ForeignKeyConstraint(
            ["org_machine_id", "tenant_org_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_compute_allocations_org_machine_tenant",
            ondelete="SET NULL (org_machine_id)",
            use_alter=True,
        ),
        CheckConstraint(
            "storage_price_per_minute_nanos >= 0",
            name="ck_compute_allocations_storage_price_nonnegative",
        ),
        CheckConstraint(
            "provision_attempts >= 0", name="ck_compute_allocations_provision_attempts_nonnegative"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    machine_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("compute_machine_types.id", ondelete="RESTRICT"), nullable=False
    )
    # session = an ephemeral per-session pod with a lease ceiling; workspace = the
    # long-lived machine a customer's chats bind to (no ceiling, heartbeated).
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, server_default="session")
    # Who put the machine on the plane, and so who vouches that it is still
    # there. ``provisioned``: the plane asked the provider to create it, and the
    # provider's answer to "does this pod exist" is final (the meter stops it as
    # ``provider_gone``). ``registered``: a daemon on a box the plane did not
    # create announced it through ``POST /machines/register``; the provider
    # never made that pod and cannot be asked about it (RunPod does not know a
    # pod it did not create, the container sandbox refuses every question until
    # it exists), so the daemon's heartbeat is the only liveness it has — metered
    # while the heartbeat is fresh, stopped as ``heartbeat_lost`` once it lapses.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PROVISIONED, server_default=PROVISIONED
    )
    # A human label for a workspace machine (what the machine banner shows).
    name: Mapped[str] = mapped_column(String(MACHINE_NAME_MAX), nullable=False, server_default="")
    # One of COMPUTE_ALLOCATION_STATES. Written only through
    # ``alkera_core.compute.transitions.transition`` by new code, which also
    # records the edge in ``compute_allocation_events``.
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    provider_machine_id: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    public_ip: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    ssh_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ssh_user: Mapped[str] = mapped_column(String(32), nullable=False, server_default="root")
    ssh_public_key: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # The private half, sealed with alkera_core.auth.secret_box (MultiFernet). Never
    # plaintext: write through ``alkera_core.compute.keys.seal_private_key`` and read
    # through ``open_private_key``.
    ssh_private_key_enc: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    project_path: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    error: Mapped[str] = mapped_column(String(1024), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- admission + the two pinned prices ------------------------------------
    # The grant that admitted this machine; its ceiling counts live rows by this.
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("compute_grants.id", ondelete="SET NULL"), nullable=True
    )
    # The BILLED rate, pinned at allocate from the grant: the meter bills against
    # THIS, never the live catalog, so a mid-lease price change cannot re-rate a
    # running machine. 0 for a comped grant.
    price_per_minute_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    # The provider's TRUE cost per minute, pinned from the catalog at allocate.
    # Platform-visibility only: never on a tenant response model or event payload.
    true_cost_per_minute_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    # The account whose credits this machine debits is billing's record, not a
    # column here (compute_funding()), resolved at allocate the way the gateway
    # resolves a user's funding unless the grant names its own. None = unbillable.
    # High-water mark of billed wall-clock: the meter accrues WHOLE minutes past
    # this and advances it by exactly the minutes it billed, which is also the
    # idempotency guard against double-billing a minute.
    last_metered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    minutes_billed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # What the grantee was charged so far (at the pinned billed rate).
    billed_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # What the machine cost us so far (at the pinned true cost). Platform-only.
    true_cost_nanos: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # Why a machine stopped: "" while live, else one of credits_exhausted |
    # grant_expired | provider_gone | provider_error | heartbeat_lost |
    # user_released | max_minutes. Set (and committed) the moment the stop is DECIDED — while
    # the row is still ``releasing`` — so a crash before the provider confirms
    # cannot lose the reason.
    terminated_reason: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    # Set when the NEXT minute can no longer be covered — an imminent cutoff.
    low_credit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Optional self-imposed lease ceiling (total minutes) on a session machine.
    # None = no time cap; a workspace machine never has one.
    max_lease_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- workspace reachability --------------------------------------------
    # The daemon on a workspace machine heartbeats; its reachability status is
    # DERIVED from this stamp (ready within the window, unreachable past it,
    # starting when never stamped) rather than stored.
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The reachability status last announced on the event outbox, so a heartbeat
    # or a sweep emits on a TRANSITION and never on every tick.
    last_reported_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    # Whose chats this box serves: its own org's (``org``), any org's from the
    # shared pool (``pool``), or the one org it is assigned to (``dedicated``,
    # through ``org_compute_assignments``). A platform box carries the
    # operator org as ``org_team_id`` — the org whose admin minted its
    # credential — and is never that org's OWN machine: placement reads the
    # tenancy, not the org column, to decide whom a box may serve.
    tenancy: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ORG_TENANCY, server_default=ORG_TENANCY
    )
    # The sandbox mode the box reports it enforces (``gvisor`` or ``none``),
    # carried by its claim and heartbeat. The placement guard reads it: a
    # ``none`` box is never placed on the shared pool, and a writable chat is
    # refused on a non-``gvisor`` pool allocation. Fail closed: a row that has
    # not reported reads ``none`` and stays off the pool.
    sandbox: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NO_SANDBOX_MODE, server_default=NO_SANDBOX_MODE
    )
    # What the daemon said it can hold and holds now, carried by its heartbeat:
    # how placement spreads a pool by load. Soft — a box past its capacity still
    # takes a chat when every box is past it (it makes room by closing an idle
    # mirror) rather than stranding the chat.
    capacity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MACHINE_CAPACITY, server_default="6"
    )
    chats_served: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The daemon release the box reported it runs, for the admin machines page.
    daemon_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    # The ``jti`` of the session token the box registered with: the box IS
    # that credential. An agent assertion naming this machine verifies only on
    # a request that carries it — the operator's browser session, or a device
    # token minted later, is a person, not the box. "" until a box registers
    # (a provisioned row, a registration by a token with no ``jti``).
    registered_jti: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    # The daemon process that registered last: a fresh id per process, sent
    # with every heartbeat. Only that process may move the row, so a late
    # ``draining`` beat from the process that just exited cannot drain the row
    # its successor registered. NULL until a daemon that sends one registers.
    daemon_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Why a draining row is draining: ``restart`` when the box's supervisor is
    # restarting the daemon in place (the leases stay, nothing is handed back,
    # and the row reads ``restarting``); NULL for a stop that hands the box's
    # chats on. Meaningless unless ``state`` is ``draining``.
    drain_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # -- machines the plane provisions -------------------------------------
    # The data volume the machine was created with (0 = the provider default).
    storage_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # When ``state`` last changed; what the reconcile's time-outs are measured
    # from. NULL on a row no transition has touched yet.
    state_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # An operator's drain: when it was asked for and why. Set only by the admin
    # drain, cleared by undrain; a daemon re-registering never clears it.
    drain_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drain_reason: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    # A wake the provider refused while the machine was still stopping (EC2
    # answers ``IncorrectInstanceState`` until the stop finishes): when it was
    # asked for, and by whom — ``{"actor", "chain", "reason"}``, the same words
    # the start writes when it lands. The node reconcile starts the machine once
    # the provider allows, then clears both; a request older than its bound is
    # dropped. NULL when no wake is waiting.
    wake_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    wake_request_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Terminate the machine once a drain has emptied it.
    auto_terminate: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # The last resource sample the box's heartbeat carried (cpu, memory, disk),
    # NULL until a daemon that sends one beats.
    resources_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # What the box said it can do on its last heartbeat, as capability names
    # (``workspaces``: it runs a workspace's chats in one sandbox;
    # ``model_switch_v1``: a model switch runs on the next turn). NULL for a
    # daemon that says nothing, which is read as able to do nothing new.
    capabilities_json: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # When the box first said it runs a worker per org (``org_workers``).
    # Sticky: a new process registering and a beat that no longer says it
    # leave it set, so a box rolled back to a build that serves every org from
    # one process keeps the narrowed reach of its machine credential and is
    # given no chat. NULL for a box that never said it.
    ran_org_workers_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # How far apart the box keeps its orgs, as its last beat reported it:
    # ``{"profile", "mechanisms"}`` (``alkera_core.compute.box_isolation``).
    # For the console only: placement reads the ``org_isolation`` capability.
    # NULL for a box that said nothing.
    isolation_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # While the box answers but no worker of it can serve: why (a
    # ``WorkerFault`` code and the box's scrubbed words) and since when.
    # ``fault_until`` is when a beat said it serves again; the meter leaves
    # the minutes in [since, until) unbilled, then clears all four
    # (``alkera_core.compute.unservable``). NULL code: no fault.
    fault_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fault_summary: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    fault_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fault_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The end of the last minute the meter charged. It moves past a fault's or
    # a silent box's minutes without charging them, so a lost machine's refund
    # counts to here, not to ``last_metered_at``. NULL: nothing charged yet
    # (or charged before this column, where the two agree).
    billed_through_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The ``jti`` of the CLI login a provisioned node was handed beside its
    # machine credential, so the release revokes it by id rather than leaving
    # a live token on a disk nobody manages. NULL when the node was given none.
    node_token_jti: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The org whose chats this dedicated box has been assigned to serve. Its
    # disk may still hold that org's files, so it is never assigned to another
    # org: serving a second org means a fresh machine. Set on the first
    # assignment and never cleared; NULL for a box no org was ever given.
    tenant_org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    # -- machines an org holds ---------------------------------------------
    # The org machine this allocation backs, if any. Set at creation together
    # with ``tenant_org_id`` (the org machine's org), which a trigger then
    # keeps from ever changing; the pair is a foreign key onto
    # ``org_machines(id, org_team_id)``, so no allocation can back another
    # org's machine.
    org_machine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # The storage price billed per minute while the provider holds the volume
    # (every state in ``COMPUTE_STORAGE_METERED_STATES``), pinned at each start
    # and wake like the compute rate. 0 bills no storage.
    storage_price_per_minute_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    # What the volume costs us per minute. Platform-visibility only.
    true_storage_cost_per_minute_nanos: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    # High-water mark of billed storage time, the storage twin of
    # ``last_metered_at``.
    storage_metered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When a stop's drain gives up waiting for the turns the box still holds.
    drain_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # How many times the provider was asked to create this machine.
    provision_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The last moment anything the box runs did work, from its heartbeat; what
    # an idle stop is measured from.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # How the last provider call failed (one of ``FAILURE_KINDS``), "" when it
    # did not.
    failure_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    # When the reconcile stopped retrying a start this allocation could not get
    # hardware for: the capacity window ended. ``failure_kind`` still says why;
    # this says the waiting is over, so the machine reads "couldn't start".
    capacity_gave_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ComputeAllocationEvent(Base):
    """One edge of a machine's life: from which state to which, why, and who."""

    __tablename__ = "compute_allocation_events"
    __table_args__ = (Index("ix_compute_allocation_events_allocation_at", "allocation_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    allocation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "compute_allocations.id",
            ondelete="CASCADE",
            name="fk_compute_allocation_events_allocation_id",
        ),
        nullable=False,
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_state: Mapped[str] = mapped_column(String(16), nullable=False)
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    actor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


__all__ = [
    "ASLEEP",
    "BOOTSTRAPPING",
    "COMPUTE_ACTIVE_STATES",
    "COMPUTE_ALLOCATION_STATES",
    "COMPUTE_DORMANT_STATES",
    "COMPUTE_LIFECYCLES",
    "COMPUTE_ORIGINS",
    "COMPUTE_SANDBOXES",
    "COMPUTE_STORAGE_METERED_STATES",
    "COMPUTE_TENANCIES",
    "COMPUTE_TERMINAL_STATES",
    "DEDICATED_TENANCY",
    "DEFAULT_MACHINE_CAPACITY",
    "DRAINING",
    "DRAIN_CAP",
    "DRAIN_CREDITS",
    "DRAIN_IDLE",
    "DRAIN_KINDS",
    "DRAIN_MOVE",
    "DRAIN_RESTART",
    "DRAIN_USER",
    "FAILED",
    "FAILURE_AUTH",
    "FAILURE_CAPACITY",
    "FAILURE_INVALID",
    "FAILURE_KINDS",
    "FAILURE_QUOTA",
    "FAILURE_TRANSIENT",
    "FAILURE_UNKNOWN",
    "GVISOR_SANDBOX",
    "LEDGER_REASON_COMPUTE",
    "LEDGER_REASON_COMPUTE_REFUND",
    "LEDGER_REASON_COMPUTE_STORAGE",
    "LOST",
    "MACHINE_NAME_MAX",
    "NO_SANDBOX_MODE",
    "ORG_TENANCY",
    "PENDING",
    "PERSONAL_TENANCY",
    "POOL_TENANCY",
    "PROVISIONED",
    "PROVISIONING",
    "READY",
    "REGISTERED",
    "RELEASED",
    "RELEASING",
    "TERMINATED_BOOT_FAILED",
    "TERMINATED_ORG_DELETED",
    "TERMINATED_PROVIDER_CAPACITY",
    "TERMINATED_REPLACED",
    "TERMINATED_UNFUNDED_RETENTION",
    "ComputeAllocation",
    "ComputeAllocationEvent",
    "ComputeGrant",
    "ComputeLifecycle",
    "ComputeMachineType",
    "ComputeOrigin",
    "ComputeSandbox",
    "ComputeTenancy",
    "compute_ledger_ref",
    "drain_hold_request_id",
    "refund_ledger_ref",
    "storage_ledger_ref",
]
