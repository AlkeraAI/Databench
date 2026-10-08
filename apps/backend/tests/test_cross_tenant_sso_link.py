"""Two orgs, one existing identity: an org's single sign-on and SCIM may offer
the identity a place in the org, and only the identity can accept it.

The world: an identity U whose home is org A, with an email in a domain org B's
IdP claims. Org B's IdP (OIDC through the real callback route, its network
edge faked; SAML with a really signed response) and org B's SCIM token are the
attacker's tools. Every attachment must wait for U, signed in with one of U's
own methods, and every door is inert while multi-org is off.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from alkera_core.auth import decode_oauth_state
from alkera_core.auth.tenancy import MembershipRefused
from alkera_core.authz import ActingContext
from alkera_core.authz.roles import RoleResolver
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    IdentitySecurityEvent,
    MembershipStatus,
    OAuthIdentity,
    OrgAuditEvent,
    OrgMembership,
    SsoConnection,
    SsoLinkRequest,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from backend.api.routes.identity.sso import SSO_LINK_COOKIE, SSO_LINK_COOKIE_PATH
from backend.auth.membership_tokens import mint_for_membership
from backend.auth.oauth.profile import FederatedProfile
from backend.services.identity import sso as sso_service
from backend.services.identity.sso_link import token_hash
from backend.services.org import invitations as invitation_service
from backend.services.org import org_memberships
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service
from freezegun import freeze_time
from sqlalchemy import func, select
from tests.conftest import app_client, hold_sso_domains, login, make_member

pytestmark = pytest.mark.asyncio

CSRF = {"X-Requested-With": "alkera"}
LINK = "/api/v1/auth/sso-link"


@pytest.fixture(autouse=True)
def _enterprise_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO and SCIM are Enterprise features; a self-hosted install has them."""
    monkeypatch.setattr(settings, "self_hosted", True)


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


# --------------------------------------------------------------------------- #
# the world
# --------------------------------------------------------------------------- #


@dataclass
class World:
    domain: str
    org_a: UUID
    org_b: UUID
    org_b_name: str
    admin_b: User
    admin_b_password: str
    user: User
    password: str
    scim_token: str
    profiles: dict[str, FederatedProfile] = field(default_factory=dict)


class _FakeIdp:
    """The org's IdP at its network edge: hands back the profile the test
    scripted for the code it is given. Everything past the edge is real."""

    def __init__(self, key: str, profiles: dict[str, FederatedProfile]) -> None:
        self.key = key
        self._profiles = profiles

    async def authorization_url(self, *, redirect_uri: str, state: str, nonce: str) -> str:
        return f"https://idp.example.com/authorize?state={state}"

    async def fetch_profile(self, *, code: str, redirect_uri: str, nonce: str) -> FederatedProfile:
        return self._profiles[code]


@pytest_asyncio.fixture
async def world(real_session: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    domain = f"acme-{secrets.token_hex(4)}.example.com"
    _org_a, admin_a = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Org A {secrets.token_hex(3)}",
        admin_email=f"admin-a-{secrets.token_hex(4)}@alkera.dev",
        admin_first_name="Admin",
        admin_last_name="A",
        admin_password="admin-pass-12345",
    )
    org_b_name = f"Org B {secrets.token_hex(3)}"
    _org_b, admin_b = await team_service.create_org_with_admin(
        real_session,
        org_name=org_b_name,
        admin_email=f"admin-b-{secrets.token_hex(4)}@alkera.dev",
        admin_first_name="Admin",
        admin_last_name="B",
        admin_password="admin-pass-12345",
    )
    await real_session.commit()
    org_a, org_b = admin_a.home_org_team_id, admin_b.home_org_team_id
    user, password = await make_member(
        real_session,
        org_id=org_a,
        email=f"u-{secrets.token_hex(4)}@{domain}",
        first_name="Una",
        last_name="Original",
        verified=True,
    )
    assert password is not None
    real_session.add(
        SsoConnection(
            org_team_id=org_b,
            protocol="oidc",
            enabled=True,
            oidc_issuer="https://idp.example.com",
            oidc_client_id="cid",
        )
    )
    await real_session.flush()
    _conn, scim_token = await sso_service.mint_scim_token(real_session, org_b)
    await real_session.commit()
    await hold_sso_domains(org_b, domain)
    profiles: dict[str, FederatedProfile] = {}
    monkeypatch.setattr(
        sso_service,
        "build_oidc_provider",
        lambda conn: _FakeIdp(sso_service.provider_key(conn.org_team_id), profiles),
    )
    yield World(
        domain=domain,
        org_a=org_a,
        org_b=org_b,
        org_b_name=org_b_name,
        admin_b=admin_b,
        admin_b_password="admin-pass-12345",
        user=user,
        password=password,
        scim_token=scim_token,
        profiles=profiles,
    )


def _profile(w: World, *, email: str | None = None, subject: str | None = None) -> FederatedProfile:
    return FederatedProfile(
        provider=sso_service.provider_key(w.org_b),
        subject=subject or f"sub-{uuid.uuid4().hex}",
        email=email or w.user.email,
        email_verified=True,
        first_name="Idp",
        last_name="Asserted",
    )


