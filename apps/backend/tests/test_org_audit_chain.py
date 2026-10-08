"""Tamper-evident hash chain on the org audit trail + the two new coverage events
(auth.logout, auth.password_reset_completed).

A plain SHA-256 chain detects any in-place edit, deletion, re-ordering, or
insertion within an org's events; the per-org advisory lock + strictly-monotonic
created_at keep the chain a single total order even under concurrent appends.
"""

from __future__ import annotations

import asyncio
import uuid
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import IdentitySecurityEvent
from backend.services.audit import org_audit as org_audit_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import select, text
from tests.conftest import OrgWithAdmin, login_via_route

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _audit_feature_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit log is an Enterprise feature; run this suite on a self-hosted
    deployment where the read/verify routes are reachable. The SaaS Enterprise gate is
    covered in test_enterprise_gating.py."""
    monkeypatch.setattr(settings, "self_hosted", True)


async def _emit(org_id: uuid.UUID, action: str, *, detail: dict | None = None) -> None:
    async with AsyncSessionLocal() as s:
        await org_audit_service.record(
            s, org_id=org_id, actor=None, action=action, target="t", detail=detail
        )
        await s.commit()


async def _verify(org_id: uuid.UUID) -> org_audit_service.ChainVerification:
    async with AsyncSessionLocal() as s:
        return await org_audit_service.verify_chain(s, org_id=org_id)


async def _raw(sql: str, **params: object) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(text(sql), params)
        await s.commit()


# --------------------------------------------------------------------------- #
# Chain integrity
# --------------------------------------------------------------------------- #


async def test_an_intact_chain_verifies(org_admin: OrgWithAdmin) -> None:
    for i in range(3):
        await _emit(org_admin.org_id, f"test.event{i}", detail={"i": i, "secret": "hide-me"})
    res = await _verify(org_admin.org_id)
    assert res.ok is True
    assert res.checked >= 3
    assert res.head_hash is not None and len(res.head_hash) == 64


async def test_editing_a_row_breaks_the_chain(org_admin: OrgWithAdmin) -> None:
    await _emit(org_admin.org_id, "test.a")
    await _emit(org_admin.org_id, "test.b")
    await _emit(org_admin.org_id, "test.c")
    assert (await _verify(org_admin.org_id)).ok is True

    # A malicious in-place edit of a stored row's content.
    await _raw(
        "UPDATE org_audit_events SET target = 'tampered' "
        "WHERE org_team_id = :o AND action = 'test.b'",
        o=str(org_admin.org_id),
    )
    res = await _verify(org_admin.org_id)
    assert res.ok is False
    assert res.broken_event_id is not None


async def test_deleting_a_row_breaks_the_chain(org_admin: OrgWithAdmin) -> None:
    await _emit(org_admin.org_id, "test.x")
    await _emit(org_admin.org_id, "test.y")
    await _emit(org_admin.org_id, "test.z")

    # Delete a middle row — the row after it still points at the now-missing hash.
    await _raw(
        "DELETE FROM org_audit_events WHERE org_team_id = :o AND action = 'test.y'",
        o=str(org_admin.org_id),
    )
    assert (await _verify(org_admin.org_id)).ok is False


async def test_chains_are_isolated_per_org(org_admin: OrgWithAdmin, real_session: object) -> None:
    # A second, independent org.
    async with AsyncSessionLocal() as s:
        org_b, _admin_b = await team_service.create_org_with_admin(
            s,
            org_name=f"Other-{uuid.uuid4().hex[:8]}",
            admin_email=f"b-{uuid.uuid4().hex[:8]}@b.example",
            admin_first_name="B",
            admin_last_name="Admin",
            admin_password="b-pass-123456",
        )
        org_b_id = org_b.id
        await s.commit()

    await _emit(org_admin.org_id, "test.a")
    await _emit(org_b_id, "test.a")
    assert (await _verify(org_admin.org_id)).ok is True
    assert (await _verify(org_b_id)).ok is True

    # Tampering org A must NOT break org B's independent chain.
    await _raw(
        "UPDATE org_audit_events SET target = 'x' WHERE org_team_id = :o AND action = 'test.a'",
        o=str(org_admin.org_id),
    )
    assert (await _verify(org_admin.org_id)).ok is False
    assert (await _verify(org_b_id)).ok is True


async def test_detail_secrets_are_redacted_before_hashing(org_admin: OrgWithAdmin) -> None:
    await _emit(org_admin.org_id, "test.redact", detail={"client_secret": "top", "ok": "v"})
    async with AsyncSessionLocal() as s:
        events, _ = await org_audit_service.list_page(
            s, org_id=org_admin.org_id, offset=0, limit=10
        )
    redacted = next(e for e in events if e.action == "test.redact")
    assert redacted.detail == {"client_secret": "***", "ok": "v"}
    assert (await _verify(org_admin.org_id)).ok is True  # chain hashes the redacted form


async def _identity_events(user_id: UUID) -> set[str]:
    """The events in the identity's own security log."""
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(IdentitySecurityEvent.event).where(IdentitySecurityEvent.user_id == user_id)
        )
        return set(rows.scalars().all())


