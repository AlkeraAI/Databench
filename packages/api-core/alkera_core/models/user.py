"""User model: an identity.

A user row authenticates (password, OAuth, SSO, MFA); what puts it inside an
org is an ``org_memberships`` row, one per org it belongs to. The ``org_team_id``
column is the identity's HOME org, mapped as ``home_org_team_id`` so no code
reads it as "the org" of a person: a request's org is its credential's. The
home org is only the org a sign-in that names none enters, and what an org's
deletion repoints (``alkera_core.auth.tenancy``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from alkera_core.db.base import Base
from alkera_core.db.errors import register_unique
from alkera_core.models._enums import PlatformRole

if TYPE_CHECKING:
    from alkera_core.models.oauth_identity import OAuthIdentity
    from alkera_core.models.team import Team
    from alkera_core.models.team_membership import TeamMembership


#: The unique index on ``users.email``: one account per address.
EMAIL_INDEX = "ix_users_email"
#: What a second account for an address is told.
EMAIL_TAKEN_MESSAGE = "An account with this email already exists."


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "provisioned_by IS NULL OR provisioned_by IN ('scim', 'sso')",
            name="ck_users_provisioned_by",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    home_org_team_id: Mapped[uuid.UUID] = mapped_column(
        "org_team_id",
        Uuid,
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    # Domain part of `email`, lowercased — denormalized + indexed for future
    # domain-scoped queries (domain→org auto-join, SSO domain claims, analytics).
    # Always kept in sync with `email` by the service layer (see user_service).
    email_domain: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="", index=True
    )
    # Structured name. Required at the API surface (schemas enforce min_length=1);
    # the empty-string default is the "profile incomplete" sentinel for users
    # provisioned without a name (future JIT/SSO) — never nullable.
    first_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    last_name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # No longer read: the break-glass flag is per org, on
    # ``org_memberships.sso_exempt``. Kept until the column is dropped.
    sso_exempt: Mapped[bool] = mapped_column(
        "sso_exempt", default=False, server_default="false", nullable=False
    )
    # The identity-level platform disable: a disabled identity cannot sign in
    # anywhere and every credential it holds is refused on its next request.
    # Only platform staff set it. An org offboards a person on their membership
    # (``org_memberships.status``), which never touches this flag, because the
    # identity may belong to other orgs.
    is_active: Mapped[bool] = mapped_column(
        default=True,
        server_default="true",
        nullable=False,
        comment=(
            "The identity-level platform disable: a disabled identity signs in nowhere. "
            "Only platform staff set it; an org offboards on org_memberships.status."
        ),
    )
    # Set when the account was erased (``alkera_core.account.erasure``): the row
    # stays as the tombstone every retained record (ledger, audit, created_by)
    # still points at, with every personal column scrubbed. A tombstone is
    # never active and never signs in.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Brute-force lockout: consecutive failures within a window lock the account for
    # a cool-off period (DB-backed so it holds across replicas without a WAF).
    failed_login_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    last_failed_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TOTP MFA. The base32 secret is stored encrypted (secret_box); backup codes are
    # an HMAC-hashed JSON list (single-use recovery codes shown once at enrollment).
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    mfa_backup_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_last_used_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The TOTP time step of the most recently accepted code. Skew tolerance
    accepts a code across several steps, which is also a replay window; refusing
    every step at or before this one is what makes an accepted code single-use
    (RFC 6238 §5.2). NULL means no code has been accepted yet."""
    # No longer written: an org's SCIM id for a person lives on their membership
    # (``org_memberships.scim_external_id``). Kept until the column is dropped.
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    platform_role: Mapped[PlatformRole | None] = mapped_column(
        Enum(
            PlatformRole,
            native_enum=False,
            length=32,
            name="platform_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=True,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the one-time founders welcome email was sent. Null = never welcomed.
    # The once-ever guard so the welcome fires at most once per user across every
    # verification path (verify-email, invite signup, OAuth). See `welcome_service`.
    welcome_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # HMAC-SHA256 hash of the latest pending verification token (the raw token
    # only ever lives in the emailed link). Overwritten on resend; cleared on
    # verify. See `alkera_core.auth.token_hash`.
    email_verification_token: Mapped[str | None] = mapped_column(
        String(96), nullable=True, unique=True, index=True
    )
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # HMAC-SHA256 hash of the latest pending password-reset token (the raw token
    # only ever lives in the emailed link). Overwritten on re-request; cleared
    # on successful reset. See `alkera_core.auth.token_hash`.
    password_reset_token: Mapped[str | None] = mapped_column(
        String(96), nullable=True, unique=True, index=True
    )
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Abuse forensics: the client IP that created the account and the one that
    # last logged in (X-Forwarded-For-aware, same resolution the Turnstile check
    # uses). Textual (max IPv6 = 45 chars) so mapped/bracketed forms round-trip
    # verbatim. Null for accounts that predate the columns; admin-only surface.
    # Set when an org's identity provider created the account rather than the
    # person: ``scim`` (provisioned through the org's SCIM token) or ``sso`` (a
    # first sign-in through the org's IdP). Such an account is the org's to
    # vouch for, so a social sign-in (Google, GitHub) is never linked into it
    # by email alone. NULL for an account the person made.
    provisioned_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    signup_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # "Revoke-all" lever: any token whose `iat` predates this instant is
    # treated as revoked. Bumped on logout-all + password reset. Defaults to
    # the unix epoch so existing/normal tokens always pass.
    token_epoch: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("'1970-01-01 00:00:00+00'"),
        nullable=False,
    )

    home_organization: Mapped[Team] = relationship(foreign_keys=[home_org_team_id])
    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    oauth_identities: Mapped[list[OAuthIdentity]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    @property
    def display_name(self) -> str:
        """Human name = ``"{first} {last}"``. Derived (no column) so there is a
        single source of truth; reads everywhere keep working via this property,
        and Pydantic ``from_attributes`` picks it up transparently."""
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def profile_complete(self) -> bool:
        """True once the user has both a first and last name. Drives the
        complete-profile gate for JIT/SSO-provisioned users."""
        return bool(self.first_name.strip()) and bool(self.last_name.strip())

    @property
    def has_password(self) -> bool:
        """True iff a local password is set. Lets the SPA label the password
        action ("Set" vs "Change") without exposing the hash."""
        return self.password_hash is not None

    @property
    def email_verification_required(self) -> bool:
        """True while this account still owes a verified email (unverified and
        not platform staff). Drives the SPA's persistent verify banner. See
        `alkera_core.verification`."""
        from alkera_core.verification import requires_verification

        return requires_verification(self)

    @property
    def email_verification_deadline(self) -> datetime | None:
        """When the grace window lapses and the account is blocked, or ``None``
        when not subject to verification. Drives the banner copy + the SPA's
        full-screen gate. See `alkera_core.verification`."""
        from alkera_core.verification import deadline

        return deadline(self)


register_unique(EMAIL_INDEX, "email", EMAIL_TAKEN_MESSAGE)
