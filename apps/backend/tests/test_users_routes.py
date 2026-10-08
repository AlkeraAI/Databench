"""User CRUD route tests — covers cross-org isolation and permission gates."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import OrgWithAdmin, TotpClock, login, make_member


@pytest.mark.asyncio
async def test_admin_cannot_set_another_members_password(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Account-takeover guard: even a verified org admin cannot set another
    member's password — the member must reset it themselves."""
    target, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{target.id}", json={"password": "admin-chosen-pass-12345"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_fix_another_members_name_for_the_org(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """An admin names a member for their org (the membership's display name);
    the member's own identity name is theirs and is not written."""
    target, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(f"/api/v1/users/{target.id}", json={"first_name": "Corrected"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == f"Corrected {target.last_name}"
    assert resp.json()["first_name"] == target.first_name


@pytest.mark.asyncio
async def test_user_can_set_their_own_password(client: AsyncClient, org_admin: OrgWithAdmin):
    """The self path is unaffected — a member sets their own password, proving
    the current one first."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    new_password = "self-chosen-pass-67890"
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"password": new_password, "current_password": org_admin.admin_password},
    )
    assert resp.status_code == 200
    relog = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": new_password},
    )
    assert relog.status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_change_another_members_email(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """An admin may not change another member's email at all — it's an indirect
    takeover lever (point it at a controlled address, then self-service reset).
    Only the member changes their own email."""
    import secrets

    target, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{target.id}",
        json={"email": f"hijack-{secrets.token_hex(4)}@evil.dev"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_cannot_change_own_email_to_an_existing_account(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Self-service email change still can't collide with another account (in ANY
    org) — the uniqueness guard prevents an account merge/takeover."""
    import secrets

    from backend.services.org import teams as team_service

    taken_email = f"taken-{secrets.token_hex(6)}@otherorg.dev"
    await team_service.create_org_with_admin(
        real_session,
        org_name=f"Taken Org {secrets.token_hex(4)}",
        admin_email=taken_email,
        admin_first_name="T",
        admin_last_name="Aken",
        admin_password="vaultkey-12345",
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"email": taken_email, "current_password": org_admin.admin_password},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_admin_cannot_patch_a_user_in_another_org(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """Cross-org isolation: an admin can't even address a user outside their org
    (the update path 404s before any field is touched)."""
    import secrets

    from backend.services.org import teams as team_service

    _other, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=f"oa-{secrets.token_hex(6)}@otherorg.dev",
        admin_first_name="O",
        admin_last_name="A",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(f"/api/v1/users/{other_admin.id}", json={"first_name": "Hacked"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_users_returns_only_caller_org(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    import secrets

    other_email = f"other-{secrets.token_hex(6)}@alkera.dev"
    # Create a second org with one user; make sure listing in the first org
    # doesn't surface them.
    from backend.services.org import teams as team_service

    _other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other Org {secrets.token_hex(4)}",
        admin_email=other_email,
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="vaultkey-10001" * 16,
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()}
    assert org_admin.admin_email in emails
    assert other_email not in emails


@pytest.mark.asyncio
async def test_create_user_requires_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)

    # Member tries to create — denied.
    await login(client, member.email, member_pw or "")
    resp = await client.post(
        "/api/v1/users",
        json={
            "email": "new-user@alkera.dev",
            "first_name": "New",
            "last_name": "Test",
            "org_team_id": str(org_admin.org_id),
            "password": "vaultkey-12345678",
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("password", "notes"),
    [
        pytest.param("vaultkey-1234", ["password_ignored"], id="with-a-password"),
        pytest.param(None, [], id="without-a-password"),
    ],
)
async def test_the_deprecated_create_adds_a_member_who_sets_their_own_password(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_password_reset_send: list[dict],
    password: str | None,
    notes: list[str],
):
    import secrets

    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import MembershipStatus, OrgMembership, TeamMembership, User
    from sqlalchemy import select

    await login(client, org_admin.admin_email, org_admin.admin_password)
    new_email = f"fresh-{secrets.token_hex(6)}@alkera.dev"
    body: dict[str, str] = {
        "email": new_email,
        "first_name": "Fresh",
        "last_name": "Test",
        "org_team_id": str(org_admin.org_id),
    }
    if password is not None:
        body["password"] = password
    resp = await client.post("/api/v1/users", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.headers["deprecation"] == "true"
    read = resp.json()
    assert read["email"] == new_email
    assert read["org_team_id"] == str(org_admin.org_id)
    assert read["notes"] == notes
    async with AsyncSessionLocal() as s:
        created = await s.scalar(select(User).where(User.email == new_email))
        assert created is not None
        membership = await s.scalar(
            select(OrgMembership).where(
                OrgMembership.user_id == created.id,
                OrgMembership.org_team_id == org_admin.org_id,
            )
        )
        seat = await s.scalar(
            select(TeamMembership).where(
                TeamMembership.user_id == created.id, TeamMembership.team_id == org_admin.org_id
            )
        )
    assert created.password_hash is None, "an admin never chooses a person's password"
    assert membership is not None and membership.status is MembershipStatus.ACTIVE
    assert seat is not None
    assert [m["email"] for m in monkeypatch_password_reset_send] == [new_email]


@pytest.mark.asyncio
async def test_the_deprecated_create_is_marked_deprecated_in_the_schema(client: AsyncClient):
    schema = (await client.get("/openapi.json")).json()
    assert schema["paths"]["/api/v1/users"]["post"]["deprecated"] is True


@pytest.mark.asyncio
async def test_the_deprecated_create_still_refuses_an_address_already_here(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/users",
        json={
            "email": member.email,
            "first_name": "Again",
            "last_name": "Here",
            "org_team_id": str(org_admin.org_id),
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_user_cross_org_returns_404(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    import secrets

    from backend.services.org import teams as team_service

    _other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Far Away {secrets.token_hex(4)}",
        admin_email=f"faraway-{secrets.token_hex(6)}@alkera.dev",
        admin_first_name="Far",
        admin_last_name="Away",
        admin_password="vaultkey-10001" * 16,
    )
    await real_session.commit()

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"/api/v1/users/{other_admin.id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_self_can_patch_own_profile(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    resp = await client.patch(f"/api/v1/users/{member.id}", json={"first_name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Renamed"


@pytest.mark.asyncio
async def test_member_cannot_patch_other_user(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id)
    other, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, member.email, member_pw or "")
    resp = await client.patch(f"/api/v1/users/{other.id}", json={"first_name": "Hacked"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_org_admin_can_delete_other_user(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    target, _ = await make_member(real_session, org_id=org_admin.org_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/users/{target.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_org_admin_cannot_delete_self(client: AsyncClient, org_admin: OrgWithAdmin):
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.delete(f"/api/v1/users/{org_admin.admin_id}")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Self-service identity + credential change
#
# `PATCH /api/v1/users/{self}` is the only route that rewrites the two fields
# that ARE the account. Each block below pins one invariant that a bearer of a
# live session must not be able to break.
# --------------------------------------------------------------------------- #


def _now_totp(secret: str) -> str:
    import time

    from alkera_core.auth import totp

    return totp._hotp(secret, int(time.time()) // 30)


async def _reload(user_id) -> object:
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import User

    async with AsyncSessionLocal() as session:
        return await session.get(User, user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved",
    [
        pytest.param("machine@svc.alkera.proxy", id="plain"),
        pytest.param("Machine@SVC.Alkera.Proxy", id="mixed-case"),
        pytest.param("  spaced@svc.alkera.proxy  ", id="padded"),
    ],
)
async def test_self_cannot_rename_onto_the_reserved_machine_domain(
    client: AsyncClient, org_admin: OrgWithAdmin, reserved: str
):
    """The reserved proxy-machine domain is refused on EVERY write of `User.email`,
    not just at signup. A human holding an `@svc.alkera.proxy` address is treated
    by the model gateway as the org's machine principal: it would trust their
    forwarded usage-meter header (invoice evasion) and fund them under the machine
    pool rule that drops the per-user postpaid cap. Normalization runs first, so
    case and padding cannot smuggle it past."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"email": reserved, "current_password": org_admin.admin_password},
    )
    assert resp.status_code == 409
    assert "reserved" in resp.json()["error"]["message"].lower()
    # ...and the address on the row is untouched.
    assert (await _reload(org_admin.admin_id)).email == org_admin.admin_email


@pytest.mark.asyncio
async def test_a_normal_domain_change_is_still_allowed(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """The asymmetric case: only the ONE reserved domain is refused. A lookalike
    that merely contains the string, or any ordinary address, still goes through —
    the guard is a domain match, not a substring blocklist."""
    import secrets

    await login(client, org_admin.admin_email, org_admin.admin_password)
    fresh = f"moved-{secrets.token_hex(6)}@svc.alkera.proxy.example.dev"
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"email": fresh, "current_password": org_admin.admin_password},
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == fresh


@pytest.mark.asyncio
async def test_changing_your_email_drops_the_verification_proof(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_verification_send
):
    """A verified account that renames itself is verified on an address nobody has
    proven. Carrying the stamp over disarms `oauth_service`'s account-pre-hijack
    eviction (it fires only when `email_verified_at is None`), so a squatter who
    verifies a throwaway then renames to the victim's address keeps the account
    when the real owner later signs in with Google. The stamp — and any pending
    token for the OLD address — is cleared."""
    import secrets

    await login(client, org_admin.admin_email, org_admin.admin_password)
    before = await _reload(org_admin.admin_id)
    assert before.email_verified_at is not None

    new_email = f"renamed-{secrets.token_hex(6)}@alkera.dev"
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"email": new_email, "current_password": org_admin.admin_password},
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == new_email

    after = await _reload(org_admin.admin_id)
    assert after.email_verified_at is None
    # The only pending token is the one just mailed to the NEW address.
    assert after.email_verification_token != before.email_verification_token


@pytest.mark.asyncio
async def test_a_name_only_edit_keeps_the_verification_proof(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """The asymmetric case: only an ACTUAL address change resets verification. The
    SPA re-sends the unchanged email on every profile save, so echoing it back must
    not silently un-verify the account."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"first_name": "Same", "email": org_admin.admin_email},
    )
    assert resp.status_code == 200
    assert (await _reload(org_admin.admin_id)).email_verified_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch_kind",
    [pytest.param("password", id="password-change"), pytest.param("email", id="email-change")],
)
async def test_a_credential_change_evicts_every_other_session(
    client: AsyncClient, org_admin: OrgWithAdmin, patch_kind: str
):
    """Changing a credential is a security event: it must kill every OTHER live
    session and CLI token, exactly as the emailed reset already does. Without it a
    victim who suspects compromise and changes their password leaves the intruder's
    90-day bearer working, and an intruder who changes it keeps the victim's
    session alive to be watched.

    Driven through a SECOND client holding a pre-change session — the observable
    contract is that its next request is refused."""
    import secrets

    from backend.app_factory import process_app

    fastapi_app = process_app()
    from httpx import ASGITransport
    from httpx import AsyncClient as Client

    async with Client(transport=ASGITransport(app=fastapi_app), base_url="http://test") as other:
        await login(other, org_admin.admin_email, org_admin.admin_password)
        assert (await other.get("/api/v1/auth/me")).status_code == 200

        await login(client, org_admin.admin_email, org_admin.admin_password)
        patch = {"current_password": org_admin.admin_password}
        if patch_kind == "password":
            patch["password"] = "brand-new-pass-24680"
        else:
            patch["email"] = f"evicted-{secrets.token_hex(6)}@alkera.dev"
        resp = await client.patch(f"/api/v1/users/{org_admin.admin_id}", json=patch)
        assert resp.status_code == 200

        # The stale session is dead...
        assert (await other.get("/api/v1/auth/me")).status_code == 401
        # ...and the acting client keeps working on the freshly-minted cookie, so
        # the remediation does not sign you out of the tab you performed it in.
        assert (await client.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_a_name_only_edit_leaves_sessions_alone(client: AsyncClient, org_admin: OrgWithAdmin):
    """The asymmetric case: a cosmetic edit is not a security event and must not
    log the user out of their other devices."""
    from backend.app_factory import process_app

    fastapi_app = process_app()
    from httpx import ASGITransport
    from httpx import AsyncClient as Client

    async with Client(transport=ASGITransport(app=fastapi_app), base_url="http://test") as other:
        await login(other, org_admin.admin_email, org_admin.admin_password)
        await login(client, org_admin.admin_email, org_admin.admin_password)
        resp = await client.patch(
            f"/api/v1/users/{org_admin.admin_id}", json={"first_name": "Cosmetic"}
        )
        assert resp.status_code == 200
        assert (await other.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proof", "expected"),
    [
        pytest.param(None, 403, id="no-current-password"),
        pytest.param("not-the-right-password", 403, id="wrong-current-password"),
        pytest.param("correct", 200, id="correct-current-password"),
    ],
)
async def test_credential_change_requires_the_current_password(
    client: AsyncClient, org_admin: OrgWithAdmin, proof: str | None, expected: int
):
    """Step-up: a stolen session alone must not convert into permanent account
    control. `/auth/mfa/disable` already refuses to act on a hijacked session
    without a live factor; the two fields that ARE the account get the same bar."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    patch: dict[str, str] = {"password": "attacker-chosen-pass-13579"}
    if proof is not None:
        patch["current_password"] = org_admin.admin_password if proof == "correct" else proof
    resp = await client.patch(f"/api/v1/users/{org_admin.admin_id}", json=patch)
    assert resp.status_code == expected


@pytest.mark.asyncio
async def test_a_refused_step_up_leaves_the_credential_untouched(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """Fail-closed, observably: after a refused change the ORIGINAL password still
    logs in (so nothing was half-applied) and the new one does not."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    refused = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}", json={"password": "attacker-chosen-pass-13579"}
    )
    assert refused.status_code == 403

    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"email": org_admin.admin_email, "password": "attacker-chosen-pass-13579"},
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login",
            json={"email": org_admin.admin_email, "password": org_admin.admin_password},
        )
    ).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code_kind", "expected"),
    [
        pytest.param("missing", 403, id="no-mfa-code"),
        pytest.param("wrong", 403, id="wrong-mfa-code"),
        pytest.param("valid", 200, id="valid-mfa-code"),
    ],
)
async def test_credential_change_also_requires_the_second_factor(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session,
    totp_clock: TotpClock,
    code_kind: str,
    expected: int,
):
    """When MFA is on, the password alone is not the whole credential — a stolen
    session plus a phished password must still be stopped."""
    member, member_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await real_session.commit()
    assert member_pw is not None

    await login(client, member.email, member_pw)
    secret = (await client.post("/api/v1/auth/mfa/enroll")).json()["secret"]
    assert (
        await client.post("/api/v1/auth/mfa/confirm", json={"code": totp_clock.code(secret)})
    ).status_code == 200

    patch = {"password": "second-factor-pass-11223", "current_password": member_pw}
    if code_kind == "wrong":
        patch["mfa_code"] = "000000"
    elif code_kind == "valid":
        # Confirmation spent the code it accepted, so the step-up needs the next.
        patch["mfa_code"] = totp_clock.next_code(secret)
    resp = await client.patch(f"/api/v1/users/{member.id}", json=patch)
    assert resp.status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "expected_code"),
    [
        pytest.param({}, "current_password_required", id="no-proof-offered"),
        pytest.param({"current_password": ""}, "current_password_required", id="blank-proof"),
        pytest.param(
            {"current_password": "not-the-right-password"},
            "current_password_invalid",
            id="wrong-proof",
        ),
    ],
)
async def test_a_refused_step_up_names_the_missing_proof(
    client: AsyncClient, org_admin: OrgWithAdmin, patch: dict[str, str], expected_code: str
):
    """A bare 403 is a dead end for the client: the SPA's profile form submits an
    email change with no password field, so without a branchable code it can only
    show "forbidden" and the feature is unusable. `/auth/login` already answers
    "collect one more factor" with a structured `code` (`mfa_required`); the
    step-up answers the same way, and distinguishes ABSENT proof from WRONG proof
    so the form can say which."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"password": "attacker-chosen-pass-13579", **patch},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_a_passwordless_account_names_its_own_refusal(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """The federated case gets its own code — the remedy is "set a password via
    the emailed link", not "type the one you have"."""
    import secrets

    from tests.conftest import mint_cli_token

    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    member.password_hash = None
    member_id, member_email, member_org = member.id, member.email, member.home_org_team_id
    await real_session.commit()

    token = await mint_cli_token(user_id=member_id, email=member_email, org_team_id=member_org)
    resp = await client.patch(
        f"/api/v1/users/{member_id}",
        json={
            "email": f"federated-{secrets.token_hex(6)}@alkera.dev",
            "current_password": "anything-at-all",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "password_not_set"


@pytest.mark.asyncio
async def test_repeated_wrong_current_passwords_lock_the_account(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """The step-up verifies the account's REAL password, so an unthrottled one is
    a password oracle handed to exactly the attacker it exists to stop: whoever
    already holds the session gets unlimited guesses at the credential (and burns
    an argon2 hash on a pooled connection for each). `/auth/login` routes every
    wrong password through `lockout_service`; so does this.

    Proven through the observable contract, not the counter: past the threshold
    the route answers 429 `account_locked`, and the CORRECT password is refused
    too — a lockout an attacker could end by finally guessing right would not be
    a lockout at all."""
    from alkera_core.config import settings

    await login(client, org_admin.admin_email, org_admin.admin_password)
    for _ in range(settings.auth_lockout_threshold):
        resp = await client.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"password": "attacker-chosen-pass-13579", "current_password": "wrong-guess"},
        )
        assert resp.status_code == 403

    locked = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"password": "attacker-chosen-pass-13579", "current_password": "wrong-guess"},
    )
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"

    with_the_real_password = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={
            "password": "attacker-chosen-pass-13579",
            "current_password": org_admin.admin_password,
        },
    )
    assert with_the_real_password.status_code == 429

    # And nothing was half-applied: the stored credential is still the original.
    from backend.auth.password import verify_password

    stored = await _reload(org_admin.admin_id)
    assert verify_password(org_admin.admin_password, stored.password_hash)


@pytest.mark.asyncio
async def test_a_wrong_current_password_says_it_was_wrong_and_what_is_left_of_the_budget(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
):
    """A wrong password used to read "Your current password is required", as if
    none was typed, and the lock arrived unannounced on the next submit. Each
    wrong try now says it was wrong and how many remain; the try that crosses
    the threshold says the account is now locked and for how long; and the
    refusal after that names the wait instead of "later"."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    monkeypatch.setattr(settings, "auth_lockout_duration_seconds", 900)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    async def attempt():
        return await client.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"email": "someone-else@alkera.dev", "current_password": "mistyped"},
        )

    seen: list[tuple[int, str, str, object]] = []
    for _ in range(3):
        resp = await attempt()
        err = resp.json()["error"]
        seen.append(
            (
                resp.status_code,
                err["code"],
                err["message"],
                (err.get("details") or {}).get("attempts_left"),
            )
        )

    assert [s[:2] for s in seen] == [(403, "current_password_invalid")] * 3
    assert seen[0][2] == (
        "That password isn't correct. 2 more tries before the account is locked for 15 minutes."
    )
    assert seen[0][3] == 2
    assert seen[1][2] == (
        "That password isn't correct. 1 more try before the account is locked for 15 minutes."
    )
    assert seen[2][2] == "That password isn't correct. The account is now locked for 15 minutes."
    assert seen[2][3] == 0
    assert all("required" not in s[2] for s in seen)

    locked = await attempt()
    assert locked.status_code == 429
    assert locked.json()["error"]["message"] == "Too many failed attempts. Try again in 15 minutes."


@pytest.mark.parametrize(
    ("seconds", "spoken"),
    [
        pytest.param(1, "1 second", id="one-second"),
        pytest.param(59, "59 seconds", id="under-a-minute"),
        pytest.param(60, "1 minute", id="exactly-a-minute"),
        pytest.param(61, "2 minutes", id="rounds-up-never-early"),
        pytest.param(900, "15 minutes", id="default-lock"),
        pytest.param(3600, "1 hour", id="exactly-an-hour"),
        pytest.param(3601, "2 hours", id="hour-rounds-up"),
    ],
)
def test_a_wait_is_spoken_rounded_up(seconds: int, spoken: str):
    from backend.services.identity.lockout import span

    assert span(seconds) == spoken


@pytest.mark.asyncio
async def test_the_step_up_counter_is_the_login_counter(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """One counter per account, not one per route — otherwise the throttle is
    just a bigger budget an attacker spends across both doors. Guesses spent here
    are visible to `/auth/login`, which is also how the counter is proven to have
    been COMMITTED: the step-up's own request rolls back on its 403."""
    from alkera_core.config import settings

    await login(client, org_admin.admin_email, org_admin.admin_password)
    for _ in range(settings.auth_lockout_threshold):
        await client.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"password": "attacker-chosen-pass-13579", "current_password": "wrong-guess"},
        )

    relog = await client.post(
        "/api/v1/auth/login",
        json={"email": org_admin.admin_email, "password": org_admin.admin_password},
    )
    assert relog.status_code == 429
    assert relog.json()["error"]["code"] == "account_locked"


@pytest.mark.asyncio
async def test_an_absent_proof_is_not_counted_as_a_guess(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """The asymmetric case that keeps the throttle from becoming the bug. The SPA
    submits an email change with no password field, learns it needs one, and
    prompts — so an ABSENT factor is a protocol round-trip, not an attempt on the
    credential, and counting it would let ordinary use lock people out of their
    own accounts. It also leaks nothing: a request carrying no guess can't be
    testing one."""
    from alkera_core.config import settings

    await login(client, org_admin.admin_email, org_admin.admin_password)
    for _ in range(settings.auth_lockout_threshold * 2):
        resp = await client.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"email": "someone-else@alkera.dev"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "current_password_required"

    # Still usable: neither the route nor login has been throttled.
    import secrets

    ok = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={
            "email": f"unthrottled-{secrets.token_hex(6)}@alkera.dev",
            "current_password": org_admin.admin_password,
        },
    )
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_a_successful_step_up_clears_the_streak(client: AsyncClient, org_admin: OrgWithAdmin):
    """A typo must not accumulate. A proven step-up resets the counter exactly as
    a successful login does, so a user who mistypes a few times, gets it right,
    and then mistypes again is not locked out by a streak that spans the success.

    Driven across the threshold twice: without the reset the second batch crosses
    it and starts answering 429."""
    from alkera_core.config import settings

    await login(client, org_admin.admin_email, org_admin.admin_password)
    for _ in range(settings.auth_lockout_threshold - 1):
        assert (
            await client.patch(
                f"/api/v1/users/{org_admin.admin_id}",
                json={"password": "typo-driven-pass-13579", "current_password": "mistyped"},
            )
        ).status_code == 403

    new_password = "finally-right-pass-24680"
    assert (
        await client.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"password": new_password, "current_password": org_admin.admin_password},
        )
    ).status_code == 200

    for _ in range(settings.auth_lockout_threshold - 1):
        again = await client.patch(
            f"/api/v1/users/{org_admin.admin_id}",
            json={"password": "another-pass-11223", "current_password": "mistyped"},
        )
        assert again.status_code == 403
        assert again.json()["error"]["code"] == "current_password_invalid"


