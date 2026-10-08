"""Device Authorization Grant (RFC 8628) — end-to-end against the real app + DB.

Exercises the full state machine through the HTTP surface: the device/code +
device/token CLI endpoints (form-encoded, flat RFC error shapes) and the SPA
consent endpoints (cookie-authed JSON). Time-dependent paths (expiry, slow_down,
single-use replay) are driven with freezegun across the relevant boundary.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator

import pytest
from alkera_core.auth import decode_session_token
from alkera_core.config import settings
from freezegun import freeze_time
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member, mint_cli_token

_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_CODE_URL = "/api/v1/auth/device/code"
_TOKEN_URL = "/api/v1/auth/device/token"


@pytest.fixture(autouse=True)
def _reset_device_limiter() -> Iterator[None]:
    from backend.services.identity.device_authorization import _user_code_limiter

    _user_code_limiter.reset()
    yield
    _user_code_limiter.reset()


async def _request_code(
    client: AsyncClient, *, client_id: str = "alkera-cli", scope: str = "cli"
) -> dict:
    resp = await client.post(_CODE_URL, data={"client_id": client_id, "scope": scope})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _poll(client: AsyncClient, device_code: str, *, client_id: str = "alkera-cli"):
    return await client.post(
        _TOKEN_URL,
        data={"grant_type": _GRANT, "device_code": device_code, "client_id": client_id},
    )


# --- device/code (CLI-facing) ----------------------------------------------


@pytest.mark.asyncio
async def test_device_code_returns_rfc_fields(client: AsyncClient) -> None:
    resp = await client.post(_CODE_URL, data={"client_id": "alkera-cli", "scope": "cli"})
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert set(body) == {
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    }
    assert body["interval"] == settings.auth_device_poll_interval_seconds == 1
    assert body["expires_in"] == settings.auth_device_code_ttl_seconds
    assert body["verification_uri"] == f"{settings.frontend_base_url.rstrip('/')}/device"
    assert body["verification_uri_complete"].endswith(f"user_code={body['user_code']}")
    # WXYZ-1234 shape from the ambiguity-free alphabet (no 0/O/1/I).
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", body["user_code"])
    assert len(body["device_code"]) > 32  # high-entropy secret, not the short code


# --- device/token state machine (CLI-facing, flat RFC errors) --------------


def _assert_flat_rfc_error(resp, error: str) -> None:
    """The CLI endpoints must NOT use the house {error:{code,message}} envelope."""
    assert resp.status_code == 400
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"error": error}


@pytest.mark.asyncio
async def test_token_pending_returns_authorization_pending(client: AsyncClient) -> None:
    code = await _request_code(client)
    _assert_flat_rfc_error(await _poll(client, code["device_code"]), "authorization_pending")


@pytest.mark.asyncio
async def test_unsupported_grant_type(client: AsyncClient) -> None:
    code = await _request_code(client)
    resp = await client.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "device_code": code["device_code"],
            "client_id": "alkera-cli",
        },
    )
    _assert_flat_rfc_error(resp, "unsupported_grant_type")


@pytest.mark.asyncio
async def test_unknown_device_code_is_invalid_grant(client: AsyncClient) -> None:
    _assert_flat_rfc_error(await _poll(client, "not-a-real-device-code"), "invalid_grant")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_id",
    [
        pytest.param("alkera-cli", id="cli"),
        pytest.param("alkera-vscode", id="vscode"),
    ],
)
async def test_shipped_clients_are_accepted(client: AsyncClient, client_id: str) -> None:
    resp = await client.post(_CODE_URL, data={"client_id": client_id, "scope": "cli"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        pytest.param("evil-client", "invalid_client", id="arbitrary"),
        pytest.param("alkera-cli ", "invalid_client", id="trailing-space"),
        pytest.param("Alkera-CLI", "invalid_client", id="wrong-case"),
        # An empty form value reads as an omitted field, so it lands on the
        # RFC's missing-parameter error — likewise a refusal, never a code.
        pytest.param("", "invalid_request", id="empty"),
    ],
)
async def test_unknown_client_id_is_refused(
    client: AsyncClient, client_id: str, expected: str
) -> None:
    """This endpoint is unauthenticated and the client_id is the only thing that
    names the requester on the consent screen. An unrecognized id must be refused
    rather than rendered under the generic label, so nobody can drive a real,
    approvable flow while calling themselves whatever they like."""
    resp = await client.post(_CODE_URL, data={"client_id": client_id, "scope": "cli"})
    _assert_flat_rfc_error(resp, expected)


@pytest.mark.asyncio
async def test_missing_form_field_is_invalid_request(client: AsyncClient) -> None:
    # An omitted required field returns the FLAT RFC `invalid_request`, never the
    # house-envelope 422 a spec-strict client couldn't parse.
    _assert_flat_rfc_error(await client.post(_TOKEN_URL, data={}), "invalid_request")
    _assert_flat_rfc_error(await client.post(_CODE_URL, data={}), "invalid_request")


@pytest.mark.asyncio
async def test_client_id_mismatch_is_invalid_grant(client: AsyncClient) -> None:
    code = await _request_code(client, client_id="alkera-cli")
    _assert_flat_rfc_error(
        await _poll(client, code["device_code"], client_id="alkera-vscode"), "invalid_grant"
    )


@pytest.mark.asyncio
async def test_full_approval_flow(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    code = await _request_code(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    info = await client.get("/api/v1/auth/device/info", params={"user_code": code["user_code"]})
    assert info.status_code == 200
    assert info.json()["client_name"] == "Alkera CLI"
    assert info.json()["user_code"] == code["user_code"]

    approve = await client.post(
        "/api/v1/auth/device/approve", json={"user_code": code["user_code"]}
    )
    assert approve.status_code == 200

    token_resp = await _poll(client, code["device_code"])
    assert token_resp.status_code == 200
    assert token_resp.headers["cache-control"] == "no-store"
    body = token_resp.json()
    assert body["token_type"] == "Bearer"
    claims = decode_session_token(body["access_token"])
    assert claims.user_id == org_admin.admin_id
    # 90-day CLI TTL, echoed as expires_in.
    assert 60 * 60 * 24 * 80 < body["expires_in"] < 60 * 60 * 24 * 100
    assert claims.expires_at - claims.issued_at == body["expires_in"]


@pytest.mark.asyncio
async def test_device_login_is_audited(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """The device grant never passes /auth/login, so redemption must write its
    own org audit row. Without it, extension logins are invisible to the org."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import OrgAuditEvent
    from sqlalchemy import select

    code = await _request_code(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.post("/api/v1/auth/device/approve", json={"user_code": code["user_code"]})
    token_resp = await _poll(client, code["device_code"])
    assert token_resp.status_code == 200

    async with AsyncSessionLocal() as s:
        rows = (
            (
                await s.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org_admin.org_id,
                        OrgAuditEvent.action == "auth.device_login",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].actor_email == org_admin.admin_email
    assert rows[0].target == org_admin.admin_email
    assert rows[0].detail["client_id"] == "alkera-cli"


@pytest.mark.asyncio
async def test_vscode_client_name_on_consent(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    code = await _request_code(client, client_id="alkera-vscode")
    await login(client, org_admin.admin_email, org_admin.admin_password)
    info = await client.get("/api/v1/auth/device/info", params={"user_code": code["user_code"]})
    assert info.json()["client_name"] == "Alkera for VS Code"


@pytest.mark.asyncio
async def test_deny_returns_access_denied(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    code = await _request_code(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    deny = await client.post("/api/v1/auth/device/deny", json={"user_code": code["user_code"]})
    assert deny.status_code == 200
    _assert_flat_rfc_error(await _poll(client, code["device_code"]), "access_denied")


@pytest.mark.asyncio
async def test_expiry_returns_expired_token(client: AsyncClient) -> None:
    with freeze_time("2026-06-20T12:00:00+00:00", real_asyncio=True) as frozen:
        code = await _request_code(client)
        # Past the 10-minute TTL.
        frozen.move_to("2026-06-20T12:11:00+00:00")
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "expired_token")


@pytest.mark.asyncio
async def test_slow_down_when_polling_too_fast(client: AsyncClient) -> None:
    with freeze_time("2026-06-20T12:00:00+00:00", real_asyncio=True) as frozen:
        code = await _request_code(client)
        # First poll establishes last_polled_at.
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "authorization_pending")
        # Second poll within the same frozen second → too fast.
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "slow_down")
        # After the interval elapses, polling is accepted again.
        frozen.move_to("2026-06-20T12:00:05+00:00")
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "authorization_pending")


@pytest.mark.asyncio
async def test_slow_down_boundary_exactly_interval_is_accepted(client: AsyncClient) -> None:
    # A poll exactly `interval` seconds after the previous is NOT too fast — the
    # throttle is a strict `<`, so the boundary must succeed (no off-by-one lockout).
    with freeze_time("2026-06-20T12:00:00+00:00", real_asyncio=True) as frozen:
        code = await _request_code(client)
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "authorization_pending")
        frozen.move_to("2026-06-20T12:00:01+00:00")  # exactly interval=1s later
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "authorization_pending")


