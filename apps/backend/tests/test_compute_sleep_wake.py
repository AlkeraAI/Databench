"""Sleep / wake / off — the power axis on a provisioned machine.

Sleep stops a machine at the provider and takes it out of the metered states,
keeping the box; wake starts it back to ``ready`` and resets metering so the
stopped window is never billed; off is the ordinary terminate. The node
reconcile honours an asleep row — it keeps it stopped and never bills or
releases it — even when a stopped machine reads gone-ish at the provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alkera_core.compute.meter import meter_and_cutoff
from alkera_core.compute.node_reconcile import reconcile_nodes
from alkera_core.compute.provider import GONE, RUNNING, STOPPED, NodeLaunch, PodPhase
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential, OrgComputeAssignment, User
from alkera_core.models.compute import ComputeAllocation
from alkera_test_support.compute.fake_nodes import FakeNode, FakeNodeProvider
from backend.services.compute import provisioning
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import FakeProvider, make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.compute_rows,
    pytest.mark.xdist_group("compute-fleet"),
]

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(delete(MachineCredential))
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.state.not_in(("released", "failed")))
        .values(state="released")
    )
    await real_session.commit()


async def _ready_box(
    session: AsyncSession,
    org: OrgWithAdmin,
    *,
    machine_id: str,
    state: str = "ready",
    last_metered_at: datetime | None = None,
) -> ComputeAllocation:
    mt = await make_machine_type(session, provider="ec2")
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        tenancy="dedicated",
        state=state,
        provider_machine_id=machine_id,
        created_at=T0,
        ready_at=T0,
        state_changed_at=T0,
        last_metered_at=last_metered_at,
        price_per_minute_nanos=1000,
        true_cost_per_minute_nanos=1000,
    )
    session.add(alloc)
    await session.commit()
    return alloc


def _fake(machine_id: str, *, phase: PodPhase = RUNNING) -> FakeNodeProvider:
    fake = FakeNodeProvider(kind="ec2")
    fake.nodes[machine_id] = FakeNode(
        launch=NodeLaunch(
            allocation_id=uuid4(), name=machine_id, type_code="m6i.large", storage_gb=0, script=""
        ),
        phase=phase,
    )
    return fake


async def _caller(session: AsyncSession, org: OrgWithAdmin) -> User:
    user = await session.get(User, org.admin_id)
    assert user is not None
    return user


async def _row(alloc_id: object) -> ComputeAllocation:
    async with AsyncSessionLocal() as db:
        row = await db.get(ComputeAllocation, alloc_id)
        assert row is not None
        return row


# -- sleep ------------------------------------------------------------------


async def test_sleep_stops_the_machine_and_leaves_the_metered_states(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake("i-sleep")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-sleep")
    caller = await _caller(real_session, org_admin)

    await provisioning.sleep(real_session, alloc, caller=caller)

    row = await _row(alloc.id)
    assert row.state == "asleep"
    # The provider machine was stopped (kept), not terminated.
    assert fake.nodes["i-sleep"].stopped is True
    assert fake.nodes["i-sleep"].phase != GONE


async def test_an_asleep_box_is_not_metered(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The money invariant: the meter never bills an asleep machine. If asleep
    were wrongly in the metered states, this pass would poll and bill it."""
    alloc = await _ready_box(
        real_session, org_admin, machine_id="i-quiet", state="asleep", last_metered_at=T0
    )
    provider = FakeProvider()
    provider.mark_ready("i-quiet")  # the provider would say it is running
    async with AsyncSessionLocal() as db:
        await meter_and_cutoff(db, provider=provider, now=T0 + timedelta(minutes=30))
    row = await _row(alloc.id)
    assert row.minutes_billed == 0 and row.billed_nanos == 0
    assert row.last_metered_at == T0  # untouched
    assert "i-quiet" not in provider.status_calls  # never even polled


