"""The doors that give an existing identity another org, through the real routes.

With multi-org on, a person joins a second org four ways, and leaves one:

* accepting an invitation to their own address, by id from their list or by
  the emailed link while signed in (``POST /invitations/by-token/{token}/accept``);
* founding an org (``POST /orgs``);
* from the sign-in landing a person with no org reaches
  (``POST /auth/refresh/org/new`` and ``/join``);
* leaving the org their session is in (``POST /orgs/current/leave``).

And signup or provider registration for an address that already has an account
answers ``account_exists`` with a sign-in path that keeps the invitation.

Every door is adversarial-tested with two orgs against real Postgres: a link
redeemed by the wrong account, an unverified squatter on the invited address,
a deactivated membership, a non-browser credential, the creation cap across its
30-day window, the last admin, and a departure that must end one org's
credentials and nothing else. Each refusal asserts nothing was created. Every
door is inert with multi-org off.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from alkera_core.auth import encode_register_ticket
from alkera_core.authz import PrincipalKind, Role
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    IdentityOrgCreation,
    IdentitySecurityEvent,
    Invitation,
    InvitationStatus,
    MembershipStatus,
    OrgAuditEvent,
    OrgMembership,
    OrgSettings,
    RoleAssignment,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from backend.auth.session_issue import issue_session
from backend.services.identity.org_choice import account_exists_detail
from backend.services.org import invitations as invitation_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service
from fastapi import Request, Response
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import (
    OrgWithAdmin,
    TwoOrg,
    _unique_email,
    _unique_org_name,
    app_client,
    make_member,
)

REFRESH_COOKIE = settings.auth_refresh_cookie_name
ACCESS_COOKIE = settings.auth_cookie_name
CSRF = {"X-Requested-With": "alkera"}


@pytest.fixture(autouse=True)
def _fresh_revocation_cache() -> Iterator[None]:
    from alkera_core.auth import revocation

    revocation._cache.reset()
    yield
    revocation._cache.reset()


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Several cases sign in and call the same route repeatedly from one address;
    # a throttle 429 must never stand in for the refusal under test.
    monkeypatch.setattr(settings, "rate_limit_enabled", False)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


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


@asynccontextmanager
async def _browser(user_id: UUID, org_id: UUID) -> AsyncIterator[AsyncClient]:
    """A browser signed in by password into ``org_id``, with both cookies."""
    carrier = Response()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        await issue_session(
            db, user, request=_request(), response=carrier, method="password", org_team_id=org_id
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _error(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    error = body.get("error")
    assert isinstance(error, dict), resp.text
    return error


async def _invite(
    session: AsyncSession,
    *,
    org_id: UUID,
    inviter_id: UUID,
    email: str,
    role: TeamRole = TeamRole.MEMBER,
    team_name: str | None = None,
) -> tuple[Invitation, str, UUID]:
    """A pending invitation into a sub-team of ``org_id`` (the root when no
    name is given). Returns the row, the raw link token and the team."""
    inviter = await session.get(User, inviter_id)
    assert inviter is not None
    if team_name is None:
        team = await session.get(Team, org_id)
    else:
        team = await team_service.create_subteam(session, org_team_id=org_id, name=team_name)
    assert team is not None
    invitation, auto, raw = await invitation_service.create_invitation(
        session, team=team, email=email, role=role, invited_by=inviter, org_team_id=org_id
    )
    assert not auto
    await session.commit()
    return invitation, raw, team.id


async def _membership(user_id: UUID, org_id: UUID) -> OrgMembership | None:
    async with AsyncSessionLocal() as s:
        return await org_membership_service.get(s, user_id=user_id, org_team_id=org_id)


async def _team_seats(user_id: UUID, org_id: UUID) -> list[TeamMembership]:
    async with AsyncSessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(TeamMembership).where(
                        TeamMembership.user_id == user_id, TeamMembership.org_team_id == org_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def _invitation_status(invitation_id: UUID) -> InvitationStatus:
    async with AsyncSessionLocal() as s:
        row = await s.get(Invitation, invitation_id)
        assert row is not None
        return row.status


async def _security_events(user_id: UUID, event: str) -> list[IdentitySecurityEvent]:
    async with AsyncSessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(IdentitySecurityEvent).where(
                        IdentitySecurityEvent.user_id == user_id,
                        IdentitySecurityEvent.event == event,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _audit_actions(org_id: UUID, action: str) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action == action
                    )
                )
            )
            .scalars()
            .all()
        )


async def _roots_named(name: str) -> int:
    async with AsyncSessionLocal() as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(Team).where(Team.name == name, Team.is_root)
            )
            or 0
        )


async def _user_count() -> int:
    async with AsyncSessionLocal() as s:
        return int(await s.scalar(select(func.count()).select_from(User)) or 0)


async def _person(
    session: AsyncSession, org_id: UUID, *, verified: bool = True, email: str | None = None
) -> tuple[User, str]:
    user, password = await make_member(
        session, org_id=org_id, email=email or _unique_email("person"), verified=verified
    )
    assert password is not None
    return user, password


# --------------------------------------------------------------------------
# pure pieces
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        pytest.param("alice@example.com", "a***@example.com", id="ordinary"),
        pytest.param("a@x.io", "a***@x.io", id="one-letter-local-part"),
        pytest.param("@x.io", "***@x.io", id="empty-local-part"),
        pytest.param("not-an-address", "***", id="no-at-sign"),
    ],
)
def test_an_invited_address_is_masked_to_its_first_letter_and_domain(
    email: str, masked: str
) -> None:
    assert invitation_service.masked_address(email) == masked


@pytest.mark.parametrize(
    ("token", "next_path"),
    [
        pytest.param(None, "/login", id="no-invitation"),
        pytest.param("", "/login", id="empty-token"),
        pytest.param("abc_DEF-123", "/login?invite=abc_DEF-123", id="token-carried"),
        pytest.param("a b&c", "/login?invite=a+b%26c", id="token-url-encoded"),
    ],
)
def test_account_exists_sends_to_sign_in_keeping_the_invitation(
    token: str | None, next_path: str
) -> None:
    detail = account_exists_detail(token)
    assert detail["code"] == "account_exists"
    assert detail["next"] == next_path


# --------------------------------------------------------------------------
# invitations: joining a second org
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_a_signed_in_person_accepts_a_link_into_a_second_org_and_switches_into_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    home, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("home-admin"),
        admin_first_name="Home",
        admin_last_name="Admin",
        admin_password="home-admin-pass-123",
    )
    await real_session.commit()
    user, _ = await _person(real_session, home.id)
    invitation, raw, team_id = await _invite(
        real_session,
        org_id=org_admin.org_id,
        inviter_id=org_admin.admin_id,
        email=user.email,
        role=TeamRole.ADMIN,
        team_name="Analytics",
    )
    b_name = (await real_session.get(Team, org_admin.org_id)).name  # type: ignore[union-attr]

    async with _browser(user.id, home.id) as browser:
        resp = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["org_team_id"] == str(org_admin.org_id)
        assert body["org_name"] == b_name
        assert body["joined_team_ids"][-1] == str(org_admin.org_id)
        assert body["invitation"]["status"] == "accepted"

        # The session that accepted is still in the home org and still works.
        me = await browser.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["org_team_id"] == str(home.id)

        # The new membership is enterable: the switch moves the browser in.
        switched = await browser.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(org_admin.org_id)}, headers=CSRF
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["user"]["org_team_id"] == str(org_admin.org_id)

    joined = await _membership(user.id, org_admin.org_id)
    assert joined is not None and joined.status is MembershipStatus.ACTIVE
    seats = {(s.team_id, s.role) for s in await _team_seats(user.id, org_admin.org_id)}
    assert (team_id, TeamRole.ADMIN) in seats
    assert (org_admin.org_id, TeamRole.MEMBER) in seats
    assert await _invitation_status(invitation.id) is InvitationStatus.ACCEPTED
    assert len(await _audit_actions(org_admin.org_id, "invitation.accepted")) == 1
    joined_events = await _security_events(user.id, "auth.org_joined")
    assert [e.org_team_id for e in joined_events] == [org_admin.org_id]
    # The home org's chain never hears of the second org.
    assert await _audit_actions(home.id, "invitation.accepted") == []


@pytest.mark.usefixtures("multi_org")
async def test_the_recipient_list_offers_another_orgs_invitation_and_accepts_it_by_id(
    real_session: AsyncSession, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    from tests.conftest import login

    home, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("home-admin"),
        admin_first_name="Home",
        admin_last_name="Admin",
        admin_password="home-admin-pass-123",
    )
    await real_session.commit()
    user, password = await _person(real_session, home.id)
    invitation, _raw, _team = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=user.email
    )

    await login(client, user.email, password)
    mine = await client.get("/api/v1/invitations/me")
    assert mine.status_code == 200, mine.text
    row = next(r for r in mine.json() if r["id"] == str(invitation.id))
    assert row["refusal"] is None

    accept = await client.post(f"/api/v1/invitations/{invitation.id}/accept")
    assert accept.status_code == 200, accept.text
    assert accept.json()["org_team_id"] == str(org_admin.org_id)
    joined = await _membership(user.id, org_admin.org_id)
    assert joined is not None and joined.status is MembershipStatus.ACTIVE


async def test_with_multi_org_off_the_link_route_is_absent_and_joins_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    home, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("home-admin"),
        admin_first_name="Home",
        admin_last_name="Admin",
        admin_password="home-admin-pass-123",
    )
    await real_session.commit()
    user, _ = await _person(real_session, home.id)
    invitation, raw, _team = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=user.email
    )
    async with _browser(user.id, home.id) as browser:
        resp = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert resp.status_code == 404, resp.text
        # By id it is still the single-org rule's refusal.
        by_id = await browser.post(f"/api/v1/invitations/{invitation.id}/accept")
        assert by_id.status_code == 409, by_id.text
        assert _error(by_id)["code"] == "other_org"
        mine = await browser.get("/api/v1/invitations/me")
        row = next(r for r in mine.json() if r["id"] == str(invitation.id))
        assert row["refusal"]["code"] == "other_org"
    assert await _membership(user.id, org_admin.org_id) is None
    assert await _invitation_status(invitation.id) is InvitationStatus.PENDING


@pytest.mark.usefixtures("multi_org")
async def test_a_link_for_another_address_is_refused_with_it_masked_and_creates_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Invitation hijack: b@x, signed in, presents a@x's link."""
    home, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("home-admin"),
        admin_first_name="Home",
        admin_last_name="Admin",
        admin_password="home-admin-pass-123",
    )
    await real_session.commit()
    invited_email = _unique_email("alice")
    attacker, _ = await _person(real_session, home.id)
    invitation, raw, _team = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=invited_email
    )

    async with _browser(attacker.id, home.id) as browser:
        resp = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert resp.status_code == 409, resp.text
        error = _error(resp)
        assert error["code"] == "invitation_other_account"
        assert error["message"] == (
            f"This invitation is for {invitation_service.masked_address(invited_email)}. "
            "Sign in with that email to accept."
        )
        assert invited_email not in resp.text
        # By id the invitation does not even exist for this account.
        by_id = await browser.post(f"/api/v1/invitations/{invitation.id}/accept")
        assert by_id.status_code == 404, by_id.text

    assert await _membership(attacker.id, org_admin.org_id) is None
    assert await _team_seats(attacker.id, org_admin.org_id) == []
    assert await _invitation_status(invitation.id) is InvitationStatus.PENDING


