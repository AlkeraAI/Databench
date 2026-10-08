"""The compute plane as Temporal workflows: the real workflows and the real
activities through a real Worker, against the test database.

One meter run bills a running machine at the pinned clock and reports what it
metered; one sweep run announces a box that went quiet; a run that loses the
advisory lock does nothing and reports zero; a dropped connection is retried
(the meter's policy is transient retry); the types are on the queues the
contract names; the retry table classifies the family explicitly; and the two
catalog entries are created by the schedule reconciler.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import TaskQueue, WorkflowType
from sqlalchemy.exc import OperationalError
from temporalio.client import Client
from tests._compute_fakes import COST, PRICE, T0, FakeProvider, machine_frames, reload, seed_ready
from worker.activities import compute as activities
from worker.activities.compute import METER_LOCK, SWEEP_LOCK, compute_meter, compute_sweep
from worker.schedules import SCHEDULES, ScheduleSyncReport, fingerprint, sync_schedules
from worker.tasks import compute as compute_tasks
from worker.tasks._hardening import advisory_lock
from worker.temporal import queues
from worker.temporal.retry import NO_RETRY, TRANSIENT_RETRY, policy_for
from worker.workflows.compute import ComputeMeter, ComputeSweep

pytestmark = pytest.mark.temporal


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(compute_tasks, "make_provider", lambda: fake)
    return fake


async def _run(
    temporal_worker: Any, temporal_client: Client, workflow: type, at: datetime | None
) -> Any:
    async with temporal_worker(
        workflows=[ComputeMeter, ComputeSweep], activities=[compute_meter, compute_sweep]
    ) as w:
        return await temporal_client.execute_workflow(
            workflow.run,
            SweepInput(now=at) if at is not None else None,
            id=f"{queues.workflow_type_name(workflow)}-{uuid4().hex[:8]}",
            task_queue=w.task_queue,
            execution_timeout=timedelta(seconds=60),
        )


async def test_one_meter_run_bills_the_running_machine_at_the_pinned_clock(
    temporal_worker: Any, temporal_client: Client, provider: FakeProvider
) -> None:
    alloc = await seed_ready(granted_nanos=1_000 * PRICE)
    provider.running.add(alloc.provider_machine_id)
    metered = await _run(temporal_worker, temporal_client, ComputeMeter, T0 + timedelta(minutes=4))
    assert isinstance(metered, int) and metered >= 1
    row = await reload(alloc.id)
    assert (row.minutes_billed, row.billed_nanos, row.true_cost_nanos) == (4, 4 * PRICE, 4 * COST)


async def test_one_sweep_run_announces_the_box_that_went_quiet(
    temporal_worker: Any, temporal_client: Client
) -> None:
    from alkera_core.config import settings

    alloc = await seed_ready(
        granted_nanos=0,
        lifecycle="workspace",
        price=0,
        last_heartbeat_at=T0,
        last_reported_status="ready",
    )
    at = T0 + timedelta(seconds=settings.compute_heartbeat_ready_seconds + 1)
    changed = await _run(temporal_worker, temporal_client, ComputeSweep, at)
    assert isinstance(changed, int) and changed >= 1
    (frame,) = await machine_frames(str(alloc.id))
    assert frame.payload == {"status": "unreachable", "reason": None}


async def test_a_run_that_loses_the_lock_does_nothing_and_reports_zero(
    temporal_worker: Any, temporal_client: Client, provider: FakeProvider
) -> None:
    alloc = await seed_ready(granted_nanos=1_000 * PRICE)
    provider.running.add(alloc.provider_machine_id)
    async with advisory_lock(METER_LOCK) as held:
        assert held is True
        metered = await _run(
            temporal_worker, temporal_client, ComputeMeter, T0 + timedelta(minutes=4)
        )
    assert metered == 0
    assert (await reload(alloc.id)).minutes_billed == 0
    quiet = await seed_ready(
        granted_nanos=0,
        lifecycle="workspace",
        price=0,
        last_heartbeat_at=T0,
        last_reported_status="ready",
    )
    async with advisory_lock(SWEEP_LOCK) as held:
        assert held is True
        changed = await _run(
            temporal_worker, temporal_client, ComputeSweep, T0 + timedelta(hours=1)
        )
    assert changed == 0
    assert await machine_frames(str(quiet.id)) == []


async def test_a_scheduled_run_with_no_input_uses_the_wall_clock(
    temporal_worker: Any, temporal_client: Client, provider: FakeProvider
) -> None:
    alloc = await seed_ready(granted_nanos=1_000 * PRICE)
    provider.running.add(alloc.provider_machine_id)
    await _run(temporal_worker, temporal_client, ComputeMeter, None)
    row = await reload(alloc.id)
    assert row.minutes_billed >= 1
    assert row.last_metered_at is not None and row.last_metered_at > T0


async def test_a_dropped_connection_is_retried_and_the_second_attempt_meters(
    temporal_worker: Any,
    temporal_client: Client,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The meter's policy is transient retry: the first attempt loses its
    connection, the second does the work."""
    alloc = await seed_ready(granted_nanos=1_000 * PRICE)
    provider.running.add(alloc.provider_machine_id)
    real_meter = compute_tasks.run_meter
    attempts: list[int] = []

    async def flaky(now: datetime | None = None) -> dict[str, int]:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection dropped"))
        return await real_meter(now)

    # The activity bound the name at import time; rebind it there.
    monkeypatch.setattr(activities, "run_meter", flaky)
    metered = await _run(temporal_worker, temporal_client, ComputeMeter, T0 + timedelta(minutes=2))
    assert attempts == [1, 2]
    assert metered >= 1
    assert (await reload(alloc.id)).minutes_billed == 2


