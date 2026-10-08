"""What the owner of an invited address is told, through the real routes.

An address whose account already belongs to another organization used to be
invited into a dead end: the invitation was hidden from the recipient's own
list, and the one sentence explaining the single-org rule — reachable only by
calling the accept route by hand — told them to leave their organization, which
the product refuses. The recipient's list now carries every pending invitation
addressed to them, each named, with the refusal an accept would answer for the
ones the account cannot take; and a used invitation link says it was used.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

import pytest
from _statement_log import counting
from alkera_core.models import Invitation, InvitationStatus, Team, TeamRole, User
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, login, make_member


async def _admin(session: AsyncSession, org_admin: OrgWithAdmin) -> User:
    from backend.services.identity import users as user_service

    admin = await user_service.get_by_id(session, org_admin.admin_id)
    assert admin is not None
    return admin


async def _foreign_member_invited_here(
    session: AsyncSession, org_admin: OrgWithAdmin, *, team_name: str
) -> tuple[User, str, str, str]:
    """An account of ANOTHER org, invited (before it existed) into a sub-team of
    `org_admin`'s org. Returns (member, password, invitation id, other org name)."""
    from backend.services.org import invitations as invitation_service
    from backend.services.org import teams as team_service

    admin = await _admin(session, org_admin)
    team = await team_service.create_subteam(session, org_team_id=org_admin.org_id, name=team_name)
    email = _unique_email("foreign-invitee")
    invitation, auto, _token = await invitation_service.create_invitation(
        session,
        team=team,
        email=email,
        role=TeamRole.MEMBER,
        invited_by=admin,
        org_team_id=org_admin.org_id,
    )
    assert not auto
    other_name = f"Vendor Inc {secrets.token_hex(3)}"
    other_org, _ = await team_service.create_org_with_admin(
        session,
        org_name=other_name,
        admin_email=_unique_email("vendor-admin"),
        admin_first_name="Vendor",
        admin_last_name="Admin",
        admin_password="vendor-pass-12345",
    )
    await session.commit()
    member, password = await make_member(session, org_id=other_org.id, email=email, verified=True)
    assert password is not None
    return member, password, str(invitation.id), other_name


@pytest.mark.asyncio
async def test_a_foreign_org_invitation_is_listed_for_its_recipient_with_the_truthful_refusal(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    from backend.services.org import teams as team_service

    inviting_org = await team_service.get_by_id(real_session, org_admin.org_id)
    assert inviting_org is not None
    member, password, invitation_id, own_org_name = await _foreign_member_invited_here(
        real_session, org_admin, team_name="Contractors"
    )

    await login(client, member.email, password)
    mine = await client.get("/api/v1/invitations/me")
    assert mine.status_code == 200, mine.text
    listed = {row["id"]: row for row in mine.json()}
    assert invitation_id in listed
    row = listed[invitation_id]

    # Named, since the destination is not in the recipient's own team list.
    assert row["team_name"] == "Contractors"
    assert row["org_name"] == inviting_org.name
    assert row["inviter_display_name"]

    refusal = row["refusal"]
    assert refusal is not None
    assert refusal["code"] == "other_org"
    # Truthful: names the account's own org and the destination, and never
    # advises leaving an organization — the product refuses that.
    assert own_org_name in refusal["message"]
    assert inviting_org.name in refusal["message"]
    assert "leave" not in refusal["message"].lower()

    # The accept route answers with the same code and the same sentence.
    accept = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert accept.status_code == 409, accept.text
    assert accept.json()["error"]["code"] == "other_org"
    assert accept.json()["error"]["message"] == refusal["message"]

    # The dashboard's pending list is still only what this account can accept.
    board = await client.get("/api/v1/dashboard")
    assert invitation_id not in {inv["id"] for inv in board.json()["pending_invitations"]}

    # Declining clears it for both sides: gone from the recipient's list and
    # from the inviting team's pending list.
    declined = await client.post(f"/api/v1/invitations/{invitation_id}/reject")
    assert declined.status_code == 200, declined.text
    after = await client.get("/api/v1/invitations/me")
    assert invitation_id not in {r["id"] for r in after.json()}


@pytest.mark.asyncio
async def test_an_in_org_invitation_is_listed_named_and_acceptable(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    from backend.services.org import invitations as invitation_service
    from backend.services.org import teams as team_service

    admin = await _admin(real_session, org_admin)
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Analysts"
    )
    email = _unique_email("home-invitee")
    invitation, _, _ = await invitation_service.create_invitation(
        real_session,
        team=team,
        email=email,
        role=TeamRole.ADMIN,
        invited_by=admin,
        org_team_id=org_admin.org_id,
    )
    await real_session.commit()
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=True
    )
    assert password is not None

    await login(client, member.email, password)
    rows = {r["id"]: r for r in (await client.get("/api/v1/invitations/me")).json()}
    row = rows[str(invitation.id)]
    assert row["refusal"] is None
    assert row["team_name"] == "Analysts"
    assert row["inviter_display_name"] == admin.display_name
    assert row["role"] == "admin"

    accept = await client.post(f"/api/v1/invitations/{invitation.id}/accept")
    assert accept.status_code == 200, accept.text