@pytest.mark.asyncio
async def test_hard_poll_cap_force_expires(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "auth_device_max_poll_attempts", 2)
    with freeze_time("2026-06-20T12:00:00+00:00", real_asyncio=True) as frozen:
        code = await _request_code(client)
        # Two accepted polls (spaced past interval) sit at the cap, still pending.
        for i in (5, 10):
            frozen.move_to(f"2026-06-20T12:00:{i:02d}+00:00")
            _assert_flat_rfc_error(
                await _poll(client, code["device_code"]), "authorization_pending"
            )
        # The (cap+1)-th accepted poll force-expires the code.
        frozen.move_to("2026-06-20T12:00:15+00:00")
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "expired_token")


@pytest.mark.asyncio
async def test_concurrent_redeem_mints_exactly_one_token(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Single-use under concurrency: two simultaneous polls of one approved code
    must yield exactly one token + one invalid_grant (the FOR UPDATE row lock
    serializes them), never two valid tokens from one approval."""
    code = await _request_code(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await client.post("/api/v1/auth/device/approve", json={"user_code": code["user_code"]})

    r1, r2 = await asyncio.gather(
        _poll(client, code["device_code"]), _poll(client, code["device_code"])
    )
    statuses = sorted([r1.status_code, r2.status_code])
    # Exactly one token, never two: the row lock serializes the mint, and the
    # loser finds the code consumed, which is final at any polling pace.
    assert statuses == [200, 400], f"{r1.status_code}/{r1.text} {r2.status_code}/{r2.text}"
    ok = r1 if r1.status_code == 200 else r2
    bad = r2 if r1.status_code == 200 else r1
    assert "access_token" in ok.json()
    _assert_flat_rfc_error(bad, "invalid_grant")


@pytest.mark.asyncio
async def test_single_use_replay_rejected(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    with freeze_time("2026-06-20T12:00:00+00:00", real_asyncio=True) as frozen:
        code = await _request_code(client)
        await login(client, org_admin.admin_email, org_admin.admin_password)
        await client.post("/api/v1/auth/device/approve", json={"user_code": code["user_code"]})

        first = await _poll(client, code["device_code"])
        assert first.status_code == 200
        # Space the replay past the interval so it isn't masked by slow_down.
        frozen.move_to("2026-06-20T12:00:05+00:00")
        _assert_flat_rfc_error(await _poll(client, code["device_code"]), "invalid_grant")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("end", "error"),
    [
        pytest.param("consumed", "invalid_grant", id="consumed"),
        pytest.param("denied", "access_denied", id="denied"),
        pytest.param("expired", "expired_token", id="expired"),
    ],
)
async def test_a_code_that_has_ended_answers_its_end_at_any_polling_pace(
    client: AsyncClient, org_admin: OrgWithAdmin, end: str, error: str
) -> None:
    """RFC 8628 section 3.5: ``slow_down`` means "keep polling, slower". A code
    that is consumed, denied or expired never becomes redeemable again, so a
    poll inside the interval answers with the code's end, never ``slow_down``,
    and the client stops."""
    with freeze_time("2026-06-20T12:00:00+00:00", real_asyncio=True) as frozen:
        code = await _request_code(client)
        await login(client, org_admin.admin_email, org_admin.admin_password)
        if end == "consumed":
            await client.post("/api/v1/auth/device/approve", json={"user_code": code["user_code"]})
            assert (await _poll(client, code["device_code"])).status_code == 200
        elif end == "denied":
            await client.post("/api/v1/auth/device/deny", json={"user_code": code["user_code"]})
            _assert_flat_rfc_error(await _poll(client, code["device_code"]), "access_denied")
        else:
            _assert_flat_rfc_error(
                await _poll(client, code["device_code"]), "authorization_pending"
            )
            frozen.move_to("2026-06-20T12:11:00+00:00")
            _assert_flat_rfc_error(await _poll(client, code["device_code"]), "expired_token")
        # The same frozen instant as the previous poll: well inside the interval.
        for _ in range(3):
            _assert_flat_rfc_error(await _poll(client, code["device_code"]), error)


# --- consent endpoints (SPA-facing, house envelope) ------------------------


@pytest.mark.asyncio
async def test_approve_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/device/approve", json={"user_code": "AAAA-BBBB"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_info_unknown_code_is_404(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/auth/device/info", params={"user_code": "AAAA-BBBB"})
    assert resp.status_code == 404
    # House envelope, unlike the CLI endpoints.
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_user_code_bruteforce_throttle(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    limit = settings.auth_device_max_user_code_attempts
    for _ in range(limit):
        r = await client.get("/api/v1/auth/device/info", params={"user_code": "ZZZZ-ZZZZ"})
        assert r.status_code == 404
    blocked = await client.get("/api/v1/auth/device/info", params={"user_code": "ZZZZ-ZZZZ"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_approval_binds_to_the_approver_not_the_requester(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """Whoever approves owns the minted token — the device code is anonymous at
    creation, so a second user approving binds it to themselves, not the admin."""
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert member_pw is not None

    code = await _request_code(client)
    await login(client, member.email, member_pw)
    approve = await client.post(
        "/api/v1/auth/device/approve", json={"user_code": code["user_code"]}
    )
    assert approve.status_code == 200

    token_resp = await _poll(client, code["device_code"])
    assert decode_session_token(token_resp.json()["access_token"]).user_id == member.id


@pytest.mark.asyncio
async def test_redeem_refuses_a_deactivated_approver(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
) -> None:
    """Offboarding must be complete the moment the account is deactivated.

    An approved-but-unredeemed grant survives `revoke_all_for_user` (it lives in
    a different table), so without an `is_active` check at redeem time a
    deprovisioned user can still exchange a stashed device_code for a fresh
    90-day CLI token — one that no session list shows and that the gateway,
    which bills real provider spend, would honour."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import User
    from sqlalchemy import update

    member, member_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    assert member_pw is not None

    code = await _request_code(client)
    await login(client, member.email, member_pw)
    assert (
        await client.post("/api/v1/auth/device/approve", json={"user_code": code["user_code"]})
    ).status_code == 200

    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == member.id).values(is_active=False))
        await s.commit()

    resp = await _poll(client, code["device_code"])
    assert resp.status_code == 400
    assert "access_token" not in resp.json()
    _assert_flat_rfc_error(resp, "expired_token")

    # And the grant is spent — reactivating later doesn't resurrect it.
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == member.id).values(is_active=True))
        await s.commit()
    _assert_flat_rfc_error(await _poll(client, code["device_code"]), "expired_token")


