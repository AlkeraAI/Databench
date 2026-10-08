"""Pydantic schemas for Invitation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, model_validator

from alkera_core.models._enums import InvitationStatus, TeamRole


class InvitationCreate(BaseModel):
    email: EmailStr
    role: TeamRole = TeamRole.MEMBER


class InvitationRead(BaseModel):
    """Authenticated read shape.

    The raw `token` is intentionally NOT exposed — only its keyed hash is stored,
    so a DB read can't yield a usable invite link. The recipient's
    `/dashboard/invites` page accepts/rejects by `id` (`/{invitation_id}/...`);
    the raw token only ever travels in the emailed `/signup?invite=<token>` link.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    team_id: UUID
    email: str
    role: TeamRole
    status: InvitationStatus
    expires_at: datetime
    created_at: datetime
    resolved_at: datetime | None = None
    invited_by_id: UUID | None = None
    # Friendly label for `role` (e.g. 'Member'); filled from `role` below.
    role_display: str = ""

    @model_validator(mode="after")
    def _fill_role_display(self) -> Self:
        self.role_display = self.role.display_name
        return self


class InvitationRefusalCode(StrEnum):
    """Why a pending invitation cannot be accepted by the account reading it.

    A standing fact about the recipient, not a transient failure: the same
    answer comes back on every accept until something outside the invitation
    changes. ``other_org`` is the single-org rule — the account already belongs
    to a different organization than the one the invitation is into.
    ``email_verification_required`` asks the account to prove its address
    before it joins another organization, and ``membership_deactivated`` says
    the organization offboarded this account, which an invitation does not
    undo."""

    OTHER_ORG = "other_org"
    EMAIL_VERIFICATION_REQUIRED = "email_verification_required"
    MEMBERSHIP_DEACTIVATED = "membership_deactivated"


class InvitationRefusal(BaseModel):
    """The reason an invitation cannot be accepted, and what would have to
    change for it to be — the same sentence the accept route answers with."""

    code: InvitationRefusalCode
    message: str


class RecipientInvitationRead(InvitationRead):
    """One of the caller's own pending invitations, as the recipient sees it.

    Carries the destination by name (a team in another organization is not in
    the caller's team list, so the client cannot resolve it), who sent it, and —
    when the account cannot accept it — the refusal the accept route would give.
    A refused invitation is still listed so its recipient can read why and
    decline it; only the owner of the address ever sees this shape."""

    team_name: str
    org_name: str
    inviter_display_name: str | None = None
    refusal: InvitationRefusal | None = None


class InvitationPublicRead(BaseModel):
    """No-auth preview shape used by the signup page when a token is in the
    URL. Returns enough to render 'Join {org}/{team}, invited by {inviter}'
    without leaking other invitations or the inviter's email.

    `email` is the invited address. Signup against this token only succeeds for
    that exact address, so the page prefills it rather than asking the invitee to
    retype it; the token was mailed there, so its holder already knows it.
    """

    email: str
    team_id: UUID
    team_name: str
    org_team_id: UUID
    org_name: str
    role: TeamRole
    inviter_display_name: str | None = None
    expires_at: datetime


class InvitationAcceptResponse(BaseModel):
    """An accepted invitation and the teams it seated the account in.

    ``org_team_id`` / ``org_name`` name the organization joined, so a client
    whose session is in another organization can offer to switch into it."""

    invitation: InvitationRead
    joined_team_ids: list[UUID]
    org_team_id: UUID | None = None
    org_name: str | None = None