@pytest.mark.asyncio
async def test_the_recipient_list_costs_the_same_queries_for_one_in_org_row_as_for_many(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Naming and judging each row must not cost a query per row for the common
    case — invitations into the recipient's own org."""
    from backend.services.org import invitations as invitation_service
    from backend.services.org import teams as team_service

    admin = await _admin(real_session, org_admin)

    async def seed(teams: int) -> User:
        email = _unique_email("recipient-list")
        for i in range(teams):
            team = await team_service.create_subteam(
                real_session, org_team_id=org_admin.org_id, name=f"R {secrets.token_hex(3)}-{i}"
            )
            await invitation_service.create_invitation(
                real_session,
                team=team,
                email=email,
                role=TeamRole.MEMBER,
                invited_by=admin,
                org_team_id=org_admin.org_id,
            )
        await real_session.commit()
        user, _ = await make_member(real_session, org_id=org_admin.org_id, email=email)
        return user

    counts: dict[int, int] = {}
    for n in (1, 10):
        user = await seed(n)
        with counting() as statements:
            rows = await invitation_service.list_for_recipient(
                real_session, user, org_team_id=org_admin.org_id
            )
        assert len(rows) == n
        assert all(r.refusal is None for r in rows)
        counts[n] = len(statements)
    assert counts[1] == counts[10], counts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "phrase"),
    [
        pytest.param(InvitationStatus.ACCEPTED, "invitation_accepted", "already been accepted"),
        pytest.param(InvitationStatus.REJECTED, "invitation_declined", "declined"),
        pytest.param(InvitationStatus.REVOKED, "invitation_revoked", "withdrawn"),
        pytest.param(InvitationStatus.EXPIRED, "invitation_expired", "expired"),
    ],
)
async def test_a_closed_invitation_link_says_why_it_is_closed(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    status: InvitationStatus,
    code: str,
    phrase: str,
) -> None:
    """Re-opening an accepted invitation read "invalid or expired", which tells
    someone who already joined that they did not."""
    from backend.services.org import invitations as invitation_service

    admin = await _admin(real_session, org_admin)
    root_team = await real_session.get(Team, org_admin.org_id)
    assert root_team is not None
    invitation, _, token = await invitation_service.create_invitation(
        real_session,
        team=root_team,
        email=_unique_email("closed-link"),
        role=TeamRole.MEMBER,
        invited_by=admin,
        org_team_id=org_admin.org_id,
    )
    await real_session.commit()

    live = await client.get(f"/api/v1/invitations/by-token/{token}")
    assert live.status_code == 200, live.text

    row = await real_session.get(Invitation, invitation.id)
    assert row is not None
    row.status = status
    await real_session.commit()

    closed = await client.get(f"/api/v1/invitations/by-token/{token}")
    assert closed.status_code == 410, closed.text
    assert closed.json()["error"]["code"] == code
    assert phrase in closed.json()["error"]["message"]

    unknown = await client.get(f"/api/v1/invitations/by-token/{secrets.token_urlsafe(32)}")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_a_pending_link_reads_expired_the_moment_its_ttl_passes(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch_email_send: Any,
) -> None:
    from backend.services.org import invitations as invitation_service

    with freeze_time("2026-09-24 20:00:00", real_asyncio=True) as frozen:
        admin = await _admin(real_session, org_admin)
        root_team = await real_session.get(Team, org_admin.org_id)
        assert root_team is not None
        invitation, _, token = await invitation_service.create_invitation(
            real_session,
            team=root_team,
            email=_unique_email("ttl-link"),
            role=TeamRole.MEMBER,
            invited_by=admin,
            org_team_id=org_admin.org_id,
        )
        await real_session.commit()
        expires_at = invitation.expires_at

        frozen.move_to(expires_at - timedelta(seconds=1))
        assert (await client.get(f"/api/v1/invitations/by-token/{token}")).status_code == 200

        frozen.move_to(expires_at + timedelta(seconds=1))
        late = await client.get(f"/api/v1/invitations/by-token/{token}")
        assert late.status_code == 410, late.text
        assert late.json()["error"]["code"] == "invitation_expired"
