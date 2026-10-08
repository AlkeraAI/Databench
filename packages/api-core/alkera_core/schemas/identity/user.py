"""Pydantic schemas for User."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, model_validator

from alkera_core.models._enums import PlatformRole
from alkera_core.validation.display_name import DisplayNameStr
from alkera_core.validation.password import USER_PASSWORD_SCHEMA


class UserBase(BaseModel):
    # Shared read shape. Names are deliberately UNCONSTRAINED here: the DB
    # allows empty strings (the "profile incomplete" sentinel for JIT/SSO
    # provisioning and single-word legacy names), so a *read* of any user must
    # not blow up. Write schemas below re-add `min_length=1`.
    email: EmailStr
    first_name: str = Field(max_length=255)
    last_name: str = Field(max_length=255)


class UserCreate(UserBase):
    org_team_id: UUID
    # Interactive create requires real names — and DisplayNameStr, because the
    # inviter's display name is rendered in the invitation email alongside the
    # org and team names (alkera_core.validation.display_name).
    first_name: DisplayNameStr = Field(min_length=1, max_length=255)
    last_name: DisplayNameStr = Field(min_length=1, max_length=255)
    # Accepted for the clients that still send it, and never used: an org admin
    # does not choose a person's password (the deprecated create route answers
    # ``password_ignored``, and the person sets their own from the email).
    password: str | None = Field(
        default=None, min_length=8, max_length=255, json_schema_extra=USER_PASSWORD_SCHEMA
    )


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    # Read off a ``User`` row it is the identity's home org (the model maps the
    # column as ``home_org_team_id``); every read that returns the current user
    # overwrites it with the credential's org.
    org_team_id: UUID = Field(validation_alias=AliasChoices("org_team_id", "home_org_team_id"))
    # The caller's organization name. Empty string when the org was created by a
    # minimal (email + password) signup and not yet named — the signal the SPA
    # uses to show the "organization name" field on the complete-profile step.
    # Populated by the auth reads that return the current user (`/me`,
    # `/complete-profile`, login, signup, verify-email); defaults to "" on any
    # other producer, since only those reads consume it.
    org_name: str = ""
    # `org_team_id` and `org_name` describe the org the calling credential is in
    # (the active org), which for a person in several orgs is not necessarily
    # their home org. The person's role there, and how many orgs they can switch
    # between (1 while multi-org is off). Filled by the same reads as `org_name`.
    org_role: Literal["admin", "member"] = "member"
    membership_count: int = 1
    # Derived `"{first} {last}"` — read straight off the model property, kept in
    # the response so the SPA has a convenient combined name.
    display_name: str
    platform_role: PlatformRole | None = None
    email_verified_at: datetime | None = None
    # Verification gate (see `alkera_core.verification`). `required` is true while
    # the account still owes a verified email; `deadline` is when the grace
    # window lapses and the account is blocked from the API + gateway. Both are
    # false/null for verified or platform-staff accounts. The SPA uses them to
    # render the persistent banner (in grace) and the full-screen gate (blocked).
    email_verification_required: bool = False
    email_verification_deadline: datetime | None = None
    # When the next self-service verification resend becomes available (the
    # resend route 429s until then). Null when verified/exempt, or when no
    # verification email is pending so a resend is allowed immediately. Not a
    # model attribute — filled by the auth reads that return the current user;
    # clients render the resend-cooldown countdown from this instead of
    # guessing locally from a 429.
    verification_resend_available_at: datetime | None = None
    # Every team this account administers, descent included: an ADMIN role is
    # granted on one team and reaches every team below it, so this is the
    # granted teams plus their subtrees. Filled by the auth reads that return
    # the current user; it is what lets a client offer only the teams a person
    # may actually configure something for, instead of asking the server team by
    # team. Empty on any other producer, since only those reads consume it.
    admin_team_ids: list[UUID] = Field(default_factory=list)
    # True iff the user has set a local password. Lets the SPA label the
    # account action "Set password" vs "Change password" without leaking the
    # hash. (Whether a password exists is not sensitive; the hash is.)
    has_password: bool = False
    # Whether the account has TOTP MFA active — lets the SPA show the right
    # security-settings state + (when an org requires MFA) nudge enrollment.
    mfa_enabled: bool = False
    created_at: datetime
    # Friendly label for `platform_role` (e.g. 'Platform support'), filled from the
    # role below so clients don't hard-code their own mapping.
    platform_role_display: str | None = None

    @model_validator(mode="after")
    def _fill_platform_role_display(self) -> Self:
        self.platform_role_display = self.platform_role.display_name if self.platform_role else None
        return self


class LinkedIdentity(BaseModel):
    """One linked external-login provider for the current user."""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    email_at_link: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class LinkedIdentityList(BaseModel):
    identities: list[LinkedIdentity]