async def _sso_callback(
    client: httpx.AsyncClient, w: World, profile: FederatedProfile
) -> httpx.Response:
    """Org B's OIDC sign-in, start to callback, through the real routes."""
    start = await client.get(f"/api/v1/auth/sso/{w.org_b}/login", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = decode_oauth_state(client.cookies["alkera_oauth_tx"]).state
    code = f"code-{uuid.uuid4().hex}"
    w.profiles[code] = profile
    return await client.get(
        f"/api/v1/auth/sso/{w.org_b}/login/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def _location(resp: httpx.Response) -> tuple[str, dict[str, list[str]]]:
    url = urlsplit(resp.headers["location"])
    return url.path, parse_qs(url.query)


def _link_cookie_header(resp: httpx.Response) -> str:
    found = [v for k, v in resp.headers.multi_items() if k == "set-cookie" and SSO_LINK_COOKIE in v]
    assert len(found) == 1, found
    return found[0]


async def _parked(client: httpx.AsyncClient, w: World, profile: FederatedProfile) -> str:
    """Drive org B's sign-in until it parks; return the cookie's raw value."""
    resp = await _sso_callback(client, w, profile)
    assert resp.status_code == 302
    path, query = _location(resp)
    assert path == "/link-sso", resp.headers["location"]
    assert query == {"org": [str(w.org_b)]}
    return str(client.cookies[SSO_LINK_COOKIE])


async def _links(user_id: UUID, provider: str | None = None) -> list[OAuthIdentity]:
    async with AsyncSessionLocal() as db:
        stmt = select(OAuthIdentity).where(OAuthIdentity.user_id == user_id)
        if provider is not None:
            stmt = stmt.where(OAuthIdentity.provider == provider)
        return list((await db.execute(stmt)).scalars().all())


async def _membership(user_id: UUID, org: UUID) -> OrgMembership | None:
    async with AsyncSessionLocal() as db:
        return await org_membership_service.get(db, user_id=user_id, org_team_id=org)


async def _seats(user_id: UUID, org: UUID) -> list[TeamMembership]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(TeamMembership).where(
                        TeamMembership.user_id == user_id, TeamMembership.org_team_id == org
                    )
                )
            )
            .scalars()
            .all()
        )


async def _requests(org: UUID) -> list[SsoLinkRequest]:
    async with AsyncSessionLocal() as db:
        return list(
            (await db.execute(select(SsoLinkRequest).where(SsoLinkRequest.org_team_id == org)))
            .scalars()
            .all()
        )


async def _identity(user_id: UUID) -> User:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        return user


async def _signed_in(w: World) -> httpx.AsyncClient:
    client = app_client()
    await login(client, w.user.email, w.password)
    return client


async def _pend(w: World, user: User | None = None, **body: Any) -> httpx.Response:
    """Org B's SCIM creates ``user`` (U by default), who lives in org A."""
    target = user or w.user
    async with app_client(headers={"Authorization": f"Bearer {w.scim_token}"}) as c:
        return await c.post("/api/v1/scim/v2/Users", json={"userName": target.email, **body})


def _error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    assert isinstance(body, dict) and isinstance(body.get("error"), dict), body
    return dict(body["error"])


# --------------------------------------------------------------------------- #
# the callback parks and attaches nothing
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("multi_org")
async def test_the_callback_parks_the_assertion_and_attaches_nothing(world: World) -> None:
    w = world
    profile = _profile(w)
    async with app_client() as client:
        resp = await _sso_callback(client, w, profile)
        assert resp.status_code == 302
        assert _location(resp) == ("/link-sso", {"org": [str(w.org_b)]})
        raw = client.cookies[SSO_LINK_COOKIE]
        header = _link_cookie_header(resp).lower()
        # No session for anybody: the IdP alone signs nobody in.
        assert settings.auth_cookie_name not in client.cookies
    assert "httponly" in header and "samesite=lax" in header
    assert f"path={SSO_LINK_COOKIE_PATH}" in header
    assert "max-age=600" in header
    [parked] = await _requests(w.org_b)
    assert parked.token_hash == token_hash(raw) and parked.token_hash != raw
    assert (parked.provider, parked.subject) == (sso_service.provider_key(w.org_b), profile.subject)
    assert parked.email == w.user.email and parked.consumed_at is None
    assert await _links(w.user.id) == []
    assert await _membership(w.user.id, w.org_b) is None


async def test_with_multi_org_off_the_callback_still_refuses_the_identity(world: World) -> None:
    w = world
    async with app_client() as client:
        resp = await _sso_callback(client, w, _profile(w))
    assert _location(resp) == ("/login", {"oauth_error": ["sso_org_mismatch"]})
    assert SSO_LINK_COOKIE not in resp.cookies
    assert await _requests(w.org_b) == []
    assert await _links(w.user.id) == []


