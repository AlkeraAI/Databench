"""One identity, two orgs: every lifecycle and sign-in decision stays in its org.

The person ``U`` is a member of org A (their home) and of org B (the
``two_org_identity`` factory, with the ``multi_org`` fixture letting B's
credentials through the doors). Each case does something to U in ONE org (an
admin deactivates or removes them, the org's IdP deprovisions them, the org
turns SSO enforcement on, the org is deleted) and pins both halves: what the
org decided holds in that org, and nothing reaches the other org or the
identity itself (``users.token_epoch``, ``users.is_active``, the password, the
email, the MFA factor).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from alkera_core.auth import (
    decode_session_token,
    mint_gateway_token,
    register_token,
    revocation,
    sign_in_policy,
)
from alkera_core.auth.sign_in_policy import Allowed, StepUp
from alkera_core.authz import PrincipalKind, Role, ScopeKind
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    AuthRefreshToken,
    IdentitySecurityEvent,
    MembershipStatus,
    OrgAuditEvent,
    OrgMembership,
    OrgSettings,
    PlatformRole,
    RoleAssignment,
    SsoConnection,
    Team,
    TeamMembership,
    TeamRole,
    TokenType,
    User,
    WorkspaceObject,
)
from backend.auth.membership_tokens import mint_for_membership
from backend.auth.session_issue import issue_session, record_grant
from backend.services.identity import org_choice
from backend.services.identity import sso as sso_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service
from fastapi import Request, Response
from freezegun import freeze_time
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from tests.conftest import TwoOrg, app_client, hold_sso_domains, make_member
from tests.test_cross_tenant_auth import (
    REFRESH_COOKIE,
    REVOKED,
    _code,
    _door_current_user,
    _door_sse_recheck,
    _door_ws_admission,
    _pat,
    _socket,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSO, SCIM and the audit log are Enterprise surfaces; a self-hosted
    deployment has them all, so these cases need no plan rows."""
    monkeypatch.setattr(settings, "self_hosted", True)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- credentials ----------------------------------------------------------------


def _request(path: str = "/api/v1/auth/login", cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "query_string": b"",
        }
    )


def _cookie(carrier: Response, name: str) -> str:
    for key, value in carrier.raw_headers:
        decoded = value.decode()
        if key == b"set-cookie" and decoded.startswith(f"{name}="):
            return decoded.split(";")[0].split("=", 1)[1]
    raise AssertionError(f"no {name} cookie was set")


@dataclass
class Browser:
    """A browser login session: its access token, refresh token and family."""

    access: str
    refresh: str
    family_id: UUID

    @property
    def cookies(self) -> str:
        return f"{settings.auth_cookie_name}={self.access}; {REFRESH_COOKIE}={self.refresh}"


async def _browser(user_id: UUID, org: UUID, *, method: str = "password") -> Browser:
    """A session minted the way every sign-in route mints one."""
    carrier = Response()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        claims = await issue_session(
            db, user, request=_request(), response=carrier, method=method, org_team_id=org
        )
        await db.commit()
        family = await db.scalar(
            select(AuthRefreshToken.family_id).where(AuthRefreshToken.access_jti == claims.jti)
        )
    assert family is not None
    return Browser(
        access=_cookie(carrier, settings.auth_cookie_name),
        refresh=_cookie(carrier, REFRESH_COOKIE),
        family_id=family,
    )


async def _cli(user_id: UUID, org: UUID) -> str:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "multi_org_enabled", True)
            token, claims = await mint_for_membership(db, user, org, kind="cli")
        await register_token(db, claims=claims, token_type=TokenType.CLI)
        await db.commit()
    return token


async def _status(token: str) -> tuple[int, str | None]:
    """The API door's answer for a bearer token: 200, or the status and code."""
    async with app_client() as c:
        resp = await c.get("/api/v1/auth/me", headers=_bearer(token))
    return resp.status_code, None if resp.status_code == 200 else _code(resp)


async def _gateway_status(parent: str) -> int:
    """The model gateway's door for a chat token minted under ``parent``."""
    from model_gateway.auth import authenticate

    claims = decode_session_token(parent)
    token, _ = mint_gateway_token(
        user_id=claims.user_id,
        org_team_id=claims.org_team_id,
        chat_id=uuid4(),
        session=claims,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        }
    )
    try:
        await authenticate(request)
    except Exception as exc:
        return int(getattr(exc, "status_code", 500))
    return 200


async def _refresh(browser: Browser) -> httpx.Response:
    async with app_client() as c:
        return await c.post(
            "/api/v1/auth/refresh", headers={"Cookie": f"{REFRESH_COOKIE}={browser.refresh}"}
        )


async def _point(family_id: UUID, org: UUID | None) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AuthRefreshToken)
            .where(AuthRefreshToken.family_id == family_id)
            .values(active_org_team_id=org)
        )
        await db.commit()


async def _verify(*user_ids: UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User).where(User.id.in_(user_ids)).values(email_verified_at=datetime.now(UTC))
        )
        await db.commit()


async def _admin(t: TwoOrg, which: str) -> str:
    """A verified org admin's CLI token for their org."""
    admin = t.admin_a if which == "a" else t.admin_b
    await _verify(admin.id)
    return await _cli(admin.id, t.org_a if which == "a" else t.org_b)


async def _membership(user_id: UUID, org: UUID) -> OrgMembership | None:
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(OrgMembership).where(
                OrgMembership.user_id == user_id, OrgMembership.org_team_id == org
            )
        )


async def _identity(user_id: UUID) -> User:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
    assert user is not None
    return user


async def _awake_chat(org: UUID, owner: UUID) -> UUID:
    async with AsyncSessionLocal() as db:
        chat = WorkspaceObject(
            org_team_id=org,
            logical_id=f"chat-{secrets.token_hex(6)}",
            type="chat",
            title="running",
            owner_user_id=owner,
            visibility_scope="org",
            spec={"permission_mode": "default", "mirror_state": "awake"},
        )
        db.add(chat)
        await db.commit()
        return chat.id


async def _chat_spec(chat_id: UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        chat = await db.get(WorkspaceObject, chat_id)
    assert chat is not None
    return dict(chat.spec or {})


async def _pat_status(raw: str) -> int:
    from tests.test_cross_tenant_auth import _probe

    return (await _probe("/principal", _bearer(raw))).status_code


@dataclass
class Footprint:
    """Everything U holds in one org: a CLI token, a browser session, a
    personal access token and a live chat."""

    cli: str
    browser: Browser
    pat: str
    chat: UUID


async def _footprint(t: TwoOrg, which: str) -> Footprint:
    org = t.org_a if which == "a" else t.org_b
    membership = await _membership(t.user.id, org)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "multi_org_enabled", True)
        browser = await _browser(t.user.id, org)
    return Footprint(
        cli=await _cli(t.user.id, org),
        browser=browser,
        pat=await _pat(t.user.id, org, membership),
        chat=await _awake_chat(org, t.user.id),
    )