# --------------------------------------------------------------------------- #
# Verify endpoint + coverage events
# --------------------------------------------------------------------------- #


async def test_verify_endpoint_reports_ok(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await _emit(org_admin.org_id, "test.x")
    await login_via_route(client, org_admin.admin_email, org_admin.admin_password)
    r = await client.get("/api/v1/org/audit-events/verify")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["head_hash"] is not None


async def test_the_sign_in_is_the_orgs_and_the_logout_the_identitys(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Entering the org is recorded in its chain; signing out is the
    identity's own event, recorded in its security log and not in the org's."""
    await login_via_route(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    async with AsyncSessionLocal() as s:
        events, _ = await org_audit_service.list_page(
            s, org_id=org_admin.org_id, offset=0, limit=50
        )
    actions = {e.action for e in events}
    assert "auth.login" in actions and "auth.logout" not in actions
    assert "auth.logout" in await _identity_events(org_admin.admin_id)
    assert (await _verify(org_admin.org_id)).ok is True


async def test_a_completed_password_reset_is_the_identitys_event(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_password_reset_send: list[dict]
) -> None:
    await client.post("/api/v1/auth/password-reset/request", json={"email": org_admin.admin_email})
    token = monkeypatch_password_reset_send[0]["token"]
    r = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": "brand-new-pass-123"}
    )
    assert r.status_code == 200
    async with AsyncSessionLocal() as s:
        events, _ = await org_audit_service.list_page(
            s, org_id=org_admin.org_id, offset=0, limit=50
        )
    assert "auth.password_reset_completed" not in {e.action for e in events}
    assert "auth.password_reset_completed" in await _identity_events(org_admin.admin_id)


# --------------------------------------------------------------------------- #
# Sealed at commit
# --------------------------------------------------------------------------- #


async def test_an_open_audited_write_does_not_hold_up_another_in_its_org(
    org_admin: OrgWithAdmin,
) -> None:
    """A request that has written its audit row and is still at its work holds
    nothing another audited write in the org waits on: the second commits
    inside half a second, and the chain both leave verifies, in commit order.

    The chain's lock used to be taken at the audit write and held to the end
    of the request, so every audited write in an org queued behind every
    other one for as long as it took."""
    async with AsyncSessionLocal() as slow, AsyncSessionLocal() as quick:
        await org_audit_service.record(
            slow, org_id=org_admin.org_id, actor=None, action="test.slow", target="t"
        )
        await quick.execute(text("SET LOCAL lock_timeout = '500ms'"))
        await org_audit_service.record(
            quick, org_id=org_admin.org_id, actor=None, action="test.quick", target="t"
        )
        await quick.commit()
        await slow.commit()

    verified = await _verify(org_admin.org_id)
    assert (verified.ok, verified.broken_event_id) == (True, None)
    async with AsyncSessionLocal() as s:
        actions = (
            (
                await s.execute(
                    text(
                        "SELECT action FROM org_audit_events WHERE org_team_id = :org "
                        "AND action LIKE 'test.%' ORDER BY created_at"
                    ),
                    {"org": org_admin.org_id},
                )
            )
            .scalars()
            .all()
        )
    assert actions == ["test.quick", "test.slow"]


async def test_audited_writes_committed_at_once_form_one_chain(org_admin: OrgWithAdmin) -> None:
    """Thirty audited commits in one org at the same moment: one chain, no
    fork, every row on it."""
    before = (await _verify(org_admin.org_id)).checked
    await asyncio.gather(*(_emit(org_admin.org_id, f"test.burst.{n}") for n in range(30)))

    verified = await _verify(org_admin.org_id)
    assert (verified.ok, verified.checked) == (True, before + 30)


async def test_an_unsealed_row_after_the_chain_began_is_a_break(org_admin: OrgWithAdmin) -> None:
    """Every row committed once the chain has begun is sealed as it commits,
    so a row with no hash after a sealed one was written past the chain (or
    had its hashes wiped): verification reports it rather than skipping it."""
    await _emit(org_admin.org_id, "test.sealed")
    planted = uuid.uuid4()
    await _raw(
        "INSERT INTO org_audit_events (id, org_team_id, actor_email, action, created_at) "
        "VALUES (:id, :org, '', 'test.planted', now() + interval '1 second')",
        id=planted,
        org=org_admin.org_id,
    )

    verified = await _verify(org_admin.org_id)
    assert (verified.ok, verified.broken_event_id) == (False, planted)
