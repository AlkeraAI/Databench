"""The org-machine reconcile as a Temporal workflow: the real workflow and the
real activity through a real Worker, against the test database.

One run launches an org machine's pending allocation through the provider the
task builds for its kind; a run that finds the advisory lock held does nothing
and reports zero; the type is served on the money queue with the policy and
the schedule the contract names.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.compute.org_machines import AdmittedRate, create_org_machine
from alkera_core.compute.pricing import compute_rate
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import Team, TeamMembership, TeamRole, User
from alkera_core.models.compute import ComputeAllocation, ComputeMachineType
from alkera_core.models.compute_offerings import ComputeOffering
from alkera_core.models.org_machines import OrgMachine
from alkera_core.schemas.org_machines import AudienceGrant
from alkera_core.schemas.temporal import SweepInput
from alkera_core.temporal import QUEUE_FOR, TaskQueue, WorkflowType
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from sqlalchemy import select
from temporalio.client import Client
from worker.activities.compute import ORG_RECONCILE_LOCK, org_machine_reconcile
from worker.schedules import SCHEDULES
from worker.tasks import compute as compute_tasks
from worker.tasks._hardening import advisory_lock
from worker.temporal import queues
from worker.temporal.retry import NO_RETRY, policy_for
from worker.workflows.compute import OrgMachineReconcile

pytestmark = pytest.mark.temporal


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeNodeProvider:
    fake = FakeNodeProvider(kind="localdev")
    monkeypatch.setattr(compute_tasks, "make_node_provider", lambda _kind: fake)
    return fake


async def _org_machine() -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        team = Team(name=f"om-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"om-{secrets.token_hex(6)}@alkera.dev",
            first_name="O",
            last_name="M",
        )
        s.add(user)
        await s.flush()
        s.add(TeamMembership(user_id=user.id, team_id=team.id, role=TeamRole.ADMIN))
        machine_type = ComputeMachineType(
            provider="localdev",
            provider_type_id=f"local {uuid.uuid4().hex[:8]}",
            display_name="Local box",
            compute_class="cpu",
            vcpu=2,
            memory_gb=4,
            provider_price_per_minute_nanos=0,
        )
        s.add(machine_type)
        await s.flush()
        offering = ComputeOffering(
            machine_type_id=machine_type.id,
            name="Local box",
            pricing_mode="fixed",
            fixed_rate_per_minute_nanos=0,
            storage_gb_default=10,
            storage_gb_max=10,
            audience="all",
        )
        s.add(offering)
        await s.flush()
        om = await create_org_machine(
            s,
            org_id=team.id,
            owner_team_id=team.id,
            offering=offering,
            machine_type=machine_type,
            name="Local",
            acquisition="granted",
            free_until=datetime(2099, 1, 1, tzinfo=UTC),
            use_mode="assigned",
            storage_gb=10,
            audience=[AudienceGrant(kind="org")],
            idle_stop_minutes=None,
            monthly_cap_nanos=None,
            admitted=AdmittedRate(compute_rate(offering, machine_type), None),
            created_by=user.id,
        )
        await s.commit()
        return om.id


async def _run(temporal_worker: Any, temporal_client: Client) -> Any:
    async with temporal_worker(
        workflows=[OrgMachineReconcile], activities=[org_machine_reconcile]
    ) as w:
        return await temporal_client.execute_workflow(
            OrgMachineReconcile.run,
            SweepInput(now=datetime.now(UTC)),
            id=f"org-machine-reconcile-{uuid.uuid4().hex[:8]}",
            task_queue=w.task_queue,
            execution_timeout=timedelta(seconds=120),
        )


async def test_one_run_launches_the_pending_org_machine(
    temporal_worker: Any, temporal_client: Client, provider: FakeNodeProvider
) -> None:
    om_id = await _org_machine()
    acted = await _run(temporal_worker, temporal_client)
    assert isinstance(acted, int) and acted >= 1
    async with AsyncSessionLocal() as s:
        om = (await s.execute(select(OrgMachine).where(OrgMachine.id == om_id))).scalar_one()
        alloc = await s.get(ComputeAllocation, om.current_allocation_id)
    assert alloc is not None and alloc.state == "provisioning"
    assert alloc.provider_machine_id in provider.nodes
    assert provider.nodes[alloc.provider_machine_id].launch.allocation_id == alloc.id


async def test_a_run_that_loses_the_lock_does_nothing(
    temporal_worker: Any, temporal_client: Client, provider: FakeNodeProvider
) -> None:
    om_id = await _org_machine()
    async with advisory_lock(ORG_RECONCILE_LOCK) as held:
        assert held
        assert await _run(temporal_worker, temporal_client) == 0
    async with AsyncSessionLocal() as s:
        om = (await s.execute(select(OrgMachine).where(OrgMachine.id == om_id))).scalar_one()
        alloc = await s.get(ComputeAllocation, om.current_allocation_id)
    assert alloc is not None and alloc.state == "pending"
    assert provider.nodes == {}


def test_it_is_a_never_retried_money_job_every_30_seconds() -> None:
    assert QUEUE_FOR[WorkflowType.ORG_MACHINE_RECONCILE] is TaskQueue.MONEY
    assert OrgMachineReconcile in queues.WORKFLOWS_BY_QUEUE[TaskQueue.MONEY]
    policy = policy_for(WorkflowType.ORG_MACHINE_RECONCILE.value)
    assert policy.retry == NO_RETRY
    (entry,) = [e for e in SCHEDULES if e.workflow is WorkflowType.ORG_MACHINE_RECONCILE]
    assert entry.every == timedelta(seconds=30)
