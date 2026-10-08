"""The identity security log: what it records, and how a person pages through it.

Every event here is driven through the real route (or, for SSO enforcement,
the one service every enforcing path calls) and read back from the table, so a
recording call that is removed, or that lands in the wrong person's log, fails
the test that names it. The self-read is paged on ``(created_at, id)``: rows
that share the boundary timestamp are neither skipped nor shown twice.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import revocation, totp
from alkera_core.auth.secret_box import encrypt_secret
from alkera_core.auth.token_hash import hash_lookup_token
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    AuthRefreshToken,
    IdentitySecurityEvent,
    SsoConnection,
    TeamRole,
    User,
)
from backend.services.audit import identity_security
from backend.services.identity import sso as sso_service
from backend.services.org import org_memberships
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import (
    OrgWithAdmin,
    TwoOrg,
    app_client,
    hold_sso_domains,
    login,
    login_via_route,
    make_member,
    mint_cli_token,
)
from tests.test_cross_tenant_identity import _browser, _enforce, _refresh, _request

pytestmark = pytest.mark.asyncio

GRANT = "urn:ietf:params:oauth:grant-type:device_code"
EVENTS_PATH = "/api/v1/me/security-events"


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _quiet_device_limits() -> Iterator[None]:
    from backend.services.identity.device_authorization import _user_code_limiter

    _user_code_limiter.reset()
    yield
    _user_code_limiter.reset()


# --- helpers ----------------------------------------------------------------------------


async def _events(user_id: UUID, event: str | None = None) -> list[IdentitySecurityEvent]:
    async with AsyncSessionLocal() as db:
        stmt = select(IdentitySecurityEvent).where(IdentitySecurityEvent.user_id == user_id)
        if event is not None:
            stmt = stmt.where(IdentitySecurityEvent.event == event)
        rows = await db.execute(stmt.order_by(IdentitySecurityEvent.created_at))
        return list(rows.scalars().all())


async def _member(org_id: UUID, *, role: TeamRole = TeamRole.MEMBER) -> tuple[User, str]:
    async with AsyncSessionLocal() as db:
        user, password = await make_member(db, org_id=org_id, role=role, verified=True)
    assert password is not None
    return user, password


async def _signed_in(email: str, password: str) -> AsyncClient:
    client = app_client()
    await login(client, email, password)
    return client


async def _admin_client(org_admin: OrgWithAdmin) -> AsyncClient:
    return await _signed_in(org_admin.admin_email, org_admin.admin_password)


# --- the cursor -------------------------------------------------------------------------


async def test_pages_never_skip_or_repeat_events_sharing_a_timestamp(
    org_admin: OrgWithAdmin,
) -> None:
    """Five events at one instant and two older: walking two at a time from the
    first page must see every one of them exactly once. Keyed on the timestamp
    alone, the page boundary falls inside the tie and the rest of it is lost."""
    tied_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    async with AsyncSessionLocal() as db:
        for _ in range(5):
            db.add(
                IdentitySecurityEvent(
                    id=uuid4(),
                    user_id=org_admin.admin_id,
                    event="auth.login_failed",
                    created_at=tied_at,
                )
            )
        for days in (1, 2):
            db.add(
                IdentitySecurityEvent(
                    id=uuid4(),
                    user_id=org_admin.admin_id,
                    event="auth.mfa_enabled",
                    created_at=tied_at - timedelta(days=days),
                )
            )
        await db.commit()
    client = await _admin_client(org_admin)
    expected = {row.id for row in await _events(org_admin.admin_id)}
    seen: list[UUID] = []
    params: dict[str, Any] = {"limit": 2}
    try:
        for _ in range(20):
            resp = await client.get(EVENTS_PATH, params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            seen.extend(UUID(e["id"]) for e in body["events"])
            if body["next_before"] is None:
                assert body["next_before_id"] is None
                break
            params = {
                "limit": 2,
                "before": body["next_before"],
                "before_id": body["next_before_id"],
            }
    finally:
        await client.aclose()
    assert len(seen) == len(set(seen)), "a row was shown twice"
    assert set(seen) == expected, "a row sharing the boundary timestamp was skipped"
    # Newest first, ties broken by id descending, as the cursor assumes.
    rows = {row.id: row for row in await _events(org_admin.admin_id)}
    keys = [(rows[i].created_at, rows[i].id) for i in seen]
    assert keys == sorted(keys, reverse=True)


async def test_before_alone_still_means_strictly_older(org_admin: OrgWithAdmin) -> None:
    """A client that sends only ``before`` keeps the meaning it always had."""
    at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    async with AsyncSessionLocal() as db:
        for offset in (0, 0, 1):
            db.add(
                IdentitySecurityEvent(
                    id=uuid4(),
                    user_id=org_admin.admin_id,
                    event="auth.login_failed",
                    created_at=at - timedelta(seconds=offset),
                )
            )
        await db.commit()
        page = await identity_security.list_for_user(db, org_admin.admin_id, limit=10, before=at)
    assert [row.created_at for row in page] == [at - timedelta(seconds=1)]


# --- the bulk writer --------------------------------------------------------------------


async def test_record_many_writes_one_row_per_person_and_refuses_unknown_events(
    org_admin: OrgWithAdmin,
) -> None:
    other, _ = await _member(org_admin.org_id)
    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="not an identity security event"):
            await identity_security.record_many(
                db, user_ids=[other.id], event="auth.made_up", org_team_id=org_admin.org_id
            )
        written = await identity_security.record_many(
            db,
            user_ids=[other.id, other.id, org_admin.admin_id],
            event="auth.sso_enforced_signout",
            org_team_id=org_admin.org_id,
            detail={"secret": "hunter2", "note": "kept"},
        )
        await db.commit()
    assert written == 2
    rows = await _events(other.id, "auth.sso_enforced_signout")
    assert len(rows) == 1
    assert rows[0].org_team_id == org_admin.org_id
    assert rows[0].detail is not None
    assert rows[0].detail["note"] == "kept"
    assert rows[0].detail["secret"] != "hunter2", "the detail was not scrubbed"
    assert len(await _events(org_admin.admin_id, "auth.sso_enforced_signout")) == 1


# --- sign-ins ---------------------------------------------------------------------------


async def test_a_password_sign_in_is_on_the_log(org_admin: OrgWithAdmin) -> None:
    async with app_client() as client:
        await login_via_route(client, org_admin.admin_email, org_admin.admin_password)
    rows = await _events(org_admin.admin_id, "auth.signed_in")
    assert len(rows) == 1
    assert rows[0].detail == {"method": "password"}
    assert rows[0].org_team_id == org_admin.org_id
    assert rows[0].ip_prefix is not None, "the coarse network was not kept"


async def test_a_refused_password_is_not_a_sign_in(org_admin: OrgWithAdmin) -> None:
    async with app_client() as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": org_admin.admin_email, "password": "not-the-password-1"},
        )
    assert resp.status_code == 401
    assert await _events(org_admin.admin_id, "auth.signed_in") == []
    assert len(await _events(org_admin.admin_id, "auth.login_failed")) == 1


@pytest.mark.usefixtures("multi_org")
async def test_a_sign_in_into_no_org_is_on_the_log(org_admin: OrgWithAdmin) -> None:
    """A person every org has removed still signs in (into no org): that is a
    sign-in, with no org on it."""
    user, password = await _member(org_admin.org_id)
    async with AsyncSessionLocal() as db:
        membership = await org_memberships.get(db, user_id=user.id, org_team_id=org_admin.org_id)
        assert membership is not None
        await org_memberships.remove(db, membership, actor=None)
        await db.commit()
    async with app_client() as client:
        resp = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": password}
        )
    assert resp.status_code == 403, resp.text
    rows = await _events(user.id, "auth.signed_in")
    assert [(r.org_team_id, r.detail) for r in rows] == [(None, {"method": "password"})]


@pytest.mark.parametrize(
    ("client_id", "scope"),
    [
        pytest.param("alkera-cli", None, id="the-cli"),
        pytest.param("alkera-box", "box", id="a-personal-box"),
    ],
)
async def test_a_device_sign_in_is_on_the_log(
    org_admin: OrgWithAdmin, client_id: str, scope: str | None
) -> None:
    user, password = await _member(org_admin.org_id)
    async with app_client() as cli:
        form = {"client_id": client_id, **({"scope": scope} if scope else {})}
        code = await cli.post("/api/v1/auth/device/code", data=form)
        assert code.status_code == 200, code.text
        browser = await _signed_in(user.email, password)
        try:
            approved = await browser.post(
                "/api/v1/auth/device/approve", json={"user_code": code.json()["user_code"]}
            )
            assert approved.status_code == 200, approved.text
        finally:
            await browser.aclose()
        before = len(await _events(user.id, "auth.signed_in"))
        token = await cli.post(
            "/api/v1/auth/device/token",
            data={
                "grant_type": GRANT,
                "device_code": code.json()["device_code"],
                "client_id": client_id,
            },
        )
    assert token.status_code == 200, token.text
    rows = await _events(user.id, "auth.signed_in")
    assert len(rows) == before + 1
    assert rows[-1].detail == {"method": "device", "client_id": client_id}
    assert rows[-1].org_team_id == org_admin.org_id


@pytest.mark.usefixtures("strict_sso")
@pytest.mark.parametrize(
    "stepped_up",
    [
        pytest.param(False, id="a-fresh-browser"),
        pytest.param(True, id="the-browsers-own-session-stepped-up"),
    ],
)
async def test_an_sso_sign_in_is_on_the_log(two_org_identity: TwoOrg, stepped_up: bool) -> None:
    """Whether the IdP's answer starts a session or steps up the one the
    browser already holds, the person signed in to the org through its IdP."""
    from backend.api.routes.identity.sso import _complete_login

    t = two_org_identity
    await _enforce(t.org_a)
    cookie: str | None = None
    if stepped_up:
        session = await _browser(t.user.id, t.org_a)
        assert (await _refresh(session)).status_code == 401
        cookie = session.cookies
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        await _complete_login(
            db,
            _request("/api/v1/auth/sso/x/login/callback", cookie=cookie),
            user=user,
            org_id=t.org_a,
            return_to="/dashboard",
        )
        await db.commit()
    rows = [r for r in await _events(t.user.id, "auth.signed_in") if r.detail == {"method": "sso"}]
    assert len(rows) == 1
    assert rows[0].org_team_id == t.org_a


# --- second factor ----------------------------------------------------------------------


async def _with_factor(user_id: UUID, *, backup: list[str]) -> str:
    secret = totp.generate_secret()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        user.mfa_secret_encrypted = encrypt_secret(secret)
        user.mfa_enabled = True
        user.mfa_backup_codes = json.dumps([hash_lookup_token(c) for c in backup])
        await db.commit()
    return secret


@pytest.mark.parametrize(
    ("factor", "recorded"),
    [
        pytest.param("backup", True, id="a-backup-code-is-on-the-log"),
        pytest.param("totp", False, id="an-authenticator-code-is-not"),
    ],
)
async def test_a_backup_code_spent_at_sign_in_is_on_the_log(
    org_admin: OrgWithAdmin, factor: str, recorded: bool
) -> None:
    user, password = await _member(org_admin.org_id)
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(2)]
    secret = await _with_factor(user.id, backup=codes)
    code = codes[0] if factor == "backup" else totp._hotp(secret, int(time.time() // 30))
    async with app_client() as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": password, "mfa_code": code},
        )
    assert resp.status_code == 200, resp.text
    rows = await _events(user.id, "auth.mfa_backup_code_used")
    if recorded:
        assert [r.detail for r in rows] == [{"remaining": 1}]
    else:
        assert rows == []


async def test_a_refused_backup_code_is_not_on_the_log(org_admin: OrgWithAdmin) -> None:
    user, password = await _member(org_admin.org_id)
    await _with_factor(user.id, backup=["aaaaa-bbbbb"])
    async with app_client() as client:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": password, "mfa_code": "ccccc-ddddd"},
        )
    assert resp.status_code == 401
    assert await _events(user.id, "auth.mfa_backup_code_used") == []


# --- ending sessions --------------------------------------------------------------------


async def test_signing_out_everywhere_is_on_the_log(org_admin: OrgWithAdmin) -> None:
    client = await _admin_client(org_admin)
    try:
        resp = await client.post("/api/v1/auth/logout-all")
    finally:
        await client.aclose()
    assert resp.status_code == 200, resp.text
    rows = await _events(org_admin.admin_id, "auth.sessions_revoked_all")
    assert len(rows) == 1
    assert rows[0].org_team_id == org_admin.org_id


@pytest.mark.parametrize("kind", [pytest.param("browser"), pytest.param("token")])
async def test_ending_one_session_is_on_the_log(org_admin: OrgWithAdmin, kind: str) -> None:
    other = await _admin_client(org_admin)
    await other.aclose()
    if kind == "browser":
        async with AsyncSessionLocal() as db:
            family = await db.scalar(
                select(AuthRefreshToken.family_id)
                .where(AuthRefreshToken.user_id == org_admin.admin_id)
                .limit(1)
            )
        assert family is not None
        target = family.hex
    else:
        from alkera_core.auth import decode_session_token

        token = await mint_cli_token(
            user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
        )
        jti = decode_session_token(token).jti
        assert jti is not None
        target = jti
    client = await _admin_client(org_admin)
    try:
        resp = await client.delete(f"/api/v1/auth/sessions/{target}")
    finally:
        await client.aclose()
    assert resp.status_code == 200, resp.text
    rows = await _events(org_admin.admin_id, "auth.session_revoked")
    assert [r.detail for r in rows] == [{"kind": kind}]


async def test_ending_a_session_that_is_not_yours_is_not_on_the_log(
    org_admin: OrgWithAdmin,
) -> None:
    client = await _admin_client(org_admin)
    try:
        resp = await client.delete(f"/api/v1/auth/sessions/{uuid4().hex}")
    finally:
        await client.aclose()
    assert resp.status_code == 404
    assert await _events(org_admin.admin_id, "auth.session_revoked") == []


# --- password reset requests ------------------------------------------------------------


async def test_a_reset_request_from_the_sign_in_page_is_on_the_log(
    org_admin: OrgWithAdmin, monkeypatch_password_reset_send: list[dict[str, Any]]
) -> None:
    async with app_client() as client:
        known = await client.post(
            "/api/v1/auth/password-reset/request", json={"email": org_admin.admin_email}
        )
        unknown = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": f"nobody-{secrets.token_hex(4)}@alkera.dev"},
        )
    assert known.status_code == unknown.status_code == 200
    rows = await _events(org_admin.admin_id, "auth.password_reset_requested")
    assert [r.detail for r in rows] == [{"source": "sign_in_page"}]


async def test_a_reset_request_inside_the_cooldown_is_not_on_the_log(
    org_admin: OrgWithAdmin, monkeypatch_password_reset_send: list[dict[str, Any]]
) -> None:
    """Nothing is issued inside the cooldown, so nothing happened to the account."""
    async with app_client() as client:
        for _ in range(2):
            resp = await client.post(
                "/api/v1/auth/password-reset/request", json={"email": org_admin.admin_email}
            )
            assert resp.status_code == 200
    assert len(await _events(org_admin.admin_id, "auth.password_reset_requested")) == 1


async def test_a_password_link_asked_for_while_signed_in_is_on_the_log(
    org_admin: OrgWithAdmin, monkeypatch_password_reset_send: list[dict[str, Any]]
) -> None:
    client = await _admin_client(org_admin)
    try:
        resp = await client.post("/api/v1/auth/password/send-reset")
    finally:
        await client.aclose()
    assert resp.status_code == 200, resp.text
    rows = await _events(org_admin.admin_id, "auth.password_reset_requested")
    assert [r.detail for r in rows] == [{"source": "account"}]


# --- what an org does to the person -----------------------------------------------------


async def test_deactivation_and_reactivation_are_on_the_persons_log(
    org_admin: OrgWithAdmin,
) -> None:
    user, _ = await _member(org_admin.org_id)
    client = await _admin_client(org_admin)
    try:
        off = await client.put(f"/api/v1/org/members/{user.id}/active", json={"active": False})
        again = await client.put(f"/api/v1/org/members/{user.id}/active", json={"active": False})
        on = await client.put(f"/api/v1/org/members/{user.id}/active", json={"active": True})
    finally:
        await client.aclose()
    assert off.status_code == again.status_code == on.status_code == 200, off.text
    events = [
        (r.event, r.org_team_id)
        for r in await _events(user.id)
        if r.event in {"auth.org_deactivated", "auth.org_reactivated"}
    ]
    # A deactivation of a membership already deactivated writes nothing.
    assert events == [
        ("auth.org_deactivated", org_admin.org_id),
        ("auth.org_reactivated", org_admin.org_id),
    ]
    assert await _events(org_admin.admin_id, "auth.org_deactivated") == []


@pytest.mark.parametrize(
    "route",
    [
        pytest.param("users", id="the-users-route"),
        pytest.param("memberships", id="the-org-root-membership-route"),
    ],
)
async def test_removal_from_the_org_is_on_the_persons_log(
    org_admin: OrgWithAdmin, route: str
) -> None:
    user, _ = await _member(org_admin.org_id)
    path = (
        f"/api/v1/users/{user.id}"
        if route == "users"
        else f"/api/v1/teams/{org_admin.org_id}/memberships/{user.id}"
    )
    client = await _admin_client(org_admin)
    try:
        resp = await client.delete(path)
    finally:
        await client.aclose()
    assert resp.status_code == 204, resp.text
    rows = await _events(user.id, "auth.org_removed")
    assert [r.org_team_id for r in rows] == [org_admin.org_id]


async def test_leaving_an_org_is_not_recorded_as_a_removal(org_admin: OrgWithAdmin) -> None:
    """The person leaving is ``auth.org_left``; only an org's own decision is a removal."""
    from backend.services.org import own_orgs

    user, _ = await _member(org_admin.org_id)
    async with AsyncSessionLocal() as db:
        person = await db.get(User, user.id)
        membership = await org_memberships.get(db, user_id=user.id, org_team_id=org_admin.org_id)
        assert person is not None and membership is not None
        await own_orgs.leave_org(db, user=person, membership=membership, actor=None)
        await db.commit()
    assert await _events(user.id, "auth.org_removed") == []
    assert len(await _events(user.id, "auth.org_left")) == 1


