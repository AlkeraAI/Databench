"""End-to-end graceful rotation of the auth secrets, through the real request
path + real Postgres.

- A live session cookie keeps authenticating after AUTH_JWT_SECRET rotates (its
  signer moved to AUTH_JWT_SECRET_PREVIOUS), and stops once the retired secret is
  dropped.
- An outstanding password-reset row (its digest written under the OLD pepper) is
  still resolved after TOKEN_HASH_PEPPER rotates, exercising the real
  ``token_hash IN (candidate digests)`` SQL — and is stranded once the retired
  pepper is dropped.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.auth.token_hash import _digest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from backend.services.identity import password_reset as password_reset_service
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


async def test_session_cookie_survives_jwt_secret_rotation(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    # Rotate: the cookie was signed under the (currently active) secret, which now
    # becomes the PREVIOUS one; a fresh secret takes over as active.
    signer = settings.effective_jwt_secret
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", signer)
    monkeypatch.setattr(settings, "auth_jwt_secret", "rotated-" + uuid.uuid4().hex)
    assert (await client.get("/api/v1/auth/me")).status_code == 200  # still authenticated

    # Retire the old secret entirely → the pre-rotation cookie is now refused.
    monkeypatch.setattr(settings, "auth_jwt_secret_previous", "")
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_outstanding_reset_link_survives_pepper_rotation(
    org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "reset-" + uuid.uuid4().hex
    async with AsyncSessionLocal() as s:
        member, _ = await make_member(
            s, org_id=org_admin.org_id, email=f"u-{uuid.uuid4().hex[:8]}@m.example", verified=True
        )
        # Simulate a link that was issued (and stored) under the OLD pepper.
        member.password_reset_token = _digest("pepper-old", raw)
        member_id = member.id
        await s.commit()

    monkeypatch.setattr(settings, "token_hash_pepper", "pepper-new")
    monkeypatch.setattr(settings, "token_hash_pepper_previous", "pepper-old")
    async with AsyncSessionLocal() as s:
        found = await password_reset_service.get_by_token(s, raw)
    assert found is not None and found.id == member_id  # resolved across the rotation

    # Drop the retired pepper → the old digest can no longer be matched.
    monkeypatch.setattr(settings, "token_hash_pepper_previous", "")
    async with AsyncSessionLocal() as s:
        assert await password_reset_service.get_by_token(s, raw) is None
