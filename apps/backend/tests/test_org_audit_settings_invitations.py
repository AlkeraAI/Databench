"""Org settings, knowledge-sync settings and the invitation lifecycle land on the
org audit chain, driven through the real routes.

Each case reads the row back from the database: the action, who it is
attributed to, what it targets, what changed, and the acting chain
(``detail.actor``) that says which principal did it. A write that changes
nothing, and a refused write, leave no row: the log is a record of what
happened, not of what was asked.
"""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OrgAuditEvent
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, _unique_email, login, make_member

pytestmark = pytest.mark.asyncio


async def _rows(org_id: UUID, action: str) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(OrgAuditEvent)
                    .where(OrgAuditEvent.org_team_id == org_id, OrgAuditEvent.action == action)
                    .order_by(OrgAuditEvent.created_at)
                )
            ).scalars()
        )


def _acting_id(row: OrgAuditEvent) -> str:
    assert row.detail is not None
    actor: dict[str, Any] = row.detail["actor"]
    return str(actor["acting"]["id"])


async def _admin(client: AsyncClient, org: OrgWithAdmin) -> None:
    await login(client, org.admin_email, org.admin_password)


# --------------------------------------------------------------------------- #
# Sign-in methods and the other org settings
# --------------------------------------------------------------------------- #


