"""`require_email_verified` — durable org-structure mutations need a PROVEN email.

Regression for the pre-emptive account-squatter path: an attacker can bare-sign-up
a victim's email (never proving it) and, during the verification grace window,
plant a co-admin or disable the OAuth providers the rightful owner would use to
reclaim the account. The verified-OAuth claim defense evicts the squatter's
credential but NOT those planted artifacts (and a disabled provider blocks the
reclaim outright). So an unverified account must not be able to create them.
"""

from __future__ import annotations

import secrets
from uuid import UUID

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login, make_member


async def _signup_unverified_org(client: AsyncClient) -> dict:
    """Bare signup → a new org whose admin is UNVERIFIED (in grace). Leaves the
    client logged in (cookie). Returns the user dict (incl. ``org_team_id``)."""
    suffix = secrets.token_hex(6)
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"squatter-{suffix}@acmecorp.dev",
            "first_name": "Squat",
            "last_name": "Ter",
            "password": "vaultkey-12345",
            "org_name": f"Acme {suffix}",
        },
    )
    assert resp.status_code == 201, resp.text
    user = resp.json()["user"]
    assert user["email_verified_at"] is None  # confirms it's unverified (in grace)
    return user


def _assert_gated(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "email_verification_required"


@pytest.mark.asyncio
async def test_unverified_admin_cannot_create_invitation(
    client: AsyncClient, monkeypatch_verification_send, monkeypatch_email_send
):
    user = await _signup_unverified_org(client)
    resp = await client.post(
        f"/api/v1/teams/{user['org_team_id']}/invitations",
        json={"email": f"co-admin-{secrets.token_hex(4)}@evilcorp.dev", "role": "admin"},
    )
    _assert_gated(resp)
    assert monkeypatch_email_send == []  # the co-admin invite never went out


@pytest.mark.asyncio
async def test_unverified_admin_cannot_disable_oauth_provider(
    client: AsyncClient, monkeypatch_verification_send
):
    # The reclaim-lockout vector: a squatter disabling Google so the real owner
    # can't OAuth-claim the squatted account.
    await _signup_unverified_org(client)
    resp = await client.put("/api/v1/org/settings", json={"allow_login_google": False})
    _assert_gated(resp)


@pytest.mark.asyncio
async def test_unverified_admin_cannot_create_team(
    client: AsyncClient, monkeypatch_verification_send
):
    await _signup_unverified_org(client)
    resp = await client.post("/api/v1/teams", json={"name": "Eng"})
    _assert_gated(resp)


@pytest.mark.asyncio
async def test_unverified_admin_cannot_add_membership(
    client: AsyncClient, real_session, monkeypatch_verification_send
):
    user = await _signup_unverified_org(client)
    org_id = user["org_team_id"]
    # A valid target so the body passes validation — only the gate can 403 here.
    target, _ = await make_member(real_session, org_id=UUID(org_id))
    resp = await client.post(
        f"/api/v1/teams/{org_id}/memberships",
        json={"user_id": str(target.id), "team_id": org_id, "role": "member"},
    )
    _assert_gated(resp)


@pytest.mark.asyncio
async def test_verified_admin_passes_the_gate(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    """Positive control: a VERIFIED admin sails through the same gated routes."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    invite = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/invitations",
        json={"email": f"teammate-{secrets.token_hex(4)}@alkera.dev", "role": "member"},
    )
    assert invite.status_code == 201, invite.text
    settings_resp = await client.put("/api/v1/org/settings", json={"allow_login_google": False})
    assert settings_resp.status_code == 200
    team = await client.post("/api/v1/teams", json={"name": "Eng"})
    assert team.status_code == 201


@pytest.mark.asyncio
async def test_unverified_admin_cannot_create_user(
    client: AsyncClient, monkeypatch_verification_send
):
    # Planting a durable credentialed account (a backdoor that survives the
    # squatter-credential eviction).
    user = await _signup_unverified_org(client)
    resp = await client.post(
        "/api/v1/users",
        json={
            "org_team_id": user["org_team_id"],
            "email": f"planted-{secrets.token_hex(4)}@acmecorp.dev",
            "first_name": "Planted",
            "last_name": "Backdoor",
            "password": "vaultkey-12345-2",
        },
    )
    _assert_gated(resp)


@pytest.mark.asyncio
async def test_unverified_admin_cannot_reset_another_members_password(
    client: AsyncClient, real_session, monkeypatch_verification_send
):
    # The account-takeover lever: an unverified admin resetting an existing
    # member's password via the admin path of PATCH /users/{id}.
    user = await _signup_unverified_org(client)
    target, _ = await make_member(real_session, org_id=UUID(user["org_team_id"]))
    resp = await client.patch(
        f"/api/v1/users/{target.id}", json={"password": "hijacked-pass-12345"}
    )
    _assert_gated(resp)


@pytest.mark.asyncio
async def test_unverified_admin_cannot_delete_user(
    client: AsyncClient, real_session, monkeypatch_verification_send
):
    user = await _signup_unverified_org(client)
    target, _ = await make_member(real_session, org_id=UUID(user["org_team_id"]))
    resp = await client.delete(f"/api/v1/users/{target.id}")
    _assert_gated(resp)


@pytest.mark.asyncio
async def test_unverified_user_can_still_edit_own_profile(
    client: AsyncClient, monkeypatch_verification_send
):
    """The gate is targeted: self-profile edits stay open during the grace
    window (only admin-mutating-OTHERS is blocked)."""
    user = await _signup_unverified_org(client)
    resp = await client.patch(f"/api/v1/users/{user['id']}", json={"first_name": "Renamed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["first_name"] == "Renamed"