#: The refusals a credential whose membership was revoked meets: its registry
#: row is marked revoked (``session_revoked``) and its epoch has moved
#: (``session_org_revoked``); which the door names first is its own business.
ENDED = {REVOKED, "session_revoked"}


def _ended(answer: tuple[int, str | None]) -> bool:
    return answer[0] == 401 and answer[1] in ENDED


async def _all_refused(t: TwoOrg, which: str, fp: Footprint) -> None:
    token = t.token_a if which == "a" else t.token_b
    assert _ended(await _status(fp.cli))
    assert (await _status(fp.browser.access))[0] == 401
    assert await _pat_status(fp.pat) == 401
    assert await _gateway_status(token) == 401
    assert await _door_sse_recheck(t, which) == (401, REVOKED)
    assert (await _door_ws_admission(t, which))[0] == 401
    refreshed = await _refresh(fp.browser)
    assert refreshed.status_code == 401
    assert _code(refreshed) == REVOKED


async def _all_standing(t: TwoOrg, which: str, fp: Footprint) -> None:
    token = t.token_a if which == "a" else t.token_b
    assert await _status(fp.cli) == (200, None)
    assert await _status(fp.browser.access) == (200, None)
    assert await _pat_status(fp.pat) == 200
    assert await _gateway_status(token) == 200
    assert await _door_current_user(t, which) == (200, None)
    assert await _door_sse_recheck(t, which) == (200, None)
    assert await _door_ws_admission(t, which) == (200, None)


# --- deactivation ----------------------------------------------------------------


async def _deactivate_by_admin(t: TwoOrg, active: bool) -> httpx.Response:
    admin_token = await _admin(t, "a")
    async with app_client() as c:
        return await c.put(
            f"/api/v1/org/members/{t.user.id}/active",
            json={"active": active},
            headers=_bearer(admin_token),
        )


