"""Public app-config endpoint — branding the SPA needs before login.

Unauthenticated and safe to cache: returns only non-secret branding (product
name, support email) sourced from settings, so a self-hosted deployment serves
its own brand from one image without a rebuild.
"""

from __future__ import annotations

from alkera_core.auth.tenancy import multi_org_enabled
from alkera_core.brand import product_name, sales_email, support_email
from alkera_core.config import settings
from alkera_core.schemas.system.config import PublicConfigResponse
from fastapi import APIRouter

from backend.auth.dependencies import DbSession
from backend.services.identity import sso as sso_service

router = APIRouter(prefix="/api/v1/config", tags=["config"])


@router.get("", response_model=PublicConfigResponse)
async def public_config(db: DbSession) -> PublicConfigResponse:
    """Non-secret branding for the pre-auth SPA (login / verification screens)."""
    return PublicConfigResponse(
        product_name=product_name(),
        support_email=support_email(),
        sales_email=sales_email(),
        # Self-hosted SPAs never load browser telemetry (the server gate is separate).
        telemetry_enabled=not settings.is_self_hosted,
        self_hosted=settings.is_self_hosted,
        # Self-hosted single-org SSO-only → the login screen redirects straight to the IdP.
        sso_enforced_login_url=await sso_service.enforced_login_url(db),
        workspaces_multi_chat=settings.workspaces_multi_chat,
        multi_org_enabled=multi_org_enabled(),
    )