async def test_turning_a_sign_in_method_off_and_on_writes_one_row_each(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _admin(client, org_admin)

    off = await client.put("/api/v1/org/settings", json={"allow_login_github": False})
    assert off.status_code == 200, off.text
    on = await client.put("/api/v1/org/settings", json={"allow_login_github": True})
    assert on.status_code == 200, on.text

    first, second = await _rows(org_admin.org_id, "org_settings.updated")
    assert first.detail is not None and second.detail is not None
    assert first.detail["changes"] == {"allow_login_github": {"from": True, "to": False}}
    assert second.detail["changes"] == {"allow_login_github": {"from": False, "to": True}}
    assert first.target == "allow_login_github"
    for row in (first, second):
        assert row.actor_id == org_admin.admin_id
        assert row.actor_email == org_admin.admin_email
        assert _acting_id(row) == str(org_admin.admin_id)


async def test_a_save_records_only_the_fields_it_moved(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The settings page re-sends every toggle; flipping one must read as that
    one change, and a save that moves nothing leaves no row at all."""
    await _admin(client, org_admin)
    current = (await client.get("/api/v1/org/settings")).json()

    unchanged = {
        "allow_login_google": current["allow_login_google"],
        "allow_login_github": current["allow_login_github"],
    }
    assert (await client.put("/api/v1/org/settings", json=unchanged)).status_code == 200
    assert await _rows(org_admin.org_id, "org_settings.updated") == []

    flipped = {**unchanged, "allow_login_google": not current["allow_login_google"]}
    assert (await client.put("/api/v1/org/settings", json=flipped)).status_code == 200
    (row,) = await _rows(org_admin.org_id, "org_settings.updated")
    assert row.detail is not None
    assert set(row.detail["changes"]) == {"allow_login_google"}


async def test_a_refused_settings_write_leaves_no_row(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> None:
    member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await login(client, member.email, password or "")
    resp = await client.put("/api/v1/org/settings", json={"allow_login_github": False})
    assert resp.status_code == 403
    assert await _rows(org_admin.org_id, "org_settings.updated") == []


# --------------------------------------------------------------------------- #
# Knowledge sync
# --------------------------------------------------------------------------- #


async def test_knowledge_sync_changes_are_audited_with_what_moved(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _admin(client, org_admin)

    # A new org starts with sync on, so the first change is turning it off.
    off = await client.put("/api/v1/org/sync-settings", json={"sync_enabled": False})
    assert off.status_code == 200, off.text
    # The same value again changes nothing and records nothing.
    again = await client.put("/api/v1/org/sync-settings", json={"sync_enabled": False})
    assert again.status_code == 200
    on = await client.put(
        "/api/v1/org/sync-settings", json={"sync_enabled": True, "pull_cadence_seconds": 600}
    )
    assert on.status_code == 200, on.text

    first, second = await _rows(org_admin.org_id, "org_settings.knowledge_sync_updated")
    assert first.detail is not None and second.detail is not None
    assert first.detail["changes"] == {"sync_enabled": {"from": True, "to": False}}
    assert second.detail["changes"] == {
        "sync_enabled": {"from": False, "to": True},
        "pull_cadence_seconds": {"from": 300, "to": 600},
    }
    assert second.target == "pull_cadence_seconds, sync_enabled"
    assert _acting_id(first) == str(org_admin.admin_id)
    assert first.actor_id == org_admin.admin_id


# --------------------------------------------------------------------------- #
# Invitations: created, revoked, accepted (three ways in), rejected
# --------------------------------------------------------------------------- #


async def _invite(client: AsyncClient, team_id: UUID | str, email: str) -> str:
    resp = await client.post(
        f"/api/v1/teams/{team_id}/invitations", json={"email": email, "role": "member"}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _subteam(client: AsyncClient) -> str:
    """A team below the root, so a recipient already seated in the org has a
    team left to join."""
    resp = await client.post("/api/v1/teams", json={"name": f"team-{secrets.token_hex(3)}"})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_creating_an_invitation_carries_the_acting_chain(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send: list[dict]
) -> None:
    await _admin(client, org_admin)
    email = _unique_email("invitee")
    await _invite(client, org_admin.org_id, email)
    (row,) = await _rows(org_admin.org_id, "invitation.created")
    assert row.target == email
    assert row.detail is not None
    assert row.detail["auto_accepted"] is False
    assert _acting_id(row) == str(org_admin.admin_id)


async def test_revoking_an_invitation_is_audited_once(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send: list[dict]
) -> None:
    await _admin(client, org_admin)
    email = _unique_email("revoked")
    invitation_id = await _invite(client, org_admin.org_id, email)

    url = f"/api/v1/teams/{org_admin.org_id}/invitations/{invitation_id}"
    assert (await client.delete(url)).status_code == 204
    # Revoking it again is refused, and the refusal writes nothing.
    assert (await client.delete(url)).status_code == 409

    (row,) = await _rows(org_admin.org_id, "invitation.revoked")
    assert row.target == email
    assert row.actor_id == org_admin.admin_id
    assert row.detail is not None
    assert row.detail["team_id"] == str(org_admin.org_id)
    assert row.detail["role"] == "member"
    assert _acting_id(row) == str(org_admin.admin_id)


async def test_accepting_by_id_is_audited_as_the_recipient(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch_email_send: list[dict],
) -> None:
    await _admin(client, org_admin)
    email = _unique_email("accepts")
    invitation_id = await _invite(client, await _subteam(client), email)
    await client.post("/api/v1/auth/logout")

    invitee, password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=True
    )
    await login(client, invitee.email, password or "")
    accept = await client.post(f"/api/v1/invitations/{invitation_id}/accept")
    assert accept.status_code == 200, accept.text

    (row,) = await _rows(org_admin.org_id, "invitation.accepted")
    assert row.target == email
    assert row.actor_id == invitee.id
    assert _acting_id(row) == str(invitee.id)


async def test_accepting_through_signup_is_audited(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send: list[dict]
) -> None:
    """The common way a new member arrives: the invite link's signup. It never
    touches the accept route, and the acceptance must be on the record anyway."""
    await _admin(client, org_admin)
    email = f"joins-{secrets.token_hex(4)}@alkera.dev"
    await _invite(client, org_admin.org_id, email)
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    signup = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "a-good-password-1", "invite_token": token},
    )
    assert signup.status_code == 201, signup.text

    (row,) = await _rows(org_admin.org_id, "invitation.accepted")
    assert row.target == email
    assert row.actor_email == email
    assert row.actor_id is not None
    assert _acting_id(row) == str(row.actor_id)


async def test_rejecting_is_audited_on_the_inviting_org(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch_email_send: list[dict],
) -> None:
    await _admin(client, org_admin)
    email = _unique_email("declines")
    invitation_id = await _invite(client, await _subteam(client), email)
    await client.post("/api/v1/auth/logout")

    invitee, password = await make_member(
        real_session, org_id=org_admin.org_id, email=email, verified=True
    )
    await login(client, invitee.email, password or "")
    reject = await client.post(f"/api/v1/invitations/{invitation_id}/reject")
    assert reject.status_code == 200, reject.text

    (row,) = await _rows(org_admin.org_id, "invitation.rejected")
    assert row.target == email
    assert row.actor_id == invitee.id
    assert _acting_id(row) == str(invitee.id)
    assert await _rows(org_admin.org_id, "invitation.accepted") == []
