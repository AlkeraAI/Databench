"""Machines an org holds, who may use them, and workspaces moving between them.

- ``org_machines`` — a machine an org holds: the stable id people, workspace
  pins and the audit trail see, its name, the offering it was bought from, its
  storage size, its use mode (``pool``: serves the org's regular chats;
  ``assigned``: serves only the workspaces pinned to it), what the org asked
  of its power (``desired_power``) and why it is off (``stop_reason``). It
  outlives any one provider machine: ``current_allocation_id`` names the
  ``compute_allocations`` row backing it now, and a stop, a start, a provider
  loss or a replacement moves that pointer, never the org machine. The pointer
  is a composite foreign key onto ``compute_allocations(id, tenant_org_id)``,
  so the database refuses an org machine backed by another org's allocation.
  Soft-deleted (``deleted_at``): the row stays for invoices and audit. Writes
  carry ``version`` for ``If-Match``.
- ``org_machine_audiences`` — who may use an ``assigned`` machine: the whole
  org, a team (and through materialized membership its sub-teams), or one
  person. A trigger refuses a team outside the row's org.
- ``org_compute_settings`` — one row per org: whether its regular chats may run
  on the shared pool while no org pool machine can serve them, and how many
  pool machines to keep awake.
- ``workspace_machine_moves`` — one request to move a workspace from one org
  machine to another (or to the default placement), driven through its states
  by a workflow. At most one is active per workspace.

All four are tenant tables: each carries ``org_team_id``, is listed in
:data:`alkera_core.db.row_security.CONTENT_TABLES`, and has row security
enabled and forced with the ``tenant_isolation`` policy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.db.errors import register_unique

#: Names are unique among an org's live machines, ignoring case.
NAME_LIVE_INDEX = "ux_org_machines_org_name_live"
#: The code and sentence a taken name is refused with, by a check or by the index.
NAME_TAKEN_CODE = "name_taken"
NAME_TAKEN_MESSAGE = "Another machine already has this name."

OrgMachineAcquisition = Literal["purchased", "granted", "added"]
#: Bought with the org's credits.
PURCHASED: OrgMachineAcquisition = "purchased"
#: Given by Alkera, free until ``free_until``.
GRANTED: OrgMachineAcquisition = "granted"
#: A host the org runs itself, attached by its connection details
#: (``ssh_machine_endpoints``). Never bought, never billed.
ADDED: OrgMachineAcquisition = "added"
ACQUISITIONS: tuple[str, ...] = (PURCHASED, GRANTED, ADDED)
#: The acquisitions nothing is charged for while they run: admission asks no
#: credit and every rate pinned on their allocations is zero.
UNBILLED_ACQUISITIONS: frozenset[str] = frozenset({ADDED})

OrgMachineUseMode = Literal["pool", "assigned"]
#: Serves the org's regular chats (honored for enterprise orgs and
#: self-hosted deployments only).
USE_POOL: OrgMachineUseMode = "pool"
#: Serves only workspaces pinned to it by someone in its audience.
USE_ASSIGNED: OrgMachineUseMode = "assigned"
USE_MODES: tuple[str, ...] = (USE_POOL, USE_ASSIGNED)

DesiredPower = Literal["on", "off"]
POWER_ON: DesiredPower = "on"
POWER_OFF: DesiredPower = "off"
DESIRED_POWERS: tuple[str, ...] = (POWER_ON, POWER_OFF)

#: Why an org machine is off. ``''`` while it is wanted on.
STOP_NONE = ""
STOP_USER = "user"
STOP_IDLE = "idle"
STOP_CREDITS = "credits"
STOP_CAP = "cap"
STOP_FREE_EXPIRED = "free_expired"
STOP_PROVIDER = "provider"
STOP_MOVED = "moved"
#: Stopped by the platform after it stayed silent, or unable to run chats,
#: for ``machine_unservable_stop_minutes``.
STOP_NOT_RESPONDING = "not_responding"
STOP_REASONS: tuple[str, ...] = (
    STOP_NONE,
    STOP_USER,
    STOP_IDLE,
    STOP_CREDITS,
    STOP_CAP,
    STOP_FREE_EXPIRED,
    STOP_PROVIDER,
    STOP_MOVED,
    STOP_NOT_RESPONDING,
)

GranteeKind = Literal["org", "team", "user"]
GRANTEE_ORG: GranteeKind = "org"
GRANTEE_TEAM: GranteeKind = "team"
GRANTEE_USER: GranteeKind = "user"
GRANTEE_KINDS: tuple[str, ...] = (GRANTEE_ORG, GRANTEE_TEAM, GRANTEE_USER)

MoveState = Literal["requested", "draining", "switching", "waking", "done", "failed", "canceled"]
MOVE_REQUESTED: MoveState = "requested"
MOVE_DRAINING: MoveState = "draining"
MOVE_SWITCHING: MoveState = "switching"
MOVE_WAKING: MoveState = "waking"
MOVE_DONE: MoveState = "done"
MOVE_FAILED: MoveState = "failed"
MOVE_CANCELED: MoveState = "canceled"
#: The states a move ends in; any other is active.
MOVE_FINISHED_STATES: tuple[str, ...] = (MOVE_DONE, MOVE_FAILED, MOVE_CANCELED)
MOVE_STATES: tuple[str, ...] = (
    MOVE_REQUESTED,
    MOVE_DRAINING,
    MOVE_SWITCHING,
    MOVE_WAKING,
    *MOVE_FINISHED_STATES,
)

ORG_MACHINE_NAME_MAX = 64
#: The shortest idle window an org machine may be stopped after.
IDLE_STOP_MINUTES_MIN = 5


class OrgMachine(Base):
    __tablename__ = "org_machines"
    __table_args__ = (
        CheckConstraint(f"acquisition IN {ACQUISITIONS}", name="ck_org_machines_acquisition"),
        CheckConstraint(
            "acquisition <> 'granted' OR free_until IS NOT NULL",
            name="ck_org_machines_granted_free_until",
        ),
        CheckConstraint(f"use_mode IN {USE_MODES}", name="ck_org_machines_use_mode"),
        CheckConstraint("storage_gb > 0", name="ck_org_machines_storage_positive"),
        CheckConstraint(
            f"idle_stop_minutes IS NULL OR idle_stop_minutes >= {IDLE_STOP_MINUTES_MIN}",
            name="ck_org_machines_idle_stop_minutes",
        ),
        CheckConstraint(
            "monthly_cap_nanos IS NULL OR monthly_cap_nanos >= 0",
            name="ck_org_machines_monthly_cap_nonnegative",
        ),
        CheckConstraint(f"desired_power IN {DESIRED_POWERS}", name="ck_org_machines_desired_power"),
        CheckConstraint(f"stop_reason IN {STOP_REASONS}", name="ck_org_machines_stop_reason"),
        CheckConstraint("version >= 1", name="ck_org_machines_version_positive"),
        # The pointer at the provider machine backing it now names an
        # allocation of THIS org: (id, tenant_org_id) is unique on
        # compute_allocations, so a pointer at another org's allocation has no
        # row to reference. Losing the allocation clears the pointer only.
        ForeignKeyConstraint(
            ["current_allocation_id", "org_team_id"],
            ["compute_allocations.id", "compute_allocations.tenant_org_id"],
            name="fk_org_machines_current_allocation_tenant",
            ondelete="SET NULL (current_allocation_id)",
        ),
        Index(
            "ux_org_machines_current_allocation_id",
            "current_allocation_id",
            unique=True,
        ),
        Index(
            NAME_LIVE_INDEX,
            "org_team_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The key the tenant policy and every org listing reads by.
        Index("ix_org_machines_org_team_id", "org_team_id"),
        Index("ix_org_machines_owner_team_id", "owner_team_id"),
        Index("ix_org_machines_offering_id", "offering_id"),
        # The target of compute_allocations' (org_machine_id, tenant_org_id)
        # foreign key.
        Index("ux_org_machines_id_org", "id", "org_team_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The org root.
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_org_machines_org_team_id"),
        nullable=False,
    )
    # The team that holds it: the org root, or the team whose admin bought it.
    # Its managers are org admins and admins of this team or any team above.
    owner_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_org_machines_owner_team_id"),
        nullable=False,
    )
    offering_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("compute_offerings.id", ondelete="RESTRICT", name="fk_org_machines_offering_id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(ORG_MACHINE_NAME_MAX), nullable=False)
    acquisition: Mapped[str] = mapped_column(String(16), nullable=False)
    # A granted machine is free until this moment, then stopped unless bought.
    free_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stop it after this many minutes with nothing running; NULL never does.
    idle_stop_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # An optional spend cap per billing cycle, integer nano-USD.
    monthly_cap_nanos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The provider machine backing it now; NULL while none does.
    current_allocation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # What the org asked for; the reconcile converges the allocation to it.
    desired_power: Mapped[str] = mapped_column(
        String(8), nullable=False, default=POWER_ON, server_default=POWER_ON
    )
    stop_reason: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STOP_NONE, server_default=""
    )
    # When it was stopped because its funding ran out; what disk retention
    # counts from.
    unfunded_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_org_machines_created_by"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class OrgMachineAudience(Base):
    """One grant of use on an ``assigned`` org machine."""

    __tablename__ = "org_machine_audiences"
    __table_args__ = (
        CheckConstraint(
            f"grantee_kind IN {GRANTEE_KINDS}", name="ck_org_machine_audiences_grantee_kind"
        ),
        CheckConstraint(
            "(grantee_kind = 'org' AND team_id IS NULL AND user_id IS NULL)"
            " OR (grantee_kind = 'team' AND team_id IS NOT NULL AND user_id IS NULL)"
            " OR (grantee_kind = 'user' AND user_id IS NOT NULL AND team_id IS NULL)",
            name="ck_org_machine_audiences_grantee",
        ),
        Index(
            "ux_org_machine_audiences_grant",
            "org_machine_id",
            "grantee_kind",
            "team_id",
            "user_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        # The machine a grant is on is a machine of the grant's own org.
        ForeignKeyConstraint(
            ["org_machine_id", "org_team_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_org_machine_audiences_org_machine_tenant",
            ondelete="CASCADE",
        ),
        Index("ix_org_machine_audiences_org_team_id", "org_team_id"),
        Index("ix_org_machine_audiences_team_id", "team_id"),
        Index("ix_org_machine_audiences_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_org_machine_audiences_org_team_id"),
        nullable=False,
    )
    org_machine_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    grantee_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_org_machine_audiences_team_id"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_org_machine_audiences_user_id"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_org_machine_audiences_created_by"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrgComputeSettings(Base):
    """An org's compute settings: one row per org, absent means the defaults."""

    __tablename__ = "org_compute_settings"
    __table_args__ = (
        CheckConstraint("min_awake_pool >= 0", name="ck_org_compute_settings_min_awake_pool"),
        CheckConstraint("version >= 1", name="ck_org_compute_settings_version_positive"),
        # The default is a machine of the setting's own org; losing it clears
        # the default only.
        ForeignKeyConstraint(
            ["default_org_machine_id", "org_team_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_org_compute_settings_default_org_machine_tenant",
            ondelete="SET NULL (default_org_machine_id)",
        ),
        Index("ix_org_compute_settings_default_org_machine_id", "default_org_machine_id"),
    )

    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_org_compute_settings_org_team_id"),
        primary_key=True,
    )
    # Whether the org's regular chats may run on the shared pool while no org
    # pool machine can serve them.
    shared_pool_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # How many org pool machines to keep awake whatever their idle windows say.
    min_awake_pool: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # The org machine a new workspace is pinned to when its creator names none
    # and may use it; NULL leaves new workspaces on the default placement.
    default_org_machine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkspaceMachineMove(Base):
    """One request to move a workspace onto another machine."""

    __tablename__ = "workspace_machine_moves"
    __table_args__ = (
        CheckConstraint(f"state IN {MOVE_STATES}", name="ck_workspace_machine_moves_state"),
        # One active move per workspace.
        Index(
            "ux_workspace_machine_moves_active",
            "workspace_id",
            unique=True,
            postgresql_where=text(f"state NOT IN {MOVE_FINISHED_STATES}"),
        ),
        # Both ends of a move are machines of the move's own org; losing one
        # clears that end only.
        ForeignKeyConstraint(
            ["from_org_machine_id", "org_team_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_workspace_machine_moves_from_org_machine_tenant",
            ondelete="SET NULL (from_org_machine_id)",
        ),
        ForeignKeyConstraint(
            ["to_org_machine_id", "org_team_id"],
            ["org_machines.id", "org_machines.org_team_id"],
            name="fk_workspace_machine_moves_to_org_machine_tenant",
            ondelete="SET NULL (to_org_machine_id)",
        ),
        Index("ix_workspace_machine_moves_org_team_id", "org_team_id"),
        Index("ix_workspace_machine_moves_workspace_id", "workspace_id"),
        Index("ix_workspace_machine_moves_from_org_machine_id", "from_org_machine_id"),
        Index("ix_workspace_machine_moves_to_org_machine_id", "to_org_machine_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("teams.id", ondelete="CASCADE", name="fk_workspace_machine_moves_org_team_id"),
        nullable=False,
    )
    # The workspace object being moved.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspace_objects.id",
            ondelete="CASCADE",
            name="fk_workspace_machine_moves_workspace_id",
        ),
        nullable=False,
    )
    # Where it runs and where it is going; NULL is the default placement.
    from_org_machine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    to_org_machine_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MOVE_REQUESTED, server_default=MOVE_REQUESTED
    )
    # End the chats still answering instead of waiting for their turns.
    stop_running: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    error_code: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    error: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_workspace_machine_moves_requested_by"),
        nullable=True,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Whether the departing box flushed the workspace's files before the switch:
    # true, false, or NULL while unknown (a box too old to flush says nothing).
    flushed_before_switch: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


