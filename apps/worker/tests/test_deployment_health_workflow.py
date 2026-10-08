"""The deployment-health run as a Temporal workflow: the real workflow and the
real activity through a real Worker, against the test database.

The scheduled run must persist a ``scheduled`` snapshot and stamp
``last_scheduled_at`` — and keep stamping it on every later run, because that
stamp is what the Health tab's worker-liveness check reads; it must self-skip
on SaaS without touching the tables; it must step aside when another run
holds the lock; its types are the historical task name on the default queue;
a transient failure is retried and the second attempt's snapshot lands (the
policy table says transient retry — this proves the workflow attaches it);
and the catalog entry is now created by the schedule reconciler instead of
being reported pending.

The probes themselves are the costly, environment-shaped boundary (SMTP, the
model gateway, the broker), so ``run_and_persist`` is replaced by a fake that
persists a fixed result set through the real ``persist_snapshot``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core import deployment_health as dh
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import DeploymentHealthCheck, DeploymentHealthRun
from alkera_core.temporal import TaskQueue, WorkflowType
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from temporalio.client import Client
from worker.activities.deployment_health import run_deployment_health
from worker.schedules import SCHEDULES, ScheduleSyncReport, fingerprint, sync_schedules
from worker.tasks._hardening import advisory_lock
from worker.temporal import queues
from worker.workflows.deployment_health import DeploymentHealthRun as HealthWorkflow

pytestmark = pytest.mark.temporal


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


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the probe fan-out with a fixed result set persisted through the
    real ``persist_snapshot``; returns the list of triggers it was called with."""
    triggers: list[str] = []

    async def fake_run_and_persist(*, trigger: str, **kw: Any) -> list[dh.CheckResult]:
        triggers.append(trigger)
        results = [
            dh.CheckResult("postgres", "Postgres", "ok", "reachable", 1),
            dh.CheckResult("smtp", "SMTP", "warn", "slow", 900),
        ]
        await dh.persist_snapshot(
            AsyncSessionLocal,
            results,
            trigger=trigger,  # type: ignore[arg-type]
            started_at=datetime.now(UTC),
            duration_ms=7,
        )
        return results

    monkeypatch.setattr("worker.tasks.deployment_health.run_and_persist", fake_run_and_persist)
    return triggers


def _self_hosted(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(settings, "self_hosted", value)


async def _snapshot() -> tuple[list[str], DeploymentHealthRun | None]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(DeploymentHealthCheck))).scalars().all()
        run = await db.get(DeploymentHealthRun, 1)
        return sorted(r.trigger for r in rows), run


async def _execute(temporal_worker: Any, temporal_client: Client) -> Any:
    async with temporal_worker(
        workflows=[HealthWorkflow], activities=[run_deployment_health]
    ) as running:
        return await temporal_client.execute_workflow(
            HealthWorkflow.run,
            id=f"deployment_health.run-{uuid4().hex[:8]}",
            task_queue=running.task_queue,
            execution_timeout=timedelta(seconds=60),
        )


async def test_a_scheduled_run_persists_the_snapshot_and_keeps_bumping_the_liveness_stamp(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch, probes: list
) -> None:
    _self_hosted(monkeypatch, True)

    assert await _execute(temporal_worker, temporal_client) == {"ok": 1, "warn": 1}
    triggers, run = await _snapshot()
    assert triggers == ["scheduled", "scheduled"]
    assert run is not None and run.last_scheduled_at is not None
    first_stamp = run.last_scheduled_at

    assert await _execute(temporal_worker, temporal_client) == {"ok": 1, "warn": 1}
    triggers, run = await _snapshot()
    assert triggers == ["scheduled", "scheduled"], "the snapshot is replaced, not appended"
    assert run is not None and run.last_scheduled_at is not None
    assert run.last_scheduled_at > first_stamp
    assert probes == ["scheduled", "scheduled"]


async def test_the_run_self_skips_on_saas_without_touching_the_tables(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch, probes: list
) -> None:
    _self_hosted(monkeypatch, False)
    assert await _execute(temporal_worker, temporal_client) == {"skipped": "saas"}
    assert probes == []
    assert await _snapshot() == ([], None)


async def test_a_run_that_loses_the_lock_steps_aside(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch, probes: list
) -> None:
    _self_hosted(monkeypatch, True)
    async with advisory_lock("deployment_health") as held:
        assert held is True
        assert await _execute(temporal_worker, temporal_client) == {"skipped": "locked"}
    assert probes == []
    assert await _snapshot() == ([], None)


def test_the_types_are_the_historical_task_name_on_the_default_queue() -> None:
    assert queues.workflow_type_name(HealthWorkflow) == "deployment_health.run"
    assert queues.activity_type_name(run_deployment_health) == "deployment_health.run"
    for queue in TaskQueue:
        expect = queue is TaskQueue.DEFAULT
        assert (HealthWorkflow in queues.WORKFLOWS_BY_QUEUE[queue]) is expect, queue
        assert (run_deployment_health in queues.ACTIVITIES_BY_QUEUE[queue]) is expect, queue


async def test_a_dropped_connection_is_retried_and_the_second_attempt_persists(
    temporal_worker: Any, temporal_client: Client, monkeypatch: pytest.MonkeyPatch, probes: list
) -> None:
    _self_hosted(monkeypatch, True)
    import worker.tasks.deployment_health as health_tasks

    real = health_tasks.run_and_persist
    attempts: list[int] = []

    async def flaky(*, trigger: str, **kw: Any) -> list[dh.CheckResult]:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OperationalError("INSERT", {}, Exception("connection dropped"))
        return await real(trigger=trigger, **kw)

    monkeypatch.setattr(health_tasks, "run_and_persist", flaky)
    assert await _execute(temporal_worker, temporal_client) == {"ok": 1, "warn": 1}
    assert attempts == [1, 2]
    assert probes == ["scheduled"]
    triggers, run = await _snapshot()
    assert triggers == ["scheduled", "scheduled"]
    assert run is not None and run.last_scheduled_at is not None


async def test_the_catalog_entry_is_created_now_that_the_workflow_is_served(
    temporal_client: Client,
) -> None:
    assert WorkflowType.DEPLOYMENT_HEALTH.value in queues.served_workflow_types()
    [entry] = [e for e in SCHEDULES if e.workflow is WorkflowType.DEPLOYMENT_HEALTH]
    assert entry.id == "deployment-health"
    owner = f"test-{uuid4().hex[:10]}"
    try:
        report = await sync_schedules(temporal_client, [entry], managed_by=owner)
        assert report == ScheduleSyncReport(created=(entry.id,))
        described = await temporal_client.get_schedule_handle(entry.id).describe()
        assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
    finally:
        await temporal_client.get_schedule_handle(entry.id).delete()