@pytest.mark.parametrize("state", ["provisioning", "bootstrapping", "draining", "asleep"])
async def test_sleep_refuses_a_machine_that_is_not_ready(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    fake = _fake("i-x")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-x", state=state)
    caller = await _caller(real_session, org_admin)
    with pytest.raises(provisioning.ProvisionError) as info:
        await provisioning.sleep(real_session, alloc, caller=caller)
    assert info.value.status == 409
    assert fake.nodes["i-x"].stopped is False  # nothing was stopped


# -- wake -------------------------------------------------------------------


async def test_wake_starts_the_machine_and_resets_metering(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waking resumes billing from NOW, never from before the sleep — otherwise
    the next meter tick would bill the whole stopped window as elapsed minutes."""
    fake = _fake("i-wake")
    fake.nodes["i-wake"].stopped = True
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    slept_at = T0 - timedelta(days=2)
    alloc = await _ready_box(
        real_session, org_admin, machine_id="i-wake", state="asleep", last_metered_at=slept_at
    )
    caller = await _caller(real_session, org_admin)
    wake_at = T0 + timedelta(minutes=5)

    await provisioning.wake(real_session, alloc, caller=caller, now=wake_at)

    row = await _row(alloc.id)
    assert row.state == "ready"
    assert fake.nodes["i-wake"].stopped is False  # started
    assert row.last_metered_at == wake_at  # not slept_at — the gap is not billed


async def test_a_woken_box_reads_starting_with_no_readings_until_its_daemon_beats(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake starts the provider machine; the daemon on it has not said a word
    yet. Until it does the box is ``starting`` — never ``ready`` off the beat
    and the cpu/memory readings it sent before the sleep — and the first
    heartbeat after the wake is what makes it ready again."""
    from alkera_core.compute.machines import machine_status

    fake = _fake("i-woken")
    fake.nodes["i-woken"].stopped = True
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-woken", state="asleep")
    alloc.last_heartbeat_at = T0 - timedelta(minutes=3)
    alloc.resources_json = {"cpu_percent": 12.0, "memory_used_bytes": 1, "memory_limit_bytes": 8}
    await real_session.commit()
    caller = await _caller(real_session, org_admin)
    wake_at = T0 + timedelta(minutes=5)

    await provisioning.wake(real_session, alloc, caller=caller, now=wake_at)

    row = await _row(alloc.id)
    assert (row.last_heartbeat_at, row.resources_json) == (None, None)
    assert machine_status(row, now=wake_at + timedelta(minutes=12)) == "starting"
    row.last_heartbeat_at = wake_at + timedelta(minutes=1)
    assert machine_status(row, now=wake_at + timedelta(minutes=1, seconds=5)) == "ready"


async def test_wake_that_the_provider_refuses_leaves_the_row_asleep(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-safe: if the machine will not start, the row stays asleep and
    unbilled rather than flipping to a ready row for a machine that is not up.
    The wake is kept for the reconcile and answered ``409 wake_pending``."""

    class _NoStart(FakeNodeProvider):
        async def start(self, machine_id: str) -> None:
            from alkera_core.compute.provider import ComputeProviderError

            raise ComputeProviderError("start refused")

    fake = _NoStart(kind="ec2")
    fake.nodes["i-stuck"] = FakeNode(
        launch=NodeLaunch(
            allocation_id=uuid4(), name="i-stuck", type_code="m6i.large", storage_gb=0, script=""
        ),
        phase=RUNNING,
    )
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    alloc = await _ready_box(
        real_session, org_admin, machine_id="i-stuck", state="asleep", last_metered_at=T0
    )
    caller = await _caller(real_session, org_admin)
    with pytest.raises(provisioning.ProvisionError) as info:
        await provisioning.wake(real_session, alloc, caller=caller)
    assert (info.value.status, info.value.code) == (409, "wake_pending")
    row = await _row(alloc.id)
    assert row.state == "asleep"
    assert row.wake_requested_at is not None
    assert row.last_metered_at == T0  # untouched, so nothing is billed


async def test_wake_refuses_a_machine_that_is_not_asleep(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake("i-ready")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-ready", state="ready")
    caller = await _caller(real_session, org_admin)
    with pytest.raises(provisioning.ProvisionError) as info:
        await provisioning.wake(real_session, alloc, caller=caller)
    assert info.value.status == 409


# -- the reconcile honours asleep -------------------------------------------


async def test_the_reconcile_restops_a_running_asleep_box_and_keeps_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A sleep whose stop never landed leaves the box running; the reconcile
    tries the stop again, and the row stays asleep either way."""
    fake = _fake("i-runaway", phase=RUNNING)  # never actually stopped
    alloc = await _ready_box(real_session, org_admin, machine_id="i-runaway", state="asleep")
    async with AsyncSessionLocal() as db:
        await reconcile_nodes(db, providers=lambda kind: fake, now=T0 + timedelta(minutes=1))
    row = await _row(alloc.id)
    assert row.state == "asleep"
    assert fake.nodes["i-runaway"].stopped is True  # the reconcile re-issued the stop


async def test_the_reconcile_leaves_a_stopped_asleep_box_asleep(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """A slept machine reads ``stopped`` at its provider (a stopped RunPod pod is
    EXITED, a stopped EC2 instance ``stopped``): the reconcile must NOT release
    it, which would reap a box the org deliberately kept."""
    fake = _fake("i-exited", phase=STOPPED)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-exited", state="asleep")
    async with AsyncSessionLocal() as db:
        await reconcile_nodes(db, providers=lambda kind: fake, now=T0 + timedelta(minutes=1))
    row = await _row(alloc.id)
    assert row.state == "asleep"  # not released, not failed


async def test_an_asleep_box_the_provider_no_longer_has_is_lost_and_released(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Gone is not stopped: a slept machine terminated under us (its disk with
    it) is not kept asleep for ever; it is lost, and released."""
    fake = _fake("i-vanished", phase=GONE)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-vanished", state="asleep")
    async with AsyncSessionLocal() as db:
        await reconcile_nodes(db, providers=lambda kind: fake, now=T0 + timedelta(minutes=1))
    row = await _row(alloc.id)
    assert row.state == "released"
    assert row.terminated_reason == "provider_gone"


# -- off = terminate --------------------------------------------------------


async def test_off_is_a_terminate_from_asleep(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake("i-off")
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    alloc = await _ready_box(real_session, org_admin, machine_id="i-off", state="asleep")
    caller = await _caller(real_session, org_admin)

    await provisioning.terminate(real_session, alloc, caller=caller, force=True)

    row = await _row(alloc.id)
    # The provider confirmed the terminate, so the release finished inline.
    assert row.state == "released"
    assert row.terminated_reason == "user_released"