async def _deactivate_by_scim(t: TwoOrg, how: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(SsoConnection(org_team_id=t.org_a, enabled=True))
        await db.flush()
        _conn, raw = await sso_service.mint_scim_token(db, t.org_a)
        await db.commit()
    await hold_sso_domains(t.org_a, "alkera.dev")
    async with app_client() as c:
        if how == "patch":
            resp = await c.patch(
                f"/api/v1/scim/v2/Users/{t.user.id}",
                json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
                headers=_bearer(raw),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["active"] is False
        else:
            resp = await c.delete(f"/api/v1/scim/v2/Users/{t.user.id}", headers=_bearer(raw))
            assert resp.status_code == 204, resp.text


DEACTIVATIONS = [
    pytest.param("admin", id="org-admin"),
    pytest.param("scim-patch", id="scim-active-false"),
    pytest.param("scim-delete", id="scim-delete"),
]


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize("how", DEACTIVATIONS)
async def test_deactivating_u_in_a_ends_every_a_credential_and_spares_b(
    two_org_identity: TwoOrg, how: str
) -> None:
    t = two_org_identity
    before = await _identity(t.user.id)
    a, b = await _footprint(t, "a"), await _footprint(t, "b")
    socket_a = await _socket(t, decode_session_token(t.token_a))
    socket_b = await _socket(t, decode_session_token(t.token_b))
    await _all_standing(t, "a", a)
    await _all_standing(t, "b", b)

    if how == "admin":
        resp = await _deactivate_by_admin(t, active=False)
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_active"] is False
    else:
        await _deactivate_by_scim(t, how.removeprefix("scim-"))

    await _all_refused(t, "a", a)
    assert await socket_a._recheck() is False
    await _all_standing(t, "b", b)
    assert await socket_b._recheck() is True
    # U's chat in A ended; the one in B keeps running.
    assert (await _chat_spec(a.chat)).get("ended_reason") == "access_removed"
    assert (await _chat_spec(a.chat)).get("mirror_state") == "asleep"
    assert (await _chat_spec(b.chat)).get("mirror_state") == "awake"
    # The identity is untouched.
    after = await _identity(t.user.id)
    assert after.token_epoch == before.token_epoch
    assert after.is_active is True
    membership_a = await _membership(t.user.id, t.org_a)
    membership_b = await _membership(t.user.id, t.org_b)
    assert membership_a is not None and membership_a.status is MembershipStatus.DEACTIVATED
    assert membership_b is not None and membership_b.status is MembershipStatus.ACTIVE
    assert membership_b.credential_epoch == t.membership_b.credential_epoch


@pytest.mark.usefixtures("multi_org")
async def test_reactivation_admits_new_credentials_and_never_the_old_ones(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    a = await _footprint(t, "a")
    assert (await _deactivate_by_admin(t, active=False)).status_code == 200
    resp = await _deactivate_by_admin(t, active=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True

    assert _ended(await _status(a.cli))
    assert await _pat_status(a.pat) == 401
    fresh = await _cli(t.user.id, t.org_a)
    assert await _status(fresh) == (200, None)


@pytest.mark.parametrize(
    "case",
    [pytest.param("self", id="no-self-deactivation"), pytest.param("last", id="last-admin")],
)
async def test_the_deactivation_guards_hold_on_the_membership(
    two_org_identity: TwoOrg, case: str
) -> None:
    t = two_org_identity
    admin_token = await _admin(t, "a")
    async with app_client() as c:
        resp = await c.put(
            f"/api/v1/org/members/{t.admin_a.id}/active",
            json={"active": False},
            headers=_bearer(admin_token),
        )
    assert resp.status_code == 400
    if case == "last":
        # Another admin who is deactivated does not count toward "another".
        other, _ = await make_member_in(t.org_a, role=TeamRole.ADMIN)
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(OrgMembership)
                .where(OrgMembership.user_id == other.id)
                .values(status=MembershipStatus.DEACTIVATED)
            )
            await db.commit()
            admin_membership = await db.scalar(
                select(OrgMembership).where(
                    OrgMembership.user_id == t.admin_a.id, OrgMembership.org_team_id == t.org_a
                )
            )
            assert admin_membership is not None
            with pytest.raises(org_membership_service.LastActiveAdminError):
                await org_membership_service.deactivate(db, admin_membership, actor=None)


async def make_member_in(org: UUID, *, role: TeamRole = TeamRole.MEMBER) -> tuple[User, str | None]:
    async with AsyncSessionLocal() as db:
        user, password = await make_member(db, org_id=org, role=role, verified=True)
        await db.commit()
    return user, password


# --- removal ------------------------------------------------------------------------


async def _seat_and_grants(t: TwoOrg) -> UUID:
    """Give U a sub-team seat and an owner grant in A, and an owner grant in B.
    Returns the sub-team."""
    async with AsyncSessionLocal() as db:
        sub = Team(name=f"sub-{secrets.token_hex(3)}", parent_team_id=t.org_a, is_root=False)
        db.add(sub)
        await db.flush()
        db.add(TeamMembership(user_id=t.user.id, team_id=sub.id, role=TeamRole.MEMBER))
        for org in (t.org_a, t.org_b):
            db.add(
                RoleAssignment(
                    org_team_id=org,
                    principal_kind=PrincipalKind.USER,
                    principal_id=t.user.id,
                    scope_kind=ScopeKind.ORG,
                    scope_id=org,
                    role=Role.OWNER,
                )
            )
        await db.commit()
        return sub.id


async def _remove(t: TwoOrg, route: str) -> httpx.Response:
    admin_token = await _admin(t, "a")
    path = (
        f"/api/v1/users/{t.user.id}"
        if route == "users"
        else f"/api/v1/teams/{t.org_a}/memberships/{t.user.id}"
    )
    async with app_client() as c:
        return await c.delete(path, headers=_bearer(admin_token))


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    "route",
    [pytest.param("users", id="delete-user-route"), pytest.param("root", id="root-membership")],
)
async def test_removing_u_from_a_takes_a_seats_and_grants_and_leaves_the_identity_and_b(
    two_org_identity: TwoOrg, route: str
) -> None:
    t = two_org_identity
    await _seat_and_grants(t)
    a, b = await _footprint(t, "a"), await _footprint(t, "b")
    before = await _identity(t.user.id)

    resp = await _remove(t, route)
    assert resp.status_code == 204, resp.text

    assert await _membership(t.user.id, t.org_a) is None
    assert await _membership(t.user.id, t.org_b) is not None
    async with AsyncSessionLocal() as db:
        seats = (
            await db.execute(
                select(TeamMembership.org_team_id, func.count())
                .where(TeamMembership.user_id == t.user.id)
                .group_by(TeamMembership.org_team_id)
            )
        ).all()
        live_grants = (
            await db.execute(
                select(RoleAssignment.org_team_id).where(
                    RoleAssignment.principal_id == t.user.id, RoleAssignment.revoked_at.is_(None)
                )
            )
        ).scalars()
        live = set(live_grants.all())
    assert dict(seats) == {t.org_b: 1}, "every seat in A went with the membership"
    assert live == {t.org_b}
    after = await _identity(t.user.id)
    assert after.token_epoch == before.token_epoch and after.is_active
    await _all_standing(t, "b", b)
    assert _ended(await _status(a.cli))
    assert (await _chat_spec(a.chat)).get("ended_reason") == "access_removed"


async def test_removing_someone_with_no_membership_here_is_a_404(
    two_org_identity: TwoOrg,
) -> None:
    """B's admin cannot remove U from A by naming them: U is not B's to remove
    from anywhere but B."""
    t = two_org_identity
    outsider, _ = await make_member_in(t.org_a)
    admin_b = await _admin(t, "b")
    async with app_client() as c:
        resp = await c.delete(f"/api/v1/users/{outsider.id}", headers=_bearer(admin_b))
    assert resp.status_code == 404
    assert await _membership(outsider.id, t.org_a) is not None


async def test_removal_revokes_only_the_invitations_into_that_org(
    two_org_identity: TwoOrg,
) -> None:
    from alkera_core.models import Invitation, InvitationStatus

    t = two_org_identity
    async with AsyncSessionLocal() as db:
        for org in (t.org_a, t.org_b):
            db.add(
                Invitation(
                    email=f"invitee-{secrets.token_hex(4)}@alkera.dev",
                    team_id=org,
                    role=TeamRole.MEMBER,
                    invited_by_id=t.user.id,
                    token=secrets.token_hex(32),
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
        await db.commit()
    assert (await _remove(t, "users")).status_code == 204
    async with AsyncSessionLocal() as db:
        rows = dict(
            (
                await db.execute(
                    select(Invitation.team_id, Invitation.status).where(
                        Invitation.invited_by_id == t.user.id
                    )
                )
            )
            .tuples()
            .all()
        )
    assert rows == {t.org_a: InvitationStatus.REVOKED, t.org_b: InvitationStatus.PENDING}


# --- SCIM ---------------------------------------------------------------------------


async def _scim_token(org: UUID, *, domains: str = "alkera.dev") -> str:
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(select(SsoConnection).where(SsoConnection.org_team_id == org))
        if conn is None:
            db.add(SsoConnection(org_team_id=org, enabled=True))
            await db.flush()
        _conn, raw = await sso_service.mint_scim_token(db, org)
        await db.commit()
    await hold_sso_domains(org, domains)
    return raw


async def _directory_name(admin_token: str, user_id: UUID) -> str:
    async with app_client() as c:
        resp = await c.get(f"/api/v1/users/{user_id}", headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    return str(resp.json()["display_name"])


@pytest.mark.parametrize(
    "operations",
    [
        pytest.param(
            [{"op": "replace", "path": "name.givenName", "value": "Scimmed"}], id="path-given-name"
        ),
        pytest.param(
            [{"op": "replace", "value": {"name": {"givenName": "Scimmed"}}}], id="pathless-name"
        ),
    ],
)
async def test_scim_names_write_the_orgs_display_name_and_never_the_identity(
    two_org_identity: TwoOrg, operations: list[dict[str, Any]]
) -> None:
    t = two_org_identity
    raw = await _scim_token(t.org_a)
    async with app_client() as c:
        resp = await c.patch(
            f"/api/v1/scim/v2/Users/{t.user.id}",
            json={"Operations": operations},
            headers=_bearer(raw),
        )
    assert resp.status_code == 200, resp.text
    identity = await _identity(t.user.id)
    assert (identity.first_name, identity.last_name) == (t.user.first_name, t.user.last_name)
    membership_a = await _membership(t.user.id, t.org_a)
    membership_b = await _membership(t.user.id, t.org_b)
    assert membership_a is not None and membership_a.display_name == f"Scimmed {t.user.last_name}"
    assert membership_b is not None and membership_b.display_name is None
    assert await _directory_name(await _admin(t, "a"), t.user.id) == f"Scimmed {t.user.last_name}"
    assert await _directory_name(await _admin(t, "b"), t.user.id) == identity.display_name


async def test_scim_creates_a_new_identity_with_the_org_scim_id_on_its_membership(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    raw = await _scim_token(t.org_a)
    email = f"fresh-{secrets.token_hex(4)}@alkera.dev"
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/scim/v2/Users",
            json={"userName": email, "externalId": "okta-77", "active": True},
            headers=_bearer(raw),
        )
        assert resp.status_code == 201, resp.text
        found = await c.get(
            "/api/v1/scim/v2/Users",
            params={"filter": 'externalId eq "okta-77"'},
            headers=_bearer(raw),
        )
    created = UUID(resp.json()["id"])
    membership = await _membership(created, t.org_a)
    assert membership is not None and membership.scim_external_id == "okta-77"
    identity = await _identity(created)
    assert identity.scim_external_id is None, "the identity column is no longer written"
    assert [r["id"] for r in found.json()["Resources"]] == [str(created)]


async def test_scim_never_changes_the_identitys_credentials(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    raw = await _scim_token(t.org_a)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(User.id == t.user.id)
            .values(mfa_enabled=True, mfa_secret_encrypted="x")
        )
        await db.commit()
    before = await _identity(t.user.id)
    async with app_client() as c:
        put = await c.put(
            f"/api/v1/scim/v2/Users/{t.user.id}",
            json={
                "userName": t.user.email,
                "name": {"givenName": "New", "familyName": "Name"},
                "emails": [{"value": "taken-over@elsewhere.example", "primary": True}],
                "password": "attacker-chosen-password",
                "active": True,
            },
            headers=_bearer(raw),
        )
        patch = await c.patch(
            f"/api/v1/scim/v2/Users/{t.user.id}",
            json={
                "Operations": [
                    {"op": "replace", "path": "password", "value": "another-one"},
                    {"op": "replace", "path": "emails", "value": [{"value": "x@y.example"}]},
                ]
            },
            headers=_bearer(raw),
        )
    assert put.status_code == 200, put.text
    assert patch.status_code == 200, patch.text
    after = await _identity(t.user.id)
    assert after.password_hash == before.password_hash
    assert after.email == before.email
    assert (after.mfa_enabled, after.mfa_secret_encrypted) == (True, "x")
    assert (after.first_name, after.last_name) == (before.first_name, before.last_name)


# --- an org admin's powers over a person ----------------------------------------------


async def test_the_deprecated_create_never_reaches_an_identity_elsewhere(
    two_org_identity: TwoOrg, monkeypatch_email_send: list[dict]
) -> None:
    """B's admin names an address whose identity lives in A: no account is
    created or changed, no membership is written, and an invitation into B is
    pending instead. The answer carries nothing about the identity."""
    from alkera_core.models import Invitation, InvitationStatus

    t = two_org_identity
    outsider, _ = await make_member_in(t.org_a)
    before = await _identity(outsider.id)
    admin_b = await _admin(t, "b")
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/users",
            json={
                "email": outsider.email,
                "first_name": "Typed",
                "last_name": "ByB",
                "password": "admin-chosen-pass-1",
                "org_team_id": str(t.org_b),
            },
            headers=_bearer(admin_b),
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["notes"] == ["password_ignored", "invitation_pending"]
    assert body["org_team_id"] == str(t.org_b)
    assert body["id"] == body["invitation_id"] != str(outsider.id)
    assert (body["first_name"], body["last_name"]) == ("Typed", "ByB")
    assert await _membership(outsider.id, t.org_b) is None
    after = await _identity(outsider.id)
    assert (after.password_hash, after.first_name, after.token_epoch) == (
        before.password_hash,
        before.first_name,
        before.token_epoch,
    )
    async with AsyncSessionLocal() as db:
        invitation = await db.get(Invitation, UUID(body["invitation_id"]))
    assert invitation is not None
    assert (invitation.team_id, invitation.status) == (t.org_b, InvitationStatus.PENDING)
    assert [m["email"] for m in monkeypatch_email_send] == [outsider.email]


@pytest.mark.usefixtures("multi_org")
async def test_a_ban_reaches_the_chain_of_every_org_the_person_belongs_to(
    two_org_identity: TwoOrg, platform_admin: Any
) -> None:
    from tests.conftest import mint_cli_token

    t = two_org_identity
    staff = await mint_cli_token(
        user_id=platform_admin.admin_id,
        email=platform_admin.admin_email,
        org_team_id=platform_admin.org_id,
        platform_role=PlatformRole.ALKERA_ADMIN,
    )
    async with app_client() as c:
        banned = await c.post(
            "/admin/v1/bans/users",
            json={"user_id": str(t.user.id), "reason": "conduct in another org"},
            headers=_bearer(staff),
        )
        assert banned.status_code == 201, banned.text
        lifted = await c.delete(f"/admin/v1/bans/users/{t.user.id}", headers=_bearer(staff))
        assert lifted.status_code == 204, lifted.text
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.target == t.user.email,
                        OrgAuditEvent.action.in_(
                            ["platform.user_banned", "platform.user_ban_lifted"]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        logged = (
            (
                await db.execute(
                    select(IdentitySecurityEvent.event).where(
                        IdentitySecurityEvent.user_id == t.user.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted((r.org_team_id, r.action) for r in rows) == sorted(
        (org, action)
        for org in (t.org_a, t.org_b)
        for action in ("platform.user_banned", "platform.user_ban_lifted")
    )
    for row in rows:
        # What the org may know: who acted, on which member. The reason stays
        # in the identity's own log and no row names another org.
        detail = row.detail or {}
        assert "reason" not in detail
        assert str(t.org_a if row.org_team_id == t.org_b else t.org_b) not in str(detail)
        assert row.actor_email == platform_admin.admin_email
    assert set(logged) >= {"platform.user_banned", "platform.user_ban_lifted"}


def _walk(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


async def test_an_admin_names_a_member_for_their_org_only_and_sees_no_other_org(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    admin_a, admin_b = await _admin(t, "a"), await _admin(t, "b")
    async with app_client() as c:
        named_b = await c.patch(
            f"/api/v1/users/{t.user.id}",
            json={"display_name": "Known in B only"},
            headers=_bearer(admin_b),
        )
        assert named_b.status_code == 200, named_b.text
        patched = await c.patch(
            f"/api/v1/users/{t.user.id}",
            json={"first_name": "Renamed"},
            headers=_bearer(admin_a),
        )
        listing = await c.get("/api/v1/users", headers=_bearer(admin_a))
        one = await c.get(f"/api/v1/users/{t.user.id}", headers=_bearer(admin_a))
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == f"Renamed {t.user.last_name}"
    identity = await _identity(t.user.id)
    assert identity.first_name == t.user.first_name, "the identity's own name is not the org's"
    for body in (patched.json(), listing.json(), one.json()):
        keys = {key for key, _ in _walk(body)}
        assert "org_team_id" not in keys
        assert "Known in B only" not in str(body)
        assert "mfa_enabled" not in keys and "platform_role" not in keys
    assert await _directory_name(admin_b, t.user.id) == "Known in B only"


async def test_a_member_cannot_set_the_orgs_name_for_themselves(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with app_client() as c:
        resp = await c.patch(
            f"/api/v1/users/{t.user.id}",
            json={"display_name": "Self-styled"},
            headers=_bearer(t.token_a),
        )
    assert resp.status_code == 403
    membership = await _membership(t.user.id, t.org_a)
    assert membership is not None and membership.display_name is None


# --- per-org sign-in policy ------------------------------------------------------------


async def _enforce(org: UUID, *, domains: str = "alkera.dev", max_age: int = 86_400) -> None:
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(select(SsoConnection).where(SsoConnection.org_team_id == org))
        if conn is None:
            conn = SsoConnection(org_team_id=org, protocol="oidc")
            db.add(conn)
        conn.enabled, conn.enforced = True, True
        conn.session_max_age_seconds = max_age
        conn.oidc_issuer, conn.oidc_client_id = "https://idp.example.com", "cid"
        await db.commit()
    await hold_sso_domains(org, domains)


async def _set_membership(user_id: UUID, org: UUID, **values: Any) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership)
            .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org)
            .values(**values)
        )
        await db.commit()


async def _evaluate(
    user_id: UUID, org: UUID, family: UUID | None, method: str | None = None
) -> Allowed | StepUp:
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        return await sign_in_policy.evaluate(
            db, user=user, org_team_id=org, family_id=family, method=method
        )


async def _grant(family: UUID, org: UUID | None, method: str, at: datetime | None = None) -> None:
    async with AsyncSessionLocal() as db:
        await record_grant(
            db, family_id=family, org_team_id=org, method=method, at=at or datetime.now(UTC)
        )
        await db.commit()


async def _disallow_google(org: UUID) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(OrgSettings, org)
        if row is None:
            db.add(OrgSettings(org_team_id=org, allow_login_google=False))
        else:
            row.allow_login_google = False
        await db.commit()


def _kind(outcome: Allowed | StepUp) -> str | None:
    return outcome.kind if isinstance(outcome, StepUp) else None


@pytest.mark.usefixtures("multi_org")
async def test_a_enforcing_sso_refuses_a_password_session_in_a_and_never_in_b(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _enforce(t.org_a)
    session = await _browser(t.user.id, t.org_b)
    assert _kind(await _evaluate(t.user.id, t.org_b, session.family_id)) is None
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) == "sso_required"
    await _grant(session.family_id, t.org_a, "sso")
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) is None


@pytest.mark.usefixtures("multi_org")
async def test_b_enforcing_sso_never_refuses_entering_a(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _enforce(t.org_b)
    session = await _browser(t.user.id, t.org_a)
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) is None
    assert _kind(await _evaluate(t.user.id, t.org_b, session.family_id)) == "sso_required"


@pytest.mark.usefixtures("multi_org")
async def test_an_sso_grant_from_one_orgs_idp_is_not_a_sign_in_for_another_org(
    two_org_identity: TwoOrg,
) -> None:
    """An org's IdP speaks for that org: a session it started enters another org
    only with a sign-in of the identity's own or that org's IdP."""
    t = two_org_identity
    session = await _browser(t.user.id, t.org_b, method="sso")
    assert _kind(await _evaluate(t.user.id, t.org_b, session.family_id)) is None
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) == (
        "login_method_not_allowed"
    )


@pytest.mark.parametrize(
    ("exempt_in", "expected_in_a"),
    [
        pytest.param("a", None, id="exempt-in-a-passes-a"),
        pytest.param("b", "sso_required", id="exempt-in-b-does-nothing-in-a"),
    ],
)
async def test_the_break_glass_flag_is_per_membership(
    two_org_identity: TwoOrg, exempt_in: str, expected_in_a: str | None
) -> None:
    t = two_org_identity
    await _enforce(t.org_a)
    await _set_membership(t.user.id, t.org_a if exempt_in == "a" else t.org_b, sso_exempt=True)
    assert _kind(await _evaluate(t.user.id, t.org_a, None, "password")) == expected_in_a


async def test_platform_staff_pass_an_enforcing_org(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    await _enforce(t.org_a)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User)
            .where(User.id == t.user.id)
            .values(platform_role=PlatformRole.ALKERA_SUPPORT)
        )
        await db.commit()
    assert _kind(await _evaluate(t.user.id, t.org_a, None, "password")) is None


@pytest.mark.parametrize(
    ("method", "org", "expected"),
    [
        pytest.param("google", "a", "login_method_not_allowed", id="google-refused-in-a"),
        pytest.param("google", "b", None, id="google-allowed-in-b"),
        pytest.param("password", "a", None, id="password-untouched-in-a"),
    ],
)
async def test_a_disallowing_google_refuses_it_in_a_alone(
    two_org_identity: TwoOrg, method: str, org: str, expected: str | None
) -> None:
    t = two_org_identity
    await _disallow_google(t.org_a)
    session = await _browser(t.user.id, t.org_a, method=method)
    target = t.org_a if org == "a" else t.org_b
    assert _kind(await _evaluate(t.user.id, target, session.family_id)) == expected


@pytest.mark.parametrize(
    "grants",
    [
        pytest.param([("renewal", None)], id="a-renewal-alone-is-not-a-sign-in"),
        pytest.param([("google", None), ("renewal", None)], id="renewal-carries-google-only"),
    ],
)
async def test_a_reissue_that_proved_nothing_does_not_dodge_a_toggle(
    two_org_identity: TwoOrg, grants: list[tuple[str, UUID | None]]
) -> None:
    t = two_org_identity
    await _disallow_google(t.org_a)
    session = await _browser(t.user.id, t.org_a, method="google")
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM auth_session_org_grants WHERE family_id = :f"),
            {"f": session.family_id},
        )
        await db.commit()
    for method, org in grants:
        await _grant(session.family_id, org, method)
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) == (
        "login_method_not_allowed"
    )


@pytest.mark.usefixtures("strict_sso")
async def test_a_session_from_before_grants_enters_except_where_sso_is_enforced(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    session = await _browser(t.user.id, t.org_a)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM auth_session_org_grants WHERE family_id = :f"),
            {"f": session.family_id},
        )
        await db.commit()
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) is None
    await _enforce(t.org_a)
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) == "sso_required"


@pytest.mark.usefixtures("strict_sso")
async def test_an_sso_grant_expires_at_the_orgs_max_age_and_the_refresh_says_so(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _enforce(t.org_a, max_age=3600)
    with freeze_time("2026-10-04 12:00:00", real_asyncio=True) as frozen:
        session = await _browser(t.user.id, t.org_a, method="sso")
        within = await _refresh(session)
        assert within.status_code == 200, within.text
        renewed = Browser(
            access=within.cookies[settings.auth_cookie_name],
            refresh=within.cookies[REFRESH_COOKIE],
            family_id=session.family_id,
        )
        frozen.move_to("2026-10-04 13:00:01")
        expired = await _refresh(renewed)
    assert expired.status_code == 401
    assert _code(expired) == "sso_required"
    assert expired.json()["error"]["details"]["login_url"].endswith(f"/sso/{t.org_a}/login")
    # The family stands (the browser steps up on it); its access token is revoked.
    assert expired.cookies.get(REFRESH_COOKIE)
    assert (await _status(renewed.access))[0] == 401
    async with AsyncSessionLocal() as db:
        alive = await db.scalar(
            select(func.count())
            .select_from(AuthRefreshToken)
            .where(
                AuthRefreshToken.family_id == session.family_id,
                AuthRefreshToken.revoked_at.is_(None),
            )
        )
    assert alive


@pytest.mark.usefixtures("strict_sso")
async def test_an_sso_sign_in_steps_up_the_browsers_own_session(two_org_identity: TwoOrg) -> None:
    """After the refresh answered ``sso_required``, the SSO callback writes its
    grant onto the same family and mints into the org, instead of a second
    session. A browser session of someone else is never stepped up."""
    from backend.api.routes.identity.sso import _complete_login

    t = two_org_identity
    await _enforce(t.org_a)
    session = await _browser(t.user.id, t.org_a)
    assert (await _refresh(session)).status_code == 401
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        resp = await _complete_login(
            db,
            _request("/api/v1/auth/sso/x/login/callback", cookie=session.cookies),
            user=user,
            org_id=t.org_a,
            return_to="//evil.example/steal",
        )
        await db.commit()
    assert resp.headers["location"].endswith("/dashboard"), "an off-site return is refused"
    new_access = _cookie(cast(Response, resp), settings.auth_cookie_name)
    assert await _status(new_access) == (200, None)
    families = await _families(t.user.id)
    assert families == {session.family_id}
    assert _kind(await _evaluate(t.user.id, t.org_a, session.family_id)) is None

    # Somebody else's browser cookie is not U's session to step up.
    other, _ = await make_member_in(t.org_a)
    stranger = await _browser(other.id, t.org_a, method="sso")
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
        assert user is not None
        await _complete_login(
            db,
            _request("/api/v1/auth/sso/x/login/callback", cookie=stranger.cookies),
            user=user,
            org_id=t.org_a,
            return_to=None,
        )
        await db.commit()
    assert len(await _families(t.user.id)) == 2


async def _families(user_id: UUID) -> set[UUID]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(AuthRefreshToken.family_id).where(AuthRefreshToken.user_id == user_id)
        )
        return set(rows.scalars().all())


# --- turning enforcement on ------------------------------------------------------------


async def _put_sso(
    browser: Browser, *, enforced: bool, issuer: str = "https://idp.example.com"
) -> httpx.Response:
    async with app_client() as c:
        return await c.put(
            "/api/v1/org/sso",
            json={
                "protocol": "oidc",
                "enabled": True,
                "enforced": enforced,
                "oidc_issuer": issuer,
                "oidc_client_id": "cid",
                "oidc_client_secret": "secret",
            },
            headers={"Cookie": browser.cookies},
        )


async def test_enforcing_sso_needs_the_admins_own_fresh_sso_sign_in(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _verify(t.admin_a.id)
    admin = await _browser(t.admin_a.id, t.org_a)
    assert (await _put_sso(admin, enforced=False)).status_code == 200
    await hold_sso_domains(t.org_a, "alkera.dev")
    refused = await _put_sso(admin, enforced=True)
    assert refused.status_code == 409
    assert _code(refused) == "sso_unverified"
    async with AsyncSessionLocal() as db:
        conn = await db.scalar(select(SsoConnection).where(SsoConnection.org_team_id == t.org_a))
    assert conn is not None and conn.enforced is False


@pytest.mark.usefixtures("multi_org", "strict_sso")
async def test_enforcing_sso_ends_non_sso_credentials_in_that_org_alone(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _verify(t.admin_a.id)
    admin = await _browser(t.admin_a.id, t.org_a)
    assert (await _put_sso(admin, enforced=False)).status_code == 200
    await hold_sso_domains(t.org_a, "alkera.dev")
    await _grant(admin.family_id, t.org_a, "sso")
    exempt, _ = await make_member_in(t.org_a)
    await _set_membership(exempt.id, t.org_a, sso_exempt=True)
    exempt_token = await _cli(exempt.id, t.org_a)
    a, b = await _footprint(t, "a"), await _footprint(t, "b")

    resp = await _put_sso(admin, enforced=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["enforced"] is True

    assert _ended(await _status(a.cli))
    assert await _pat_status(a.pat) == 401
    await _all_standing(t, "b", b)
    assert await _status(exempt_token) == (200, None)
    async with AsyncSessionLocal() as db:
        actions = (
            await db.execute(
                select(OrgAuditEvent.action).where(OrgAuditEvent.org_team_id == t.org_a)
            )
        ).scalars()
        assert "sso.enforcement_enabled" in set(actions.all())


async def test_moving_to_another_idp_while_enforced_turns_enforcement_off(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    await _verify(t.admin_a.id)
    admin = await _browser(t.admin_a.id, t.org_a)
    assert (await _put_sso(admin, enforced=False)).status_code == 200
    await hold_sso_domains(t.org_a, "alkera.dev")
    await _grant(admin.family_id, t.org_a, "sso")
    assert (await _put_sso(admin, enforced=True)).status_code == 200
    admin = await _browser(t.admin_a.id, t.org_a)
    await _grant(admin.family_id, t.org_a, "sso")
    same = await _put_sso(admin, enforced=True)
    assert same.status_code == 200 and same.json()["enforced"] is True
    moved = await _put_sso(admin, enforced=True, issuer="https://other-idp.example.com")
    assert moved.status_code == 200, moved.text
    assert moved.json()["enforced"] is False
    async with AsyncSessionLocal() as db:
        audit = await db.scalar(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_team_id == t.org_a,
                OrgAuditEvent.action == "sso.enforcement_disabled",
            )
        )
    assert audit is not None and audit.detail is not None
    assert audit.detail["reason"] == "idp_changed"


@pytest.mark.parametrize(
    ("seconds", "status"),
    [
        pytest.param(3600, 200, id="one-hour"),
        pytest.param(2_592_000, 200, id="thirty-days"),
        pytest.param(3599, 422, id="under-an-hour"),
        pytest.param(2_592_001, 422, id="over-thirty-days"),
    ],
)
async def test_the_sso_max_age_is_bounded(
    two_org_identity: TwoOrg, seconds: int, status: int
) -> None:
    t = two_org_identity
    await _verify(t.admin_a.id)
    admin = await _browser(t.admin_a.id, t.org_a)
    async with app_client() as c:
        resp = await c.put(
            "/api/v1/org/sso",
            json={
                "protocol": "oidc",
                "oidc_issuer": "https://idp.example.com",
                "oidc_client_id": "cid",
                "oidc_client_secret": "secret",
                "session_max_age_seconds": seconds,
            },
            headers={"Cookie": admin.cookies},
        )
    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.json()["session_max_age_seconds"] == seconds


# --- device approval ------------------------------------------------------------------


async def _approve(browser: Browser) -> httpx.Response:
    from backend.services.identity import device_authorization as device_service

    async with AsyncSessionLocal() as db:
        _raw, row = await device_service.create_device_code(db, client_id="alkera-cli", scope=None)
        await db.commit()
    async with app_client() as c:
        return await c.post(
            "/api/v1/auth/device/approve",
            json={"user_code": row.user_code},
            headers={"Cookie": browser.cookies},
        )


@pytest.mark.usefixtures("strict_sso")
async def test_device_approval_for_an_sso_org_needs_a_fresh_sso_grant(
    two_org_identity: TwoOrg,
) -> None:
    """A CLI token for an org that requires SSO is only ever born under a live
    sign-in through its IdP: the approving session's grant is weighed against
    the org's max age at the moment of approval."""
    t = two_org_identity
    await _enforce(t.org_a, max_age=3600)
    stale = await _browser(t.user.id, t.org_a)
    await _grant(stale.family_id, t.org_a, "sso", at=datetime.now(UTC) - timedelta(seconds=3601))
    refused = await _approve(stale)
    assert refused.status_code == 403
    assert _code(refused) == "sso_required"

    fresh = await _browser(t.user.id, t.org_a)
    await _grant(fresh.family_id, t.org_a, "sso", at=datetime.now(UTC) - timedelta(seconds=3500))
    assert (await _approve(fresh)).status_code == 200


# --- deleting an org --------------------------------------------------------------------


async def _delete_org(org: UUID) -> None:
    async with AsyncSessionLocal() as db:
        team = await db.get(Team, org)
        assert team is not None
        await team_service.delete_org(db, team)
        await db.commit()


@pytest.mark.usefixtures("multi_org")
async def test_deleting_a_keeps_a_person_who_belongs_to_b_and_rehomes_them(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    a, b = await _footprint(t, "a"), await _footprint(t, "b")
    jti_a = decode_session_token(a.cli).jti
    assert jti_a is not None

    await _delete_org(t.org_a)

    identity = await _identity(t.user.id)
    assert identity.home_org_team_id == t.org_b
    assert await _membership(t.user.id, t.org_b) is not None
    await _all_standing(t, "b", b)
    # A's credentials were ended before the rows went: this process refuses
    # them from the revocation cache without asking the database.
    assert jti_a in revocation._cache._revoked
    assert (await _status(a.cli))[0] == 401


async def test_deleting_a_deletes_a_person_who_belongs_nowhere_else(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    only_a, _ = await make_member_in(t.org_a)
    await _delete_org(t.org_a)
    async with AsyncSessionLocal() as db:
        assert await db.get(User, only_a.id) is None
        assert await db.get(User, t.user.id) is not None


async def test_the_home_org_cannot_move_outside_an_org_deletion(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(User).where(User.id == t.user.id).values(home_org_team_id=t.org_b)
            )
        await db.rollback()
    async with AsyncSessionLocal() as db:
        third = Team(name=f"third-{secrets.token_hex(3)}", is_root=True)
        db.add(third)
        await db.flush()
        team = await db.get(Team, t.org_a)
        assert team is not None
        await team_service.delete_org(db, team)
        assert (await db.get(User, t.user.id)) is not None
        # The deletion opened the guard for its own re-homing statement only:
        # later in the same transaction the column is immutable again.
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(User).where(User.id == t.admin_b.id).values(home_org_team_id=third.id)
            )
        await db.rollback()


# --- audit -------------------------------------------------------------------------------


async def test_a_password_change_is_an_identity_event_and_never_an_org_row(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    session = await _browser(t.user.id, t.org_a)
    async with app_client() as c:
        resp = await c.patch(
            f"/api/v1/users/{t.user.id}",
            json={"password": "a-brand-new-pass-99", "current_password": t.password},
            headers={"Cookie": session.cookies},
        )
    assert resp.status_code == 200, resp.text
    async with AsyncSessionLocal() as db:
        events = (
            await db.execute(
                select(IdentitySecurityEvent.event, IdentitySecurityEvent.org_team_id).where(
                    IdentitySecurityEvent.user_id == t.user.id
                )
            )
        ).all()
        org_rows = await db.scalar(
            select(func.count())
            .select_from(OrgAuditEvent)
            .where(OrgAuditEvent.action == "auth.password_changed")
            .where(OrgAuditEvent.actor_id == t.user.id)
        )
    assert ("auth.password_changed", t.org_a) in {tuple(e) for e in events}
    assert org_rows == 0


async def test_a_sign_in_into_b_is_audited_in_b_and_not_in_a(two_org_identity: TwoOrg) -> None:
    from backend.api.routes.identity.sso import _complete_login

    t = two_org_identity
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "multi_org_enabled", True)
        async with AsyncSessionLocal() as db:
            user = await db.get(User, t.user.id)
            assert user is not None
            await _complete_login(db, _request(), user=user, org_id=t.org_b, return_to=None)
            await db.commit()
    async with AsyncSessionLocal() as db:
        orgs = (
            await db.execute(
                select(OrgAuditEvent.org_team_id).where(
                    OrgAuditEvent.actor_id == t.user.id, OrgAuditEvent.action == "auth.login"
                )
            )
        ).scalars()
        assert set(orgs.all()) == {t.org_b}


async def test_as_audit_listing_never_shows_us_activity_in_b(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    admin_a, admin_b = await _admin(t, "a"), await _admin(t, "b")
    async with app_client() as c:
        assert (
            await c.patch(
                f"/api/v1/users/{t.user.id}",
                json={"display_name": "Renamed in B"},
                headers=_bearer(admin_b),
            )
        ).status_code == 200
        listing_a = await c.get("/api/v1/org/audit-events", headers=_bearer(admin_a))
        listing_b = await c.get("/api/v1/org/audit-events", headers=_bearer(admin_b))
    assert listing_a.status_code == 200, listing_a.text
    assert "Renamed in B" not in listing_a.text
    assert "member.display_name_changed" not in listing_a.text
    assert "member.display_name_changed" in listing_b.text


async def test_the_security_log_is_the_callers_own(two_org_identity: TwoOrg) -> None:
    from backend.services.audit import identity_security

    t = two_org_identity
    async with AsyncSessionLocal() as db:
        await identity_security.record(db, user_id=t.user.id, event="auth.mfa_enabled")
        await identity_security.record(db, user_id=t.admin_a.id, event="auth.login_failed")
        await db.commit()
    async with app_client() as c:
        mine = await c.get("/api/v1/me/security-events", headers=_bearer(t.token_a))
    assert mine.status_code == 200, mine.text
    events = mine.json()["events"]
    assert [e["event"] for e in events] == ["auth.mfa_enabled"]


async def test_an_unknown_security_event_is_refused(two_org_identity: TwoOrg) -> None:
    from backend.services.audit import identity_security

    async with AsyncSessionLocal() as db:
        with pytest.raises(ValueError, match="not an identity security event"):
            await identity_security.record(
                db, user_id=two_org_identity.user.id, event="auth.made_up"
            )


# --- SSO role sync ------------------------------------------------------------------------


async def test_role_sync_never_attaches_a_person_to_an_org(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    outsider, _ = await make_member_in(t.org_a)
    async with AsyncSessionLocal() as db:
        conn = SsoConnection(
            org_team_id=t.org_b,
            enabled=True,
            groups_mapping={"admins": "admin"},
        )
        db.add(conn)
        await db.flush()
        user = await db.get(User, outsider.id)
        assert user is not None
        applied = await sso_service.sync_org_role(
            db, user=user, connection=conn, idp_groups=("admins",)
        )
        await db.commit()
    assert applied is None
    assert await _membership(outsider.id, t.org_b) is None
    async with AsyncSessionLocal() as db:
        seats = await db.scalar(
            select(func.count())
            .select_from(TeamMembership)
            .where(TeamMembership.user_id == outsider.id, TeamMembership.org_team_id == t.org_b)
        )
    assert seats == 0


async def test_role_sync_updates_a_seat_the_person_already_holds(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        conn = SsoConnection(
            org_team_id=t.org_b,
            enabled=True,
            groups_mapping={"admins": "admin"},
        )
        db.add(conn)
        await db.flush()
        user = await db.get(User, t.user.id)
        assert user is not None
        applied = await sso_service.sync_org_role(
            db, user=user, connection=conn, idp_groups=("admins",)
        )
        await db.commit()
    assert applied is TeamRole.ADMIN


def _as_the_org_sees_it(user: dict[str, Any]) -> dict[str, Any]:
    """A SCIM User without the parts that name the address itself."""
    return {k: v for k, v in user.items() if k not in {"id", "userName", "emails", "meta"}}


@pytest.mark.parametrize("multi_org_on", [True, False], ids=["multi-org-on", "multi-org-off"])
@pytest.mark.parametrize("standing", ["in-good-standing", "disabled"])
@pytest.mark.parametrize("active", [True, False], ids=["created-active", "created-inactive"])
async def test_scim_answers_alike_whether_or_not_the_address_has_an_account_elsewhere(
    two_org_identity: TwoOrg,
    monkeypatch: pytest.MonkeyPatch,
    multi_org_on: bool,
    standing: str,
    active: bool,
) -> None:
    """Provisioning an address tells an org nothing about other orgs.

    An address that already had an account elsewhere was a 409 while multi-org
    was off, a 409 when that account was disabled or the IdP created the user
    inactive, and otherwise came back with blank names where a new address
    came back with the names the IdP sent. Each difference told the org which
    addresses had accounts elsewhere. The create now answers the same status
    and the same user for both, on every one of those branches."""
    monkeypatch.setattr(settings, "multi_org_enabled", multi_org_on)
    t = two_org_identity
    raw = await _scim_token(t.org_b)
    elsewhere, _ = await make_member_in(t.org_a)
    if standing == "disabled":
        async with AsyncSessionLocal() as db:
            await db.execute(update(User).where(User.id == elsewhere.id).values(is_active=False))
            await db.commit()
    fresh = f"fresh-{secrets.token_hex(4)}@alkera.dev"
    body = {"name": {"givenName": "Ada", "familyName": "Lovelace"}, "active": active}
    async with app_client() as c:
        made_elsewhere = await c.post(
            "/api/v1/scim/v2/Users",
            json={**body, "userName": elsewhere.email, "externalId": "okta-1"},
            headers=_bearer(raw),
        )
        made_fresh = await c.post(
            "/api/v1/scim/v2/Users",
            json={**body, "userName": fresh, "externalId": "okta-1b"},
            headers=_bearer(raw),
        )

    def seen(resp: Any) -> dict[str, Any]:
        shown = _as_the_org_sees_it(resp.json())
        shown.pop("externalId")
        return shown

    assert (made_elsewhere.status_code, made_fresh.status_code) == (201, 201), (
        made_elsewhere.text,
        made_fresh.text,
    )
    assert seen(made_elsewhere) == seen(made_fresh)
    assert seen(made_elsewhere)["name"] == {"givenName": "Ada", "familyName": "Lovelace"}
    assert seen(made_elsewhere)["active"] is active
    assert set(made_elsewhere.headers) == set(made_fresh.headers)
    # Nothing was attached: the identity holds no seat in the org and its own
    # names and standing are as they were.
    membership = await _membership(elsewhere.id, t.org_b)
    if active:
        assert membership is not None and membership.status is MembershipStatus.PENDING
    else:
        assert membership is None
    identity = await _identity(elsewhere.id)
    assert (identity.first_name, identity.last_name) == (elsewhere.first_name, elsewhere.last_name)
    assert identity.is_active is (standing != "disabled")
    assert identity.home_org_team_id == t.org_a


@pytest.mark.usefixtures("multi_org")
async def test_scim_reads_alike_whether_or_not_the_address_has_an_account_elsewhere(
    two_org_identity: TwoOrg,
) -> None:
    """Provisioning an address that already has an account in another org came
    back with blank names, while a new address came back with the names the
    IdP sent: the org learned which addresses had accounts elsewhere. Both are
    the names the org's IdP pushed, on the create and on every read."""
    t = two_org_identity
    raw = await _scim_token(t.org_b)
    elsewhere, _ = await make_member_in(t.org_a)
    fresh = f"fresh-{secrets.token_hex(4)}@alkera.dev"
    body = {"name": {"givenName": "Ada", "familyName": "Lovelace"}, "active": True}
    async with app_client() as c:
        made_elsewhere = await c.post(
            "/api/v1/scim/v2/Users",
            json={**body, "userName": elsewhere.email, "externalId": "okta-1"},
            headers=_bearer(raw),
        )
        made_fresh = await c.post(
            "/api/v1/scim/v2/Users",
            json={**body, "userName": fresh, "externalId": "okta-1b"},
            headers=_bearer(raw),
        )
        assert made_elsewhere.status_code == made_fresh.status_code == 201, (
            made_elsewhere.text,
            made_fresh.text,
        )
        read_elsewhere = await c.get(
            f"/api/v1/scim/v2/Users/{made_elsewhere.json()['id']}", headers=_bearer(raw)
        )
        read_fresh = await c.get(
            f"/api/v1/scim/v2/Users/{made_fresh.json()['id']}", headers=_bearer(raw)
        )

    def seen(resp: Any) -> dict[str, Any]:
        shown = _as_the_org_sees_it(resp.json())
        shown.pop("externalId")
        return shown

    assert seen(made_elsewhere) == seen(made_fresh)
    assert seen(read_elsewhere) == seen(read_fresh)
    assert seen(made_elsewhere)["name"] == {"givenName": "Ada", "familyName": "Lovelace"}
    assert seen(read_elsewhere)["displayName"] == "Ada Lovelace"
    identity = await _identity(elsewhere.id)
    assert (identity.first_name, identity.last_name) == (elsewhere.first_name, elsewhere.last_name)


async def test_a_membership_scim_left_pending_cannot_be_joined_while_multi_org_is_off(
    two_org_identity: TwoOrg,
) -> None:
    """With multi-org off the pending membership is a record of what the IdP
    asked for and nothing more: the person is not offered it and cannot take
    it, so an identity still belongs to one org."""
    t = two_org_identity
    raw = await _scim_token(t.org_b)
    elsewhere, _ = await make_member_in(t.org_a)
    async with app_client() as c:
        made = await c.post(
            "/api/v1/scim/v2/Users",
            json={"userName": elsewhere.email, "name": {"givenName": "X", "familyName": "Y"}},
            headers=_bearer(raw),
        )
    assert made.status_code == 201, made.text
    async with AsyncSessionLocal() as db:
        identity = await db.get(User, elsewhere.id)
        assert identity is not None
        assert await org_choice.joinable(db, identity, t.org_b) is None
        offered = await org_choice.memberships_of(db, identity)
        assert [view.status for view in offered if view.status == "pending"] == []
        active = await org_membership_service.list_active_for_user(db, identity.id)
    assert [m.org_team_id for m in active] == [t.org_a]
