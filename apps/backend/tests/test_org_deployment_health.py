"""The org-admin deployment-health surface: self-hosted-only (404 on SaaS),
member-forbidden, a read-time stale-worker overlay, and an audited manual run."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import DeploymentHealthCheck, DeploymentHealthRun, OrgAuditEvent
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import delete, select
from tests.conftest import OrgWithAdmin, login, make_member

BASE = "/api/v1/org/deployment-health"


@pytest.fixture(autouse=True)
async def _self_hosted_clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "self_hosted", True)
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()


async def _seed_snapshot(*, scheduled_at: datetime, ok: bool = True) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            DeploymentHealthCheck(
                check_key="postgres",
                label="Postgres",
                status="ok" if ok else "fail",
                detail="database reachable" if ok else "unreachable",
                latency_ms=3,
                ran_at=scheduled_at,
                trigger="scheduled",
            )
        )
        db.add(
            DeploymentHealthRun(
                id=1,
                last_run_at=scheduled_at,
                last_trigger="scheduled",
                last_duration_ms=120,
                last_scheduled_at=scheduled_at,
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_404_on_saas(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(BASE)).status_code == 404
    assert (await client.post(f"{BASE}/run")).status_code == 404


@pytest.mark.asyncio
async def test_member_forbidden(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, password="member-pass-123", verified=True
    )
    await real_session.commit()
    await login(client, member.email, password or "")
    assert (await client.get(BASE)).status_code == 403


@pytest.mark.asyncio
async def test_get_returns_fresh_snapshot(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    with freeze_time("2026-07-04 12:00:00", real_asyncio=True):
        await _seed_snapshot(scheduled_at=datetime.now(UTC))
        await login(client, org_admin.admin_email, org_admin.admin_password)
        body = (await client.get(BASE)).json()
    assert body["overall"] == "ok"
    keys = {c["key"] for c in body["checks"]}
    assert "postgres" in keys and "worker_beat" in keys
    beat = next(c for c in body["checks"] if c["key"] == "worker_beat")
    assert beat["status"] == "ok"


@pytest.mark.asyncio
async def test_get_overlays_stale_worker_as_fail(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A stale-but-green snapshot must turn red at read time — the one failure
    mode this page most needs to catch."""
    with freeze_time("2026-07-04 12:00:00", real_asyncio=True) as frozen:
        await _seed_snapshot(scheduled_at=datetime.now(UTC))
        frozen.move_to("2026-07-04 12:30:00")  # 30 min later — well past the 12-min stale window
        await login(client, org_admin.admin_email, org_admin.admin_password)
        body = (await client.get(BASE)).json()
    beat = next(c for c in body["checks"] if c["key"] == "worker_beat")
    assert beat["status"] == "fail"
    assert body["overall"] == "fail"  # the overlay drives the banner


@pytest.mark.asyncio
async def test_manual_run_persists_and_audits(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_core import deployment_health as dh

    async def fake_run_and_persist(*, trigger: str, **kw: Any) -> list[dh.CheckResult]:
        results = [dh.CheckResult("postgres", "Postgres", "ok", "database reachable", 2)]
        await dh.persist_snapshot(
            AsyncSessionLocal,
            results,
            trigger=trigger,  # type: ignore[arg-type]
            started_at=datetime.now(UTC),
            duration_ms=42,
        )
        return results

    monkeypatch.setattr("backend.api.routes.ops.deployment.run_and_persist", fake_run_and_persist)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(f"{BASE}/run")
    assert resp.status_code == 200
    assert resp.json()["last_trigger"] == "manual"

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(DeploymentHealthCheck))).scalars().all()
        assert any(r.trigger == "manual" for r in rows)
        events = (
            (
                await db.execute(
                    select(OrgAuditEvent).where(
                        OrgAuditEvent.org_team_id == org_admin.org_id,
                        OrgAuditEvent.action == "deployment_health.ran",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert events, "a manual run must be audited"


@pytest.mark.asyncio
async def test_manual_run_requires_verified_admin(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: Any
) -> None:
    member, password = await make_member(
        real_session, org_id=org_admin.org_id, password="member-pass-123", verified=True
    )
    await real_session.commit()
    await login(client, member.email, password or "")
    assert (await client.post(f"{BASE}/run")).status_code == 403


@pytest.mark.asyncio
async def test_snapshot_filtered_to_callers_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A provider probe row for a FOREIGN org is invisible to this admin."""
    import secrets

    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as db:
        foreign, _ = await team_service.create_org_with_admin(
            db,
            org_name=f"Foreign {secrets.token_hex(4)}",
            admin_email=f"foreign-{secrets.token_hex(4)}@x.example",
            admin_first_name="F",
            admin_last_name="O",
            admin_password="foreign-pass-12345",
        )
        await db.flush()
        db.add(
            DeploymentHealthCheck(
                check_key="provider_anthropic",
                label="Anthropic API key",
                org_team_id=foreign.id,  # some other org
                status="fail",
                detail="invalid key",
                latency_ms=5,
                ran_at=datetime.now(UTC),
                trigger="scheduled",
            )
        )
        db.add(
            DeploymentHealthRun(
                id=1,
                last_run_at=datetime.now(UTC),
                last_trigger="scheduled",
                last_duration_ms=1,
                last_scheduled_at=datetime.now(UTC),
            )
        )
        await db.commit()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = (await client.get(BASE)).json()
    assert not any(c["key"] == "provider_anthropic" for c in body["checks"])
