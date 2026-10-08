"""Tenant-layer role enums.

Used by both ORM models (column types) and Pydantic schemas. `StrEnum`
serializes naturally to JSON and compares against string literals.
"""

from __future__ import annotations

from enum import StrEnum


class TeamRole(StrEnum):
    """Role of a user within a specific team (per `team_memberships`).

    `ADMIN` of the org's root team is what the spec calls 'Org Admin';
    `ADMIN` of any non-root team is 'Team Admin'.
    """

    MEMBER = "member"
    ADMIN = "admin"

    @property
    def display_name(self) -> str:
        """Human-friendly label for UI surfaces. Returned by the API alongside
        the raw value so clients don't hard-code their own mapping."""
        return _TEAM_ROLE_LABELS[self]


_TEAM_ROLE_LABELS: dict[TeamRole, str] = {
    TeamRole.MEMBER: "Member",
    TeamRole.ADMIN: "Admin",
}


class PlatformRole(StrEnum):
    """Cross-tenant platform staff role. Nullable on `users` — most users
    have no platform role at all."""

    ALKERA_SUPPORT = "alkera_support"
    ALKERA_ADMIN = "alkera_admin"

    @property
    def display_name(self) -> str:
        """Human-friendly label (e.g. 'Platform support'). Returned by the API."""
        return _PLATFORM_ROLE_LABELS[self]


_PLATFORM_ROLE_LABELS: dict[PlatformRole, str] = {
    PlatformRole.ALKERA_SUPPORT: "Platform support",
    PlatformRole.ALKERA_ADMIN: "Platform admin",
}


class InvitationStatus(StrEnum):
    """Lifecycle of an `invitations` row.

    `pending` is the only status with effect; others are terminal and exist
    for audit. `expired` may be marked lazily on read.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


class MembershipStatus(StrEnum):
    """Lifecycle of an `org_memberships` row: the tenancy binding of one
    identity to one org.

    Only `active` admits a credential into the org. `deactivated` keeps the row
    (and its history) while every credential bound to it is refused. `pending`
    is a seat an org's IdP provisioned for an identity that already exists
    elsewhere: it grants nothing until the person joins it themselves.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    PENDING = "pending"


class TokenType(StrEnum):
    """Kind of auth token recorded in `auth_tokens`.

    `session` = short-lived cookie JWT (SPA); `cli` = long-lived Bearer JWT
    (the `alkera` CLI + daemon). Same JWT shape; differ only in TTL + origin.
    """

    SESSION = "session"
    CLI = "cli"


class DeviceAuthStatus(StrEnum):
    """Lifecycle of a `device_authorizations` row (RFC 8628 device grant).

    `pending` is the only pollable-but-unredeemable state; `approved` is
    redeemable exactly once (then becomes `consumed`); `denied`/`consumed`/
    `expired` are terminal. Transitions are strictly forward — no re-approve,
    no approve-after-deny, no redeem-after-consume.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"
