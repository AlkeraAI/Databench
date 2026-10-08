"""Public (unauthenticated) app configuration schema.

Served by `GET /api/v1/config` and consumed by the SPA before login (the brand
shown on the login / verification screens, the support link). Holds only
non-secret, safe-to-expose branding so a self-hosted deployment can run under its
own identity without a rebuild.
"""

from __future__ import annotations

from pydantic import BaseModel


class PublicConfigResponse(BaseModel):
    """Branding the SPA needs before a user is authenticated."""

    product_name: str
    #: Null when the deployment names no support inbox.
    support_email: str | None
    #: Null when the deployment names no sales contact.
    sales_email: str | None = None
    # Whether browser telemetry (Sentry) may initialize. False on a self-hosted /
    # on-prem deployment — the SPA refuses to phone home even if a DSN was baked into
    # the image. The server components are gated independently (observability/sentry.py).
    telemetry_enabled: bool
    # Whether this is a customer self-hosted / on-prem deployment (vs Alkera SaaS).
    # Drives self-hosted-only SPA surfaces (the org-admin Deployment page). Default
    # False so the nav stays hidden until the config loads (fail-closed on SaaS).
    self_hosted: bool = False
    # When set, the login screen redirects straight to this SSO start URL (a
    # self-hosted instance whose single org has enforced SSO-only). None otherwise —
    # the login screen shows its normal form. A break-glass query param lets a
    # password-holding admin bypass the redirect (so a broken IdP can't lock everyone out).
    sso_enforced_login_url: str | None = None
    # Whether a workspace may hold several chats (``WORKSPACES_MULTI_CHAT``). The
    # SPA offers "New workspace" and groups chats under workspaces only when it is;
    # off, every chat is a workspace of its own and the rail lists chats as before.
    workspaces_multi_chat: bool = False
    # Whether one sign-in may belong to several orgs here (``MULTI_ORG_ENABLED``).
    # The SPA offers creating, leaving and joining another org only when true.
    # Default False so those doors stay hidden until the config loads.
    multi_org_enabled: bool = False