@pytest.mark.usefixtures("multi_org")
async def test_a_deactivated_membership_is_refused_not_offered_a_link(world: World) -> None:
    w = world
    async with AsyncSessionLocal() as db:
        membership = await org_membership_service.create(
            db, user_id=w.user.id, org_team_id=w.org_b, status=MembershipStatus.DEACTIVATED
        )
        await db.commit()
    assert membership.status is MembershipStatus.DEACTIVATED
    async with app_client() as client:
        resp = await _sso_callback(client, w, _profile(w))
    assert _location(resp) == ("/login", {"oauth_error": ["account_deactivated"]})
    assert await _requests(w.org_b) == []
    assert await _links(w.user.id) == []


@pytest.mark.usefixtures("multi_org")
async def test_a_new_email_is_still_jit_provisioned_into_the_idp_org(world: World) -> None:
    w = world
    email = f"newhire-{secrets.token_hex(3)}@{w.domain}"
    async with app_client() as client:
        resp = await _sso_callback(client, w, _profile(w, email=email))
    assert "oauth_error" not in resp.headers["location"]
    assert await _requests(w.org_b) == []
    async with AsyncSessionLocal() as db:
        created = await db.scalar(select(User).where(User.email == email))
        assert created is not None
        assert created.home_org_team_id == w.org_b


@pytest.mark.usefixtures("multi_org")
async def test_a_saml_assertion_parks_the_same_way(world: World) -> None:
    from tests.test_saml import _CERT_PEM, IDP_ENTITY, build_response

    w = world
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(select(SsoConnection).where(SsoConnection.org_team_id == w.org_b))
        assert conn is not None
        conn.protocol = "saml"
        conn.saml_entity_id = IDP_ENTITY
        conn.saml_sso_url = "https://idp/sso"
        conn.saml_x509_cert = _CERT_PEM
        await db.commit()
    sp = sso_service.saml_sp_config(w.org_b)
    async with app_client() as client:
        start = await client.get(f"/api/v1/auth/sso/{w.org_b}/saml/login", follow_redirects=False)
        assert start.status_code == 302
        request_id = decode_oauth_state(client.cookies["alkera_oauth_tx"]).state
        acs = await client.post(
            f"/api/v1/auth/sso/{w.org_b}/saml/acs",
            data={
                "SAMLResponse": build_response(
                    email=w.user.email,
                    audience=sp.entity_id,
                    recipient=sp.acs_url,
                    in_response_to=request_id,
                )
            },
            follow_redirects=False,
        )
        assert acs.status_code == 302
        assert _location(acs) == ("/link-sso", {"org": [str(w.org_b)]})
        assert client.cookies.get(SSO_LINK_COOKIE)
    assert len(await _requests(w.org_b)) == 1
    assert await _links(w.user.id) == []
    assert await _membership(w.user.id, w.org_b) is None


# --------------------------------------------------------------------------- #
# confirming
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("multi_org")
async def test_confirming_links_the_subject_and_without_an_offer_joins_nothing(
    world: World,
) -> None:
    w = world
    profile = _profile(w)
    client = await _signed_in(w)
    async with client:
        await _parked(client, w, profile)
        seen = await client.get(LINK)
        assert seen.status_code == 200, seen.text
        local = w.user.email.split("@")[0]
        assert seen.json() == {
            "org_team_id": str(w.org_b),
            "org_name": w.org_b_name,
            "email_masked": f"{local[0]}***@{w.domain}",
        }
        done = await client.post(f"{LINK}/confirm")
        assert done.status_code == 200, done.text
        assert done.json() == {
            "linked": True,
            "joined": False,
            "org_team_id": str(w.org_b),
            "message": f"Ask an admin of {w.org_b_name} to invite you.",
        }
        assert SSO_LINK_COOKIE not in client.cookies, "the cookie is cleared"
        # The A session keeps working: linking touched nothing of A's.
        assert (await client.get("/api/v1/auth/me")).status_code == 200
    [link] = await _links(w.user.id, sso_service.provider_key(w.org_b))
    assert link.subject == profile.subject
    assert await _membership(w.user.id, w.org_b) is None
    assert await _seats(w.user.id, w.org_b) == []
    [parked] = await _requests(w.org_b)
    assert parked.consumed_at is not None
    async with AsyncSessionLocal() as db:
        audit = await db.scalar(
            select(func.count())
            .select_from(OrgAuditEvent)
            .where(OrgAuditEvent.org_team_id == w.org_b, OrgAuditEvent.action == "auth.sso_linked")
        )
        security = await db.scalar(
            select(func.count())
            .select_from(IdentitySecurityEvent)
            .where(
                IdentitySecurityEvent.user_id == w.user.id,
                IdentitySecurityEvent.event == "auth.sso_linked",
            )
        )
        leaked_to_a = await db.scalar(
            select(func.count())
            .select_from(OrgAuditEvent)
            .where(OrgAuditEvent.org_team_id == w.org_a, OrgAuditEvent.action == "auth.sso_linked")
        )
    assert (audit, security, leaked_to_a) == (1, 1, 0)


