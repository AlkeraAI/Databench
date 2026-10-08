"""A credential minted before memberships existed ends with the person's
membership in their home org, and stays ended when they join it again.

Such a token names no membership and no epoch; the door holds it to the
legacy epoch, and the registry row it left names no membership either. Two
things keep it dead after a leave and a rejoin: the registry row is revoked
with the home membership, and a membership created since starts past the
legacy epoch, so the rejoined membership is not one the old token matches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import pytest
from alkera_core.auth import encode_cli_token, revocation
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuthToken, MembershipStatus, Team, TeamRole, TokenType, User
from backend.auth.session_issue import issue_session
from backend.services.org import invitations as invitation_service
from backend.services.org import org_memberships as org_membership_service
from fastapi import Request, Response
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import TwoOrg, app_client, mint_cli_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    return body["error"] if "error" in body else body["detail"]


@asynccontextmanager
async def _browser(user_id: UUID, org_id: UUID) -> AsyncIterator[AsyncClient]:
    """A browser signed in by password into ``org_id``, with both cookies."""
    carrier = Response()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "query_string": b"",
        }
    )
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        await issue_session(
            db, user, request=request, response=carrier, method="password", org_team_id=org_id
        )
        await db.commit()
    async with app_client() as client:
        client.cookies.extract_cookies(
            httpx.Response(
                200,
                headers=list(carrier.raw_headers),
                request=httpx.Request("POST", f"{client.base_url}/api/v1/auth/login"),
            )
        )
        yield client


def _unregistered_legacy_token(user: User, org_id: UUID) -> str:
    """A token from before memberships that no registry row records: only the
    membership's epoch can refuse it."""
    token, claims = encode_cli_token(
        user_id=user.id, email=user.email, org_team_id=org_id, platform_role=None
    )
    assert claims.membership_id is None and claims.membership_epoch is None
    return token


async def _registry_row_revoked(token: str) -> bool:
    from alkera_core.auth import decode_session_token

    jti = decode_session_token(token).jti
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(AuthToken).where(AuthToken.jti == jti))).scalar_one()
    return row.revoked_at is not None


async def _leave_and_rejoin_home(t: TwoOrg) -> None:
    """The person leaves their home org A from a browser, A's admin invites
    them back, and they accept from their other org."""
    async with _browser(t.user.id, t.org_a) as browser:
        left = await browser.post("/api/v1/orgs/current/leave")
        assert left.status_code == 200, left.text
    assert await _membership(t.user.id, t.org_a) is None

    async with AsyncSessionLocal() as s:
        inviter = await s.get(User, t.admin_a.id)
        root = await s.get(Team, t.org_a)
        assert inviter is not None and root is not None
        _invitation, auto, raw = await invitation_service.create_invitation(
            s,
            team=root,
            email=t.user.email,
            role=TeamRole.MEMBER,
            invited_by=inviter,
            org_team_id=t.org_a,
        )
        assert not auto
        await s.commit()
    async with _browser(t.user.id, t.org_b) as browser:
        accepted = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert accepted.status_code == 200, accepted.text
    rejoined = await _membership(t.user.id, t.org_a)
    assert rejoined is not None and rejoined.status is MembershipStatus.ACTIVE


async def _membership(user_id: UUID, org_id: UUID) -> Any:
    async with AsyncSessionLocal() as s:
        return await org_membership_service.get(s, user_id=user_id, org_team_id=org_id)


@pytest.mark.usefixtures("multi_org")
async def test_a_legacy_token_stays_dead_after_leaving_and_rejoining_the_home_org(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    registered = await mint_cli_token(user_id=t.user.id, email=t.user.email, org_team_id=t.org_a)
    unregistered = _unregistered_legacy_token(t.user, t.org_a)
    async with app_client() as cli:
        for token in (registered, unregistered):
            before = await cli.get("/api/v1/auth/me", headers=_bearer(token))
            assert before.status_code == 200, before.text

    await _leave_and_rejoin_home(t)

    async with app_client() as cli:
        for token in (registered, unregistered):
            after = await cli.get("/api/v1/auth/me", headers=_bearer(token))
            assert after.status_code == 401, after.text
        # The epoch is what refuses a token no registry row records.
        unrecorded = await cli.get("/api/v1/auth/me", headers=_bearer(unregistered))
        assert _error(unrecorded)["code"] == "session_org_revoked"
        # A credential minted for the new membership works.
        fresh_token = await _membership_token(t.user.id, t.org_a)
        fresh = await cli.get("/api/v1/auth/me", headers=_bearer(fresh_token))
        assert fresh.status_code == 200, fresh.text
    assert await _registry_row_revoked(registered)


async def _membership_token(user_id: UUID, org_id: UUID) -> str:
    from alkera_core.auth import register_token
    from backend.auth.membership_tokens import mint_for_membership

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "multi_org_enabled", True)
        async with AsyncSessionLocal() as s:
            user = await s.get(User, user_id)
            assert user is not None
            token, claims = await mint_for_membership(s, user, org_id, kind="cli")
            await register_token(s, claims=claims, token_type=TokenType.CLI)
            await s.commit()
    return token


async def test_a_new_membership_starts_past_the_legacy_epoch(two_org_identity: TwoOrg) -> None:
    """A membership created by the service (the fixture's second org) is not
    one a token from before memberships can stand on."""
    t = two_org_identity
    async with app_client() as cli:
        legacy_into_b = _unregistered_legacy_token(t.user, t.org_b)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "multi_org_enabled", True)
            resp = await cli.get("/api/v1/auth/me", headers=_bearer(legacy_into_b))
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == "session_org_revoked"


@pytest.mark.parametrize(
    ("revoked_org", "legacy_revoked"),
    [
        pytest.param("home", True, id="home-org-ends-the-legacy-row"),
        pytest.param("other", False, id="another-org-leaves-it"),
    ],
)
async def test_revoking_a_membership_revokes_legacy_registry_rows_only_in_the_home_org(
    two_org_identity: TwoOrg, revoked_org: str, legacy_revoked: bool
) -> None:
    t = two_org_identity
    legacy = await mint_cli_token(user_id=t.user.id, email=t.user.email, org_team_id=t.org_a)
    stranger_legacy = await mint_cli_token(
        user_id=t.admin_a.id, email=t.admin_a.email, org_team_id=t.org_a
    )
    org = t.org_a if revoked_org == "home" else t.org_b
    async with AsyncSessionLocal() as s:
        membership = await org_membership_service.get(s, user_id=t.user.id, org_team_id=org)
        assert membership is not None
        await revocation.revoke_membership(s, membership, reason="removed")
        await s.commit()
    assert await _registry_row_revoked(legacy) is legacy_revoked
    # Somebody else's legacy row in the same org is never touched.
    assert await _registry_row_revoked(stranger_legacy) is False
    # The other org's bound token is that org's, and only revoked with it.
    assert await _registry_row_revoked(t.token_b) is (revoked_org == "other")
