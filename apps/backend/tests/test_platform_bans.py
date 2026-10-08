"""Platform bans: a banned person is answered, on every path, exactly as one
who does not exist — and the admin surface that bans and lifts.

Each auth path is pinned as a PAIR: the response for the path's own
"no such user" case and the response for a banned account are captured and
compared whole (status and body, the per-request trace id aside), so a ban
that leaked through a different status, message or shape would fail here.
The admin routes pin the status contract, the decision rows the policy leaves
behind, the audit rows, the users-list columns, and the revocation the ban
performs.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.auth import COOKIE_NAME, encode_cli_token, encode_session_token
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    EmailDomainBan,
    EventOutbox,
    IdentitySecurityEvent,
    OrgAuditEvent,
    PlatformRole,
    User,
    UserBan,
)
from backend.auth.dependencies import CurrentPrincipal
from backend.auth.oauth.profile import FederatedProfile
from backend.services.credentials import pats as pat_service
from backend.services.identity import email_verification as email_verification_service
from backend.services.identity import oauth as oauth_service
from backend.services.identity import password_reset as password_reset_service
from backend.services.identity.oauth import LoginOutcome, RegisterOutcome
from fastapi import APIRouter
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = pytest.mark.asyncio

_PREFIX = "/api/v1/_test/bans"
_router = APIRouter(prefix=_PREFIX)


@_router.get("/principal")
async def _principal(ctx: CurrentPrincipal) -> dict[str, Any]:
    return {"subject_id": ctx.subject.id}


@pytest.fixture(autouse=True)
def _mounted_router() -> Iterator[None]:
    """A route that accepts every credential shape, so a personal access
    token can be driven through the real dependency."""
    before = len(fastapi_app.router.routes)
    fastapi_app.include_router(_router)
    try:
        yield
    finally:
        del fastapi_app.router.routes[before:]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

STRONG_PASSWORD = "Correct-Horse-Battery-9-Staple"


def _captured(resp: Response) -> tuple[int, Any]:
    """Status + body with the per-request trace id removed — everything else
    must match between the pair."""
    body = resp.json()
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body["error"].pop("trace_id", None)
    return resp.status_code, body


def _domain(prefix: str = "acme") -> str:
    return f"{prefix}-{secrets.token_hex(4)}.com"


async def _ban_user_row(user_id: UUID, *, reason: str = "") -> None:
    """A ban written straight to the register (no revocation) — for the auth
    paths, which must refuse on the row alone."""
    async with AsyncSessionLocal() as session:
        session.add(UserBan(user_id=user_id, reason=reason))
        await session.commit()


async def _lift_user_row(user_id: UUID) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(UserBan)
            .where(UserBan.user_id == user_id, UserBan.lifted_at.is_(None))
            .values(lifted_at=func.now())
        )
        await session.commit()


async def _ban_domain_row(domain: str, *, reason: str = "") -> None:
    async with AsyncSessionLocal() as session:
        session.add(EmailDomainBan(domain=domain, reason=reason))
        await session.commit()


async def _member(org: OrgWithAdmin, *, email: str | None = None) -> tuple[User, str]:
    async with AsyncSessionLocal() as session:
        user, pw = await make_member(
            session, org_id=org.org_id, email=email, password=STRONG_PASSWORD, verified=True
        )
        return user, pw or ""


def _ghost_cookie(org: OrgWithAdmin) -> str:
    """A well-formed session for a user id that was never created — the
    dependency's own "no such user" case."""
    token, _claims = encode_session_token(
        user_id=uuid4(), email="ghost@alkera.dev", org_team_id=org.org_id, platform_role=None
    )
    return token


def _ghost_bearer(org: OrgWithAdmin) -> dict[str, str]:
    token, _claims = encode_cli_token(
        user_id=uuid4(), email="ghost@alkera.dev", org_team_id=org.org_id, platform_role=None
    )
    return {"Authorization": f"Bearer {token}"}