@pytest.mark.usefixtures("multi_org")
async def test_a_pending_invitation_is_accepted_by_the_confirmation(world: World) -> None:
    w = world
    async with AsyncSessionLocal() as db:
        team_b = await db.get(Team, w.org_b)
        admin_b = await db.get(User, w.admin_b.id)
        assert team_b is not None and admin_b is not None
        invitation, auto, _raw = await invitation_service.create_invitation(
            db,
            team=team_b,
            email=w.user.email,
            role=TeamRole.MEMBER,
            invited_by=admin_b,
            org_team_id=w.org_b,
        )
        await db.commit()
    assert auto is False
    client = await _signed_in(w)
    async with client:
        await _parked(client, w, _profile(w))
        done = await client.post(f"{LINK}/confirm")
    assert done.status_code == 200, done.text
    assert done.json()["joined"] is True and done.json()["message"] is None
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.ACTIVE
    assert {s.team_id for s in await _seats(w.user.id, w.org_b)} == {w.org_b}
    async with AsyncSessionLocal() as db:
        refreshed = await invitation_service.get_by_id(db, invitation.id)
    assert refreshed is not None and refreshed.status.value == "accepted"


@pytest.mark.usefixtures("multi_org")
async def test_a_scim_pending_membership_parks_and_the_confirmation_activates_it(
    world: World,
) -> None:
    w = world
    assert (await _pend(w)).status_code == 201
    client = await _signed_in(w)
    async with client:
        await _parked(client, w, _profile(w))
        pending = await _membership(w.user.id, w.org_b)
        assert pending is not None and pending.status is MembershipStatus.PENDING
        done = await client.post(f"{LINK}/confirm")
    assert done.status_code == 200, done.text
    assert done.json()["joined"] is True
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.ACTIVE
    assert {s.team_id for s in await _seats(w.user.id, w.org_b)} == {w.org_b}


@pytest.mark.usefixtures("multi_org")
async def test_a_subject_already_linked_to_a_linked_identity_without_a_membership_parks_again(
    world: World,
) -> None:
    """The link is kept after a confirmation that joined nothing; the next
    sign-in through the org's IdP is parked again rather than signing in."""
    w = world
    profile = _profile(w)
    client = await _signed_in(w)
    async with client:
        await _parked(client, w, profile)
        assert (await client.post(f"{LINK}/confirm")).status_code == 200
    async with app_client() as browser:
        resp = await _sso_callback(browser, w, profile)
        assert _location(resp)[0] == "/link-sso"
        assert settings.auth_cookie_name not in browser.cookies
    assert await _membership(w.user.id, w.org_b) is None


# --------------------------------------------------------------------------- #
# refusals: nothing is created, nothing is consumed
# --------------------------------------------------------------------------- #


async def _assert_untouched(w: World) -> None:
    assert await _links(w.user.id) == []
    assert await _membership(w.user.id, w.org_b) is None
    assert all(r.consumed_at is None for r in await _requests(w.org_b))


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("method", ["GET", "confirm"])
@pytest.mark.parametrize(
    "cookie",
    [
        pytest.param("none", id="no-cookie"),
        pytest.param("forged", id="a-value-nobody-was-given"),
        pytest.param("another-browser", id="another-browsers-cookie"),
    ],
)
async def test_without_this_browsers_cookie_nothing_is_found(
    world: World, method: str, cookie: str
) -> None:
    w = world
    async with app_client() as victim_browser:
        victims_raw = await _parked(victim_browser, w, _profile(w))
    other = await make_member_in_domain(w)
    async with app_client() as other_browser:
        others_raw = await _parked(other_browser, w, _profile(w, email=other.email))
    assert victims_raw != others_raw
    client = await _signed_in(w)
    async with client:
        if cookie == "forged":
            client.cookies.set(
                SSO_LINK_COOKIE, secrets.token_urlsafe(32), path=SSO_LINK_COOKIE_PATH
            )
        elif cookie == "another-browser":
            # Somebody else's parked request: theirs is not U's to confirm.
            client.cookies.set(SSO_LINK_COOKIE, others_raw, path=SSO_LINK_COOKIE_PATH)
        resp = await client.get(LINK) if method == "GET" else await client.post(f"{LINK}/confirm")
    if cookie == "another-browser":
        assert resp.status_code == 409
        assert _error(resp)["code"] == "sso_link_other_account"
    else:
        assert resp.status_code == 404
    await _assert_untouched(w)
    assert await _links(other.id) == []


async def make_member_in_domain(w: World) -> User:
    async with AsyncSessionLocal() as db:
        other, _ = await make_member(
            db, org_id=w.org_a, email=f"o-{secrets.token_hex(4)}@{w.domain}", verified=True
        )
    return other


@pytest.mark.usefixtures("multi_org")
async def test_a_browser_signed_in_as_another_identity_is_told_and_links_nothing(
    world: World,
) -> None:
    w = world
    async with app_client() as client:
        await _parked(client, w, _profile(w))
        await login(client, w.admin_b.email, w.admin_b_password)
        seen = await client.get(LINK)
        done = await client.post(f"{LINK}/confirm")
    for resp in (seen, done):
        assert resp.status_code == 409
        assert _error(resp)["code"] == "sso_link_other_account"
    await _assert_untouched(w)
    assert await _links(w.admin_b.id) == []


