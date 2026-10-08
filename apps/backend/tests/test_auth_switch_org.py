"""One person, several orgs: listing them, approving a device into one.

The subject is ``two_org_identity``: a person whose home is org A who also
holds a membership in org B, each org with its own admin. Every case drives the
real routes against real Postgres.

* ``GET /auth/memberships`` lists exactly the caller's own active memberships,
  with their role, most recently used first, and only the home org while
  multi-org is off.
* ``POST /auth/device/approve`` with ``org_team_id`` records the chosen org
  (redemption then mints the CLI token for it), refuses an org that is not one
  of the approver's active memberships with one indistinguishable 404, and
  holds the chosen org's sign-in policy.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from alkera_core.auth import decode_session_token
from alkera_core.auth.sign_in_policy import sso_provider_key
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    DeviceAuthorization,
    MembershipStatus,
    OAuthIdentity,
    OrgMembership,
    SsoConnection,
    TeamMembership,
    TeamRole,
)
from alkera_core.utils.email import email_domain
from backend.api.return_path import safe_return_path
from backend.services.identity import device_authorization as device_service
from httpx import AsyncClient
from sqlalchemy import select, update
from tests.conftest import TwoOrg, app_client, hold_sso_domains


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    assert isinstance(body, dict) and isinstance(body.get("error"), dict), body
    return dict(body["error"])


async def _set_membership(user_id: UUID, org: UUID, **values: Any) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership)
            .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org)
            .values(**values)
        )
        await db.commit()


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


async def _memberships(token: str) -> dict[str, Any]:
    async with app_client() as c:
        resp = await c.get("/api/v1/auth/memberships", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


# --- the caller's memberships ----------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_memberships_lists_the_callers_own_orgs_with_roles_most_recent_first(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    now = datetime.now(UTC)
    await _set_membership(t.user.id, t.org_a, last_active_at=now - timedelta(days=2))
    await _set_membership(t.user.id, t.org_b, last_active_at=now - timedelta(hours=1))
    body = await _memberships(t.token_a)
    assert body["active_org_team_id"] == str(t.org_a)
    listed = [(m["org_team_id"], m["role"], m["sso_required"]) for m in body["memberships"]]
    # B was used most recently, so it comes first. The person administers
    # neither org: each org's admin is somebody else.
    assert listed == [(str(t.org_b), "member", False), (str(t.org_a), "member", False)]
    # The org admins' own memberships never show up in this person's list.
    assert {m["org_team_id"] for m in body["memberships"]} == {str(t.org_a), str(t.org_b)}


@pytest.mark.usefixtures("multi_org")
async def test_memberships_names_the_calling_credentials_org_as_active(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    assert (await _memberships(t.token_b))["active_org_team_id"] == str(t.org_b)


@pytest.mark.usefixtures("multi_org")
async def test_memberships_marks_an_admin_role_and_an_sso_org(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _enforce_sso(t.org_b, domain=email_domain(t.admin_b.email))
    async with AsyncSessionLocal() as db:
        cli = await _cli_token(db, t.admin_b.id, t.org_b)
    body = await _memberships(cli)
    assert [(m["role"], m["sso_required"]) for m in body["memberships"]] == [("admin", True)]


@pytest.mark.usefixtures("multi_org")
async def test_an_exempt_membership_is_not_marked_sso(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _enforce_sso(t.org_b, domain=email_domain(t.user.email))
    before = {
        m["org_team_id"]: m["sso_required"] for m in (await _memberships(t.token_a))["memberships"]
    }
    await _set_membership(t.user.id, t.org_b, sso_exempt=True)
    after = {
        m["org_team_id"]: m["sso_required"] for m in (await _memberships(t.token_a))["memberships"]
    }
    assert (before[str(t.org_b)], after[str(t.org_b)]) == (True, False)
    assert before[str(t.org_a)] is after[str(t.org_a)] is False


@pytest.mark.usefixtures("multi_org")
async def test_a_deactivated_membership_is_not_listed(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _set_membership(t.user.id, t.org_b, status=MembershipStatus.DEACTIVATED)
    listed = [m["org_team_id"] for m in (await _memberships(t.token_a))["memberships"]]
    assert listed == [str(t.org_a)]


async def test_with_multi_org_off_only_the_home_org_is_listed(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    listed = [m["org_team_id"] for m in (await _memberships(t.token_a))["memberships"]]
    assert listed == [str(t.org_a)]


async def test_memberships_needs_a_credential(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/memberships")
    assert resp.status_code == 401


async def _cli_token(db: Any, user_id: UUID, org: UUID) -> str:
    from alkera_core.auth import register_token
    from alkera_core.models import TokenType, User
    from backend.auth.membership_tokens import mint_for_membership

    user = await db.get(User, user_id)
    assert user is not None
    token, claims = await mint_for_membership(db, user, org, kind="cli")
    await register_token(db, claims=claims, token_type=TokenType.CLI)
    await db.commit()
    return token


# --- device approval into a chosen org ---------------------------------------------


async def _pending_code() -> tuple[str, DeviceAuthorization]:
    async with AsyncSessionLocal() as db:
        raw, row = await device_service.create_device_code(db, client_id="alkera-cli", scope=None)
        await db.commit()
    return raw, row


async def _signed_in(c: AsyncClient, t: TwoOrg) -> None:
    """A browser signed into A: A is the org the person used last."""
    await _last_used(t, t.org_a)
    login = await c.post("/api/v1/auth/login", json={"email": t.user.email, "password": t.password})
    assert login.status_code == 200, login.text


async def _redeem(raw: str) -> httpx.Response:
    async with app_client() as c:
        return await c.post(
            "/api/v1/auth/device/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": raw,
                "client_id": "alkera-cli",
            },
        )


async def _stored_org(row_id: UUID) -> UUID | None:
    async with AsyncSessionLocal() as db:
        stored = await db.get(DeviceAuthorization, row_id)
        assert stored is not None
        return stored.org_team_id


@pytest.mark.usefixtures("multi_org")
async def test_approving_into_a_chosen_org_records_it_and_redeems_for_it(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    raw, row = await _pending_code()
    async with app_client() as c:
        await _signed_in(c, t)
        resp = await c.post(
            "/api/v1/auth/device/approve",
            json={"user_code": row.user_code, "org_team_id": str(t.org_b)},
        )
    assert resp.status_code == 200, resp.text
    assert await _stored_org(row.id) == t.org_b
    redeemed = await _redeem(raw)
    assert redeemed.status_code == 200, redeemed.text
    claims = decode_session_token(redeemed.json()["access_token"])
    assert (claims.org_team_id, claims.membership_id) == (t.org_b, t.membership_b.id)


@pytest.mark.usefixtures("multi_org")
async def test_approving_without_an_org_records_the_session_org(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    _raw, row = await _pending_code()
    async with app_client() as c:
        await _signed_in(c, t)
        resp = await c.post("/api/v1/auth/device/approve", json={"user_code": row.user_code})
    assert resp.status_code == 200, resp.text
    assert await _stored_org(row.id) == t.org_a


@pytest.mark.parametrize(
    ("target", "flag"),
    [
        pytest.param("stranger", True, id="an-org-the-person-is-not-in"),
        pytest.param("missing", True, id="an-org-that-does-not-exist"),
        pytest.param("deactivated", True, id="a-deactivated-membership"),
        pytest.param("b", False, id="a-second-org-with-multi-org-off"),
    ],
)
async def test_approving_into_an_org_one_cannot_enter_is_one_404(
    two_org_identity: TwoOrg, monkeypatch: pytest.MonkeyPatch, target: str, flag: bool
) -> None:
    t = two_org_identity
    monkeypatch.setattr(settings, "multi_org_enabled", flag)
    if target == "deactivated":
        await _set_membership(t.user.id, t.org_b, status=MembershipStatus.DEACTIVATED)
    stranger = await _stranger_org()
    org = {"stranger": stranger, "missing": uuid4(), "deactivated": t.org_b, "b": t.org_b}[target]
    _raw, row = await _pending_code()
    async with app_client() as c:
        await _signed_in(c, t)
        resp = await c.post(
            "/api/v1/auth/device/approve",
            json={"user_code": row.user_code, "org_team_id": str(org)},
        )
    assert resp.status_code == 404, resp.text
    error = _error(resp)
    assert (error["code"], error["message"], error.get("details")) == (
        "not_found",
        "Not found",
        None,
    )
    assert await _stored_org(row.id) is None, "a refused approval records nothing"


@pytest.mark.usefixtures("multi_org")
async def test_approving_into_an_org_that_wants_sso_is_a_step_up(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _enforce_sso(t.org_b, domain=email_domain(t.user.email))
    _raw, row = await _pending_code()
    async with app_client() as c:
        await _signed_in(c, t)
        resp = await c.post(
            "/api/v1/auth/device/approve",
            json={"user_code": row.user_code, "org_team_id": str(t.org_b)},
        )
    assert resp.status_code == 403, resp.text
    error = _error(resp)
    assert error["code"] == "sso_required"
    url = urlsplit(error["details"]["login_url"])
    assert url.path == f"/api/v1/auth/sso/{t.org_b}/login"
    back = parse_qs(url.query)["return_to"][0]
    assert safe_return_path(back) == back, "the return path survives the SSO route's check"
    assert parse_qs(urlsplit(back).query) == {"user_code": [row.user_code], "org": [str(t.org_b)]}
    assert await _stored_org(row.id) is None


async def _stranger_org() -> UUID:
    """An org the person has nothing to do with."""
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


# --- where a sign-in lands ----------------------------------------------------------


async def _login(t: TwoOrg) -> httpx.Response:
    async with app_client() as c:
        return await c.post(
            "/api/v1/auth/login", json={"email": t.user.email, "password": t.password}
        )


async def _last_used(t: TwoOrg, org: UUID) -> None:
    now = datetime.now(UTC)
    other = t.org_b if org == t.org_a else t.org_a
    await _set_membership(t.user.id, org, last_active_at=now - timedelta(minutes=1))
    await _set_membership(t.user.id, other, last_active_at=now - timedelta(days=3))


def _landed(resp: httpx.Response) -> UUID:
    return decode_session_token(resp.cookies[settings.auth_cookie_name]).org_team_id


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("last", ["a", "b"])
async def test_a_sign_in_lands_in_the_most_recently_used_org(
    two_org_identity: TwoOrg, last: str
) -> None:
    t = two_org_identity
    org = t.org_a if last == "a" else t.org_b
    await _last_used(t, org)
    resp = await _login(t)
    assert resp.status_code == 200, resp.text
    assert _landed(resp) == org
    assert resp.json()["user"]["org_team_id"] == str(org)
    assert resp.json()["choose_org"] is False


@pytest.mark.usefixtures("multi_org")
async def test_a_last_used_org_that_needs_a_step_up_lands_elsewhere_and_asks_to_choose(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _last_used(t, t.org_b)
    await _enforce_sso(t.org_b, domain=email_domain(t.user.email))
    resp = await _login(t)
    assert resp.status_code == 200, resp.text
    assert _landed(resp) == t.org_a
    assert resp.json()["choose_org"] is True


@pytest.mark.usefixtures("multi_org")
async def test_when_no_org_admits_the_sign_in_it_is_refused_as_before(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _last_used(t, t.org_b)
    # A domain belongs to one org, so org_a speaks for the person by their
    # address and org_b by the identity its IdP federated for them.
    await _enforce_sso(t.org_a, domain=email_domain(t.user.email))
    await _enforce_sso(t.org_b, domain=f"elsewhere-{uuid4().hex[:8]}.example.com")
    async with AsyncSessionLocal() as db:
        db.add(
            OAuthIdentity(
                user_id=t.user.id,
                provider=sso_provider_key(t.org_b),
                subject=uuid4().hex,
                email_at_link=t.user.email,
                email_verified=True,
            )
        )
        await db.commit()
    resp = await _login(t)
    assert resp.status_code == 403
    assert _error(resp)["code"] == "sso_required"
    assert settings.auth_cookie_name not in resp.cookies


async def test_with_multi_org_off_a_sign_in_lands_home_whatever_was_used_last(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _last_used(t, t.org_b)
    resp = await _login(t)
    assert resp.status_code == 200, resp.text
    assert _landed(resp) == t.org_a
    assert resp.json()["choose_org"] is False
    assert resp.json()["user"]["membership_count"] == 1


@pytest.mark.usefixtures("multi_org")
async def test_a_deactivated_last_used_org_is_skipped(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _last_used(t, t.org_b)
    await _set_membership(t.user.id, t.org_b, status=MembershipStatus.DEACTIVATED)
    resp = await _login(t)
    assert resp.status_code == 200, resp.text
    assert _landed(resp) == t.org_a


async def _root_role(user_id: UUID, org: UUID, role: TeamRole) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TeamMembership)
            .where(
                TeamMembership.user_id == user_id,
                TeamMembership.org_team_id == org,
                TeamMembership.team_id == org,
            )
            .values(role=role)
        )
        await db.commit()


async def _sub_team_admin(user_id: UUID, org: UUID) -> None:
    """``user_id`` as an admin of a team under ``org``'s root, not of the root."""
    from backend.services.org import memberships as membership_service
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as db:
        team = await team_service.create_subteam(db, org_team_id=org, name=f"sub {uuid4().hex[:6]}")
        await membership_service.add_member(
            db, team_id=team.id, user_id=user_id, role=TeamRole.ADMIN
        )
        await db.commit()


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    ("signed_into", "admin_of_b_root", "expected"),
    [
        pytest.param("a", True, "admin", id="admin-of-another-org-reads-as-admin"),
        pytest.param("b", True, "admin", id="admin-of-the-current-org-reads-as-admin"),
        pytest.param("a", False, "member", id="a-sub-team-admin-of-another-org-is-a-member"),
    ],
)
async def test_memberships_reads_each_orgs_role_whatever_org_the_session_is_in(
    two_org_identity: TwoOrg, signed_into: str, admin_of_b_root: bool, expected: str
) -> None:
    """The session is held to the org it is signed in to, and the role rows of
    every other org are another tenant's: the listing still answers the
    person's own role in each."""
    t = two_org_identity
    if admin_of_b_root:
        await _root_role(t.user.id, t.org_b, TeamRole.ADMIN)
    else:
        await _sub_team_admin(t.user.id, t.org_b)
    token = t.token_a if signed_into == "a" else t.token_b
    roles = {m["org_team_id"]: m["role"] for m in (await _memberships(token))["memberships"]}
    assert roles == {str(t.org_a): "member", str(t.org_b): expected}
