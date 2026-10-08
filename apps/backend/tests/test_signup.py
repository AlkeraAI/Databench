"""Signup route tests — covers both bare (new org) and invite-token paths."""

from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, login


@pytest.mark.asyncio
async def test_bare_signup_creates_new_org_and_logs_in(
    client: AsyncClient, monkeypatch_verification_send
):
    suffix = secrets.token_hex(4)
    email = f"founder-{suffix}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Founder",
            "last_name": "Test",
            "password": "vaultkey-12345",
            "org_name": f"FounderCo {suffix}",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"]["email"] == email
    assert "alkera_session" in client.cookies

    # Logged in: /me works.
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email_verified_at"] is None
    # A verification email was queued for the new account.
    assert any(s["email"] == email for s in monkeypatch_verification_send)


@pytest.mark.asyncio
async def test_signup_rejects_the_reserved_proxy_machine_domain(
    client: AsyncClient, monkeypatch_verification_send
):
    """A human can't register on the reserved proxy-machine domain — otherwise their
    JWT would pass is_proxy_machine_email and let them forge the gateway usage meter
    to dodge invoicing. Every signup path routes through create_user, which blocks it."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": "attacker@svc.alkera.proxy",
            "first_name": "Mal",
            "last_name": "Lory",
            "password": "vaultkey-54321",
            "org_name": f"EvilCo {secrets.token_hex(4)}",
        },
    )
    assert resp.status_code == 409
    assert "reserved" in resp.json()["error"]["message"].lower()
    assert "alkera_session" not in client.cookies


@pytest.mark.asyncio
async def test_invite_signup_marks_email_verified(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send,
    monkeypatch_verification_send,
):
    """Invite-driven signup auto-verifies; no verification email is sent."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = (await client.post("/api/v1/teams", json={"name": "Verified"})).json()["id"]
    invitee_email = f"verified-{secrets.token_hex(4)}@alkera.dev"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "member"},
    )
    invite_token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": invitee_email,
            "first_name": "Invited",
            "last_name": "Test",
            "password": "vaultkey-12345-2",
            "invite_token": invite_token,
        },
    )
    assert resp.status_code == 201
    me = await client.get("/api/v1/auth/me")
    assert me.json()["email_verified_at"] is not None
    # No verification email should have fired for this email.
    assert not any(s["email"] == invitee_email for s in monkeypatch_verification_send)


@pytest.mark.asyncio
async def test_minimal_signup_creates_unnamed_org(
    client: AsyncClient, monkeypatch_verification_send
):
    """Minimal signup — just email + password — creates a new UNNAMED org and a
    name-less account (the profile-incomplete sentinel). The SPA finishes both on
    the complete-profile step; `org_name` is "" so it knows to prompt for it."""
    email = f"minimal-{secrets.token_hex(4)}@alkera.dev"
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "minimal-pass-12345"},
    )
    assert resp.status_code == 201, resp.text
    user = resp.json()["user"]
    assert user["email"] == email
    assert user["first_name"] == ""
    assert user["last_name"] == ""
    assert user["org_name"] == ""
    assert "alkera_session" in client.cookies
    # The email still isn't vouched for, so a verification email fires.
    assert any(s["email"] == email for s in monkeypatch_verification_send)