async def _pat_for(org: OrgWithAdmin, user_id: UUID) -> str:
    async with AsyncSessionLocal() as session:
        _row, raw = await pat_service.mint(
            session, org_id=org.org_id, user_id=user_id, label="pat-label"
        )
        await session.commit()
        return raw


def _bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _decisions(org_id: UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "platform_ban",
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars().all())


async def _org_audit(org_id: UUID, action: str) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(OrgAuditEvent)
            .where(OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action == action)
            .order_by(OrgAuditEvent.created_at)
        )
        return list(rows.scalars().all())


def _fresh_client(client: AsyncClient) -> AsyncClient:
    return app_client()


# --------------------------------------------------------------------------- #
# the session dependency: cookie, Bearer JWT, personal access token
# --------------------------------------------------------------------------- #


async def test_a_banned_cookie_session_is_answered_as_a_missing_user(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member, pw = await _member(org_admin)
    await login(client, member.email, pw)
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    ghost = _fresh_client(client)
    ghost.cookies.set(COOKIE_NAME, _ghost_cookie(org_admin))
    unknown = _captured(await ghost.get("/api/v1/auth/me"))

    await _ban_user_row(member.id)
    banned = _captured(await client.get("/api/v1/auth/me"))
    assert unknown[0] == 401
    assert banned == unknown


async def test_a_banned_bearer_jwt_is_answered_as_a_missing_user(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member, _pw = await _member(org_admin)
    token, _claims = encode_cli_token(
        user_id=member.id, email=member.email, org_team_id=org_admin.org_id, platform_role=None
    )
    assert (await client.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 200
    unknown = _captured(await client.get("/api/v1/auth/me", headers=_ghost_bearer(org_admin)))

    await _ban_user_row(member.id)
    banned = _captured(await client.get("/api/v1/auth/me", headers=_bearer(token)))
    assert unknown[0] == 401
    assert banned == unknown


async def test_a_personal_access_token_of_a_banned_owner_is_answered_as_an_unknown_token(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member, _pw = await _member(org_admin)
    raw = await _pat_for(org_admin, member.id)
    ok = await client.get(f"{_PREFIX}/principal", headers=_bearer(raw))
    assert ok.status_code == 200 and ok.json()["subject_id"] == str(member.id)
    unknown = _captured(
        await client.get(f"{_PREFIX}/principal", headers=_bearer("alk_pat_" + "x" * 64))
    )

    await _ban_user_row(member.id)
    banned = _captured(await client.get(f"{_PREFIX}/principal", headers=_bearer(raw)))
    assert unknown[0] == 401
    assert banned == unknown


async def test_the_verification_resend_of_a_banned_session_is_the_missing_user_answer(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member, pw = await _member(org_admin)
    await login(client, member.email, pw)
    ghost = _fresh_client(client)
    ghost.cookies.set(COOKIE_NAME, _ghost_cookie(org_admin))
    unknown = _captured(await ghost.post("/api/v1/auth/verify-email/resend"))

    await _ban_user_row(member.id)
    banned = _captured(await client.post("/api/v1/auth/verify-email/resend"))
    assert unknown[0] == 401
    assert banned == unknown


# --------------------------------------------------------------------------- #
# login, reset request, the emailed links
# --------------------------------------------------------------------------- #


async def test_a_banned_login_with_the_right_password_is_the_unknown_address_answer(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member, pw = await _member(org_admin)
    unknown = _captured(
        await client.post(
            "/api/v1/auth/login",
            json={"email": f"nobody-{secrets.token_hex(4)}@alkera.dev", "password": pw},
        )
    )
    await _ban_user_row(member.id)
    banned = _captured(
        await client.post("/api/v1/auth/login", json={"email": member.email, "password": pw})
    )
    assert unknown[0] == 401
    assert banned == unknown


async def test_a_lifted_ban_is_a_normal_login(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    member, pw = await _member(org_admin)
    await _ban_user_row(member.id)
    refused = await client.post("/api/v1/auth/login", json={"email": member.email, "password": pw})
    assert refused.status_code == 401
    await _lift_user_row(member.id)
    allowed = await client.post("/api/v1/auth/login", json={"email": member.email, "password": pw})
    assert allowed.status_code == 200, allowed.text
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_a_banned_reset_request_answers_like_an_unknown_address_and_sends_nothing(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_password_reset_send: list[dict[str, Any]],
) -> None:
    member, _pw = await _member(org_admin)
    unknown = _captured(
        await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": f"nobody-{secrets.token_hex(4)}@alkera.dev"},
        )
    )
    await _ban_user_row(member.id)
    banned = _captured(
        await client.post("/api/v1/auth/password-reset/request", json={"email": member.email})
    )
    assert unknown[0] == 200
    assert banned == unknown
    assert monkeypatch_password_reset_send == []


async def test_a_banned_holders_verification_link_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    async with AsyncSessionLocal() as session:
        member, _pw = await make_member(session, org_id=org_admin.org_id)
        token = await email_verification_service.issue_token(session, member)
        await session.commit()
        member_id = member.id
    unknown = _captured(await client.post(f"/api/v1/auth/verify-email/{secrets.token_hex(24)}"))
    await _ban_user_row(member_id)
    banned = _captured(await client.post(f"/api/v1/auth/verify-email/{token}"))
    assert unknown[0] == 400
    assert banned == unknown


async def test_a_banned_holders_password_reset_link_is_not_found(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    async with AsyncSessionLocal() as session:
        member, _pw = await make_member(session, org_id=org_admin.org_id, verified=True)
        token = await password_reset_service.issue_token(session, member)
        await session.commit()
        member_id = member.id
    body = {"password": "Another-Strong-Passphrase-77"}
    unknown = _captured(
        await client.post(f"/api/v1/auth/password-reset/{secrets.token_hex(24)}", json=body)
    )
    await _ban_user_row(member_id)
    banned = _captured(await client.post(f"/api/v1/auth/password-reset/{token}", json=body))
    assert unknown[0] == 400
    assert banned == unknown


# --------------------------------------------------------------------------- #
# signup + invitation: the ban reads as "enter a valid email"
# --------------------------------------------------------------------------- #


def _signup(email: str) -> dict[str, Any]:
    return {"email": email, "password": STRONG_PASSWORD, "allow_personal_email": True}


async def test_signup_at_a_banned_address_is_the_email_validation_refusal(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = _domain()
    member, _pw = await _member(org_admin)
    await _ban_user_row(member.id)
    await _ban_domain_row(domain)

    invalid = _captured(await client.post("/api/v1/auth/signup", json=_signup("bob@nodot")))
    assert invalid[0] == 422
    assert invalid[1]["error"]["details"]["errors"][0]["loc"] == ["body", "email"]

    banned_account = _captured(await client.post("/api/v1/auth/signup", json=_signup(member.email)))
    banned_domain = _captured(
        await client.post(
            "/api/v1/auth/signup", json=_signup(f"new-{secrets.token_hex(3)}@{domain}")
        )
    )
    mixed_case = _captured(
        await client.post(
            "/api/v1/auth/signup", json=_signup(f"New-{secrets.token_hex(3)}@{domain.upper()}")
        )
    )
    assert banned_account == invalid
    assert banned_domain == invalid
    assert mixed_case == invalid


async def test_signup_at_a_subdomain_of_a_banned_domain_is_not_covered(
    client: AsyncClient,
) -> None:
    domain = _domain()
    await _ban_domain_row(domain)
    resp = await client.post(
        "/api/v1/auth/signup", json=_signup(f"eve-{secrets.token_hex(3)}@sub.{domain}")
    )
    assert resp.status_code == 201, resp.text


async def test_an_invitation_to_a_banned_address_is_the_email_validation_refusal(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send: list[dict[str, Any]],
) -> None:
    domain = _domain()
    await _ban_domain_row(domain)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    url = f"/api/v1/teams/{org_admin.org_id}/invitations"

    invalid = _captured(await client.post(url, json={"email": "x@nodot"}))
    assert invalid[0] == 422
    banned = _captured(await client.post(url, json={"email": f"y-{secrets.token_hex(3)}@{domain}"}))
    assert banned == invalid
    assert monkeypatch_email_send == []


# --------------------------------------------------------------------------- #
# domain coverage
# --------------------------------------------------------------------------- #


async def test_a_domain_ban_covers_the_domain_case_insensitively_but_not_a_subdomain(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = _domain()
    alice, pw = await _member(org_admin, email=f"Alice-{secrets.token_hex(2)}@{domain.upper()}")
    below, pw_below = await _member(org_admin, email=f"bob-{secrets.token_hex(2)}@sub.{domain}")
    await _ban_domain_row(domain)

    refused = await client.post(
        "/api/v1/auth/login", json={"email": alice.email.upper(), "password": pw}
    )
    assert refused.status_code == 401
    allowed = await client.post(
        "/api/v1/auth/login", json={"email": below.email, "password": pw_below}
    )
    assert allowed.status_code == 200, allowed.text


async def test_a_domain_ban_never_covers_a_platform_staff_account(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    domain = _domain("ops")
    staff, staff_pw = await _member(org_admin, email=f"staff@{domain}")
    plain, plain_pw = await _member(org_admin, email=f"plain@{domain}")
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(User)
            .where(User.id == staff.id)
            .values(platform_role=PlatformRole.ALKERA_SUPPORT)
        )
        await session.commit()
    await _ban_domain_row(domain)
    assert (
        await client.post("/api/v1/auth/login", json={"email": plain.email, "password": plain_pw})
    ).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={"email": staff.email, "password": staff_pw})
    ).status_code == 200


# --------------------------------------------------------------------------- #
# OAuth resolves a banned person as nobody
# --------------------------------------------------------------------------- #


async def test_oauth_resolves_a_banned_linked_identity_as_a_stranger_and_keeps_the_link(
    org_admin: OrgWithAdmin,
) -> None:
    member, _pw = await _member(org_admin)
    profile = FederatedProfile(
        provider="google",
        subject=f"sub-{secrets.token_hex(6)}",
        email=member.email,
        email_verified=True,
    )
    async with AsyncSessionLocal() as session:
        first = await oauth_service.resolve(session, profile, invite_token=None)
        await session.commit()
    assert isinstance(first, LoginOutcome) and first.user.id == member.id

    await _ban_user_row(member.id)
    async with AsyncSessionLocal() as session:
        while_banned = await oauth_service.resolve(session, profile, invite_token=None)
        await session.commit()
    assert isinstance(while_banned, RegisterOutcome)

    await _lift_user_row(member.id)
    async with AsyncSessionLocal() as session:
        after = await oauth_service.resolve(session, profile, invite_token=None)
    assert isinstance(after, LoginOutcome) and after.user.id == member.id


# --------------------------------------------------------------------------- #
# the admin API
# --------------------------------------------------------------------------- #

USERS = "/admin/v1/bans/users"
DOMAINS = "/admin/v1/bans/domains"


async def test_an_admin_bans_and_lifts_a_user_and_the_register_shows_both(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    member, _pw = await _member(org_admin)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    created = await client.post(USERS, json={"user_id": str(member.id), "reason": "spam"})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["user_id"] == str(member.id)
    assert row["user_email"] == member.email
    assert row["user_display_name"] == member.display_name
    assert row["reason"] == "spam"
    assert row["created_by_id"] == str(platform_admin.admin_id)
    assert row["created_by_email"] == platform_admin.admin_email
    assert row["active"] is True and row["lifted_at"] is None

    again = await client.post(USERS, json={"user_id": str(member.id), "reason": "twice"})
    assert again.status_code == 409

    listed = {r["user_id"]: r for r in (await client.get(USERS)).json()}
    assert listed[str(member.id)]["active"] is True

    users = {r["id"]: r for r in (await client.get("/admin/v1/users")).json()}
    assert users[str(member.id)]["banned"] is True
    assert users[str(member.id)]["ban_reason"] == "spam"
    assert users[str(platform_admin.admin_id)]["banned"] is False
    assert users[str(platform_admin.admin_id)]["ban_reason"] is None

    lifted = await client.delete(f"{USERS}/{member.id}")
    assert lifted.status_code == 204
    assert (await client.delete(f"{USERS}/{member.id}")).status_code == 404

    listed = {r["user_id"]: r for r in (await client.get(USERS)).json()}
    assert listed[str(member.id)]["active"] is False
    assert listed[str(member.id)]["lifted_by_email"] == platform_admin.admin_email
    assert listed[str(member.id)]["lifted_at"] is not None
    users = {r["id"]: r for r in (await client.get("/admin/v1/users")).json()}
    assert users[str(member.id)]["banned"] is False


async def test_a_ban_is_refused_for_the_caller_a_staff_account_and_an_unknown_user(
    client: AsyncClient, platform_admin: OrgWithAdmin, platform_support: OrgWithAdmin
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    me = await client.post(USERS, json={"user_id": str(platform_admin.admin_id)})
    assert me.status_code == 422
    assert me.json()["error"]["code"] == "ban_refused"
    staff = await client.post(USERS, json={"user_id": str(platform_support.admin_id)})
    assert staff.status_code == 422
    assert staff.json()["error"]["code"] == "ban_refused"
    unknown = await client.post(USERS, json={"user_id": str(uuid4())})
    assert unknown.status_code == 404
    # None of the refusals wrote a ban.
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(UserBan).where(
                UserBan.user_id.in_([platform_admin.admin_id, platform_support.admin_id])
            )
        )
        assert rows.scalars().all() == []


async def test_a_reason_over_the_limit_is_a_422(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    member, _pw = await _member(org_admin)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(USERS, json={"user_id": str(member.id), "reason": "x" * 2001})
    assert resp.status_code == 422


async def test_an_admin_bans_and_lifts_a_domain_normalized(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    domain = _domain()
    covered, covered_pw = await _member(org_admin, email=f"c@{domain}")
    await login(client, platform_admin.admin_email, platform_admin.admin_password)

    created = await client.post(DOMAINS, json={"domain": f"  @{domain.upper()} ", "reason": "farm"})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["domain"] == domain
    assert row["reason"] == "farm"
    assert row["created_by_email"] == platform_admin.admin_email
    assert row["active"] is True

    assert (await client.post(DOMAINS, json={"domain": domain})).status_code == 409
    invalid = await client.post(DOMAINS, json={"domain": "not a domain"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"

    users = {r["id"]: r for r in (await client.get("/admin/v1/users")).json()}
    assert users[str(covered.id)]["banned"] is True
    assert users[str(covered.id)]["ban_reason"] == "farm"

    other = _fresh_client(client)
    refused = await other.post(
        "/api/v1/auth/login", json={"email": covered.email, "password": covered_pw}
    )
    assert refused.status_code == 401

    assert (await client.get(DOMAINS)).json()[0]["domain"] == domain
    assert (await client.delete(f"{DOMAINS}/{domain.upper()}")).status_code == 204
    assert (await client.delete(f"{DOMAINS}/{domain}")).status_code == 404
    assert (await client.delete(f"{DOMAINS}/not-a-domain")).status_code == 404
    listed = {r["domain"]: r for r in (await client.get(DOMAINS)).json()}
    assert listed[domain]["active"] is False
    assert listed[domain]["lifted_by_email"] == platform_admin.admin_email

    allowed = await other.post(
        "/api/v1/auth/login", json={"email": covered.email, "password": covered_pw}
    )
    assert allowed.status_code == 200, allowed.text


async def test_a_user_ban_wins_over_a_domain_ban_in_the_users_list(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    domain = _domain()
    member, _pw = await _member(org_admin, email=f"m@{domain}")
    await _ban_domain_row(domain, reason="domain reason")
    await _ban_user_row(member.id, reason="account reason")
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    users = {r["id"]: r for r in (await client.get("/admin/v1/users")).json()}
    assert users[str(member.id)] == {
        **users[str(member.id)],
        "banned": True,
        "ban_reason": "account reason",
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", USERS, None, id="list-users"),
        pytest.param("POST", USERS, {"user_id": str(uuid4())}, id="ban-user"),
        pytest.param("DELETE", f"{USERS}/{uuid4()}", None, id="lift-user"),
        pytest.param("GET", DOMAINS, None, id="list-domains"),
        pytest.param("POST", DOMAINS, {"domain": "example.com"}, id="ban-domain"),
        pytest.param("DELETE", f"{DOMAINS}/example.com", None, id="lift-domain"),
    ],
)
async def test_platform_support_is_refused_by_the_policy_and_the_deny_is_recorded(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    before = len(await _decisions(platform_support.org_id))
    resp = await client.request(method, path, json=body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["message"] == "Platform admin role required"
    rows = await _decisions(platform_support.org_id)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.payload["effect"] == "deny"
    assert row.payload["reason"] == "platform_admin_required"
    assert row.payload["policy"] == "platform.ban"
    assert row.payload["attrs"]["platform_staff"] is True
    assert row.payload["attrs"]["platform_admin"] is False
    assert row.visibility == "platform"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", USERS, id="list-users"),
        pytest.param("POST", USERS, id="ban-user"),
        pytest.param("GET", DOMAINS, id="list-domains"),
    ],
)
async def test_an_org_admin_never_reaches_the_ban_routes(
    client: AsyncClient, org_admin: OrgWithAdmin, method: str, path: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.request(method, path, json={"user_id": str(uuid4())})
    assert resp.status_code == 403
    assert await _decisions(org_admin.org_id) == []


async def test_an_admins_ban_leaves_an_allow_row_an_audit_row_and_both_trails(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    member, _pw = await _member(org_admin)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    before = len(await _decisions(platform_admin.org_id))
    resp = await client.post(USERS, json={"user_id": str(member.id), "reason": "abuse"})
    assert resp.status_code == 201

    rows = await _decisions(platform_admin.org_id)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.payload["effect"] == "allow"
    assert row.payload["reason"] == "admin_changes"
    assert row.payload["policy"] == "platform.ban"
    assert row.payload["action"] == "admin"
    assert row.entity_id == str(member.id)
    assert row.payload["attrs"] == {
        "platform_staff": True,
        "platform_admin": True,
        "kind": "user",
        "operation": "ban",
    }
    assert [(link["kind"], link["id"]) for link in row.actor["chain"]] == [
        ("user", str(platform_admin.admin_id))
    ]

    # The org's chain says what Alkera did to its member and who did it; the
    # reason is the platform's and stays in the person's own security log.
    trail = await _org_audit(org_admin.org_id, "platform.user_banned")
    assert [e.target for e in trail] == [member.email]
    assert trail[0].actor_email == platform_admin.admin_email
    assert "reason" not in (trail[0].detail or {})
    assert trail[0].detail is not None
    assert trail[0].detail["actor"]["chain"][0]["id"] == str(platform_admin.admin_id)
    async with AsyncSessionLocal() as session:
        recorded = (
            (
                await session.execute(
                    select(IdentitySecurityEvent).where(IdentitySecurityEvent.user_id == member.id)
                )
            )
            .scalars()
            .all()
        )
    assert [(e.event, (e.detail or {}).get("reason")) for e in recorded] == [
        ("platform.user_banned", "abuse")
    ]

    assert (await client.delete(f"{USERS}/{member.id}")).status_code == 204
    lifted_trail = await _org_audit(org_admin.org_id, "platform.user_ban_lifted")
    assert [e.target for e in lifted_trail] == [member.email]
    async with AsyncSessionLocal() as session:
        lifted = await session.scalar(
            select(func.count())
            .select_from(IdentitySecurityEvent)
            .where(
                IdentitySecurityEvent.user_id == member.id,
                IdentitySecurityEvent.event == "platform.user_ban_lifted",
            )
        )
    assert lifted == 1

    page = (await client.get("/admin/v1/audit-logs?page_size=50")).json()
    actions = {(e["action"], e["method"]) for e in page["items"]}
    assert ("ban_user", "POST") in actions
    assert ("lift_user_ban", "DELETE") in actions
    banned_entry = next(e for e in page["items"] if e["action"] == "ban_user")
    assert banned_entry["actor_email"] == platform_admin.admin_email
    assert banned_entry["detail"]["body"]["user_id"] == str(member.id)


async def test_a_ban_revokes_live_sessions_and_a_lift_restores_the_untouched_credentials(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    member, pw = await _member(org_admin)
    session_client = _fresh_client(client)
    await login(session_client, member.email, pw)
    pat = await _pat_for(org_admin, member.id)
    # A client of its own: a cookie wins over a Bearer header, so the token
    # must be presented without the admin's session beside it.
    pat_client = _fresh_client(client)
    assert (await session_client.get("/api/v1/auth/me")).status_code == 200
    assert (await pat_client.get(f"{_PREFIX}/principal", headers=_bearer(pat))).status_code == 200

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.post(USERS, json={"user_id": str(member.id)})).status_code == 201

    while_banned = await session_client.get("/api/v1/auth/me")
    assert while_banned.status_code == 401
    assert while_banned.json()["error"]["message"] == "user no longer exists"
    assert (await pat_client.get(f"{_PREFIX}/principal", headers=_bearer(pat))).status_code == 401

    assert (await client.delete(f"{USERS}/{member.id}")).status_code == 204

    # The personal access token was never revoked: it works again with no re-login.
    restored = await pat_client.get(f"{_PREFIX}/principal", headers=_bearer(pat))
    assert restored.status_code == 200 and restored.json()["subject_id"] == str(member.id)
    # The cookie session was revoked by the ban, as a logout-all would have; a
    # fresh login is what restores it.
    revoked = await session_client.get("/api/v1/auth/me")
    assert revoked.status_code == 401
    assert revoked.json()["error"]["message"].startswith("session revoked")
    await login(session_client, member.email, pw)
    assert (await session_client.get("/api/v1/auth/me")).status_code == 200


async def test_a_domain_ban_revokes_the_sessions_of_every_account_at_the_domain(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    domain = _domain()
    one, one_pw = await _member(org_admin, email=f"one@{domain}")
    two, two_pw = await _member(org_admin, email=f"two@{domain}")
    one_client, two_client = _fresh_client(client), _fresh_client(client)
    await login(one_client, one.email, one_pw)
    await login(two_client, two.email, two_pw)

    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.post(DOMAINS, json={"domain": domain})).status_code == 201
    assert (await one_client.get("/api/v1/auth/me")).status_code == 401
    assert (await two_client.get("/api/v1/auth/me")).status_code == 401

    assert (await client.delete(f"{DOMAINS}/{domain}")).status_code == 204
    # Revoked stays revoked; the accounts themselves are back.
    assert (await one_client.get("/api/v1/auth/me")).status_code == 401
    await login(one_client, one.email, one_pw)
    assert (await one_client.get("/api/v1/auth/me")).status_code == 200