@pytest.mark.asyncio
async def test_a_wrong_second_factor_is_counted_but_an_absent_one_is_not(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """The second factor gets the same treatment as the first, and for the same
    reason: a phished password plus an unthrottled TOTP field is a six-digit
    brute force. A MISSING code stays a prompt (`mfa_required`), a WRONG one is a
    guess (`mfa_invalid`) and counts."""
    from alkera_core.config import settings

    member, member_pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    await real_session.commit()
    assert member_pw is not None

    await login(client, member.email, member_pw)
    secret = (await client.post("/api/v1/auth/mfa/enroll")).json()["secret"]
    assert (
        await client.post("/api/v1/auth/mfa/confirm", json={"code": _now_totp(secret)})
    ).status_code == 200

    base = {"password": "second-factor-pass-11223", "current_password": member_pw}
    for _ in range(settings.auth_lockout_threshold * 2):
        resp = await client.patch(f"/api/v1/users/{member.id}", json=base)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "mfa_required"

    for _ in range(settings.auth_lockout_threshold):
        resp = await client.patch(f"/api/v1/users/{member.id}", json={**base, "mfa_code": "000000"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "mfa_invalid"

    locked = await client.patch(f"/api/v1/users/{member.id}", json={**base, "mfa_code": "000000"})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"


@pytest.mark.asyncio
async def test_a_passwordless_account_is_refused_and_pointed_at_the_emailed_link(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session
):
    """An OAuth/SSO account has no current password to prove, so this route refuses
    rather than waving the change through. Delivery of `POST /auth/password/send-reset`
    to the address on file IS the proof of possession, so there is a real path
    forward — it just isn't a bare session."""
    import secrets

    from tests.conftest import mint_cli_token

    member, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    member.password_hash = None
    member_id, member_email, member_org = member.id, member.email, member.home_org_team_id
    await real_session.commit()

    # Sign in the way a federated user does — a bearer, no local password.
    token = await mint_cli_token(user_id=member_id, email=member_email, org_team_id=member_org)
    new_email = f"federated-{secrets.token_hex(6)}@alkera.dev"
    resp = await client.patch(
        f"/api/v1/users/{member_id}",
        json={"email": new_email, "current_password": "anything-at-all"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "password" in resp.json()["error"]["message"].lower()
    assert (await _reload(member_id)).email == member_email


@pytest.mark.asyncio
async def test_identity_and_credential_changes_are_recorded_in_the_identitys_log(
    client: AsyncClient, org_admin: OrgWithAdmin
):
    """A credential/identity change is recorded in the identity's own security
    log, naming the org the session was in, and in no org's chain (the person
    may belong to several orgs). The old address is recorded only as its
    DOMAIN — enough to spot a redirect to an attacker-controlled inbox without
    copying a personal address into the log."""
    import secrets

    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import IdentitySecurityEvent, OrgAuditEvent
    from sqlalchemy import select

    await login(client, org_admin.admin_email, org_admin.admin_password)
    new_email = f"audited-{secrets.token_hex(6)}@newdomain.dev"
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={
            "email": new_email,
            "password": "audited-new-pass-99887",
            "current_password": org_admin.admin_password,
        },
    )
    assert resp.status_code == 200

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(OrgAuditEvent).where(OrgAuditEvent.org_team_id == org_admin.org_id)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(IdentitySecurityEvent).where(
                        IdentitySecurityEvent.user_id == org_admin.admin_id
                    )
                )
            )
            .scalars()
            .all()
        )
    org_actions = {row.action for row in rows}
    assert "auth.email_changed" not in org_actions
    assert "auth.password_changed" not in org_actions
    by_event = {e.event: e for e in events}
    assert {"auth.email_changed", "auth.password_changed"} <= set(by_event)
    assert by_event["auth.email_changed"].org_team_id == org_admin.org_id
    assert by_event["auth.email_changed"].detail == {"previous_email_domain": "alkera.dev"}


# --------------------------------------------------------------------------- #
# An email change mails the new address a verification link, through the same
# path the resend button uses — the SPA says "we sent a link" the moment the
# account is unverified, so the change must actually send one.
# --------------------------------------------------------------------------- #


async def _unverify(user_id) -> None:
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import User

    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.email_verified_at = None
        await session.commit()


async def _change_email(client: AsyncClient, org_admin: OrgWithAdmin, new_email: str):
    return await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={"email": new_email, "current_password": org_admin.admin_password},
    )