@pytest.mark.usefixtures("multi_org")
async def test_the_cookie_alone_without_a_session_is_unauthorized(world: World) -> None:
    w = world
    async with app_client() as client:
        await _parked(client, w, _profile(w))
        for resp in (await client.get(LINK), await client.post(f"{LINK}/confirm")):
            assert resp.status_code == 401
    await _assert_untouched(w)


@pytest.mark.usefixtures("multi_org")
async def test_a_consumed_request_is_single_use(world: World) -> None:
    w = world
    client = await _signed_in(w)
    async with client:
        raw = await _parked(client, w, _profile(w))
        assert (await client.post(f"{LINK}/confirm")).status_code == 200
        client.cookies.set(SSO_LINK_COOKIE, raw, path=SSO_LINK_COOKIE_PATH)
        assert (await client.post(f"{LINK}/confirm")).status_code == 404
        assert (await client.get(LINK)).status_code == 404
        assert (await client.post(f"{LINK}/cancel")).status_code == 404
    assert len(await _links(w.user.id)) == 1


@pytest.mark.usefixtures("multi_org")
async def test_a_subject_linked_to_another_identity_is_refused(world: World) -> None:
    w = world
    profile = _profile(w)
    other = await make_member_in_domain(w)
    async with AsyncSessionLocal() as db:
        db.add(
            OAuthIdentity(
                user_id=other.id,
                provider=profile.provider,
                subject=profile.subject,
                email_at_link=other.email,
                email_verified=True,
            )
        )
        await db.commit()
    client = await _signed_in(w)
    async with client:
        # Park directly: the callback would sign the subject's owner in.
        raw = await _park_directly(w, profile)
        client.cookies.set(SSO_LINK_COOKIE, raw, path=SSO_LINK_COOKIE_PATH)
        resp = await client.post(f"{LINK}/confirm")
    assert resp.status_code == 409
    assert _error(resp)["code"] == "sso_subject_linked"
    await _assert_untouched(w)
    [held] = await _links(other.id)
    assert held.subject == profile.subject


@pytest.mark.usefixtures("multi_org")
async def test_a_second_subject_at_the_same_idp_is_refused(world: World) -> None:
    w = world
    async with AsyncSessionLocal() as db:
        db.add(
            OAuthIdentity(
                user_id=w.user.id,
                provider=sso_service.provider_key(w.org_b),
                subject="the-first-subject",
                email_at_link=w.user.email,
                email_verified=True,
            )
        )
        await db.commit()
    client = await _signed_in(w)
    async with client:
        client.cookies.set(
            SSO_LINK_COOKIE, await _park_directly(w, _profile(w)), path=SSO_LINK_COOKIE_PATH
        )
        resp = await client.post(f"{LINK}/confirm")
    assert resp.status_code == 409
    assert _error(resp)["code"] == "sso_provider_linked"
    assert [link.subject for link in await _links(w.user.id)] == ["the-first-subject"]
    assert await _membership(w.user.id, w.org_b) is None


async def _park_directly(w: World, profile: FederatedProfile) -> str:
    from backend.services.identity import sso_link

    async with AsyncSessionLocal() as db:
        raw = await sso_link.park(db, org_team_id=w.org_b, profile=profile)
        await db.commit()
    return raw


@pytest.mark.usefixtures("multi_org")
async def test_cancelling_consumes_the_request_and_links_nothing(world: World) -> None:
    w = world
    client = await _signed_in(w)
    async with client:
        raw = await _parked(client, w, _profile(w))
        cancelled = await client.post(f"{LINK}/cancel")
        assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": True}
        client.cookies.set(SSO_LINK_COOKIE, raw, path=SSO_LINK_COOKIE_PATH)
        assert (await client.post(f"{LINK}/confirm")).status_code == 404
    assert await _links(w.user.id) == []
    [parked] = await _requests(w.org_b)
    assert parked.consumed_at is not None


@pytest.mark.usefixtures("multi_org")
async def test_a_request_expires_after_ten_minutes_and_is_purged_by_the_next_park(
    world: World,
) -> None:
    w = world
    with freeze_time("2026-10-04 12:00:00", real_asyncio=True) as frozen:
        client = await _signed_in(w)
        async with client:
            raw = await _park_directly(w, _profile(w))
            client.cookies.set(SSO_LINK_COOKIE, raw, path=SSO_LINK_COOKIE_PATH)
            frozen.move_to("2026-10-04 12:09:59")
            assert (await client.get(LINK)).status_code == 200
            frozen.move_to("2026-10-04 12:10:00")
            assert (await client.get(LINK)).status_code == 404
            assert (await client.post(f"{LINK}/confirm")).status_code == 404
            assert [r.token_hash for r in await _requests(w.org_b)] == [token_hash(raw)]
            fresh = await _park_directly(w, _profile(w))
    assert [r.token_hash for r in await _requests(w.org_b)] == [token_hash(fresh)]
    assert await _links(w.user.id) == []


