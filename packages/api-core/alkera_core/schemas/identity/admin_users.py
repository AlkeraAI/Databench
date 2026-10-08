"""Admin console user-register shapes (cross-tenant, staff-only).

A purpose-built row instead of the shared ``UserRead``: the register carries
abuse-forensics fields (spend, IPs, disposable-domain flag) that would tax and
leak on the ``/auth/me`` shape every login reads.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from alkera_core.models import PlatformRole


class AdminUserRow(BaseModel):
    id: UUID
    email: str
    display_name: str
    org_team_id: UUID
    org_name: str | None = None
    platform_role: PlatformRole | None = None
    is_active: bool
    created_at: datetime
    email_verified_at: datetime | None = None
    #: The address's domain is a known disposable-mail provider.
    disposable_email: bool = False
    signup_ip: str | None = None
    last_login_ip: str | None = None
    #: Settled spend + request count for the current UTC calendar month.
    mtd_billed_nanos: int = 0
    mtd_request_count: int = 0
    #: Shut out by an active ban — on the account itself, or on the domain of
    #: its email. ``ban_reason`` is that ban's reason (the domain ban's when
    #: the account is covered by one); ``None`` when not banned.
    banned: bool = False
    ban_reason: str | None = None


class IpInfo(BaseModel):
    """Best-effort geo/network context for one IP (external lookup; ``error``
    set — and the rest null — when the lookup could not run)."""

    ip: str
    city: str | None = None
    region: str | None = None
    country: str | None = None
    #: Network operator (ASN organization) — VPS/hosting providers here are a
    #: strong bot signal; residential ISPs read as ordinary users.
    network: str | None = None
    error: str | None = None


class AdminUserIpInfoRead(BaseModel):
    signup: IpInfo | None = None
    last_login: IpInfo | None = None
