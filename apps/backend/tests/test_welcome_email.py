"""The founders welcome email fires exactly once, on first email verification.

Covers the once-ever guard directly, the new `welcome_email_sent_at` column's
persistence, and each user-facing verification path that should welcome a new
user: the verify-email route, invite-driven signup, and OAuth registration.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from alkera_core.auth import decode_register_ticket, encode_register_ticket, hash_lookup_token
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Invitation, InvitationStatus, TeamRole, User
from backend.services.identity import oauth as oauth_service
from backend.services.identity import welcome as welcome_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, make_member

pytestmark = pytest.mark.asyncio


async def test_guard_sends_once_and_stamps(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch_welcome_send
) -> None:
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    assert member.welcome_email_sent_at is None

    first = await welcome_service.send_welcome_if_unsent(real_session, member)
    assert first is True
    assert member.welcome_email_sent_at is not None
    assert [s["email"] for s in monkeypatch_welcome_send] == [member.email]

    # Second call is a no-op — already welcomed.
    second = await welcome_service.send_welcome_if_unsent(real_session, member)
    assert second is False
    assert len(monkeypatch_welcome_send) == 1


async def test_welcome_stamp_persists_across_sessions(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch_welcome_send
) -> None:
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    member_id = member.id
    await welcome_service.send_welcome_if_unsent(real_session, member)
    await real_session.commit()

    async with AsyncSessionLocal() as fresh:
        reloaded = (await fresh.execute(select(User).where(User.id == member_id))).scalar_one()
        assert reloaded.welcome_email_sent_at is not None


async def test_welcome_guard_atomic_across_concurrent_sessions(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch_welcome_send
) -> None:
    # Models the double-fired-verify race: two independent transactions both load
    # the user while `welcome_email_sent_at` is still NULL, then both call the
    # guard. Exactly one may send. A plain read-check-write guard sends twice here
    # (the reported bug); the atomic conditional UPDATE makes the loser skip.
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    member_id = member.id

    async with AsyncSessionLocal() as s1, AsyncSessionLocal() as s2:
        u1 = (await s1.execute(select(User).where(User.id == member_id))).scalar_one()
        u2 = (await s2.execute(select(User).where(User.id == member_id))).scalar_one()
        # Both racers see the stale NULL — the in-memory check can't save us here.
        assert u1.welcome_email_sent_at is None
        assert u2.welcome_email_sent_at is None

        won1 = await welcome_service.send_welcome_if_unsent(s1, u1)
        await s1.commit()
        won2 = await welcome_service.send_welcome_if_unsent(s2, u2)
        await s2.commit()

    assert [won1, won2].count(True) == 1  # exactly one winner
    assert len(monkeypatch_welcome_send) == 1


async def test_welcome_fires_once_after_email_verify(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_welcome_send
) -> None:
    suffix = secrets.token_hex(4)
    email = f"welcome-{suffix}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Welcome",
            "last_name": "User",
            "password": "vaultkey-12345",
            "org_name": f"WelcomeCo {suffix}",
        },
    )
    assert resp.status_code == 201
    # Bare signup is unverified → no welcome yet.
    assert len(monkeypatch_welcome_send) == 0
    token = monkeypatch_verification_send[-1]["token"]

    verified = await client.post(f"/api/v1/auth/verify-email/{token}")
    assert verified.status_code == 200
    assert [s["email"] for s in monkeypatch_welcome_send] == [email]

    # A replayed verify 409s and must NOT re-send.
    again = await client.post(f"/api/v1/auth/verify-email/{token}")
    assert again.status_code in (400, 409)
    assert len(monkeypatch_welcome_send) == 1


async def test_welcome_fires_on_invite_signup(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch_welcome_send,
) -> None:
    # An invited user is auto-verified on creation (the inviter vouched), so they
    # never hit the verify-email route — they must still be welcomed once.
    invitee = _unique_email("invited-welcome")
    raw_token = secrets.token_urlsafe(24)
    real_session.add(
        Invitation(
            team_id=org_admin.org_id,
            email=invitee.lower(),
            role=TeamRole.MEMBER,
            token=hash_lookup_token(raw_token),
            status=InvitationStatus.PENDING,
            invited_by_id=org_admin.admin_id,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await real_session.commit()

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": invitee,
            "first_name": "Invited",
            "last_name": "User",
            "password": "vaultkey-12345-2",
            "invite_token": raw_token,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["email_verified_at"] is not None  # auto-verified
    assert [s["email"] for s in monkeypatch_welcome_send] == [invitee.lower()]


async def test_welcome_fires_once_on_oauth_signup(
    real_session: AsyncSession, monkeypatch_welcome_send
) -> None:
    # A provider-verified OAuth registration auto-verifies and skips the
    # verify-email route, so the welcome must fire here — exactly once.
    email = _unique_email("oauth-welcome")
    ticket = decode_register_ticket(
        encode_register_ticket(
            provider="google",
            subject=f"google-{secrets.token_hex(6)}",
            email=email,
            email_verified=True,
            first_name="OAuth",
            last_name="User",
            invite_token=None,
        )
    )
    user = await oauth_service.complete_registration(
        real_session,
        ticket,
        first_name="OAuth",
        last_name="User",
        org_name=f"OAuthCo {secrets.token_hex(4)}",
        invite_token=None,
    )
    assert user.welcome_email_sent_at is not None
    assert [s["email"] for s in monkeypatch_welcome_send] == [email.lower()]
