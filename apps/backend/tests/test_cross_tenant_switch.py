"""Switching a browser between a person's orgs: ``POST /auth/refresh/org``.

The subject is ``two_org_identity``: a person whose home is org A who also
holds a membership in org B. A browser signed into A switches to B through the
real route, against real Postgres. The invariants:

* the switch moves the family into B, mints an access token for the B
  membership, and revokes the access token the browser held for A, so a socket
  or stream opened on it closes on its next recheck;
* each org's audit chain records its own side of the move, and neither names
  the other org;
* the org in the body is only a choice among the caller's own active
  memberships: a stranger org and a missing org get byte-identical 404s, a
  deactivated membership a 404, and with multi-org off the second org is
  refused;
* the refresh cookie and the CSRF header are both required, and an access
  cookie for another identity is refused;
* an org that enforces SSO answers 409 with its sign-in URL until the family
  holds a fresh grant from its IdP, and nothing rotates before that;
* replaying the pre-switch refresh token ends the family, as on the refresh
  route.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from alkera_core.auth import SessionClaims, decode_session_token
from alkera_core.auth.tenancy import ORG_HEADER
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    AuthRefreshToken,
    AuthToken,
    IdentitySecurityEvent,
    MembershipStatus,
    OrgAuditEvent,
    OrgMembership,
    SsoConnection,
    User,
)
from alkera_core.utils.email import email_domain
from backend.api.return_path import safe_return_path
from backend.api.routes.realtime import events as events_route
from backend.auth.session_issue import issue_session, record_grant
from backend.services.realtime.docsync import DocRegistry
from backend.services.realtime.filters import EntitlementRef, load_entitlements
from backend.services.realtime.runtime import RealtimeRuntime
from backend.services.realtime.session import SocketSession
from fastapi import Request, Response, WebSocket
from sqlalchemy import select, update
from tests.conftest import TwoOrg, app_client, hold_sso_domains

REFRESH_COOKIE = settings.auth_refresh_cookie_name
ACCESS_COOKIE = settings.auth_cookie_name
CSRF = {"X-Requested-With": "alkera"}


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


@dataclass
class Browser:
    access: str
    refresh: str
    family_id: UUID

    def cookies(self, *, access: str | None = None) -> str:
        return f"{ACCESS_COOKIE}={access or self.access}; {REFRESH_COOKIE}={self.refresh}"


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "query_string": b"",
        }
    )


def _cookie(carrier: Response | httpx.Response, name: str) -> str:
    if isinstance(carrier, httpx.Response):
        return carrier.cookies[name]
    for key, value in carrier.raw_headers:
        decoded = value.decode()
        if key == b"set-cookie" and decoded.startswith(f"{name}="):
            return decoded.split(";")[0].split("=", 1)[1]
    raise AssertionError(f"no {name} cookie was set")


async def _browser(user_id: UUID, org: UUID) -> Browser:
    """A password sign-in into ``org``, minted the way every sign-in route mints one."""
    carrier = Response()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        claims = await issue_session(
            db, user, request=_request(), response=carrier, method="password", org_team_id=org
        )
        await db.commit()
        family = await db.scalar(
            select(AuthRefreshToken.family_id).where(AuthRefreshToken.access_jti == claims.jti)
        )
    assert family is not None
    return Browser(
        access=_cookie(carrier, ACCESS_COOKIE),
        refresh=_cookie(carrier, REFRESH_COOKIE),
        family_id=family,
    )


async def _switch(
    org: UUID | str,
    *,
    cookie: str | None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {**CSRF, **(headers or {})}
    if cookie is not None:
        sent["Cookie"] = cookie
    async with app_client() as c:
        return await c.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(org)}, headers=sent
        )


def _error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    assert isinstance(body, dict) and isinstance(body.get("error"), dict), body
    return dict(body["error"])


async def _family_rows(family: UUID) -> list[AuthRefreshToken]:
    async with AsyncSessionLocal() as db:
        return list(
            (await db.execute(select(AuthRefreshToken).where(AuthRefreshToken.family_id == family)))
            .scalars()
            .all()
        )


async def _revoked(jti: str) -> bool:
    async with AsyncSessionLocal() as db:
        return (
            await db.scalar(select(AuthToken.revoked_at).where(AuthToken.jti == jti))
        ) is not None


async def _switched_rows(user: User, org: UUID) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org,
                        OrgAuditEvent.actor_id == user.id,
                        OrgAuditEvent.action == "auth.org_switched",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _enforce_sso(org: UUID, *, domain: str) -> None:
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(select(SsoConnection).where(SsoConnection.org_team_id == org))
        if conn is None:
            conn = SsoConnection(org_team_id=org, protocol="oidc")
            db.add(conn)
        conn.enabled, conn.enforced = True, True
        conn.oidc_issuer, conn.oidc_client_id = "https://idp.example.com", "cid"
        await db.commit()
    await hold_sso_domains(org, domain)


class _SocketEnd:
    async def send_text(self, _text: str) -> None:
        return None


async def _entitlements(user_id: UUID, org_id: UUID) -> tuple[User, EntitlementRef]:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        return user, EntitlementRef(await load_entitlements(db, user, org_id=org_id))


async def _socket(user_id: UUID, claims: SessionClaims) -> SocketSession:
    user, ref = await _entitlements(user_id, claims.org_team_id)
    return SocketSession(
        websocket=cast(WebSocket, _SocketEnd()),
        user=user,
        claims=claims,
        peer_id="peer-switch",
        runtime=cast(RealtimeRuntime, None),
        registry=cast(DocRegistry, None),
        ref=ref,
    )


# --- the happy path ----------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_switching_moves_the_family_into_b_and_retires_the_a_token(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    browser = await _browser(t.user.id, t.org_a)
    old = decode_session_token(browser.access)
    socket = await _socket(t.user.id, old)
    _, ref = await _entitlements(t.user.id, t.org_a)
    assert await socket._recheck() is True
    assert await events_route._recheck(old, t.user.id, ref) is True

    resp = await _switch(t.org_b, cookie=browser.cookies())

    assert resp.status_code == 200, resp.text
    new = decode_session_token(_cookie(resp, ACCESS_COOKIE))
    assert (new.org_team_id, new.membership_id) == (t.org_b, t.membership_b.id)
    assert resp.headers[ORG_HEADER] == str(t.org_b)
    body = resp.json()
    assert body["user"]["org_team_id"] == str(t.org_b)
    rows = await _family_rows(browser.family_id)
    assert rows and all(row.active_org_team_id == t.org_b for row in rows)
    assert all(row.revoked_at is None for row in rows), "the family lives on"
    assert old.jti is not None and await _revoked(old.jti)
    assert not await _revoked(cast(str, new.jti))
    # Whatever was opened on the A token stops at its next recheck.
    assert await socket._recheck() is False
    assert await events_route._recheck(old, t.user.id, ref) is False
    # The old access cookie is refused by every door from now on.
    async with app_client() as c:
        stale = await c.get(
            "/api/v1/auth/me", headers={"Cookie": f"{ACCESS_COOKIE}={browser.access}"}
        )
    assert stale.status_code == 401


@pytest.mark.usefixtures("multi_org")
async def test_each_org_records_its_own_side_and_never_names_the_other(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    browser = await _browser(t.user.id, t.org_a)
    resp = await _switch(t.org_b, cookie=browser.cookies())
    assert resp.status_code == 200, resp.text
    left, entered = await _switched_rows(t.user, t.org_a), await _switched_rows(t.user, t.org_b)
    assert [r.detail and r.detail.get("switched") for r in left] == ["out"]
    assert [r.detail and r.detail.get("switched") for r in entered] == ["in"]
    assert str(t.org_b) not in str(left[0].detail)
    assert str(t.org_a) not in str(entered[0].detail)
    # The actor chain names the org each row is in.
    assert left[0].detail is not None and entered[0].detail is not None
    assert left[0].detail["actor"]["acting"]["org_id"] == str(t.org_a)
    assert entered[0].detail["actor"]["acting"]["org_id"] == str(t.org_b)
    async with AsyncSessionLocal() as db:
        events = (
            (
                await db.execute(
                    select(IdentitySecurityEvent).where(
                        IdentitySecurityEvent.user_id == t.user.id,
                        IdentitySecurityEvent.event == "auth.org_switched",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [e.org_team_id for e in events] == [t.org_b]


@pytest.mark.usefixtures("multi_org")
async def test_me_after_the_switch_reports_b_and_the_role_there(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    # An admin in B and a plain member in A: the role must follow the switch.
    async with AsyncSessionLocal() as db:
        from alkera_core.models import TeamRole
        from backend.services.org import memberships as membership_service

        seat = await membership_service.get(db, team_id=t.org_b, user_id=t.user.id)
        assert seat is not None
        await membership_service.change_role(db, seat, TeamRole.ADMIN)
        await db.commit()
    browser = await _browser(t.user.id, t.org_a)
    before = await _me(browser.access)
    assert (before["org_team_id"], before["org_role"]) == (str(t.org_a), "member")
    resp = await _switch(t.org_b, cookie=browser.cookies())
    assert resp.status_code == 200, resp.text
    after = await _me(_cookie(resp, ACCESS_COOKIE))
    assert (after["org_team_id"], after["org_name"], after["org_role"]) == (
        str(t.org_b),
        await _org_name(t.org_b),
        "admin",
    )
    assert after["membership_count"] == 2


async def _me(access: str) -> dict[str, Any]:
    async with app_client() as c:
        me = await c.get("/api/v1/auth/me", headers={"Cookie": f"{ACCESS_COOKIE}={access}"})
    assert me.status_code == 200, me.text
    return dict(me.json())


async def _org_name(org: UUID) -> str:
    from alkera_core.models import Team

    async with AsyncSessionLocal() as db:
        team = await db.get(Team, org)
        assert team is not None
        return team.name


# --- refusals ----------------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_a_switch_without_the_refresh_cookie_is_a_401(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    browser = await _browser(t.user.id, t.org_a)
    resp = await _switch(t.org_b, cookie=f"{ACCESS_COOKIE}={browser.access}")
    assert resp.status_code == 401
    assert _error(resp)["code"] == "unauthorized"


@pytest.mark.usefixtures("multi_org")
async def test_a_switch_without_the_csrf_header_is_a_403_and_moves_nothing(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    browser = await _browser(t.user.id, t.org_a)
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/auth/refresh/org",
            json={"org_team_id": str(t.org_b)},
            headers={"Cookie": browser.cookies()},
        )
    assert resp.status_code == 403
    assert _error(resp)["code"] == "csrf"
    assert all(r.active_org_team_id == t.org_a for r in await _family_rows(browser.family_id))


@pytest.mark.usefixtures("multi_org")
async def test_an_access_cookie_of_another_identity_is_a_401(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    browser = await _browser(t.user.id, t.org_a)
    stranger = await _browser(t.admin_b.id, t.org_b)
    resp = await _switch(t.org_b, cookie=browser.cookies(access=stranger.access))
    assert resp.status_code == 401
    assert all(r.active_org_team_id == t.org_a for r in await _family_rows(browser.family_id))


@pytest.mark.usefixtures("multi_org")
async def test_a_stranger_org_and_a_missing_org_answer_byte_identical_404s(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    browser = await _browser(t.user.id, t.org_a)
    trace = {"X-Request-ID": "switch-probe"}
    stranger = await _switch(await _stranger_org(), cookie=browser.cookies(), headers=trace)
    missing = await _switch(uuid4(), cookie=browser.cookies(), headers=trace)
    assert (stranger.status_code, missing.status_code) == (404, 404)
    assert stranger.content == missing.content
    assert _error(stranger).get("details") is None
    # Nothing rotated: the browser's refresh token still works.
    assert all(r.used_at is None for r in await _family_rows(browser.family_id))


@pytest.mark.usefixtures("multi_org")
async def test_a_deactivated_membership_is_a_404(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership)
            .where(OrgMembership.id == t.membership_b.id)
            .values(status=MembershipStatus.DEACTIVATED)
        )
        await db.commit()
    browser = await _browser(t.user.id, t.org_a)
    resp = await _switch(t.org_b, cookie=browser.cookies())
    assert resp.status_code == 404


async def test_with_multi_org_off_a_second_org_is_refused(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    assert settings.multi_org_enabled is False
    browser = await _browser(t.user.id, t.org_a)
    resp = await _switch(t.org_b, cookie=browser.cookies())
    assert resp.status_code == 404
    assert all(r.active_org_team_id == t.org_a for r in await _family_rows(browser.family_id))
    # Switching to the home org is still a no-op switch that works.
    home = await _switch(t.org_a, cookie=browser.cookies())
    assert home.status_code == 200, home.text
    assert decode_session_token(_cookie(home, ACCESS_COOKIE)).org_team_id == t.org_a


async def _stranger_org() -> UUID:
    from backend.services.org import teams as team_service
    from tests.conftest import _unique_email, _unique_org_name

    async with AsyncSessionLocal() as db:
        org, _admin = await team_service.create_org_with_admin(
            db,
            org_name=_unique_org_name(),
            admin_email=_unique_email("stranger"),
            admin_first_name="Stranger",
            admin_last_name="Admin",
            admin_password="admin-pass-12345",
        )
        await db.commit()
        return org.id


# --- step-up ----------------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_an_sso_org_is_a_409_until_the_family_holds_its_grant(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _enforce_sso(t.org_b, domain=email_domain(t.user.email))
    browser = await _browser(t.user.id, t.org_a)

    refused = await _switch(t.org_b, cookie=browser.cookies())

    assert refused.status_code == 409, refused.text
    error = _error(refused)
    assert error["code"] == "sso_required"
    url = urlsplit(error["details"]["login_url"])
    assert url.path == f"/api/v1/auth/sso/{t.org_b}/login"
    back = parse_qs(url.query)["return_to"][0]
    assert safe_return_path(back) == back
    assert parse_qs(urlsplit(back).query) == {"switch_org": [str(t.org_b)]}
    rows = await _family_rows(browser.family_id)
    assert all(r.active_org_team_id == t.org_a and r.used_at is None for r in rows)
    assert not await _revoked(cast(str, decode_session_token(browser.access).jti))

    async with AsyncSessionLocal() as db:
        await record_grant(
            db, family_id=browser.family_id, org_team_id=t.org_b, method="sso", at=datetime.now(UTC)
        )
        await db.commit()
    allowed = await _switch(t.org_b, cookie=browser.cookies())
    assert allowed.status_code == 200, allowed.text
    assert decode_session_token(_cookie(allowed, ACCESS_COOKIE)).org_team_id == t.org_b


# --- reuse ----------------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_replaying_the_pre_switch_refresh_token_ends_the_family(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = two_org_identity
    monkeypatch.setattr(settings, "auth_refresh_reuse_grace_seconds", 0)
    browser = await _browser(t.user.id, t.org_a)
    switched = await _switch(t.org_b, cookie=browser.cookies())
    assert switched.status_code == 200, switched.text
    new_access = _cookie(switched, ACCESS_COOKIE)

    async with app_client() as c:
        replay = await c.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={browser.refresh}"}
        )
    assert replay.status_code == 401
    assert _error(replay)["code"] == "refresh_token_reused"
    rows = await _family_rows(browser.family_id)
    assert rows and all(r.revoked_at is not None for r in rows)
    assert await _revoked(cast(str, decode_session_token(new_access).jti))


@pytest.mark.usefixtures("multi_org")
async def test_replaying_it_into_the_switch_route_ends_the_family_too(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = two_org_identity
    monkeypatch.setattr(settings, "auth_refresh_reuse_grace_seconds", 0)
    browser = await _browser(t.user.id, t.org_a)
    assert (await _switch(t.org_b, cookie=browser.cookies())).status_code == 200
    replay = await _switch(t.org_a, cookie=f"{REFRESH_COOKIE}={browser.refresh}")
    assert replay.status_code == 401
    assert all(r.revoked_at is not None for r in await _family_rows(browser.family_id))