async def test_with_multi_org_off_every_link_route_finds_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setattr(settings, "multi_org_enabled", True)
    raw = await _park_directly(w, _profile(w))
    monkeypatch.setattr(settings, "multi_org_enabled", False)
    client = await _signed_in(w)
    async with client:
        client.cookies.set(SSO_LINK_COOKIE, raw, path=SSO_LINK_COOKIE_PATH)
        assert (await client.get(LINK)).status_code == 404
        assert (await client.post(f"{LINK}/confirm")).status_code == 404
        assert (await client.post(f"{LINK}/cancel")).status_code == 404
    await _assert_untouched(w)


# --------------------------------------------------------------------------- #
# SCIM: a pending membership for an identity that lives elsewhere
# --------------------------------------------------------------------------- #


async def test_with_multi_org_off_scim_answers_as_for_a_new_address_and_seats_nobody(
    world: World,
) -> None:
    """A refusal here told the org the address had an account elsewhere. The
    create is answered as any other is, and all it leaves is a pending
    membership nobody can take while multi-org is off."""
    w = world
    resp = await _pend(w)
    assert resp.status_code == 201, resp.text
    assert resp.json()["active"] is True
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.PENDING
    assert await _seats(w.user.id, w.org_b) == []
    home = await _membership(w.user.id, w.org_a)
    assert home is not None and home.status is MembershipStatus.ACTIVE


@pytest.mark.usefixtures("multi_org")
async def test_scim_creates_a_pending_membership_and_never_touches_the_identity(
    world: World,
) -> None:
    w = world
    async with AsyncSessionLocal() as db:
        identity = await db.get(User, w.user.id)
        assert identity is not None
        identity.mfa_enabled = True
        await db.commit()
    before = await _identity(w.user.id)
    client = await _signed_in(w)
    async with client:
        resp = await _pend(
            w,
            externalId="okta-1",
            displayName="Pushed Name",
            name={"givenName": "Pushed", "familyName": "Name"},
        )
        assert resp.status_code == 201, resp.text
        # The A session keeps working throughout.
        assert (await client.get("/api/v1/auth/me")).status_code == 200
    body = resp.json()
    assert body["id"] == str(w.user.id) and body["active"] is True
    assert body["externalId"] == "okta-1" and body["displayName"] == "Pushed Name"
    # The names are the ones the org's IdP pushed, never the identity's own.
    assert body["name"] == {"givenName": "Pushed", "familyName": "Name"}
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.PENDING
    assert (membership.scim_external_id, membership.display_name) == ("okta-1", "Pushed Name")
    after = await _identity(w.user.id)
    for attr in ("first_name", "last_name", "email", "password_hash", "mfa_enabled", "token_epoch"):
        assert getattr(after, attr) == getattr(before, attr), attr
    home = await _membership(w.user.id, w.org_a)
    assert home is not None and home.status is MembershipStatus.ACTIVE
    assert await _seats(w.user.id, w.org_b) == []