@pytest.mark.asyncio
async def test_cannot_reapprove_or_approve_after_deny(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    code = await _request_code(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (
        await client.post("/api/v1/auth/device/deny", json={"user_code": code["user_code"]})
    ).status_code == 200
    # The code is no longer pending → an approve now reads as not-found (404),
    # never flips a denied authorization to approved.
    reapprove = await client.post(
        "/api/v1/auth/device/approve", json={"user_code": code["user_code"]}
    )
    assert reapprove.status_code == 404
    _assert_flat_rfc_error(await _poll(client, code["device_code"]), "access_denied")


# --- consent needs a browser session ----------------------------------------
#
# Approving a device mints a new long-lived CLI token. A credential that is not
# a browser sign-in must not be able to do it: a stolen CLI token would launder
# itself into a fresh one in a new family, and revoking the stolen session from
# the sessions page would no longer evict the thief.

_CONSENT_ROUTES = [
    pytest.param("GET", "/api/v1/auth/device/info", id="info"),
    pytest.param("POST", "/api/v1/auth/device/approve", id="approve"),
    pytest.param("POST", "/api/v1/auth/device/deny", id="deny"),
]


async def _consent(
    client: AsyncClient, method: str, url: str, user_code: str, headers: dict[str, str]
):
    if method == "GET":
        return await client.get(url, params={"user_code": user_code}, headers=headers)
    return await client.post(url, json={"user_code": user_code}, headers=headers)


async def _mint_service_token(kind: str, org: OrgWithAdmin) -> str:
    from alkera_core.db.session import AsyncSessionLocal
    from backend.services.credentials import ci_tokens as ci_token_service
    from backend.services.credentials import pats as pat_service
    from backend.services.credentials import proxy_tokens as proxy_token_service

    async with AsyncSessionLocal() as session:
        if kind == "ci":
            _row, raw = await ci_token_service.mint(
                session, org_id=org.org_id, created_by_id=org.admin_id, label="ci"
            )
        elif kind == "proxy":
            _row, raw = await proxy_token_service.mint(
                session, org_id=org.org_id, created_by_id=org.admin_id, label="proxy"
            )
        else:
            _row, raw = await pat_service.mint(
                session, org_id=org.org_id, user_id=org.admin_id, label="pat"
            )
        await session.commit()
    return raw


async def _present(kind: str, client: AsyncClient, org: OrgWithAdmin) -> dict[str, str]:
    """Put the credential ``kind`` on ``client`` (cookie) or return it as headers."""
    from alkera_core.auth import COOKIE_NAME, encode_session_token
    from alkera_core.auth.machine_token import machine_credential_headers, mint_machine_token
    from alkera_core.authz import agent_headers

    if kind in {"cli-bearer", "cli-in-cookie"}:
        cli = await mint_cli_token(
            user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id
        )
        if kind == "cli-in-cookie":
            client.cookies.set(COOKIE_NAME, cli)
            return {}
        return {"Authorization": f"Bearer {cli}"}
    if kind in {"ci", "proxy", "pat"}:
        return {"Authorization": f"Bearer {await _mint_service_token(kind, org)}"}
    if kind == "machine-bearer":
        return {"Authorization": f"Bearer {mint_machine_token()[0]}"}
    if kind == "unregistered-session-jwt":
        token, _claims = encode_session_token(
            user_id=org.admin_id,
            email=org.admin_email,
            org_team_id=org.org_id,
            platform_role=None,
        )
        return {"Authorization": f"Bearer {token}"}
    # The remaining kinds ride a real browser session and add something to it.
    await login(client, org.admin_email, org.admin_password)
    if kind == "machine-header-beside-session":
        return machine_credential_headers(mint_machine_token()[0])
    assert kind == "agent-in-session"
    return agent_headers("agent-session-1")


_REFUSED_CREDENTIALS = [
    pytest.param("cli-bearer", id="cli-bearer"),
    pytest.param("cli-in-cookie", id="cli-smuggled-in-cookie"),
    pytest.param("ci", id="ci-token"),
    pytest.param("proxy", id="proxy-token"),
    pytest.param("pat", id="personal-access-token"),
    pytest.param("machine-bearer", id="machine-credential-as-bearer"),
    pytest.param("machine-header-beside-session", id="machine-header-beside-session"),
    pytest.param("agent-in-session", id="agent-in-browser-session"),
    pytest.param("unregistered-session-jwt", id="unregistered-jwt"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _REFUSED_CREDENTIALS)
@pytest.mark.parametrize(("method", "url"), _CONSENT_ROUTES)
async def test_consent_refuses_a_credential_that_is_not_a_browser_session(
    client: AsyncClient, org_admin: OrgWithAdmin, kind: str, method: str, url: str
) -> None:
    code = await _request_code(client)
    headers = await _present(kind, client, org_admin)

    resp = await _consent(client, method, url, code["user_code"], headers)

    assert resp.status_code == 403, resp.text
    error = resp.json()["error"]
    assert error["code"] == "browser_session_required"
    assert error["message"] == "Approve this in a browser where you're signed in"
    # The grant is untouched: neither approved nor denied, so the device keeps
    # waiting and no token was minted.
    _assert_flat_rfc_error(await _poll(client, code["device_code"]), "authorization_pending")


@pytest.mark.asyncio
async def test_a_refused_cli_token_leaves_the_grant_for_the_browser_to_approve(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    code = await _request_code(client)
    cli = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    refused = await client.post(
        "/api/v1/auth/device/approve",
        json={"user_code": code["user_code"]},
        headers={"Authorization": f"Bearer {cli}"},
    )
    assert refused.status_code == 403

    await login(client, org_admin.admin_email, org_admin.admin_password)
    approved = await client.post(
        "/api/v1/auth/device/approve", json={"user_code": code["user_code"]}
    )
    assert approved.status_code == 200
    token_resp = await _poll(client, code["device_code"])
    assert token_resp.status_code == 200
    assert decode_session_token(token_resp.json()["access_token"]).user_id == org_admin.admin_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "url"), _CONSENT_ROUTES)
async def test_a_browser_session_token_is_accepted_whichever_way_it_travels(
    client: AsyncClient, org_admin: OrgWithAdmin, method: str, url: str
) -> None:
    """The registered row decides, not the transport: the browser's own session
    token presented as a Bearer is still a browser session."""
    from alkera_core.auth import COOKIE_NAME

    code = await _request_code(client)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    session_token = client.cookies.get(COOKIE_NAME)
    assert session_token
    client.cookies.clear()

    resp = await _consent(
        client, method, url, code["user_code"], {"Authorization": f"Bearer {session_token}"}
    )
    assert resp.status_code == 200, resp.text
