"""Storage ceilings on the wire: the org's, a member's, and the caller's own.

A figure field is a plain non-negative JSON integer no larger than the largest
ceiling the model holds (see :data:`StorageBytes`); anything else is a 422
that names the bound. A member's cap slot carries a ``version`` a conditional
write sends back in ``If-Match``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from alkera_core.schemas.tenancy.allocations import AllowanceCeiling, StorageBytes

#: Where an org's effective ceiling comes from.
OrgLimitSource = Literal["override", "plan", "default"]
#: Where the tightest ceiling on one member comes from.
MemberLimitSource = Literal["user", "team", "org", "plan"]


class OrgStorageLimitUpdate(BaseModel):
    """``null`` is an explicit "unlimited"; a figure is a ceiling in bytes."""

    limit_bytes: StorageBytes | None = None


class OrgStorageRead(BaseModel):
    """An org's storage picture as the platform sees it."""

    org_id: UUID
    plan_tier: str
    storage_limit_bytes: int | None
    """The effective ceiling; ``null`` is unlimited."""
    storage_limit_source: OrgLimitSource
    storage_used_bytes: int
    override_set: bool
    """Whether a platform admin has set the ceiling by hand."""
    updated_at: datetime | None = None


class StorageLimitUpdate(BaseModel):
    limit_bytes: StorageBytes


class UserStorageLimitRead(BaseModel):
    user_id: UUID
    team_id: UUID | None
    limit_bytes: int
    updated_at: datetime
    version: str
    """The slot's tag after this write — what the next conditional write sends."""


class MemberStorageRead(BaseModel):
    """One member's usage next to every limit that binds them."""

    user_id: UUID
    email: str
    display_name: str
    used_bytes: int
    org_wide_limit_bytes: int | None
    team_limits: list[UserStorageLimitRead]
    limit_bytes: int | None = None
    """The tightest ceiling that binds this member — the same figure their own
    storage reading shows — or ``null`` when nothing bounds them."""
    limit_source: MemberLimitSource | None = None
    limit_ceiling: AllowanceCeiling | None = None
    """When a team's allowance is the binding figure, which team's."""


class OrgStorageAdminView(BaseModel):
    """The org admin's storage dashboard."""

    org_id: UUID
    plan_tier: str
    limit_bytes: int | None
    limit_source: OrgLimitSource
    used_bytes: int
    over_limit: bool
    members: list[MemberStorageRead]


class MyStorageResponse(BaseModel):
    """The caller's own storage picture.

    ``limit_bytes`` is the tightest ceiling that applies to them, ``null`` when
    nothing bounds them (an explicitly unlimited org, no member limit).
    ``over_limit`` is safety mode: usage is at or past a ceiling that applies,
    so new writes are refused until it drops below again.
    """

    used_bytes: int
    limit_bytes: int | None
    limit_source: MemberLimitSource | None
    over_limit: bool
    org_used_bytes: int
    org_limit_bytes: int | None
    limit_ceiling: AllowanceCeiling | None = None
    """When a team's allowance is the binding figure, which team's."""


__all__ = [
    "MemberLimitSource",
    "MemberStorageRead",
    "MyStorageResponse",
    "OrgLimitSource",
    "OrgStorageAdminView",
    "OrgStorageLimitUpdate",
    "OrgStorageRead",
    "StorageLimitUpdate",
    "UserStorageLimitRead",
]
