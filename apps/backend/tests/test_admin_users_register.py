"""The admin user register's abuse-forensics columns: verification state,
signup date ordering, disposable-domain flag, month-to-date spend, recorded
IPs, and the best-effort geo lookup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from alkera_core.schemas.identity.admin_users import IpInfo
from backend.services.abuse import ip_info as ip_info_service
from httpx import AsyncClient
from sqlalchemy import update
from tests.conftest import OrgWithAdmin, login, make_member
from tests.test_admin_usage import _seat, _usage

pytestmark = pytest.mark.asyncio


async def test_register_rows_carry_forensics_columns(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin, real_session
) -> None:
    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    account_id = await _seat(member.id)
    await _usage(
        account_id=account_id,
        user_id=member.id,
        org_id=org_admin.org_id,
        model_id=f"m-{uuid.uuid4().hex[:6]}",
        billed_nanos=11_000_000_000,
        occurred_at=datetime.now(UTC),
    )
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get("/admin/v1/users")
    assert resp.status_code == 200
    rows = resp.json()

    by_id = {row["id"]: row for row in rows}
    row = by_id[str(member.id)]
    assert row["email_verified_at"] is None  # make_member creates unverified
    assert row["created_at"]
    assert row["disposable_email"] is False
    assert row["mtd_billed_nanos"] == 11_000_000_000
    assert row["mtd_request_count"] == 1
    assert row["org_name"]
    # A user with no usage this month reads zero, not a missing field.
    admin_row = by_id[str(org_admin.admin_id)]
    assert admin_row["mtd_request_count"] >= 0

    # Newest first — the ordering a spam-wave investigation wants.
    created = [row["created_at"] for row in rows]
    assert created == sorted(created, reverse=True)


async def test_disposable_domain_is_flagged(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin, real_session
) -> None:
    from alkera_core.models import User

    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    # Rewrite the member's domain to a known-disposable one (the public signup
    # route refuses these now, so the register must still flag rows that
    # predate the gate).
    await real_session.execute(
        update(User)
        .where(User.id == member.id)
        .values(email=f"farm-{uuid.uuid4().hex[:6]}@mailinator.com", email_domain="mailinator.com")
    )
    await real_session.commit()

    await login(client, platform_support.admin_email, platform_support.admin_password)
    rows = (await client.get("/admin/v1/users")).json()
    row = next(r for r in rows if r["id"] == str(member.id))
    assert row["disposable_email"] is True


async def test_ip_info_endpoint_maps_recorded_ips(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_core.models import User

    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    await real_session.execute(
        update(User).where(User.id == member.id).values(signup_ip="203.0.113.9", last_login_ip=None)
    )
    await real_session.commit()

    async def fake_fetch(ip: str) -> IpInfo:
        return IpInfo(ip=ip, city="Testville", country="Testland", network="AS0 Test Hosting")

    ip_info_service.reset_ip_info_cache()
    monkeypatch.setattr(ip_info_service, "_fetch", fake_fetch)

    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(f"/admin/v1/users/{member.id}/ip-info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["signup"]["ip"] == "203.0.113.9"
    assert body["signup"]["country"] == "Testland"
    assert body["signup"]["network"] == "AS0 Test Hosting"
    assert body["last_login"] is None  # no IP recorded → nothing to look up

    missing = await client.get(f"/admin/v1/users/{uuid.uuid4()}/ip-info")
    assert missing.status_code == 404


async def test_ip_info_degrades_offline(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alkera_core.models import User

    member, _pw = await make_member(real_session, org_id=org_admin.org_id)
    await real_session.execute(
        update(User).where(User.id == member.id).values(signup_ip="198.51.100.7")
    )
    await real_session.commit()

    async def failing_fetch(ip: str) -> IpInfo:
        return IpInfo(ip=ip, error="lookup unavailable")

    ip_info_service.reset_ip_info_cache()
    monkeypatch.setattr(ip_info_service, "_fetch", failing_fetch)

    await login(client, platform_support.admin_email, platform_support.admin_password)
    body = (await client.get(f"/admin/v1/users/{member.id}/ip-info")).json()
    # The page never breaks: the row reports the error and keeps the raw IP.
    assert body["signup"]["ip"] == "198.51.100.7"
    assert body["signup"]["error"] == "lookup unavailable"
    assert body["signup"]["city"] is None
