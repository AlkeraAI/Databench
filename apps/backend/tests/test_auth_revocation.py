"""Server-side token revocation — integration tests against the real app + DB.

Covers logout (cookie + Bearer), logout-all, the session list/revoke endpoints,
ownership, the password-reset security event, the grace path for legacy
jti-less tokens, and cross-worker propagation via the DB-backed cache refresh.

The app runs in-process (ASGITransport), so it shares the module-level
revocation cache with the test — which is exactly how a real single process
behaves (write-through is immediate). The autouse fixture resets that cache
between tests for determinism.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import pytest_asyncio
from alkera_core.auth import (
    SessionClaims,
    TokenRevokedError,
    assert_token_active,
    decode_session_token,
    encode_session_token,
    register_token,
    revoke_jti,
)
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuthToken, TokenType, User
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login, make_member, mint_cli_token

_COOKIE = "alkera_session"


@pytest.fixture(autouse=True)
def _reset_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


def _bearer_client(token: str) -> AsyncClient:
    """A fresh, cookie-less client that authenticates only via Bearer — the
    realistic CLI/daemon shape."""
    return AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest_asyncio.fixture
async def fresh_client() -> AsyncIterator[AsyncClient]:
    async with app_client() as c:
        yield c


async def _mint_for(org: OrgWithAdmin) -> str:
    return await mint_cli_token(user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id)


async def _me_status(token: str) -> int:
    async with _bearer_client(token) as bc:
        return (await bc.get("/api/v1/auth/me")).status_code


@pytest.mark.asyncio
async def test_logout_revokes_bearer_token(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    token = await _mint_for(org_admin)
    async with _bearer_client(token) as bc:
        assert (await bc.get("/api/v1/auth/me")).status_code == 200
        assert (await bc.post("/api/v1/auth/logout")).status_code == 200
        # Same process → write-through → immediately rejected.
        assert (await bc.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_logout_is_idempotent_without_a_token(fresh_client: AsyncClient) -> None:
    # No cookie, no bearer — logout still succeeds (clears nothing, revokes nothing).
    assert (await fresh_client.post("/api/v1/auth/logout")).status_code == 200


@pytest.mark.asyncio
async def test_logout_clears_cookie_and_revokes_server_side(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    cookie_val = client.cookies.get(_COOKIE)
    assert cookie_val is not None
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    assert not client.cookies.get(_COOKIE)  # cleared client-side

    # Replaying the captured cookie still fails — proves server-side revocation,
    # not merely a cleared client jar.
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        cookies={_COOKIE: cookie_val},
    ) as replay:
        assert (await replay.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_logout_all_revokes_every_session(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    cookie_val = client.cookies.get(_COOKIE)
    assert cookie_val is not None
    token_b = await _mint_for(org_admin)
    token_c = await _mint_for(org_admin)
    assert await _me_status(token_b) == 200
    assert await _me_status(token_c) == 200

    assert (await client.post("/api/v1/auth/logout-all")).status_code == 200

    assert await _me_status(token_b) == 401
    assert await _me_status(token_c) == 401
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        cookies={_COOKIE: cookie_val},
    ) as replay:
        assert (await replay.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_sessions_list_and_individual_revoke(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    token_b = await _mint_for(org_admin)
    token_c = await _mint_for(org_admin)
    jti_b = decode_session_token(token_b).jti
    jti_c = decode_session_token(token_c).jti

    resp = await client.get("/api/v1/auth/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    jtis = {s["jti"] for s in sessions}
    assert {jti_b, jti_c} <= jtis
    assert any(s["current"] for s in sessions)  # the cookie session
    assert all("token" not in s for s in sessions)  # never leak token values

    assert (await client.delete(f"/api/v1/auth/sessions/{jti_b}")).status_code == 200
    assert await _me_status(token_b) == 401
    assert await _me_status(token_c) == 200  # the other session is untouched

    after = (await client.get("/api/v1/auth/sessions")).json()["sessions"]
    assert jti_b not in {s["jti"] for s in after}


@pytest.mark.asyncio
async def test_cannot_revoke_another_users_session(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    user2, pw2 = await make_member(real_session, org_id=org_admin.org_id)
    assert pw2 is not None
    await login(client, org_admin.admin_email, org_admin.admin_password)

    token2 = await mint_cli_token(
        user_id=user2.id, email=user2.email, org_team_id=user2.home_org_team_id
    )
    jti2 = decode_session_token(token2).jti

    # user1 can neither revoke nor probe user2's token.
    assert (await client.delete(f"/api/v1/auth/sessions/{jti2}")).status_code == 404
    assert await _me_status(token2) == 200


@pytest.mark.asyncio
async def test_password_reset_revokes_all_sessions(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_password_reset_send: list[dict],
    fresh_client: AsyncClient,
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    token_b = await _mint_for(org_admin)
    assert await _me_status(token_b) == 200

    assert (
        await client.post(
            "/api/v1/auth/password-reset/request", json={"email": org_admin.admin_email}
        )
    ).status_code == 200
    reset_token = monkeypatch_password_reset_send[-1]["token"]
    new_password = "new-pass-987654"
    assert (
        await client.post(
            f"/api/v1/auth/password-reset/{reset_token}", json={"password": new_password}
        )
    ).status_code == 200

    assert await _me_status(token_b) == 401  # prior session killed
    r = await fresh_client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": new_password},
    )
    assert r.status_code == 200  # new password works


@pytest.mark.asyncio
async def test_legacy_jti_less_token_still_authenticates(org_admin: OrgWithAdmin) -> None:
    """Grace rollout: a pre-revocation token (no jti) keeps working — only the
    token_epoch check applies, and it defaults to the unix epoch."""
    now = int(time.time())
    legacy = jwt.encode(
        {
            "sub": org_admin.admin_id.hex,
            "email": org_admin.admin_email,
            "org_team_id": org_admin.org_id.hex,
            "platform_role": None,
            "iat": now,
            "exp": now + 3600,
        },
        settings.effective_jwt_secret,
        algorithm=settings.auth_jwt_alg,
    )
    assert await _me_status(legacy) == 200


@pytest.mark.asyncio
async def test_db_revocation_propagates_via_cache_refresh(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """A revoke from another process (DB only, no in-process write-through) is
    picked up after the ≤5s cache refresh — simulated here with an explicit
    reset()."""
    from alkera_core.auth import revocation

    await login(client, org_admin.admin_email, org_admin.admin_password)
    token = await _mint_for(org_admin)
    jti = decode_session_token(token).jti
    assert await _me_status(token) == 200

    await real_session.execute(
        update(AuthToken).where(AuthToken.jti == jti).values(revoked_at=func.now())
    )
    await real_session.commit()
    revocation._cache.reset()  # force this process to reload from the DB

    assert await _me_status(token) == 401


@pytest.mark.asyncio
async def test_revocation_check_does_not_load_the_whole_registry(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The per-request check must cost the same no matter how many tokens the
    rest of the deployment has revoked.

    Materialising every revoked-but-unexpired jti made the auth hot path scale
    with a number any single account can inflate at will (mint a CLI token,
    revoke it, repeat — each row stays live for 90 days), in every backend and
    gateway process, on the caller's own DB connection."""
    from alkera_core.auth import revocation

    other, _pw = await make_member(real_session, org_id=org_admin.org_id)
    noise = [
        await mint_cli_token(
            user_id=other.id, email=other.email, org_team_id=other.home_org_team_id
        )
        for _ in range(5)
    ]
    noise_jtis = {decode_session_token(t).jti for t in noise}
    await real_session.execute(
        update(AuthToken)
        .where(AuthToken.user_id == other.id, AuthToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    await real_session.commit()

    revocation._cache.reset()
    mine = await _mint_for(org_admin)
    assert await _me_status(mine) == 200

    # Only the jti actually presented was resolved — the others were never read
    # into this process.
    cached = set(revocation._cache._revoked) | set(revocation._cache._live)
    assert cached.isdisjoint(noise_jtis)

    # …and each of those revoked tokens is still refused when it IS presented.
    assert await _me_status(noise[0]) == 401


async def _run_prune() -> None:
    """Run the nightly housekeeping task's core on its own session."""
    from alkera_core.auth import prune_expired

    async with AsyncSessionLocal() as s:
        await prune_expired(s)
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoke_via",
    [
        pytest.param("logout", id="logout"),
        pytest.param("session-delete", id="revoke-one-session"),
    ],
)
async def test_a_revoked_token_is_still_refused_after_the_prune_task_runs(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    revoke_via: str,
) -> None:
    """Housekeeping must never delete the row that IS the revocation.

    Enforcement is "does a row for this jti carry `revoked_at`" — and neither
    single-token lever (`POST /auth/logout`, `DELETE /auth/sessions/{jti}`, the
    two a user reaches for when a token is STOLEN) bumps `token_epoch`. So if the
    nightly prune drops a revoked-but-unexpired row, that exact stolen token
    authenticates again for the rest of its natural life — up to 90 days for a
    CLI token.
    """
    from alkera_core.auth import revocation

    await login(client, org_admin.admin_email, org_admin.admin_password)
    token = await _mint_for(org_admin)
    jti = decode_session_token(token).jti
    assert await _me_status(token) == 200

    if revoke_via == "logout":
        async with _bearer_client(token) as bc:
            assert (await bc.post("/api/v1/auth/logout")).status_code == 200
    else:
        assert (await client.delete(f"/api/v1/auth/sessions/{jti}")).status_code == 200
    assert await _me_status(token) == 401

    # Age the revocation well past any plausible retention tail, then prune.
    await real_session.execute(
        update(AuthToken)
        .where(AuthToken.jti == jti)
        .values(revoked_at=datetime.now(UTC) - timedelta(days=30))
    )
    await real_session.commit()
    await _run_prune()

    # A freshly-started process has no write-through memory of the revoke, so
    # the DB row is the only thing between the stolen token and a live session.
    revocation._cache.reset()
    assert await _me_status(token) == 401


@pytest.mark.asyncio
async def test_prune_removes_expired_rows_and_keeps_every_live_one(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    """The boundary the prune is actually allowed to act on: natural expiry.

    An expired row is spent (the JWT itself no longer verifies), so it goes; a
    revoked row inside its TTL and an untouched live row both stay.
    """
    from sqlalchemy import select

    expired = await _mint_for(org_admin)
    revoked = await _mint_for(org_admin)
    live = await _mint_for(org_admin)
    expired_jti = decode_session_token(expired).jti
    revoked_jti = decode_session_token(revoked).jti
    live_jti = decode_session_token(live).jti

    await real_session.execute(
        update(AuthToken)
        .where(AuthToken.jti == expired_jti)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await real_session.execute(
        update(AuthToken)
        .where(AuthToken.jti == revoked_jti)
        .values(revoked_at=datetime.now(UTC) - timedelta(days=30))
    )
    await real_session.commit()
    await _run_prune()

    async with AsyncSessionLocal() as s:
        remaining = set(
            (
                await s.execute(
                    select(AuthToken.jti).where(
                        AuthToken.jti.in_([expired_jti, revoked_jti, live_jti])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert expired_jti not in remaining  # past its natural expiry → spent
    assert revoked_jti in remaining  # revoked but still within its TTL → kept
    assert live_jti in remaining  # untouched


# ---------------------------------------------------------------------------
# Re-checking a long-lived connection without counting it as activity
# ---------------------------------------------------------------------------


async def _registered_session_claims(org: OrgWithAdmin) -> SessionClaims:
    _, claims = encode_session_token(
        user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id, platform_role=None
    )
    async with AsyncSessionLocal() as s:
        await register_token(s, claims=claims, token_type=TokenType.SESSION)
        await s.commit()
    return claims


async def _last_used_at(jti: str) -> datetime | None:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(select(AuthToken.last_used_at).where(AuthToken.jti == jti))
        ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slide_idle",
    [
        pytest.param(True, id="request-slides-the-window"),
        pytest.param(False, id="recheck-does-not"),
    ],
)
async def test_a_recheck_without_slide_still_times_out_but_is_not_activity(
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    slide_idle: bool,
) -> None:
    """An open event stream or socket re-checks its session on a timer. That
    check must still enforce the idle window, but must not extend it: the same
    timeline passes when the first check was a real request and fails when it
    was only a recheck."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 60)
    with freeze_time("2026-06-01 12:00:00", real_asyncio=True) as frozen:
        claims = await _registered_session_claims(org_admin)
        assert claims.jti is not None
        user = await real_session.get(User, org_admin.admin_id)
        assert user is not None

        frozen.move_to("2026-06-01 12:00:45")  # 45 s idle: inside the window either way
        await assert_token_active(real_session, claims, user, slide_idle=slide_idle)
        await real_session.commit()
        touched = await _last_used_at(claims.jti)
        if slide_idle:
            assert touched == datetime(2026, 6, 1, 12, 0, 45, tzinfo=UTC)
        else:
            assert touched is None

        frozen.move_to("2026-06-01 12:01:30")  # 90 s since mint, 45 s since the check
        if slide_idle:
            await assert_token_active(real_session, claims, user, slide_idle=True)
        else:
            with pytest.raises(TokenRevokedError, match="idle timeout"):
                await assert_token_active(real_session, claims, user, slide_idle=False)


@pytest.mark.asyncio
async def test_a_recheck_without_slide_still_refuses_a_revoked_token(
    org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    claims = await _registered_session_claims(org_admin)
    assert claims.jti is not None
    user = await real_session.get(User, org_admin.admin_id)
    assert user is not None
    await assert_token_active(real_session, claims, user, slide_idle=False)
    await revoke_jti(real_session, claims.jti)
    await real_session.commit()
    with pytest.raises(TokenRevokedError, match="token revoked"):
        await assert_token_active(real_session, claims, user, slide_idle=False)


@pytest.mark.asyncio
async def test_concurrent_requests_of_one_session_never_queue_on_its_slide(
    org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request of one session decides to slide its idle window at the
    same moment, and the write lasts to the end of the request that made it.
    A second request arriving meanwhile is let through without waiting for
    that request to finish: the slide was a plain UPDATE, and every request
    of a busy session queued on its own token row."""
    monkeypatch.setattr(settings, "auth_idle_timeout_seconds", 3600)
    claims = await _registered_session_claims(org_admin)
    async with AsyncSessionLocal() as first, AsyncSessionLocal() as second:
        first_user = await first.get(User, org_admin.admin_id)
        second_user = await second.get(User, org_admin.admin_id)
        assert first_user is not None and second_user is not None
        await assert_token_active(first, claims, first_user, slide_idle=True)
        await second.execute(text("SET LOCAL lock_timeout = '300ms'"))
        await assert_token_active(second, claims, second_user, slide_idle=True)
        await second.rollback()
        await first.commit()
    assert claims.jti is not None
    assert await _last_used_at(claims.jti) is not None
