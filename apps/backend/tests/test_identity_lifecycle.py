"""The identity's own levers, and the paths that resolve a person without a
credential, now that an org offboards on the membership.

``users.is_active`` is the platform-level disable (only staff set it); a
failed sign-in, a lockout and a ban are the identity's events and land in its
security log. The paths that find a person through something other than a
credential (an invitation's inviter) hold them to their
membership in the org, as every credential door does: dropping the trigger
that mirrored ``is_active`` onto the home membership must not let a person an
org deactivated keep acting there through them.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.auth import revocation
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    IdentitySecurityEvent,
    Invitation,
    MembershipStatus,
    OrgAuditEvent,
    OrgMembership,
    PlatformRole,
    TeamRole,
    User,
)
from backend.services.org import invitations as invitation_service
from backend.services.org import org_memberships as org_membership_service
from backend.services.org import teams as team_service
from sqlalchemy import func, select, update
from tests.conftest import OrgWithAdmin, TwoOrg, app_client, make_member, mint_cli_token

pytestmark = pytest.mark.asyncio


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _events(user_id: UUID) -> list[str]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(IdentitySecurityEvent.event)
            .where(IdentitySecurityEvent.user_id == user_id)
            .order_by(IdentitySecurityEvent.created_at)
        )
        return list(rows.scalars().all())


async def _org_rows(email: str, action: str) -> int:
    async with AsyncSessionLocal() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(OrgAuditEvent)
            .where(OrgAuditEvent.action == action, OrgAuditEvent.target == email)
        )
    return int(count or 0)


async def _set_status(user_id: UUID, org: UUID, status: MembershipStatus) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(OrgMembership)
            .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org)
            .values(status=status)
        )
        await db.commit()


# --- the platform disable ------------------------------------------------------------


@pytest.mark.usefixtures("multi_org")
async def test_the_platform_disable_ends_the_identity_everywhere_and_leaves_its_memberships(
    two_org_identity: TwoOrg, platform_admin: OrgWithAdmin
) -> None:
    t = two_org_identity
    staff = await mint_cli_token(
        user_id=platform_admin.admin_id,
        email=platform_admin.admin_email,
        org_team_id=platform_admin.org_id,
        platform_role=PlatformRole.ALKERA_ADMIN,
    )
    async with app_client() as c:
        disabled = await c.put(
            f"/admin/v1/users/{t.user.id}/active", json={"active": False}, headers=_bearer(staff)
        )
        assert disabled.status_code == 200, disabled.text
        for token in (t.token_a, t.token_b):
            assert (await c.get("/api/v1/auth/me", headers=_bearer(token))).status_code == 401
    async with AsyncSessionLocal() as db:
        statuses = set(
            (
                await db.execute(
                    select(OrgMembership.status).where(OrgMembership.user_id == t.user.id)
                )
            )
            .scalars()
            .all()
        )
    assert statuses == {MembershipStatus.ACTIVE}
    assert "platform.user_disabled" in await _events(t.user.id)

    async with app_client() as c:
        enabled = await c.put(
            f"/admin/v1/users/{t.user.id}/active", json={"active": True}, headers=_bearer(staff)
        )
    assert enabled.status_code == 200
    assert "platform.user_enabled" in await _events(t.user.id)


async def test_an_org_admin_cannot_reach_the_platform_disable(two_org_identity: TwoOrg) -> None:
    t = two_org_identity
    async with app_client() as c:
        resp = await c.put(
            f"/admin/v1/users/{t.user.id}/active",
            json={"active": False},
            headers=_bearer(t.token_a),
        )
    assert resp.status_code in (401, 403)
    async with AsyncSessionLocal() as db:
        user = await db.get(User, t.user.id)
    assert user is not None and user.is_active


# --- failed sign-ins and lockouts ------------------------------------------------------


async def test_a_failed_sign_in_and_the_lockout_are_identity_events_not_org_rows(
    org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_lockout_threshold", 2)
    async with app_client() as c:
        for _ in range(2):
            resp = await c.post(
                "/api/v1/auth/login", json={"email": org_admin.admin_email, "password": "nope-nope"}
            )
            assert resp.status_code == 401
    assert await _events(org_admin.admin_id) == [
        "auth.login_failed",
        "auth.locked_out",
        "auth.login_failed",
    ]
    assert await _org_rows(org_admin.admin_email, "auth.login_failed") == 0


async def test_a_successful_sign_in_is_audited_in_the_org_it_entered(
    org_admin: OrgWithAdmin,
) -> None:
    async with app_client() as c:
        resp = await c.post(
            "/api/v1/auth/login",
            json={"email": org_admin.admin_email, "password": org_admin.admin_password},
        )
    assert resp.status_code == 200, resp.text
    async with AsyncSessionLocal() as db:
        orgs = (
            await db.execute(
                select(OrgAuditEvent.org_team_id).where(
                    OrgAuditEvent.actor_id == org_admin.admin_id,
                    OrgAuditEvent.action == "auth.login",
                )
            )
        ).scalars()
        assert set(orgs.all()) == {org_admin.org_id}


# --- membership standing on credential-less paths ---------------------------------------


async def _invitation(inviter: UUID, team: UUID) -> Invitation:
    async with AsyncSessionLocal() as db:
        invitation = Invitation(
            email=f"invitee-{secrets.token_hex(4)}@alkera.dev",
            team_id=team,
            role=TeamRole.MEMBER,
            invited_by_id=inviter,
            token=secrets.token_hex(32),
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        db.add(invitation)
        await db.commit()
        return invitation


@pytest.mark.parametrize(
    ("status", "authorized"),
    [
        pytest.param(MembershipStatus.ACTIVE, True, id="active-inviter"),
        pytest.param(MembershipStatus.DEACTIVATED, False, id="deactivated-inviter"),
    ],
)
async def test_an_invitation_stands_only_while_its_inviter_stands_in_the_org(
    org_admin: OrgWithAdmin, status: MembershipStatus, authorized: bool
) -> None:
    invitation = await _invitation(org_admin.admin_id, org_admin.org_id)
    await _set_status(org_admin.admin_id, org_admin.org_id, status)
    async with AsyncSessionLocal() as db:
        chain = await team_service.ancestor_chain(db, org_admin.org_id)
        stored = await db.get(Invitation, invitation.id)
        assert stored is not None
        assert await invitation_service._inviter_still_authorized(db, stored, chain) is authorized


async def test_an_invitation_from_someone_removed_from_the_org_is_dead(
    org_admin: OrgWithAdmin,
) -> None:
    async with AsyncSessionLocal() as db:
        inviter, _ = await make_member(db, org_id=org_admin.org_id, role=TeamRole.ADMIN)
        await db.commit()
    invitation = await _invitation(inviter.id, org_admin.org_id)
    async with AsyncSessionLocal() as db:
        membership = await org_membership_service.get(
            db, user_id=inviter.id, org_team_id=org_admin.org_id
        )
        assert membership is not None
        await org_membership_service.remove(db, membership, actor=None)
        await db.commit()
        chain = await team_service.ancestor_chain(db, org_admin.org_id)
        stored = await db.get(Invitation, invitation.id)
        assert stored is not None
        assert await invitation_service._inviter_still_authorized(db, stored, chain) is False


async def test_revoke_memberships_takes_one_orgs_memberships_only(
    two_org_identity: TwoOrg,
) -> None:
    t = two_org_identity
    async with AsyncSessionLocal() as db:
        rows = list(
            (await db.execute(select(OrgMembership).where(OrgMembership.user_id == t.user.id)))
            .scalars()
            .all()
        )
        with pytest.raises(ValueError, match="one org"):
            await revocation.revoke_memberships(db, rows, reason="test")
        await db.rollback()