@pytest.mark.asyncio
async def test_an_email_change_mails_one_working_link_to_the_new_address(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_verification_send,
    monkeypatch_welcome_send,
):
    import secrets

    await login(client, org_admin.admin_email, org_admin.admin_password)
    new_email = f"moved-{secrets.token_hex(6)}@alkera.dev"
    resp = await _change_email(client, org_admin, new_email)
    assert resp.status_code == 200, resp.text

    assert [s["email"] for s in monkeypatch_verification_send] == [new_email]
    token = monkeypatch_verification_send[0]["token"]

    # The cooldown the SPA counts down is armed by that send...
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["email"] == new_email
    assert me["email_verified_at"] is None
    assert me["verification_resend_available_at"] is not None

    # ...and the mailed link proves the new address.
    verified = await client.post(f"/api/v1/auth/verify-email/{token}")
    assert verified.status_code == 200, verified.text
    assert verified.json()["email"] == new_email
    assert verified.json()["email_verified_at"] is not None


@pytest.mark.asyncio
async def test_an_email_change_retires_the_old_addresses_link_and_ignores_its_cooldown(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch_verification_send,
    monkeypatch_welcome_send,
):
    """An unverified account that JUST resent to its old address (so the resend
    cooldown is running) changes its address: the new address is mailed at once,
    the old address gets nothing more, and the link it already holds is dead."""
    import secrets

    await _unverify(org_admin.admin_id)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resent = await client.post("/api/v1/auth/verify-email/resend")
    assert resent.status_code == 200, resent.text
    old_token = monkeypatch_verification_send[-1]["token"]
    # The cooldown is live: a second resend is refused.
    assert (await client.post("/api/v1/auth/verify-email/resend")).status_code == 429

    new_email = f"moved-{secrets.token_hex(6)}@alkera.dev"
    resp = await _change_email(client, org_admin, new_email)
    assert resp.status_code == 200, resp.text

    assert [s["email"] for s in monkeypatch_verification_send] == [
        org_admin.admin_email,
        new_email,
    ]
    assert (await client.post(f"/api/v1/auth/verify-email/{old_token}")).status_code == 400
    new_token = monkeypatch_verification_send[-1]["token"]
    assert (await client.post(f"/api/v1/auth/verify-email/{new_token}")).status_code == 200


