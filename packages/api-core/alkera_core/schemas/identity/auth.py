"""Auth route schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from alkera_core.models._enums import TokenType
from alkera_core.schemas.identity.user import UserRead
from alkera_core.validation.display_name import DisplayNameStr, OptionalDisplayNameStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=255)
    # Cloudflare Turnstile token from the SPA's invisible captcha widget. Verified
    # server-side before authentication when Turnstile is configured; ignored
    # otherwise (so dev/tests need no captcha).
    turnstile_token: str | None = None
    # TOTP / backup code for accounts with MFA enabled. When omitted on an
    # MFA-enabled account the login returns 401 `mfa_required`; the SPA then
    # collects the code and resubmits email + password + mfa_code.
    mfa_code: str | None = Field(default=None, max_length=64)


class LoginResponse(BaseModel):
    user: UserRead
    # When the ACCESS token lapses. The refresh cookie keeps the browser signed
    # in past it; the SPA refreshes shortly before this instant.
    expires_at: datetime
    # The person belongs to several orgs and the one they used last needs a
    # step-up first, so the sign-in landed elsewhere: the client offers the
    # org chooser instead of going straight in.
    choose_org: bool = False


class MeRead(UserRead):
    """`GET /auth/me`: the current user plus when their access token lapses, so
    a freshly loaded tab knows when to refresh without paying a failed request
    first. ``None`` for a credential with no session expiry to report."""

    session_expires_at: datetime | None = None


class MfaEnrollResponse(BaseModel):
    """One-time enrollment payload — the secret + otpauth URI to add to an
    authenticator app. Persisted PENDING; activated by /mfa/confirm."""

    secret: str
    otpauth_uri: str


class MfaCodeRequest(BaseModel):
    """A 6-digit TOTP or a backup code (used to confirm enrollment / disable MFA)."""

    code: str = Field(min_length=1, max_length=64)


class MfaConfirmResponse(BaseModel):
    """Activation result — the single-use backup codes, shown exactly once."""

    backup_codes: list[str]


class MfaStatusResponse(BaseModel):
    enabled: bool
    backup_codes_remaining: int


class OAuthProvidersResponse(BaseModel):
    """Platform-enabled external-login providers, for rendering sign-in buttons.

    Note: this is the *platform* configuration (which providers have credentials
    wired up). Per-org `allow_login_*` gating is enforced server-side at the
    callback — the login page can't know the org before sign-in.
    """

    providers: list[str]


class OAuthRegisterContext(BaseModel):
    """Prefill data for the SPA register screen, derived from a signed ticket.

    The backend always trusts the ticket (not the client) for email/provider —
    these are display-only hints.
    """

    provider: str
    email: EmailStr
    first_name: str = ""
    last_name: str = ""
    has_invite: bool = False


class OAuthRegisterRequest(BaseModel):
    """Complete an OAuth-initiated registration.

    `oauth_ticket` is the signed ticket minted by the OAuth callback; it carries
    the verified email/provider/subject. The user supplies (and may edit) their
    name, and chooses an org exactly like email/password signup: exactly one of
    `org_name` (new org, becomes Org Admin) or `invite_token` (join via invite).
    When the ticket itself carries an invite, that wins.
    """

    oauth_ticket: str
    first_name: DisplayNameStr = Field(min_length=1, max_length=255)
    last_name: DisplayNameStr = Field(min_length=1, max_length=255)
    org_name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)
    invite_token: str | None = None
    # Escape hatch for the business-email gate — set by the SPA only after the
    # user explicitly chooses to keep their personal account on the dedicated
    # "use your work email" page.
    allow_personal_email: bool = False


class CompleteProfileRequest(BaseModel):
    """Finish a profile provisioned without one (minimal email + password signup,
    or a JIT/SSO account).

    `org_name`, when present, renames the caller's org — but only when they admin
    it (the new-org / minimal-signup case). It is ignored for an invited member,
    who doesn't own the organization they joined.
    """

    first_name: DisplayNameStr = Field(min_length=1, max_length=255)
    last_name: DisplayNameStr = Field(min_length=1, max_length=255)
    org_name: OptionalDisplayNameStr = Field(default=None, min_length=1, max_length=255)


class DeviceApprovalRequest(BaseModel):
    """SPA approve/deny body — the short user_code shown on the device.

    Reached via the device flow (RFC 8628): the user opens the verification URI
    with this code pre-filled and confirms it matches what their CLI/editor
    printed before approving.
    """

    user_code: str = Field(min_length=1, max_length=32)


class DeviceApproveRequest(DeviceApprovalRequest):
    """SPA approve body: the user code, and optionally which of the approver's
    orgs the device acts in. Omitted, it is the org the approving browser
    session is in. Checked against the approver's own active memberships; it
    never grants an org a membership does not."""

    org_team_id: UUID | None = None


class DeviceInfoResponse(BaseModel):
    """Consent-screen data for GET /auth/device/info — what the SPA shows before
    the user approves a pending device authorization."""

    client_id: str
    client_name: str
    scope: str | None = None
    expires_at: datetime
    user_code: str


class MessageResponse(BaseModel):
    """Generic 'OK' response for endpoints that return only an acknowledgement."""

    message: str = "ok"


class SessionRead(BaseModel):
    """One live session in `GET /auth/sessions`. Never includes a token value.

    A browser session is a refresh-token FAMILY: `jti` carries the family id,
    `issued_at` is when it signed in, `expires_at` its absolute expiry, and
    `client` what the browser said it was. A CLI token is its own row, keyed by
    its real `jti`. `current` flags the session making the request (so a UI can
    avoid offering to revoke the user out from under themselves without warning).
    """

    model_config = ConfigDict(from_attributes=True)

    jti: str
    token_type: TokenType
    issued_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    label: str | None = None
    client: str | None = None
    current: bool = False


class SessionListResponse(BaseModel):
    sessions: list[SessionRead]