@pytest.mark.usefixtures("multi_org")
async def test_an_unverified_squatter_on_the_invited_address_cannot_accept(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-hijack: someone registered a@x without proving the inbox, inside the
    verification grace window, and waits for an invitation to a@x."""
    monkeypatch.setattr(settings, "email_enabled", True)
    squat_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("squat-admin"),
        admin_first_name="Squat",
        admin_last_name="Admin",
        admin_password="squat-admin-pass-123",
    )
    await real_session.commit()
    squatter, _ = await _person(real_session, squat_org.id, verified=False)
    invitation, raw, _team = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=squatter.email
    )

    async with _browser(squatter.id, squat_org.id) as browser:
        for path in (
            f"/api/v1/invitations/by-token/{raw}/accept",
            f"/api/v1/invitations/{invitation.id}/accept",
        ):
            resp = await browser.post(path)
            assert resp.status_code == 409, resp.text
            assert _error(resp)["code"] == "email_verification_required"
        mine = await browser.get("/api/v1/invitations/me")
        row = next(r for r in mine.json() if r["id"] == str(invitation.id))
        assert row["refusal"]["code"] == "email_verification_required"

    assert await _membership(squatter.id, org_admin.org_id) is None
    assert await _team_seats(squatter.id, org_admin.org_id) == []
    assert await _invitation_status(invitation.id) is InvitationStatus.PENDING

    # Once the address is proven, the same link joins.
    async with AsyncSessionLocal() as s:
        row_user = await s.get(User, squatter.id)
        assert row_user is not None
        row_user.email_verified_at = datetime.now(UTC)
        await s.commit()
    async with _browser(squatter.id, squat_org.id) as browser:
        resp = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert resp.status_code == 200, resp.text


@pytest.mark.usefixtures("multi_org")
async def test_an_invitation_never_restores_a_membership_the_org_deactivated(
    real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    membership = await org_membership_service.get(
        real_session, user_id=t.user.id, org_team_id=t.org_b
    )
    assert membership is not None
    await org_membership_service.deactivate(real_session, membership, actor=None)
    await real_session.commit()
    invitation, raw, sub = await _invite(
        real_session, org_id=t.org_b, inviter_id=t.admin_b.id, email=t.user.email, team_name="Ops"
    )

    async with _browser(t.user.id, t.org_a) as browser:
        resp = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert resp.status_code == 409, resp.text
        assert _error(resp)["code"] == "membership_deactivated"

    still = await _membership(t.user.id, t.org_b)
    assert still is not None and still.status is MembershipStatus.DEACTIVATED
    assert sub not in {s.team_id for s in await _team_seats(t.user.id, t.org_b)}
    assert await _invitation_status(invitation.id) is InvitationStatus.PENDING


@pytest.mark.usefixtures("multi_org")
async def test_a_pending_membership_is_activated_by_accepting(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    home, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("home-admin"),
        admin_first_name="Home",
        admin_last_name="Admin",
        admin_password="home-admin-pass-123",
    )
    await real_session.commit()
    user, _ = await _person(real_session, home.id)
    pending = await org_membership_service.create(
        real_session,
        user_id=user.id,
        org_team_id=org_admin.org_id,
        status=MembershipStatus.PENDING,
    )
    await real_session.commit()
    _invitation, raw, _team = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=user.email
    )
    async with _browser(user.id, home.id) as browser:
        resp = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert resp.status_code == 200, resp.text
    joined = await _membership(user.id, org_admin.org_id)
    assert joined is not None
    assert joined.id == pending.id
    assert joined.status is MembershipStatus.ACTIVE


@pytest.mark.usefixtures("multi_org")
async def test_inviting_an_address_from_another_org_reveals_nothing_and_seats_nothing(
    real_session: AsyncSession,
    client: AsyncClient,
    two_org_identity: TwoOrg,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send: list[Any],
) -> None:
    """The creating admin's answer for an address with an account elsewhere is
    the one an unknown address gets: a pending row and a sent email."""
    from tests.conftest import login

    t = two_org_identity
    await login(client, org_admin.admin_email, org_admin.admin_password)
    known = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/invitations", json={"email": t.user.email}
    )
    unknown = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/invitations", json={"email": _unique_email("nobody")}
    )
    assert known.status_code == unknown.status_code == 201, (known.text, unknown.text)
    assert known.json()["status"] == unknown.json()["status"] == "pending"
    assert set(known.json()) == set(unknown.json())
    assert len(monkeypatch_email_send) == 2
    assert await _membership(t.user.id, org_admin.org_id) is None


@pytest.mark.usefixtures("multi_org")
async def test_a_member_joining_another_org_is_invisible_to_their_first_orgs_admin(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    home, home_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=_unique_org_name(),
        admin_email=_unique_email("home-admin"),
        admin_first_name="Home",
        admin_last_name="Admin",
        admin_password="home-admin-pass-123",
    )
    await real_session.commit()
    user, _ = await _person(real_session, home.id)
    _invitation, raw, _team = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=user.email
    )
    b_name = (await real_session.get(Team, org_admin.org_id)).name  # type: ignore[union-attr]
    async with _browser(user.id, home.id) as browser:
        joined = await browser.post(f"/api/v1/invitations/by-token/{raw}/accept")
        assert joined.status_code == 200, joined.text

    async with _browser(home_admin.id, home.id) as admin:
        members = await admin.get("/api/v1/org/members")
        assert members.status_code == 200, members.text
        assert str(user.id) in members.text
        assert str(org_admin.org_id) not in members.text
        assert b_name not in members.text


@pytest.mark.usefixtures("multi_org")
async def test_an_invitation_into_another_org_of_the_person_is_named_and_taken_from_any_session(
    real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    """U, signed in to A, is invited to a team of B, where U already belongs:
    the list names B (not the org the session is in) and the accept seats U on
    B's team without a second membership."""
    t = two_org_identity
    # Issued before U joined B (an address already in the org is seated at
    # once instead), and still pending.
    team = await team_service.create_subteam(real_session, org_team_id=t.org_b, name="BI")
    team_id = team.id
    invitation = Invitation(
        team_id=team_id,
        email=t.user.email,
        role=TeamRole.MEMBER,
        token=secrets.token_hex(16),
        status=InvitationStatus.PENDING,
        invited_by_id=t.admin_b.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    real_session.add(invitation)
    await real_session.commit()
    async with AsyncSessionLocal() as s:
        b_name = (await s.get(Team, t.org_b)).name  # type: ignore[union-attr]
        a_name = (await s.get(Team, t.org_a)).name  # type: ignore[union-attr]
    async with _browser(t.user.id, t.org_a) as browser:
        mine = await browser.get("/api/v1/invitations/me")
        row = next(r for r in mine.json() if r["id"] == str(invitation.id))
        assert row["org_name"] == b_name != a_name
        assert row["refusal"] is None
        accept = await browser.post(f"/api/v1/invitations/{invitation.id}/accept")
        assert accept.status_code == 200, accept.text
        assert accept.json()["org_team_id"] == str(t.org_b)
    assert team_id in {s.team_id for s in await _team_seats(t.user.id, t.org_b)}
    # Already a member: nothing new for the security log.
    assert await _security_events(t.user.id, "auth.org_joined") == []


# --------------------------------------------------------------------------
# signup and provider registration for an address with an account
# --------------------------------------------------------------------------


@pytest.mark.parametrize("multi", [True, False], ids=["multi-org-on", "multi-org-off"])
@pytest.mark.parametrize("with_invite", [True, False], ids=["with-invite", "no-invite"])
async def test_signup_for_an_existing_address_answers_by_the_flag(
    real_session: AsyncSession,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    multi: bool,
    with_invite: bool,
) -> None:
    monkeypatch.setattr(settings, "multi_org_enabled", multi)
    user, _ = await _person(real_session, org_admin.org_id)
    token = secrets.token_urlsafe(16) if with_invite else None
    before = await _user_count()

    payload: dict[str, Any] = {"email": user.email, "password": "Another-pass-98765!"}
    if token is not None:
        payload["invite_token"] = token
    resp = await client.post("/api/v1/auth/signup", json=payload)

    assert resp.status_code == 409, resp.text
    error = _error(resp)
    if multi:
        assert error["code"] == "account_exists"
        assert error["message"] == "You already have an account. Sign in to continue."
        expected = f"/login?invite={token}" if token else "/login"
        assert error["details"] == {"next": expected}
    else:
        assert error["message"] == "An account with that email already exists. Sign in instead."
        assert "details" not in error
    assert await _user_count() == before
    assert ACCESS_COOKIE not in resp.cookies


@pytest.mark.parametrize("multi", [True, False], ids=["multi-org-on", "multi-org-off"])
async def test_provider_registration_for_an_existing_address_answers_by_the_flag(
    real_session: AsyncSession,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    multi: bool,
) -> None:
    monkeypatch.setattr(settings, "multi_org_enabled", multi)
    user, _ = await _person(real_session, org_admin.org_id)
    invite = secrets.token_urlsafe(16)
    ticket = encode_register_ticket(
        provider="google",
        subject=f"google-{secrets.token_hex(6)}",
        email=user.email,
        email_verified=True,
        first_name="A",
        last_name="B",
        invite_token=invite,
    )
    before = await _user_count()
    resp = await client.post(
        "/api/v1/auth/oauth/register",
        json={"oauth_ticket": ticket, "first_name": "A", "last_name": "B"},
    )
    assert resp.status_code == 409, resp.text
    error = _error(resp)
    if multi:
        assert error["code"] == "account_exists"
        assert error["details"] == {"next": f"/login?invite={invite}"}
    else:
        assert error["message"] == "An account with that email already exists. Sign in instead."
        assert "details" not in error
    assert await _user_count() == before


# --------------------------------------------------------------------------
# founding an org
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_founding_an_org_seats_the_existing_identity_as_its_owner(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    user, _ = await _person(real_session, org_admin.org_id)
    users_before = await _user_count()
    name = _unique_org_name()

    async with _browser(user.id, org_admin.org_id) as browser:
        resp = await browser.post("/api/v1/orgs", json={"name": name})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["org_name"] == name
        new_org = UUID(body["org_team_id"])
        # The session stays where it was until the client switches.
        me = await browser.get("/api/v1/auth/me")
        assert me.json()["org_team_id"] == str(org_admin.org_id)
        switched = await browser.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(new_org)}, headers=CSRF
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["user"]["org_team_id"] == str(new_org)
        assert switched.json()["user"]["org_role"] == "admin"

    assert await _user_count() == users_before
    async with AsyncSessionLocal() as s:
        org = await s.get(Team, new_org)
        assert org is not None and org.is_root and org.parent_team_id is None
        assert await s.get(OrgSettings, new_org) is not None
        owner = (
            await s.execute(
                select(RoleAssignment).where(
                    RoleAssignment.org_team_id == new_org,
                    RoleAssignment.principal_kind == PrincipalKind.USER,
                    RoleAssignment.principal_id == user.id,
                    RoleAssignment.role == Role.OWNER,
                    RoleAssignment.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        assert owner is not None
        creations = (
            (
                await s.execute(
                    select(IdentityOrgCreation).where(IdentityOrgCreation.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert [c.org_team_id for c in creations] == [new_org]
    membership = await _membership(user.id, new_org)
    assert membership is not None and membership.status is MembershipStatus.ACTIVE
    assert {(s.team_id, s.role) for s in await _team_seats(user.id, new_org)} == {
        (new_org, TeamRole.ADMIN)
    }
    assert len(await _audit_actions(new_org, "org.created")) == 1
    assert await _audit_actions(org_admin.org_id, "org.created") == []
    assert [e.org_team_id for e in await _security_events(user.id, "auth.org_created")] == [new_org]


@pytest.mark.usefixtures("multi_org")
async def test_only_a_browser_session_may_found_an_org(
    real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    name = _unique_org_name()
    async with app_client() as cli:
        resp = await cli.post("/api/v1/orgs", json={"name": name}, headers=_bearer(t.token_a))
    assert resp.status_code == 403, resp.text
    assert _error(resp)["code"] == "browser_session_required"
    assert await _roots_named(name) == 0


@pytest.mark.usefixtures("multi_org")
async def test_an_unverified_account_may_not_found_an_org(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "email_enabled", True)
    user, _ = await _person(real_session, org_admin.org_id, verified=False)
    name = _unique_org_name()
    async with _browser(user.id, org_admin.org_id) as browser:
        resp = await browser.post("/api/v1/orgs", json={"name": name})
    assert resp.status_code == 403, resp.text
    assert _error(resp)["code"] == "email_verification_required"
    assert await _roots_named(name) == 0


@pytest.mark.usefixtures("multi_org")
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * 256, id="too-long"),
        pytest.param("Visit https://evil.example", id="a-link"),
    ],
)
async def test_an_org_name_is_held_to_the_org_name_rules(
    real_session: AsyncSession, org_admin: OrgWithAdmin, name: str
) -> None:
    user, _ = await _person(real_session, org_admin.org_id)
    async with AsyncSessionLocal() as s:
        before = int(
            await s.scalar(select(func.count()).select_from(Team).where(Team.is_root)) or 0
        )
    async with _browser(user.id, org_admin.org_id) as browser:
        resp = await browser.post("/api/v1/orgs", json={"name": name})
    assert resp.status_code == 422, resp.text
    async with AsyncSessionLocal() as s:
        after = int(await s.scalar(select(func.count()).select_from(Team).where(Team.is_root)) or 0)
    assert after == before


@pytest.mark.usefixtures("multi_org")
async def test_the_creation_cap_holds_for_thirty_days_and_lifts_after(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_org_creations_per_identity_30d", 2)
    user, _ = await _person(real_session, org_admin.org_id)
    start = datetime.now(UTC)

    async def found(name: str) -> httpx.Response:
        async with _browser(user.id, org_admin.org_id) as browser:
            return await browser.post("/api/v1/orgs", json={"name": name})

    with freeze_time(start, real_asyncio=True) as frozen:
        assert (await found(_unique_org_name())).status_code == 201
        assert (await found(_unique_org_name())).status_code == 201
        refused_name = _unique_org_name()
        refused = await found(refused_name)
        assert refused.status_code == 429, refused.text
        assert _error(refused)["code"] == "org_creation_limited"
        assert await _roots_named(refused_name) == 0

        frozen.move_to(start + timedelta(days=30) - timedelta(minutes=1))
        still = await found(_unique_org_name())
        assert still.status_code == 429, still.text

        frozen.move_to(start + timedelta(days=30, seconds=1))
        lifted = await found(_unique_org_name())
        assert lifted.status_code == 201, lifted.text


async def test_with_multi_org_off_founding_is_absent_and_creates_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    user, _ = await _person(real_session, org_admin.org_id)
    name = _unique_org_name()
    async with _browser(user.id, org_admin.org_id) as browser:
        resp = await browser.post("/api/v1/orgs", json={"name": name})
    assert resp.status_code == 404, resp.text
    assert await _roots_named(name) == 0


# --------------------------------------------------------------------------
# leaving an org
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_leaving_ends_that_orgs_credentials_and_nothing_else(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    async with _browser(t.user.id, t.org_b) as browser:
        resp = await browser.post("/api/v1/orgs/current/leave")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"next_org_team_id": str(t.org_a)}

        # The browser's own B access token is dead ...
        gone = await browser.get("/api/v1/auth/me")
        assert gone.status_code == 401, gone.text
        assert _error(gone)["code"] == "session_revoked"
        # ... and its login session moves into A.
        switched = await browser.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(t.org_a)}, headers=CSRF
        )
        assert switched.status_code == 200, switched.text
        # B is no longer enterable.
        back = await browser.post(
            "/api/v1/auth/refresh/org", json={"org_team_id": str(t.org_b)}, headers=CSRF
        )
        assert back.status_code == 404, back.text

    async with app_client() as cli:
        b = await cli.get("/api/v1/auth/me", headers=_bearer(t.token_b))
        assert b.status_code == 401, b.text
        assert _error(b)["code"] == "session_revoked"
        a = await cli.get("/api/v1/auth/me", headers=_bearer(t.token_a))
        assert a.status_code == 200, a.text
        assert a.json()["org_team_id"] == str(t.org_a)

    assert await _membership(t.user.id, t.org_b) is None
    assert await _team_seats(t.user.id, t.org_b) == []
    a_membership = await _membership(t.user.id, t.org_a)
    assert a_membership is not None and a_membership.status is MembershipStatus.ACTIVE
    assert len(await _audit_actions(t.org_b, "org.member_left")) == 1
    assert await _audit_actions(t.org_a, "org.member_left") == []
    assert [e.org_team_id for e in await _security_events(t.user.id, "auth.org_left")] == [t.org_b]


@pytest.mark.usefixtures("multi_org")
async def test_the_last_admin_cannot_leave(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with _browser(t.admin_b.id, t.org_b) as browser:
        resp = await browser.post("/api/v1/orgs/current/leave")
        assert resp.status_code == 409, resp.text
        assert _error(resp)["code"] == "last_admin"
        me = await browser.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
    still = await _membership(t.admin_b.id, t.org_b)
    assert still is not None and still.status is MembershipStatus.ACTIVE
    assert await _audit_actions(t.org_b, "org.member_left") == []


@pytest.mark.usefixtures("multi_org")
async def test_an_admin_may_leave_while_another_admin_stays(
    real_session: AsyncSession, two_org_identity: TwoOrg
) -> None:
    t = two_org_identity
    second, _ = await make_member(real_session, org_id=t.org_b, role=TeamRole.ADMIN, verified=True)
    async with _browser(t.admin_b.id, t.org_b) as browser:
        resp = await browser.post("/api/v1/orgs/current/leave")
        assert resp.status_code == 200, resp.text
        # The founder had no other org.
        assert resp.json() == {"next_org_team_id": None}
    assert await _membership(t.admin_b.id, t.org_b) is None
    assert await _membership(second.id, t.org_b) is not None


@pytest.mark.usefixtures("multi_org")
async def test_only_a_browser_session_may_leave(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with app_client() as cli:
        resp = await cli.post("/api/v1/orgs/current/leave", headers=_bearer(t.token_b))
    assert resp.status_code == 403, resp.text
    assert _error(resp)["code"] == "browser_session_required"
    assert await _membership(t.user.id, t.org_b) is not None


async def test_with_multi_org_off_leaving_is_absent_and_removes_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    user, _ = await _person(real_session, org_admin.org_id)
    async with _browser(user.id, org_admin.org_id) as browser:
        resp = await browser.post("/api/v1/orgs/current/leave")
        assert resp.status_code == 404, resp.text
    assert await _membership(user.id, org_admin.org_id) is not None


# --------------------------------------------------------------------------
# the sign-in landing of a person with no org
# --------------------------------------------------------------------------


async def _orgless(real_session: AsyncSession, org_admin: OrgWithAdmin) -> tuple[User, str]:
    """A person who left the only org they belonged to."""
    user, password = await _person(real_session, org_admin.org_id)
    async with _browser(user.id, org_admin.org_id) as browser:
        left = await browser.post("/api/v1/orgs/current/leave")
        assert left.status_code == 200, left.text
        assert left.json() == {"next_org_team_id": None}
    assert await _membership(user.id, org_admin.org_id) is None
    return user, password


@pytest.mark.usefixtures("multi_org")
async def test_a_person_with_no_org_signs_in_to_the_landing_and_founds_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    user, password = await _orgless(real_session, org_admin)
    async with app_client() as browser:
        landed = await browser.post(
            "/api/v1/auth/login", json={"email": user.email, "password": password}
        )
        assert landed.status_code == 403, landed.text
        assert _error(landed)["code"] == "no_active_membership"
        assert landed.cookies.get(REFRESH_COOKIE)
        assert not landed.cookies.get(ACCESS_COOKIE)
        # Nothing else is reachable: there is no access token.
        assert (await browser.get("/api/v1/auth/me")).status_code == 401

        name = _unique_org_name()
        missing_header = await browser.post("/api/v1/auth/refresh/org/new", json={"name": name})
        assert missing_header.status_code == 403, missing_header.text
        assert _error(missing_header)["code"] == "csrf"
        assert await _roots_named(name) == 0

        created = await browser.post(
            "/api/v1/auth/refresh/org/new", json={"name": name}, headers=CSRF
        )
        assert created.status_code == 201, created.text
        new_org = created.json()["user"]["org_team_id"]
        assert created.json()["user"]["org_name"] == name
        me = await browser.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["org_team_id"] == new_org
    assert await _membership(user.id, UUID(new_org)) is not None
    assert await _membership(user.id, org_admin.org_id) is None


@pytest.mark.usefixtures("multi_org")
async def test_a_person_with_no_org_joins_through_their_own_invitation_link_only(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    user, password = await _orgless(real_session, org_admin)
    other = _unique_email("someone-else")
    _theirs, foreign_raw, _ = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=other
    )
    mine, raw, _ = await _invite(
        real_session, org_id=org_admin.org_id, inviter_id=org_admin.admin_id, email=user.email
    )
    async with app_client() as browser:
        landed = await browser.post(
            "/api/v1/auth/login", json={"email": user.email, "password": password}
        )
        assert _error(landed)["code"] == "no_active_membership"

        wrong = await browser.post(
            "/api/v1/auth/refresh/org/join", json={"token": foreign_raw}, headers=CSRF
        )
        assert wrong.status_code == 409, wrong.text
        assert _error(wrong)["code"] == "invitation_other_account"
        assert await _membership(user.id, org_admin.org_id) is None

        joined = await browser.post(
            "/api/v1/auth/refresh/org/join", json={"token": raw}, headers=CSRF
        )
        assert joined.status_code == 200, joined.text
        assert joined.json()["user"]["org_team_id"] == str(org_admin.org_id)
        me = await browser.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
    assert await _invitation_status(mine.id) is InvitationStatus.ACCEPTED


async def test_with_multi_org_off_a_person_with_no_org_is_refused_as_before(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "multi_org_enabled", True)
    user, password = await _orgless(real_session, org_admin)
    monkeypatch.setattr(settings, "multi_org_enabled", False)
    async with app_client() as browser:
        resp = await browser.post(
            "/api/v1/auth/login", json={"email": user.email, "password": password}
        )
        assert resp.status_code == 403, resp.text
        assert _error(resp)["code"] == "session_org_revoked"
        assert not resp.cookies.get(REFRESH_COOKIE)
        name = _unique_org_name()
        landing = await browser.post(
            "/api/v1/auth/refresh/org/new", json={"name": name}, headers=CSRF
        )
        assert landing.status_code == 404, landing.text
        joining = await browser.post(
            "/api/v1/auth/refresh/org/join", json={"token": "x"}, headers=CSRF
        )
        assert joining.status_code == 404, joining.text
    assert await _roots_named(name) == 0