@pytest.mark.asyncio
async def test_the_change_mail_goes_through_the_real_sender_to_the_new_address(
    client: AsyncClient, org_admin: OrgWithAdmin, smtp_outbox
):
    """No sender stub: the message the relay receives is addressed to the new
    address and carries a verify link, never a copy to the old one."""
    import secrets

    await login(client, org_admin.admin_email, org_admin.admin_password)
    before = len(smtp_outbox)
    new_email = f"moved-{secrets.token_hex(6)}@alkera.dev"
    resp = await _change_email(client, org_admin, new_email)
    assert resp.status_code == 200, resp.text

    sent = smtp_outbox[before:]
    assert [m["To"] for m, _kw in sent] == [new_email]
    assert "/verify-email/" in sent[0][0].get_body(("plain",)).get_content()


@pytest.mark.asyncio
async def test_a_refused_change_mail_is_reported_as_unsent_and_arms_no_cooldown(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
):
    """The relay refuses the mail. The change stands (the caller proved their
    password), but nothing may claim a link went out: the response and /auth/me
    carry no pending resend window — the signal the SPA reads as "not sent" — and
    the very next resend is allowed and delivered, not held by a cooldown."""
    import secrets

    import aiosmtplib

    async def _refuse(message, **kwargs):  # type: ignore[no-untyped-def]
        raise aiosmtplib.errors.SMTPException("relay down")

    await login(client, org_admin.admin_email, org_admin.admin_password)
    monkeypatch.setattr(aiosmtplib, "send", _refuse)
    new_email = f"moved-{secrets.token_hex(6)}@alkera.dev"
    resp = await _change_email(client, org_admin, new_email)

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == new_email
    assert resp.json()["verification_resend_available_at"] is None
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["email_verified_at"] is None
    assert me["verification_resend_available_at"] is None
    assert (await _reload(org_admin.admin_id)).email_verification_token is None

    delivered: list[str] = []

    async def _accept(message, **kwargs):  # type: ignore[no-untyped-def]
        delivered.append(message["To"])

    monkeypatch.setattr(aiosmtplib, "send", _accept)
    resent = await client.post("/api/v1/auth/verify-email/resend")
    assert resent.status_code == 200, resent.text
    assert delivered == [new_email]


@pytest.mark.asyncio
async def test_a_delivered_change_mail_reports_its_resend_window_in_the_response(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_verification_send
):
    """The asymmetric case: after a real send the PATCH response (which the SPA
    seeds its /auth/me cache from) carries the same resend window /auth/me does."""
    import secrets

    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await _change_email(client, org_admin, f"moved-{secrets.token_hex(6)}@alkera.dev")
    assert resp.status_code == 200, resp.text
    me = (await client.get("/api/v1/auth/me")).json()
    assert resp.json()["verification_resend_available_at"] is not None
    assert resp.json()["verification_resend_available_at"] == me["verification_resend_available_at"]