register_unique(NAME_LIVE_INDEX, "name", NAME_TAKEN_MESSAGE, code=NAME_TAKEN_CODE)

__all__ = [
    "ACQUISITIONS",
    "DESIRED_POWERS",
    "GRANTED",
    "GRANTEE_KINDS",
    "GRANTEE_ORG",
    "GRANTEE_TEAM",
    "GRANTEE_USER",
    "IDLE_STOP_MINUTES_MIN",
    "MOVE_CANCELED",
    "MOVE_DONE",
    "MOVE_DRAINING",
    "MOVE_FAILED",
    "MOVE_FINISHED_STATES",
    "MOVE_REQUESTED",
    "MOVE_STATES",
    "MOVE_SWITCHING",
    "MOVE_WAKING",
    "NAME_LIVE_INDEX",
    "NAME_TAKEN_CODE",
    "NAME_TAKEN_MESSAGE",
    "ORG_MACHINE_NAME_MAX",
    "POWER_OFF",
    "POWER_ON",
    "PURCHASED",
    "STOP_CAP",
    "STOP_CREDITS",
    "STOP_FREE_EXPIRED",
    "STOP_IDLE",
    "STOP_MOVED",
    "STOP_NONE",
    "STOP_NOT_RESPONDING",
    "STOP_PROVIDER",
    "STOP_REASONS",
    "STOP_USER",
    "USE_ASSIGNED",
    "USE_MODES",
    "USE_POOL",
    "DesiredPower",
    "GranteeKind",
    "MoveState",
    "OrgComputeSettings",
    "OrgMachine",
    "OrgMachineAcquisition",
    "OrgMachineAudience",
    "OrgMachineUseMode",
    "WorkspaceMachineMove",
]
