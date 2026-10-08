"""Tests for the first-admin bootstrap (`backend.seeds.bootstrap_admin`).

The safety invariant — refuse to do anything once any user exists — is the whole
point, so it's tested against the REAL count probe. The create branch is forced
via the `_existing_user_count` seam so it's deterministic even on the shared,
never-empty dev DB. Both tests roll back, persisting nothing.
"""

from __future__ import annotations

import secrets

import pytest
from alkera_core.models import PlatformRole
from backend.seeds import bootstrap_admin as bootstrap_mod
from backend.seeds.bootstrap_admin import bootstrap_first_admin
from backend.services.identity import users as user_service
from backend.services.org import teams as team_service
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_bootstrap_skips_on_initialized_db(real_session: AsyncSession) -> None:
    # Make the DB non-empty from this session's view (true even on a fresh CI DB):
    # create_org_with_admin flushes, so the count probe sees the pending user.
    await team_service.create_org_with_admin(
        real_session,
        org_name=f"Existing Org {secrets.token_hex(4)}",
        admin_email=f"existing-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Existing",
        admin_last_name="User",
        admin_password=secrets.token_urlsafe(16),
    )

    boot_email = f"boot-{secrets.token_hex(6)}@alkera.dev"
    summary = await bootstrap_first_admin(
        real_session,
        org_name="Bootstrap Co",
        admin_email=boot_email,
        admin_first_name="Boot",
        admin_last_name="Strap",
        admin_password=secrets.token_urlsafe(16),
    )

    assert summary.startswith("skipped")
    assert "already initialized" in summary
    # The guard must have prevented any creation — the bootstrap admin doesn't exist.
    assert await user_service.get_by_email(real_session, boot_email) is None

    await real_session.rollback()


@pytest.mark.asyncio
async def test_bootstrap_creates_alkera_admin_on_empty_db(
    real_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _zero(_session: AsyncSession) -> int:
        return 0

    monkeypatch.setattr(bootstrap_mod, "_existing_user_count", _zero)

    email = f"firstadmin-{secrets.token_hex(6)}@alkera.dev"
    summary = await bootstrap_first_admin(
        real_session,
        org_name=f"Acme {secrets.token_hex(4)}",
        admin_email=email,
        admin_first_name="Acme",
        admin_last_name="Admin",
        admin_password=secrets.token_urlsafe(16),
    )

    assert summary.startswith("created")
    admin = await user_service.get_by_email(real_session, email)
    assert admin is not None
    # The first admin is a platform admin AND pre-verified (no inbox round-trip).
    assert admin.platform_role is PlatformRole.ALKERA_ADMIN
    assert admin.email_verified_at is not None

    await real_session.rollback()
