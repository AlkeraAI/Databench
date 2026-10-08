"""Session idle-timeout policy (``auth_idle_timeout_seconds``).

A browser SESSION unused past the window is rejected on its next request; any
activity slides the window; CLI tokens are exempt; and with the policy off the
session lives until the absolute TTL. Driven across time with freezegun.
"""

from __future__ import annotations

import pytest
from alkera_core.auth import encode_cli_token, register_token
from alkera_core.auth.revocation import _cache
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import TokenType
from freezegun import freeze_time
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login

pytestmark = pytest.mark.asyncio


async def test_idle_session_is_rejected_after_the_timeout(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 60)
    _cache.reset()
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        frozen.move_to("2026-06-01 12:02:00")  # +120s, past the 60s idle window
        assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_activity_slides_the_idle_window(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 60)
    _cache.reset()
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        # 40s gaps (< the 60s window); total span 160s >> 60s, but never idle 60s straight.
        for moment in ("12:00:40", "12:01:20", "12:02:00", "12:02:40"):
            frozen.move_to(f"2026-06-01 {moment}")
            assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_cli_token_is_exempt_from_idle_timeout(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 30)
    _cache.reset()
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        token, claims = encode_cli_token(
            user_id=org_admin.admin_id,
            email=org_admin.admin_email,
            org_team_id=org_admin.org_id,
            platform_role=None,
        )
        async with AsyncSessionLocal() as s:
            await register_token(s, claims=claims, token_type=TokenType.CLI)
            await s.commit()
        frozen.move_to("2026-06-01 12:10:00")  # +600s, far past the idle window
        r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200  # CLI tokens are intentionally exempt


async def test_disabled_policy_keeps_a_long_idle_session(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the idle policy off, only the absolute TTL can end a session — so the
    TTL is pinned here rather than read off the ambient dotenv.

    ``AUTH_TOKEN_TTL_SECONDS`` is a deployment's own number (30 minutes in
    ``.env.example``, a day in a long-lived dev ``.env``). Left ambient, the +6h
    idle jump below lands past the access token's expiry on one machine and
    inside it on another, and the 401 that follows says nothing about the idle
    policy this test is about."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "auth_token_ttl_seconds", 24 * 3600)
    _cache.reset()
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        frozen.move_to("2026-06-01 18:00:00")  # +6h idle, inside the pinned 1-day TTL
        assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_disabling_the_idle_policy_does_not_disable_the_absolute_ttl(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asymmetric half of the case above: turning the idle window off opts out
    of *idleness* as a reason to reject, not out of expiry. The same +6h jump on a
    30-minute access token is still a 401 — which is why the case above has to pin
    the TTL to mean anything."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "auth_token_ttl_seconds", 1800)
    _cache.reset()
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        await login(client, org_admin.admin_email, org_admin.admin_password)
        frozen.move_to("2026-06-01 18:00:00")  # +6h, far past the 30-minute TTL
        assert (await client.get("/api/v1/auth/me")).status_code == 401
