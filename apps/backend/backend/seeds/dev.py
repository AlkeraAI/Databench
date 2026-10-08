"""Dev admin seed.

Creates a root team (``AUTH_DEV_ORG_NAME``) and an admin user with platform_role =
ALKERA_ADMIN. Runs in `APP_ENV` local or staging (so per-PR previews get a
login like local dev) — never in production.

Idempotent: query by email; if the dev admin user exists, do nothing.
"""

from __future__ import annotations

from alkera_core.config import settings
from alkera_core.models import PlatformRole
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.identity import email_verification as email_verification_service
from backend.services.identity import users as user_service
from backend.services.org import teams as team_service


async def seed_dev_admin(session: AsyncSession) -> str:
    if not (settings.is_local or settings.is_staging):
        return "skipped: APP_ENV is not 'local' or 'staging'"

    existing = await user_service.get_by_email(session, settings.auth_dev_admin_email)
    if existing is not None:
        # Sanity-check: ensure they still have ALKERA_ADMIN if someone
        # accidentally revoked it; otherwise leave alone.
        if existing.platform_role is not PlatformRole.ALKERA_ADMIN:
            await user_service.set_platform_role(session, existing, PlatformRole.ALKERA_ADMIN)
            return f"updated: {existing.email} → platform_role=alkera_admin"
        return f"unchanged: {existing.email} already exists"

    org, admin = await team_service.create_org_with_admin(
        session,
        org_name=settings.auth_dev_org_name,
        admin_email=settings.auth_dev_admin_email,
        admin_first_name=settings.auth_dev_admin_first_name,
        admin_last_name=settings.auth_dev_admin_last_name,
        admin_password=settings.auth_dev_admin_password,
        admin_platform_role=PlatformRole.ALKERA_ADMIN,
    )
    # Dev admin has no real inbox to receive a verification email; mark verified.
    await email_verification_service.mark_verified(session, admin)
    return (
        f"created: org={org.name!r} (id={org.id}); admin={admin.email} (platform_role=alkera_admin)"
    )
