"""The account routes: the deletion plan, a deletion request and its cancel,
and the support tool's side.

Every case drives the real route against real Postgres and pins both the
status contract and the ``authz.decision`` row the account policy leaves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from _account_kit import decisions, effects
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.email import drain_background_sends
from alkera_core.models import (
    AccountDeletionRequest,
    AuditLog,
    IdentitySecurityEvent,
    User,
)
from backend.auth.session_issue import issue_session
from fastapi import Response
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import (
    OrgWithAdmin,
    TotpClock,
    app_client,
    login,
    make_member,
    mint_cli_token,
)
from tests.test_login_hardening import _enable_mfa

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/me/account"


async def _requests_for(user_id: UUID) -> list[AccountDeletionRequest]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == user_id)
        )
        return list(rows.scalars().all())


async def _member_of(org: OrgWithAdmin, *, admin: bool = False) -> tuple[User, str]:
    from alkera_core.models import TeamRole

    async with AsyncSessionLocal() as session:
        user, password = await make_member(
            session,
            org_id=org.org_id,
            role=TeamRole.ADMIN if admin else TeamRole.MEMBER,
            verified=True,
        )
        assert password is not None
        return user, password


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


async def test_a_solo_admins_plan_closes_their_org_and_is_on_record(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.get(f"{BASE}/deletion/plan")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [o["fate"] for o in body["orgs"]] == ["close"]
    assert body["can_proceed"] is True
    assert body["blockers"] == []
    assert body["reauth"] == "password"
    assert body["mfa_required"] is False
    assert body["grace_days"] == 14
    (row,) = await decisions(org_admin.org_id)
    assert effects([row]) == [("allow", "account_owner")]
    assert row.entity_id == str(org_admin.admin_id)
    assert row.payload["attrs"] == {"is_self": True, "acting_directly": True, "platform_role": ""}


async def test_a_members_plan_names_who_receives_their_shared_items(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member, password = await _member_of(org_admin)
    await login(client, member.email, password)
    body = (await client.get(f"{BASE}/deletion/plan")).json()
    (org,) = body["orgs"]
    assert org["fate"] == "leave"
    assert org["transfer_to_name"] == "Test Admin"
    assert body["can_proceed"] is True


async def test_the_last_admin_with_members_is_blocked_with_a_sentence(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _member_of(org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = (await client.get(f"{BASE}/deletion/plan")).json()
    assert [o["fate"] for o in body["orgs"]] == ["blocked"]
    (blocker,) = body["blockers"]
    assert blocker["code"] == "last_admin"
    assert blocker["message"].startswith("Make another member an admin of ")
    assert body["can_proceed"] is False


async def test_a_second_admin_unblocks_the_first(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _member_of(org_admin)
    second, _ = await _member_of(org_admin, admin=True)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = (await client.get(f"{BASE}/deletion/plan")).json()
    (org,) = body["orgs"]
    assert org["fate"] == "leave"
    assert org["transfer_to_name"] == second.display_name


# --------------------------------------------------------------------------- #
# Requesting a deletion
# --------------------------------------------------------------------------- #


async def test_a_mistyped_email_schedules_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        f"{BASE}/deletion",
        json={"confirm_email": "someone@else.dev", "current_password": org_admin.admin_password},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirm_email_mismatch"
    assert await _requests_for(org_admin.admin_id) == []


@pytest.mark.parametrize(
    ("password", "code"),
    [
        pytest.param(None, "current_password_required", id="absent"),
        pytest.param("not-the-password", "current_password_invalid", id="wrong"),
    ],
)
async def test_a_password_account_must_prove_its_password(
    client: AsyncClient, org_admin: OrgWithAdmin, password: str | None, code: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        f"{BASE}/deletion",
        json={"confirm_email": org_admin.admin_email.upper(), "current_password": password},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == code
    assert await _requests_for(org_admin.admin_id) == []


async def test_an_mfa_account_must_also_prove_its_code(
    client: AsyncClient, org_admin: OrgWithAdmin, totp_clock: TotpClock
) -> None:
    member, password = await _member_of(org_admin)
    secret, _ = await _enable_mfa(client, member.email, password, totp_clock)
    body = {"confirm_email": member.email, "current_password": password}
    missing = await client.post(f"{BASE}/deletion", json=body)
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "mfa_required"
    wrong = await client.post(f"{BASE}/deletion", json={**body, "mfa_code": "000000"})
    assert wrong.json()["error"]["code"] == "mfa_invalid"
    ok = await client.post(
        f"{BASE}/deletion", json={**body, "mfa_code": totp_clock.next_code(secret)}
    )
    assert ok.status_code == 202, ok.text


async def test_a_request_schedules_after_the_grace_window_and_ends_every_other_credential(
    client: AsyncClient, org_admin: OrgWithAdmin, account_mail: list[dict[str, object]]
) -> None:
    cli = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    async with app_client() as other_browser:
        await login(other_browser, org_admin.admin_email, org_admin.admin_password)
        await login(client, org_admin.admin_email, org_admin.admin_password)
        before = datetime.now(UTC)
        response = await client.post(
            f"{BASE}/deletion",
            json={
                "confirm_email": org_admin.admin_email,
                "current_password": org_admin.admin_password,
            },
        )
        assert response.status_code == 202, response.text
        purge_after = datetime.fromisoformat(response.json()["purge_after"])
        assert before + timedelta(days=14) <= purge_after <= datetime.now(UTC) + timedelta(days=14)
        assert response.json()["status"] == "scheduled"

        # This browser stays signed in, so the person can cancel; every other
        # credential is refused from the next request on.
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        assert (await other_browser.get("/api/v1/auth/me")).status_code == 401
    async with app_client(headers={"Authorization": f"Bearer {cli}"}) as bearer:
        assert (await bearer.get("/api/v1/auth/me")).status_code == 401

    again = await client.post(
        f"{BASE}/deletion",
        json={"confirm_email": org_admin.admin_email, "current_password": org_admin.admin_password},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "deletion_already_scheduled"

    await drain_background_sends()
    (mail,) = [m for m in account_mail if m["event"] == "email.account_deletion_scheduled.sent"]
    assert mail["to"] == org_admin.admin_email
    async with AsyncSessionLocal() as session:
        events = (
            await session.execute(
                select(IdentitySecurityEvent.event).where(
                    IdentitySecurityEvent.user_id == org_admin.admin_id
                )
            )
        ).scalars()
        assert "account.deletion_requested" in set(events)


async def test_a_blocked_request_is_a_409_naming_what_to_settle(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _member_of(org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    response = await client.post(
        f"{BASE}/deletion",
        json={"confirm_email": org_admin.admin_email, "current_password": org_admin.admin_password},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "deletion_blocked"
    assert [b["code"] for b in error["details"]["blockers"]] == ["last_admin"]
    assert await _requests_for(org_admin.admin_id) == []


async def test_a_federated_account_must_have_signed_in_recently(
    org_admin: OrgWithAdmin,
) -> None:
    async with AsyncSessionLocal() as session:
        member, _ = await make_member(session, org_id=org_admin.org_id, verified=True)
        member.password_hash = None
        await session.commit()
        user_id, email = member.id, member.email

    async def signed_in(client: AsyncClient) -> None:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            assert user is not None
            carrier = Response()
            from tests.conftest import _sign_in_request

            await issue_session(
                session, user, request=_sign_in_request(client), response=carrier, method="google"
            )
            await session.commit()
        import httpx

        client.cookies.extract_cookies(
            httpx.Response(
                200,
                headers=list(carrier.raw_headers),
                request=httpx.Request("POST", f"{client.base_url}/api/v1/auth/login"),
            )
        )

    with freeze_time(datetime.now(UTC), real_asyncio=True) as frozen:
        async with app_client() as client:
            await signed_in(client)
            frozen.tick(timedelta(minutes=11))
            stale = await client.post(f"{BASE}/deletion", json={"confirm_email": email})
            assert stale.status_code == 403
            assert stale.json()["error"]["code"] == "reauth_required"
        async with app_client() as fresh:
            await signed_in(fresh)
            ok = await fresh.post(f"{BASE}/deletion", json={"confirm_email": email})
            assert ok.status_code == 202, ok.text


async def test_a_cli_token_cannot_delete_the_account_and_the_refusal_is_on_record(
    org_admin: OrgWithAdmin,
) -> None:
    cli = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    async with app_client(headers={"Authorization": f"Bearer {cli}"}) as bearer:
        response = await bearer.post(
            f"{BASE}/deletion",
            json={
                "confirm_email": org_admin.admin_email,
                "current_password": org_admin.admin_password,
            },
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_owner_session_required"
    rows = await decisions(org_admin.org_id)
    assert effects(rows) == [("deny", "not_acting_directly")]
    assert rows[0].payload["attrs"]["acting_directly"] is False
    assert await _requests_for(org_admin.admin_id) == []


# --------------------------------------------------------------------------- #
# Cancelling
# --------------------------------------------------------------------------- #


async def test_cancel_ends_the_scheduled_deletion_and_a_second_cancel_is_404(
    client: AsyncClient, org_admin: OrgWithAdmin, account_mail: list[dict[str, object]]
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    scheduled = await client.post(
        f"{BASE}/deletion",
        json={"confirm_email": org_admin.admin_email, "current_password": org_admin.admin_password},
    )
    assert scheduled.status_code == 202
    assert (await client.get(f"{BASE}/deletion")).json()["request"]["status"] == "scheduled"

    cancelled = await client.delete(f"{BASE}/deletion")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"request": None}
    assert (await client.get(f"{BASE}/deletion")).json() == {"request": None}
    (row,) = await _requests_for(org_admin.admin_id)
    assert row.status == "cancelled" and row.cancelled_at is not None

    again = await client.delete(f"{BASE}/deletion")
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "no_deletion_scheduled"
    await drain_background_sends()
    assert [m["event"] for m in account_mail] == [
        "email.account_deletion_scheduled.sent",
        "email.account_deletion_cancelled.sent",
    ]


# --------------------------------------------------------------------------- #
# The support tool
# --------------------------------------------------------------------------- #

ADMIN_BASE = "/admin/v1/users"


async def test_support_reads_a_persons_requests_but_cannot_delete(
    org_admin: OrgWithAdmin, platform_support: OrgWithAdmin
) -> None:
    async with app_client() as staff:
        await login(staff, platform_support.admin_email, platform_support.admin_password)
        read = await staff.get(f"{ADMIN_BASE}/{org_admin.admin_id}/account")
        assert read.status_code == 200, read.text
        assert read.json()["deletion"] is None
        delete = await staff.post(f"{ADMIN_BASE}/{org_admin.admin_id}/account/deletion", json={})
    assert delete.status_code == 404
    rows = await decisions(platform_support.org_id)
    assert effects(rows) == [
        ("allow", "platform_support_reads"),
        ("deny", "not_your_account"),
    ]
    assert await _requests_for(org_admin.admin_id) == []


async def test_an_admin_schedules_a_deletion_due_now_and_it_is_audited(
    org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin, account_mail: list[dict[str, object]]
) -> None:
    async with app_client() as staff:
        await login(staff, platform_admin.admin_email, platform_admin.admin_password)
        response = await staff.post(
            f"{ADMIN_BASE}/{org_admin.admin_id}/account/deletion", json={"immediate": True}
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["source"] == "support"
        assert datetime.fromisoformat(body["purge_after"]) <= datetime.now(UTC)
    (request,) = await _requests_for(org_admin.admin_id)
    assert request.requested_by_id == platform_admin.admin_id
    assert effects(await decisions(platform_admin.org_id)) == [("allow", "platform_admin")]
    async with AsyncSessionLocal() as session:
        actions = (
            await session.execute(
                select(AuditLog.path).where(AuditLog.actor_id == platform_admin.admin_id)
            )
        ).scalars()
        paths = set(actions)
    assert f"{ADMIN_BASE}/{org_admin.admin_id}/account/deletion" in paths
    await drain_background_sends()
    (mail,) = account_mail
    assert mail["to"] == org_admin.admin_email
