"""Deprovisioning: a deactivated user can't authenticate (password OR live session)
and the org-admin deactivate/reactivate route, with lockout guards."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import actor_system
from alkera_core.models import (
    EmailDomainBan,
    IdentitySecurityEvent,
    MembershipStatus,
    OrgMembership,
    User,
    UserBan,
)
from backend.services.org import org_memberships as org_membership_service
from httpx import AsyncClient
from sqlalchemy import select, update
from tests.conftest import OrgWithAdmin, TwoOrg, app_client, login, make_member

pytestmark = pytest.mark.asyncio


async def _member(
    org_id: uuid.UUID, *, pw: str = "member-pass-123", domain: str = "member.example"
) -> tuple[str, str, uuid.UUID]:
    async with AsyncSessionLocal() as s:
        m, p = await make_member(
            s,
            org_id=org_id,
            email=f"u-{uuid.uuid4().hex[:8]}@{domain}",
            password=pw,
            verified=True,
        )
        email, uid = m.email, m.id
        await s.commit()
    assert p is not None
    return email, p, uid


async def _set_active(uid: uuid.UUID, active: bool) -> None:
    async with AsyncSessionLocal() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        u.is_active = active
        await s.commit()


async def test_current_user_rejects_a_deactivated_live_session(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    email, pw, uid = await _member(org_admin.org_id)
    await login(client, email, pw)
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    # Offboard out-of-band: the still-live session must stop working immediately.
    await _set_active(uid, False)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_admin_deactivate_blocks_login_then_reactivate_restores(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    email, pw, uid = await _member(org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    deact = await client.put(f"/api/v1/org/members/{uid}/active", json={"active": False})
    assert deact.status_code == 200, deact.text
    assert deact.json()["is_active"] is False

    blocked = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "account_deactivated"

    # The admin's cookie is intact (the 403 set none) → reactivate, login works again.
    react = await client.put(f"/api/v1/org/members/{uid}/active", json={"active": True})
    assert react.status_code == 200 and react.json()["is_active"] is True
    ok = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert ok.status_code == 200


async def test_admin_cannot_deactivate_self(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    async with AsyncSessionLocal() as s:
        admin = (
            await s.execute(select(User).where(User.email == org_admin.admin_email))
        ).scalar_one()
        admin_id = admin.id
    resp = await client.put(f"/api/v1/org/members/{admin_id}/active", json={"active": False})
    assert resp.status_code == 400


async def test_list_members_reports_active_and_admin(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    email, _pw, _uid = await _member(org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    members = (await client.get("/api/v1/org/members")).json()
    by_email = {m["email"]: m for m in members}
    assert by_email[org_admin.admin_email]["is_admin"] is True
    assert by_email[email]["is_admin"] is False
    assert by_email[email]["is_active"] is True


async def test_deactivate_foreign_org_member_is_404(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.put(f"/api/v1/org/members/{uuid.uuid4()}/active", json={"active": False})
    assert resp.status_code == 404


# --- reactivating a person an org offboarded before the membership existed ---------
#
# Until deactivation moved onto the membership, an org offboarded a person by
# writing ``users.is_active = false``. The upgrade that moved it kept those
# identities inactive and deactivated their home membership. Reactivating
# through the org must let them sign in again, unless the platform itself
# disabled or banned them.


async def _offboard_the_old_way(uid: uuid.UUID, org_id: uuid.UUID, *, membership: str) -> None:
    """The identity the upgrade leaves behind for a person an org offboarded
    before this release: inactive, with its home membership in ``membership``
    (``deactivated`` as the upgrade writes it, ``active`` when nothing moved it)."""
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == uid).values(is_active=False))
        await s.execute(
            update(OrgMembership)
            .where(OrgMembership.user_id == uid, OrgMembership.org_team_id == org_id)
            .values(
                status=MembershipStatus(membership),
                credential_epoch=OrgMembership.credential_epoch + 1,
            )
        )
        await s.commit()


async def _platform_events(uid: uuid.UUID, events: list[str]) -> None:
    """Platform disable / enable records, oldest first, an hour apart."""
    start = datetime.now(UTC) - timedelta(days=1)
    async with AsyncSessionLocal() as s:
        for i, event in enumerate(events):
            s.add(
                IdentitySecurityEvent(
                    user_id=uid, event=event, created_at=start + timedelta(hours=i)
                )
            )
        await s.commit()


async def _ban_account(uid: uuid.UUID, email: str) -> None:
    async with AsyncSessionLocal() as s:
        s.add(UserBan(user_id=uid, reason="abuse"))
        await s.commit()


async def _ban_domain(uid: uuid.UUID, email: str) -> None:
    async with AsyncSessionLocal() as s:
        s.add(EmailDomainBan(domain=email.split("@", 1)[1], reason="abuse"))
        await s.commit()


async def _identity_active(uid: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as s:
        return bool(await s.scalar(select(User.is_active).where(User.id == uid)))


async def _listed_active(client: AsyncClient, email: str) -> bool:
    members = (await client.get("/api/v1/org/members")).json()
    return bool(next(m for m in members if m["email"] == email)["is_active"])


@pytest.mark.parametrize(
    ("membership", "platform_events"),
    [
        pytest.param("deactivated", [], id="membership-deactivated-by-the-upgrade"),
        pytest.param("active", [], id="membership-left-active"),
        pytest.param(
            "deactivated",
            ["platform.user_disabled", "platform.user_enabled"],
            id="an-earlier-platform-disable-was-lifted",
        ),
    ],
)
async def test_reactivating_a_person_the_org_offboarded_before_this_release_lets_them_sign_in(
    client: AsyncClient, org_admin: OrgWithAdmin, membership: str, platform_events: list[str]
) -> None:
    email, pw, uid = await _member(org_admin.org_id)
    await _offboard_the_old_way(uid, org_admin.org_id, membership=membership)
    await _platform_events(uid, platform_events)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert await _listed_active(client, email) is False

    react = await client.put(f"/api/v1/org/members/{uid}/active", json={"active": True})
    assert react.status_code == 200, react.text
    assert react.json()["is_active"] is True
    assert await _listed_active(client, email) is True
    assert await _identity_active(uid) is True

    async with app_client() as person:
        signed_in = await person.post("/api/v1/auth/login", json={"email": email, "password": pw})
        assert signed_in.status_code == 200, signed_in.text
        me = await person.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        assert me.json()["org_team_id"] == str(org_admin.org_id)


Refusal = Callable[[uuid.UUID, str], Awaitable[None]]


async def _disabled(uid: uuid.UUID, email: str) -> None:
    await _platform_events(uid, ["platform.user_disabled"])


async def _disabled_again(uid: uuid.UUID, email: str) -> None:
    await _platform_events(
        uid, ["platform.user_disabled", "platform.user_enabled", "platform.user_disabled"]
    )


@pytest.mark.parametrize(
    "platform_refusal",
    [
        pytest.param(_disabled, id="platform-disabled"),
        pytest.param(_disabled_again, id="platform-disabled-again-after-an-enable"),
        pytest.param(_ban_account, id="account-banned"),
        pytest.param(_ban_domain, id="domain-banned"),
    ],
)
async def test_an_org_reactivation_never_lifts_what_the_platform_decided(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_refusal: Refusal
) -> None:
    # A domain of its own: a domain ban must not reach anyone else's test.
    email, pw, uid = await _member(org_admin.org_id, domain=f"{uuid.uuid4().hex[:12]}.example")
    await _offboard_the_old_way(uid, org_admin.org_id, membership="deactivated")
    await platform_refusal(uid, email)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    react = await client.put(f"/api/v1/org/members/{uid}/active", json={"active": True})
    assert react.status_code == 200, react.text
    # The org restored its own decision; the person still cannot act here.
    assert react.json()["is_active"] is False
    assert await _identity_active(uid) is False

    async with app_client() as person:
        refused = await person.post("/api/v1/auth/login", json={"email": email, "password": pw})
        assert refused.status_code in (401, 403), refused.text


async def test_the_roster_reports_a_platform_disabled_member_inactive(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    email, _pw, uid = await _member(org_admin.org_id)
    await _set_active(uid, False)
    await _platform_events(uid, ["platform.user_disabled"])
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert await _listed_active(client, email) is False
    react = await client.put(f"/api/v1/org/members/{uid}/active", json={"active": True})
    assert react.status_code == 200, react.text
    assert react.json()["is_active"] is False


@pytest.mark.usefixtures("multi_org")
async def test_only_the_home_org_lifts_an_identity_deactivation(two_org_identity: TwoOrg) -> None:
    """Before this release only the home org could have written the identity's
    deactivation, so another org restoring the person leaves the identity as
    it is; the home org restoring them lifts it."""
    t = two_org_identity
    await _offboard_the_old_way(t.user.id, t.org_a, membership="deactivated")
    actor = actor_system("test")
    async with AsyncSessionLocal() as s:
        b = await org_membership_service.get(s, user_id=t.user.id, org_team_id=t.org_b)
        assert b is not None
        await org_membership_service.deactivate(s, b, actor=actor)
        await org_membership_service.reactivate(s, b, actor=actor)
        await s.commit()
    assert await _identity_active(t.user.id) is False

    async with AsyncSessionLocal() as s:
        a = await org_membership_service.get(s, user_id=t.user.id, org_team_id=t.org_a)
        assert a is not None
        await org_membership_service.reactivate(s, a, actor=actor)
        await s.commit()
    assert await _identity_active(t.user.id) is True