async def test_a_role_change_is_on_the_persons_log(org_admin: OrgWithAdmin) -> None:
    user, _ = await _member(org_admin.org_id)
    client = await _admin_client(org_admin)
    try:
        promoted = await client.patch(
            f"/api/v1/teams/{org_admin.org_id}/memberships/{user.id}", json={"role": "admin"}
        )
        unchanged = await client.patch(
            f"/api/v1/teams/{org_admin.org_id}/memberships/{user.id}", json={"role": "admin"}
        )
    finally:
        await client.aclose()
    assert promoted.status_code == unchanged.status_code == 200, promoted.text
    rows = await _events(user.id, "auth.org_role_changed")
    # The same role again writes nothing.
    assert [(r.org_team_id, r.detail) for r in rows] == [
        (org_admin.org_id, {"role": "admin", "scope": "org"})
    ]


@pytest.mark.parametrize(
    "strict", [pytest.param(True, id="strict"), pytest.param(False, id="not-strict")]
)
async def test_enforcing_sso_puts_the_sign_out_on_each_governed_members_log(
    org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    """Strict enforcement ends every governed member's credentials in the org,
    and each of them sees why. Without strict enforcement nothing is ended,
    so nothing is recorded."""
    monkeypatch.setattr(settings, "sso_strict_enforcement_enabled", strict)
    governed, _ = await _member(org_admin.org_id)
    async with AsyncSessionLocal() as db:
        domain = governed.email.rsplit("@", 1)[-1]
        conn = SsoConnection(
            org_team_id=org_admin.org_id,
            protocol="oidc",
            enabled=True,
            enforced=True,
            oidc_issuer="https://idp.example.com",
            oidc_client_id="cid",
        )
        db.add(conn)
        await db.commit()
    await hold_sso_domains(org_admin.org_id, domain)
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(
            select(SsoConnection).where(SsoConnection.org_team_id == org_admin.org_id)
        )
        assert conn is not None
        revoked = await sso_service.enforce_on(db, conn)
        await db.commit()
    rows = await _events(governed.id, "auth.sso_enforced_signout")
    if strict:
        assert revoked >= 1
        assert [r.org_team_id for r in rows] == [org_admin.org_id]
    else:
        assert revoked == 0
        assert rows == []


# --- the self-read sees them ------------------------------------------------------------


async def test_the_person_reads_their_new_events_and_nobody_elses(
    org_admin: OrgWithAdmin,
) -> None:
    user, password = await _member(org_admin.org_id)
    client = await _signed_in(user.email, password)
    try:
        await client.post("/api/v1/auth/logout-all")
        resp = await client.get(EVENTS_PATH)
    finally:
        await client.aclose()
    assert resp.status_code == 200, resp.text
    events = cast(list[dict[str, Any]], resp.json()["events"])
    assert {"auth.signed_in", "auth.sessions_revoked_all"} <= {e["event"] for e in events}
    async with AsyncSessionLocal() as db:
        mine = set(
            (
                await db.execute(
                    select(IdentitySecurityEvent.id).where(IdentitySecurityEvent.user_id == user.id)
                )
            ).scalars()
        )
    assert {UUID(e["id"]) for e in events} <= mine
