"""The signed-in person's own org memberships, and the request that switches
between them.

Only ever the caller's own: no route returns another identity's memberships,
and nothing in an org admin's view lists the other orgs a member belongs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from alkera_core.validation.display_name import DisplayNameStr

#: A person's role in an org, read off the org's root team.
OrgRole = Literal["admin", "member"]

#: Where the caller stands in an org they can see: ``active`` (they can switch
#: into it) or ``pending`` (the org provisioned them and they have not joined;
#: ``POST /auth/memberships/join`` activates it).
MembershipState = Literal["active", "pending"]


class MembershipRead(BaseModel):
    """One org the caller belongs to and can switch into, or, when ``status``
    is ``pending``, one that provisioned them and waits for them to join."""

    org_team_id: UUID
    # Empty for an org created by a minimal signup and not yet named.
    org_name: str
    role: OrgRole
    # The org's single sign-on is enabled and enforced and speaks for this
    # person: entering it takes a sign-in through the org's IdP.
    sso_required: bool
    last_active_at: datetime | None = None
    status: MembershipState = "active"


class MembershipListResponse(BaseModel):
    """`GET /auth/memberships`: the caller's active memberships, most recently
    used first, then any pending ones, and the org the calling credential is
    in."""

    active_org_team_id: UUID
    memberships: list[MembershipRead]


class SwitchOrgRequest(BaseModel):
    """`POST /auth/refresh/org`: the org to switch this browser into. Checked
    against the caller's own active memberships; it never selects anything a
    membership does not already grant."""

    org_team_id: UUID


class CreateOrgRequest(BaseModel):
    """`POST /orgs` (and the sign-in landing's `POST /auth/refresh/org/new`):
    the new org's name. Held to the same name rules as any org name."""

    name: DisplayNameStr = Field(min_length=1, max_length=255)


class OrgCreatedResponse(BaseModel):
    """The org just created, for the client to switch into."""

    org_team_id: UUID
    org_name: str


class LeaveOrgResponse(BaseModel):
    """`POST /orgs/current/leave`: the org the client should switch into next
    (the most recently used one the person can still enter), or null when the
    person has none left. Nothing is signed in by this answer."""

    next_org_team_id: UUID | None = None


class JoinOrgRequest(BaseModel):
    """`POST /auth/refresh/org/join`: the invitation link's token, accepted by a
    signed-in person who has no org to enter yet."""

    token: str = Field(min_length=1, max_length=512)


class JoinMembershipRequest(BaseModel):
    """`POST /auth/memberships/join`: the org whose pending membership the
    caller accepts. Checked against the caller's own pending memberships; it
    never selects anything else."""

    org_team_id: UUID


__all__ = [
    "CreateOrgRequest",
    "JoinMembershipRequest",
    "JoinOrgRequest",
    "LeaveOrgResponse",
    "MembershipListResponse",
    "MembershipRead",
    "MembershipState",
    "OrgCreatedResponse",
    "OrgRole",
    "SwitchOrgRequest",
]
