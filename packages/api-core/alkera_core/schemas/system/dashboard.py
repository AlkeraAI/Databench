"""Schemas for the consolidated user dashboard endpoint.

Single response shape so the SPA can poll `/api/v1/dashboard` and refresh
everything on the user's home view at once. Headroom fields (notifications,
announcements, recent activity, …) are added here as features land.
"""

from __future__ import annotations

from pydantic import BaseModel

from alkera_core.schemas.identity.invitation import InvitationRead
from alkera_core.schemas.identity.user import UserRead
from alkera_core.schemas.tenancy.team import TeamRead


class DashboardResponse(BaseModel):
    user: UserRead
    org: TeamRead
    teams: list[TeamRead]
    pending_invitations: list[InvitationRead]
    # True iff the caller is an Org Admin — drives admin-only UI (e.g. the org
    # settings page). Caller-specific, so it lives here, not on UserRead.
    is_org_admin: bool = False
    # Feature names this self-hosted install is entitled to ("byok", ...) —
    # drives the visibility of entitlement-gated admin surfaces. Always [] on
    # SaaS and on unentitled installs, so those two are indistinguishable.
    entitled_features: list[str] = []
    # Whether this org may use Enterprise-only surfaces (SSO, audit log): always
    # true self-hosted; on SaaS only for orgs on an active Enterprise plan. Drives
    # the SPA's Contact-Sales gate on those pages. Fail-closed default.
    enterprise_features_enabled: bool = False
    # Future fields (added without breaking older clients):
    # - notifications: list[NotificationRead]
    # - announcements: list[AnnouncementRead]
    # - active_chats: list[ChatSessionRead]
