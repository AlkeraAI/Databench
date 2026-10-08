"""Platform-staff shapes for the chat boxes the platform runs: minting a box's
credential, reading the machines page, and an org's dedicated compute.

A module of its own, like ``compute_admin``: these are staff-facing (a box's
true cost and its credential are exactly what an operator must see) and live
behind ``/admin/v1``. In-flight HTTP shapes only, plain ``BaseModel``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from alkera_core.models.compute import ComputeTenancy

PlatformTenancy = Literal["pool", "dedicated"]
MachineStatusLiteral = Literal[
    "starting", "ready", "draining", "restarting", "unreachable", "asleep", "none"
]


class MachineMintRequest(BaseModel):
    """Mint a credential for a box a platform admin provisioned by hand."""

    label: str = Field(min_length=1, max_length=128)
    # The catalog row the box registers as: its kind (``ec2``) and size code.
    provider: str = Field(default="ec2", min_length=1, max_length=32)
    instance_type: str = Field(min_length=1, max_length=128)
    region: str = Field(default="", max_length=32)
    tenancy: PlatformTenancy = "dedicated"


class PlatformMachineRead(BaseModel):
    """One platform box: its credential, and its registration when it has one."""

    credential_id: str
    label: str
    provider: str
    instance_type: str
    region: str = ""
    # What the credential was minted for. The console mints platform tenancies
    # only, but a person's own box holds a credential too and is listed here.
    tenancy: ComputeTenancy
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    # The registered machine, once the box has claimed the credential.
    machine_id: str | None = None
    machine_name: str = ""
    status: MachineStatusLiteral = "none"
    last_heartbeat_at: datetime | None = None
    chats_served: int = 0
    capacity: int = 0
    daemon_version: str = ""
    # What the box costs the platform per minute, from the catalog.
    true_cost_per_minute_nanos: int = 0
    # The org this box is dedicated to, when it is assigned.
    assigned_org_id: str | None = None
    assigned_org_name: str = ""


class MachineMinted(BaseModel):
    """The one answer that carries the raw secret. It is never stored and
    never shown again."""

    credential: str
    machine: PlatformMachineRead


class OrgComputeAssignmentUpdate(BaseModel):
    """Point an org's chats at one dedicated box, or return it to the pool."""

    # ``None`` unassigns: the org's chats go back to the shared pool.
    machine_id: str | None = None
    fallback_to_pool: bool = False


class OrgComputeAssignmentRead(BaseModel):
    org_team_id: str
    machine_id: str | None = None
    fallback_to_pool: bool = False
    machine: PlatformMachineRead | None = None
    assigned_at: datetime | None = None


__all__ = [
    "MachineMintRequest",
    "MachineMinted",
    "OrgComputeAssignmentRead",
    "OrgComputeAssignmentUpdate",
    "PlatformMachineRead",
    "PlatformTenancy",
]