def test_the_types_are_on_the_queues_the_contract_names() -> None:
    assert queues.workflow_type_name(ComputeMeter) == "compute.meter"
    assert queues.activity_type_name(compute_meter) == "compute.meter"
    assert queues.workflow_type_name(ComputeSweep) == "compute.sweep"
    assert queues.activity_type_name(compute_sweep) == "compute.sweep"
    for queue in TaskQueue:
        assert (ComputeMeter in queues.WORKFLOWS_BY_QUEUE[queue]) is (queue is TaskQueue.MONEY)
        assert (compute_meter in queues.ACTIVITIES_BY_QUEUE[queue]) is (queue is TaskQueue.MONEY)
        assert (ComputeSweep in queues.WORKFLOWS_BY_QUEUE[queue]) is (queue is TaskQueue.DEFAULT)
        assert (compute_sweep in queues.ACTIVITIES_BY_QUEUE[queue]) is (queue is TaskQueue.DEFAULT)


def test_the_retry_table_classifies_the_family_explicitly() -> None:
    meter = policy_for(WorkflowType.COMPUTE_METER.value)
    sweep = policy_for(WorkflowType.COMPUTE_SWEEP.value)
    assert meter.retry == TRANSIENT_RETRY and meter.heartbeat_timeout is not None
    assert sweep.retry == NO_RETRY and sweep.retry.maximum_attempts == 1


async def test_the_catalog_entries_are_created_now_that_the_workflows_are_served(
    temporal_client: Client,
) -> None:
    served = queues.served_workflow_types()
    assert {"compute.meter", "compute.sweep"} <= set(served)
    entries = [
        e
        for e in SCHEDULES
        if e.workflow in (WorkflowType.COMPUTE_METER, WorkflowType.COMPUTE_SWEEP)
    ]
    assert sorted(e.id for e in entries) == ["compute-meter", "compute-sweep"]
    assert {e.id: e.spec_text for e in entries} == {
        "compute-meter": "every 60s",
        "compute-sweep": "every 15s",
    }
    owner = f"test-{uuid4().hex[:10]}"
    try:
        report = await sync_schedules(temporal_client, entries, managed_by=owner)
        assert report == ScheduleSyncReport(created=tuple(e.id for e in entries))
        for entry in entries:
            described = await temporal_client.get_schedule_handle(entry.id).describe()
            assert fingerprint(described.schedule) == fingerprint(entry.to_schedule())
    finally:
        for entry in entries:
            await temporal_client.get_schedule_handle(entry.id).delete()