@pytest.mark.usefixtures("multi_org")
async def test_scim_creating_an_identity_elsewhere_already_inactive_creates_nothing(
    world: World,
) -> None:
    """Answered as creating a new address inactive is, with nothing left
    behind: what an IdP gets from creating the user and then deactivating."""
    resp = await _pend(world, active=False)
    assert resp.status_code == 201, resp.text
    assert resp.json()["active"] is False
    assert await _membership(world.user.id, world.org_b) is None
    assert await _seats(world.user.id, world.org_b) == []


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    "deprovision",
    [
        pytest.param({"op": "replace", "path": "active", "value": False}, id="patch-active-false"),
        pytest.param({"op": "remove", "path": "active"}, id="patch-remove-active"),
        pytest.param("delete", id="delete"),
    ],
)
async def test_scim_deprovisioning_a_pending_membership_deletes_it(
    world: World, deprovision: Any
) -> None:
    w = world
    assert (await _pend(w)).status_code == 201
    async with app_client(headers={"Authorization": f"Bearer {w.scim_token}"}) as c:
        if deprovision == "delete":
            resp = await c.delete(f"/api/v1/scim/v2/Users/{w.user.id}")
            assert resp.status_code == 204
        else:
            resp = await c.patch(
                f"/api/v1/scim/v2/Users/{w.user.id}", json={"Operations": [deprovision]}
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["active"] is False
        assert (await c.get(f"/api/v1/scim/v2/Users/{w.user.id}")).status_code == 404
    assert await _membership(w.user.id, w.org_b) is None
    home = await _membership(w.user.id, w.org_a)
    assert home is not None and home.status is MembershipStatus.ACTIVE


@pytest.mark.usefixtures("multi_org")
async def test_scim_cannot_activate_a_pending_membership_and_updates_only_its_fields(
    world: World,
) -> None:
    w = world
    assert (await _pend(w)).status_code == 201
    async with app_client(headers={"Authorization": f"Bearer {w.scim_token}"}) as c:
        patched = await c.patch(
            f"/api/v1/scim/v2/Users/{w.user.id}",
            json={
                "Operations": [
                    {"op": "replace", "path": "active", "value": True},
                    {"op": "replace", "path": "name.givenName", "value": "Given"},
                    {"op": "replace", "path": "externalId", "value": "ext-9"},
                ]
            },
        )
        replaced = await c.put(
            f"/api/v1/scim/v2/Users/{w.user.id}",
            json={"userName": w.user.email, "active": True, "externalId": "ext-10"},
        )
    assert patched.status_code == 200 and replaced.status_code == 200
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.PENDING
    # The part not pushed is not filled from the identity's own name.
    assert membership.display_name == "Given"
    assert membership.scim_external_id == "ext-10"
    identity = await _identity(w.user.id)
    assert (identity.first_name, identity.last_name) == ("Una", "Original")
    assert await _seats(w.user.id, w.org_b) == []


@pytest.mark.usefixtures("multi_org")
async def test_a_pending_membership_grants_nothing(world: World) -> None:
    w = world
    assert (await _pend(w)).status_code == 201
    async with AsyncSessionLocal() as db:
        user = await db.get(User, w.user.id)
        assert user is not None
        with pytest.raises(MembershipRefused):
            await mint_for_membership(db, user, w.org_b, kind="session")
        await db.rollback()
        resolver = RoleResolver(
            db,
            ActingContext.for_user(user_id=w.user.id, org_id=w.org_b, email=w.user.email),
            ancestor_chain=team_service.ancestor_chain,
        )
        assert (await resolver.for_team(w.org_b)).roles == frozenset()
        assert not await org_memberships.is_active_member(db, user_id=w.user.id, org_id=w.org_b)
    client = await _signed_in(w)
    async with client:
        switched = await client.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(w.org_b)}, headers=CSRF
        )
    assert switched.status_code == 404
    admin = app_client()
    await login(admin, w.admin_b.email, w.admin_b_password)
    async with admin:
        roster = await admin.get("/api/v1/org/members")
        directory = await admin.get("/api/v1/users")
        lookup = await admin.get(f"/api/v1/users/{w.user.id}")
        # An admin "reactivating" a pending membership does not seat the person.
        await admin.put(f"/api/v1/org/members/{w.user.id}/active", json={"active": True})
    assert roster.status_code == 200 and directory.status_code == 200
    assert str(w.user.id) not in roster.text and str(w.user.id) not in directory.text
    assert lookup.status_code == 404
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.PENDING
    assert await _seats(w.user.id, w.org_b) == []


# --------------------------------------------------------------------------- #
# the person's list, and joining
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("multi_org")
async def test_the_person_sees_the_pending_org_and_joining_lets_them_switch_into_it(
    world: World,
) -> None:
    w = world
    assert (await _pend(w)).status_code == 201
    client = await _signed_in(w)
    async with client:
        listed = await client.get("/api/v1/auth/memberships")
        assert listed.status_code == 200
        rows = {m["org_team_id"]: m for m in listed.json()["memberships"]}
        assert rows[str(w.org_a)]["status"] == "active"
        assert rows[str(w.org_b)]["status"] == "pending"
        assert rows[str(w.org_b)]["role"] == "member"
        assert rows[str(w.org_b)]["org_name"] == w.org_b_name

        joined = await client.post(
            "/api/v1/auth/memberships/join", json={"org_team_id": str(w.org_b)}
        )
        assert joined.status_code == 200, joined.text
        assert joined.json()["status"] == "active"
        assert joined.json()["org_team_id"] == str(w.org_b)

        switched = await client.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(w.org_b)}, headers=CSRF
        )
        assert switched.status_code == 200, switched.text
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.ACTIVE
    assert {s.team_id for s in await _seats(w.user.id, w.org_b)} == {w.org_b}
    async with AsyncSessionLocal() as db:
        joined_rows = await db.scalar(
            select(func.count())
            .select_from(OrgAuditEvent)
            .where(
                OrgAuditEvent.org_team_id == w.org_b, OrgAuditEvent.action == "membership.joined"
            )
        )
    assert joined_rows == 1


@pytest.mark.usefixtures("multi_org")
async def test_joining_anything_but_the_callers_own_pending_membership_is_not_found(
    world: World,
) -> None:
    w = world
    other = await make_member_in_domain(w)
    assert (await _pend(w, user=other)).status_code == 201
    client = await _signed_in(w)
    async with client:
        for target in (w.org_a, w.org_b, uuid.uuid4()):
            resp = await client.post(
                "/api/v1/auth/memberships/join", json={"org_team_id": str(target)}
            )
            assert resp.status_code == 404, (target, resp.text)
    # The other person's pending membership is still theirs, and still pending.
    theirs = await _membership(other.id, w.org_b)
    assert theirs is not None and theirs.status is MembershipStatus.PENDING
    assert await _membership(w.user.id, w.org_b) is None


