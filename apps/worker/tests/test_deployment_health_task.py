"""The 5-minute deployment-health job: self-skips on SaaS and persists a
scheduled snapshot on a self-host. The activity is awaited directly (outside a
Worker it is a plain coroutine); the workflow, the lock contention and the
liveness stamp across runs are covered in ``test_deployment_health_workflow.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core import deployment_health as dh
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import DeploymentHealthCheck, DeploymentHealthRun
from sqlalchemy import delete, select
from worker.activities.deployment_health import run_deployment_health


@pytest.fixture(autouse=True)
async def _clean() -> Any:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(DeploymentHealthCheck))
        await db.execute(delete(DeploymentHealthRun))
        await db.commit()


@pytest.mark.asyncio
async def test_self_skips_on_saas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    assert await run_deployment_health() == {"skipped": "saas"}


@pytest.mark.asyncio
async def test_run_persists_a_scheduled_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the async core directly: the probes are faked, the persistence
    is real, and the scheduled trigger stamps the liveness marker."""
    from worker.tasks.deployment_health import run_health_checks

    async def fake_run_and_persist(*, trigger: str, **kw: Any) -> list[dh.CheckResult]:
        results = [dh.CheckResult("postgres", "Postgres", "ok", "reachable", 1)]
        await dh.persist_snapshot(
            AsyncSessionLocal,
            results,
            trigger=trigger,  # type: ignore[arg-type]
            started_at=datetime.now(UTC),
            duration_ms=7,
        )
        return results

    monkeypatch.setattr("worker.tasks.deployment_health.run_and_persist", fake_run_and_persist)
    counts = await run_health_checks()
    assert counts == {"ok": 1}

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(DeploymentHealthCheck))).scalars().all()
        run = await db.get(DeploymentHealthRun, 1)
    assert [r.trigger for r in rows] == ["scheduled"]
    assert run is not None and run.last_scheduled_at is not None
