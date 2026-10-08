"""Complete-profile route tests — the step right after a minimal signup.

Sets the caller's name, and (only for a new-org admin whose org is still unnamed)
renames the organization. An invited member can set their name but must NOT be able
to rename the org they merely joined.
"""

from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login


async def _minimal_signup(client: AsyncClient, email: str) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "minimal-pass-12345"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_complete_profile_sets_name_and_renames_org(
    client: AsyncClient, monkeypatch_verification_send
):
    """A new-org admin names themself AND their org on complete-profile."""
    email = f"newadmin-{secrets.token_hex(4)}@alkera.dev"
    await _minimal_signup(client, email)

    resp = await client.post(
        "/api/v1/auth/complete-profile",
        json={"first_name": "Ada", "last_name": "Lovelace", "org_name": "Northwind Labs"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["first_name"] == "Ada"
    assert body["last_name"] == "Lovelace"
    assert body["org_name"] == "Northwind Labs"

    # /me reflects the completed profile + renamed org.
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["display_name"] == "Ada Lovelace"
    assert me["org_name"] == "Northwind Labs"


@pytest.mark.asyncio
async def test_complete_profile_name_only_omits_org_rename(
    client: AsyncClient, monkeypatch_verification_send
):
    """Omitting org_name leaves the org unnamed (the SPA hides the field for an
    invited member; a new-org admin who skips it stays unnamed)."""
    email = f"noorg-{secrets.token_hex(4)}@alkera.dev"
    await _minimal_signup(client, email)

    resp = await client.post(
        "/api/v1/auth/complete-profile",
        json={"first_name": "Grace", "last_name": "Hopper"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["org_name"] == ""


@pytest.mark.asyncio
async def test_complete_profile_invited_member_cannot_rename_org(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send,
    monkeypatch_verification_send,
):
    """An invited member can set their name but NOT rename the inviter's org —
    they don't admin it, so the route ignores org_name."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    original_org_name = (await client.get("/api/v1/auth/me")).json()["org_name"]
    assert original_org_name  # the fixture's org is named

    team_id = (await client.post("/api/v1/teams", json={"name": "Recruits"})).json()["id"]
    invitee = f"member-{secrets.token_hex(4)}@alkera.dev"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee, "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    await client.post(
        "/api/v1/auth/signup",
        json={"email": invitee, "password": "member-pass-12345", "invite_token": token},
    )
    resp = await client.post(
        "/api/v1/auth/complete-profile",
        json={"first_name": "Mem", "last_name": "Ber", "org_name": "Hijacked Org"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["first_name"] == "Mem"
    # The org name is unchanged — the member can't rename what they don't admin.
    assert body["org_name"] == original_org_name