@pytest.mark.asyncio
async def test_signup_rejects_when_both_provided(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"x-{secrets.token_hex(4)}@alkera.dev",
            "first_name": "X",
            "last_name": "Test",
            "password": "vaultkey-12345678",
            "org_name": "X",
            "invite_token": "tok",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_signup_with_invalid_invite_token(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"x-{secrets.token_hex(4)}@alkera.dev",
            "first_name": "X",
            "last_name": "Test",
            "password": "vaultkey-12345678",
            "invite_token": "not-a-real-token",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_signup_with_valid_invite_joins_inviter_org(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    create_team = await client.post("/api/v1/teams", json={"name": "Welcome"})
    team_id = create_team.json()["id"]
    new_email = f"newhire-{secrets.token_hex(4)}@alkera.dev"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": new_email, "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    # Sign up using the invite — caller becomes a member of the inviter's org.
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": new_email,
            "first_name": "New",
            "last_name": "Hire",
            "password": "vaultkey-12345-4",
            "invite_token": token,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"]["org_team_id"] == str(org_admin.org_id)


@pytest.mark.asyncio
async def test_signup_email_must_match_invitation(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = (await client.post("/api/v1/teams", json={"name": "MismatchTeam"})).json()["id"]
    target_email = f"target-{secrets.token_hex(4)}@alkera.dev"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": target_email, "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"impostor-{secrets.token_hex(4)}@alkera.dev",
            "first_name": "Impostor",
            "last_name": "Test",
            "password": "vaultkey-12345-5",
            "invite_token": token,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_signup_rejects_existing_email(client: AsyncClient, org_admin: OrgWithAdmin):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": org_admin.admin_email,
            "first_name": "Dupe",
            "last_name": "Test",
            "password": "vaultkey-12345-6",
            "org_name": "Dupe Org",
        },
    )
    assert resp.status_code == 409


# --- business-email gate ---------------------------------------------------


@pytest.mark.asyncio
async def test_signup_blocks_personal_email_without_flag(client: AsyncClient):
    suffix = secrets.token_hex(4)
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": f"founder-{suffix}@gmail.com",
            "first_name": "Founder",
            "last_name": "Test",
            "password": "vaultkey-12345",
            "org_name": f"GmailCo {suffix}",
        },
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    # Structured envelope so the SPA can show the "use your work email" modal.
    assert error["code"] == "personal_email_blocked"
    assert error["details"]["domain"] == "gmail.com"
    # No account/session created.
    assert "alkera_session" not in client.cookies


@pytest.mark.asyncio
async def test_signup_allows_personal_email_with_flag(
    client: AsyncClient, monkeypatch_verification_send
):
    suffix = secrets.token_hex(4)
    email = f"founder-{suffix}@gmail.com"
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "first_name": "Founder",
            "last_name": "Test",
            "password": "vaultkey-12345",
            "org_name": f"GmailCo {suffix}",
            "allow_personal_email": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["email"] == email
    assert "alkera_session" in client.cookies


@pytest.mark.asyncio
async def test_signup_invite_blocks_personal_email_without_flag(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send
):
    # The gate applies to invited users too — they see the nudge and must opt in.
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = (await client.post("/api/v1/teams", json={"name": "GmailInvite"})).json()["id"]
    invitee = f"contractor-{secrets.token_hex(4)}@gmail.com"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee, "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": invitee,
            "first_name": "Contra",
            "last_name": "Ctor",
            "password": "vaultkey-12345-7",
            "invite_token": token,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "personal_email_blocked"


@pytest.mark.asyncio
async def test_signup_invite_allows_personal_email_with_flag(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send,
    monkeypatch_verification_send,
):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = (await client.post("/api/v1/teams", json={"name": "GmailInvite2"})).json()["id"]
    invitee = f"contractor-{secrets.token_hex(4)}@gmail.com"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee, "role": "member"},
    )
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": invitee,
            "first_name": "Contra",
            "last_name": "Ctor",
            "password": "vaultkey-12345-7",
            "invite_token": token,
            "allow_personal_email": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["org_team_id"] == str(org_admin.org_id)


@pytest.mark.asyncio
async def test_invite_signup_never_mails_a_verification_link(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_email_send,
    monkeypatch_verification_send,
):
    """An invited member is vouched for, so the product never sends them a
    verification link — not at signup, and not from the self-service resend
    either. A link they can't spend would be the first mail they ever get from
    us, and it would answer with an error instead of a welcome.
    """
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team_id = (await client.post("/api/v1/teams", json={"name": "NoVerifyMail"})).json()["id"]
    invitee_email = f"nomail-{secrets.token_hex(4)}@alkera.dev"
    await client.post(
        f"/api/v1/teams/{team_id}/invitations",
        json={"email": invitee_email, "role": "member"},
    )
    invite_token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": invitee_email,
            "first_name": "NoMail",
            "last_name": "Test",
            "password": "vaultkey-12345-9",
            "invite_token": invite_token,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["email_verification_required"] is False
    assert monkeypatch_verification_send == []

    # The one route that could still mail them one refuses, because they are
    # already verified — so no verification link for this address exists at all.
    resend = await client.post("/api/v1/auth/verify-email/resend")
    assert resend.status_code == 200, resend.text
    assert "already verified" in resend.json()["message"].lower()
    assert monkeypatch_verification_send == []


@pytest.mark.asyncio
async def test_bare_signup_answers_before_the_verification_email_is_sent(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public route: the relay round-trip stays off its response time, so the
    signup answers while the verification send is still held open."""
    import asyncio

    from alkera_core.email import drain_background_sends

    release = asyncio.Event()
    finished: list[tuple[str, str]] = []

    async def _held(user, *, token):  # type: ignore[no-untyped-def]
        await release.wait()
        finished.append((user.email, token))

    monkeypatch.setattr("backend.api.routes.identity.auth.send_email_verification", _held)
    suffix = secrets.token_hex(4)
    email = f"held-signup-{suffix}@alkera.dev"
    resp = await asyncio.wait_for(
        client.post(
            "/api/v1/auth/signup",
            json={"email": email, "password": "vaultkey-12345", "org_name": f"HeldCo {suffix}"},
        ),
        timeout=10.0,
    )
    assert resp.status_code == 201, resp.text
    assert finished == []

    release.set()
    await drain_background_sends()
    assert [sent_to for sent_to, _token in finished] == [email]