@pytest.mark.usefixtures("multi_org")
async def test_joining_an_sso_enforced_org_answers_the_switch_step_up(world: World) -> None:
    w = world
    assert (await _pend(w, externalId="okta-2")).status_code == 201
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(select(SsoConnection).where(SsoConnection.org_team_id == w.org_b))
        assert conn is not None
        conn.enforced = True
        await db.commit()
    client = await _signed_in(w)
    async with client:
        listed = await client.get("/api/v1/auth/memberships")
        row = next(m for m in listed.json()["memberships"] if m["org_team_id"] == str(w.org_b))
        assert row["sso_required"] is True and row["status"] == "pending"
        resp = await client.post(
            "/api/v1/auth/memberships/join", json={"org_team_id": str(w.org_b)}
        )
    assert resp.status_code == 409
    error = _error(resp)
    assert error["code"] == "sso_required"
    assert urlsplit(error["details"]["login_url"]).path == f"/api/v1/auth/sso/{w.org_b}/login"
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.PENDING


async def test_with_multi_org_off_pending_rows_are_unlisted_and_joining_is_not_found(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setattr(settings, "multi_org_enabled", True)
    assert (await _pend(w)).status_code == 201
    monkeypatch.setattr(settings, "multi_org_enabled", False)
    client = await _signed_in(w)
    async with client:
        listed = await client.get("/api/v1/auth/memberships")
        joined = await client.post(
            "/api/v1/auth/memberships/join", json={"org_team_id": str(w.org_b)}
        )
    assert [m["org_team_id"] for m in listed.json()["memberships"]] == [str(w.org_a)]
    assert joined.status_code == 404
    membership = await _membership(w.user.id, w.org_b)
    assert membership is not None and membership.status is MembershipStatus.PENDING


# --------------------------------------------------------------------------- #
# the pending-membership service: only the person turns it active
# --------------------------------------------------------------------------- #


async def _membership_with(w: World, status: MembershipStatus) -> OrgMembership:
    async with AsyncSessionLocal() as db:
        membership = await org_membership_service.create(
            db, user_id=w.user.id, org_team_id=w.org_b, status=status
        )
        await db.commit()
    return membership


@pytest.mark.parametrize(
    "status", [MembershipStatus.ACTIVE, MembershipStatus.DEACTIVATED], ids=lambda s: s.value
)
@pytest.mark.parametrize("operation", ["activate_pending", "withdraw_pending"])
async def test_join_and_withdraw_refuse_a_membership_that_is_not_pending(
    world: World, status: MembershipStatus, operation: str
) -> None:
    await _membership_with(world, status)
    async with AsyncSessionLocal() as db:
        membership = await org_membership_service.get(
            db, user_id=world.user.id, org_team_id=world.org_b
        )
        assert membership is not None
        with pytest.raises(org_membership_service.NotPendingError):
            await getattr(org_membership_service, operation)(db, membership, actor=None)
        await db.rollback()
    still = await _membership(world.user.id, world.org_b)
    assert still is not None and still.status is status


async def test_reactivating_a_pending_membership_leaves_it_pending(world: World) -> None:
    await _membership_with(world, MembershipStatus.PENDING)
    async with AsyncSessionLocal() as db:
        membership = await org_membership_service.get(
            db, user_id=world.user.id, org_team_id=world.org_b
        )
        assert membership is not None
        await org_membership_service.reactivate(db, membership, actor=None)
        await db.commit()
    still = await _membership(world.user.id, world.org_b)
    assert still is not None and still.status is MembershipStatus.PENDING
    assert await _seats(world.user.id, world.org_b) == []


async def test_deactivating_a_pending_membership_withdraws_it(world: World) -> None:
    await _membership_with(world, MembershipStatus.PENDING)
    async with AsyncSessionLocal() as db:
        membership = await org_membership_service.get(
            db, user_id=world.user.id, org_team_id=world.org_b
        )
        assert membership is not None
        await org_membership_service.deactivate(db, membership, actor=None)
        await db.commit()
    # Not a deactivated row a later reactivation could turn active.
    assert await _membership(world.user.id, world.org_b) is None


async def test_the_pending_reads_name_only_pending_rows(world: World) -> None:
    await _membership_with(world, MembershipStatus.PENDING)
    async with AsyncSessionLocal() as db:
        assert await org_membership_service.pending(
            db, user_id=world.user.id, org_team_id=world.org_b
        )
        assert not await org_membership_service.pending(
            db, user_id=world.user.id, org_team_id=world.org_a
        )
        listed = await org_membership_service.list_pending_for_user(db, world.user.id)
        active = await org_membership_service.list_active_for_user(db, world.user.id)
    assert [m.org_team_id for m in listed] == [world.org_b]
    assert [m.org_team_id for m in active] == [world.org_a]


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        pytest.param("jane@acme.com", "j***@acme.com", id="keeps-first-letter-and-domain"),
        pytest.param("j@acme.com", "j***@acme.com", id="one-letter-local"),
        pytest.param("not-an-address", "***", id="no-domain-shows-nothing"),
    ],
)
def test_mask_email(email: str, masked: str) -> None:
    from backend.services.identity.sso_link import mask_email

    assert mask_email(email) == masked
