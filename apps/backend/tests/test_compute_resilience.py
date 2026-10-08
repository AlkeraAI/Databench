"""What a reader and an operator see when a machine fails in each way it can.

Every case drives the real reconcile, placement, heartbeat and admin routes
with the scripted node provider and an explicit clock, and asserts what the
chat's binding, the machine row, its event log and the provider were left
with. Each one fails when the recovery it names is taken out.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute import availability as availability_core
from alkera_core.compute.machines import sweep_reachability
from alkera_core.compute.node_reconcile import ProviderErrorLog, reconcile_nodes
from alkera_core.compute.nodes import NodeDescription, NodeLaunch
from alkera_core.compute.provider import EC2, ComputeProviderError
from alkera_core.compute.transitions import transition
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import MachineCredential, OrgComputeAssignment, User, WorkspaceObject
from alkera_core.models.compute import ComputeAllocation, ComputeAllocationEvent
from alkera_test_support.compute.fake_nodes import FakeNodeProvider
from backend.services.chats import chat_service
from backend.services.compute import machines, placement, provisioning
from backend.services.credentials import machine_credentials as machine_credential_service
from httpx import AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs
from tests._compute_helpers import make_machine_type
from tests.conftest import OrgWithAdmin, login

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.compute_rows,
    pytest.mark.xdist_group("compute-fleet"),
]

MACHINES = "/admin/v1/machines"
KIND = "fakenode"


@pytest.fixture(autouse=True)
async def _quiet_plane(real_session: AsyncSession) -> None:
    """The pool is global and the node reconcile reads every provisioned box,
    so whatever another test left live is taken off the plane first."""
    await real_session.execute(delete(OrgComputeAssignment))
    await real_session.execute(
        update(ComputeAllocation)
        .where(
            ComputeAllocation.lifecycle == "workspace",
            ComputeAllocation.state.not_in(("released", "failed")),
        )
        .values(state="released")
    )
    await real_session.commit()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNodeProvider]:
    provider = FakeNodeProvider(kind=EC2)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: provider)
    availability_core.CACHE.clear()
    yield provider
    availability_core.CACHE.clear()


# --------------------------------------------------------------------------- #
# seeds
# --------------------------------------------------------------------------- #


async def _node(
    session: AsyncSession,
    org: OrgWithAdmin,
    fake: FakeNodeProvider,
    *,
    state: str,
    phase: str,
    tenancy: str = "pool",
    beat: datetime | None = None,
    chats: int = 0,
    at: datetime | None = None,
) -> ComputeAllocation:
    """A provisioned box the fake provider is running, holding its credential
    and its node secret, the way a claim leaves one."""
    moment = at or datetime.now(UTC)
    mt = await make_machine_type(session, provider="runpod")
    alloc = ComputeAllocation(
        user_id=org.admin_id,
        org_team_id=org.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        tenancy=tenancy,
        name=f"box-{uuid4().hex[:8]}",
        state=state,
        created_at=moment,
        state_changed_at=moment,
        ready_at=moment if state in ("ready", "draining") else None,
        last_heartbeat_at=beat,
        chats_served=chats,
        capacity=6,
        # A pool box serves only under gVisor; the seed is one that reported it.
        sandbox="gvisor" if tenancy == "pool" else "none",
    )
    session.add(alloc)
    await session.flush()
    machine_id = await fake.run(
        NodeLaunch(allocation_id=alloc.id, name="n", type_code="t", storage_gb=10, script="")
    )
    fake.script(machine_id, phase)  # type: ignore[arg-type]
    await fake.store_credential(alloc.id, {"ALKERA_MACHINE_CREDENTIAL": "c"})
    alloc.provider_machine_id = machine_id
    credential, _raw = await machine_credential_service.mint(
        session,
        org_id=org.org_id,
        created_by=org.admin_id,
        machine_type=mt,
        tenancy=tenancy,
        label=alloc.name,
    )
    credential.machine_id = alloc.id
    await session.commit()
    return alloc


async def _chat_on(session: AsyncSession, org: OrgWithAdmin, box: ComputeAllocation | None) -> Any:
    owner = await session.get(User, org.admin_id)
    assert owner is not None
    chat, _ = await chat_service.create_chat(
        session,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="c",
        client_id=None,
        machine_id=str(box.id) if box is not None else None,
        machine_status="ready" if box is not None else "none",
    )
    await session.commit()
    return chat


async def _bound(chat_id: UUID) -> str | None:
    async with AsyncSessionLocal() as db:
        chat = await db.get(WorkspaceObject, chat_id)
        assert chat is not None
        return chat_service.chat_spec_of(chat).machine_id


async def _edges(alloc_id: UUID) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(ComputeAllocationEvent.from_state, ComputeAllocationEvent.to_state)
                .where(ComputeAllocationEvent.allocation_id == alloc_id)
                .order_by(ComputeAllocationEvent.at, ComputeAllocationEvent.id)
            )
        ).all()
        return [(a, b) for a, b in rows]


async def _row(alloc_id: UUID) -> ComputeAllocation:
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, alloc_id)
        assert alloc is not None
        return alloc


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _pass(
    fake: FakeNodeProvider, at: datetime, *, errors: ProviderErrorLog | None = None
) -> Any:
    async with AsyncSessionLocal() as db:
        return await reconcile_nodes(
            db, providers=lambda kind: fake, now=at, errors=errors or ProviderErrorLog()
        )


# --------------------------------------------------------------------------- #
# a provider reports a machine gone
# --------------------------------------------------------------------------- #


async def test_a_machine_the_provider_lost_is_released_once_and_its_chats_move_on(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The box vanished outside us (an EC2 terminate, a RunPod pod gone). One
    pass records ready -> lost -> released, revokes its credential and deletes
    its node secret; a second pass changes nothing; the chat that was on it
    reads as having no machine and its next message lands on the survivor."""
    now = datetime.now(UTC)
    fake = FakeNodeProvider(kind=KIND)
    dead = await _node(
        real_session, platform_admin, fake, state="ready", phase="gone", beat=now, chats=1
    )
    survivor = await _node(real_session, platform_admin, fake, state="ready", phase="running")
    survivor.last_heartbeat_at = now
    await real_session.commit()
    chat = await _chat_on(real_session, org_admin, dead)

    first = await _pass(fake, now)
    assert first.edges == [("ready", "released")]
    assert (await _row(dead.id)).state == "released"
    assert await _edges(dead.id) == [("ready", "lost"), ("lost", "released")]
    # The history is read in time order, so the two edges of one pass must not
    # share a moment: the page would show the box released before it was lost.
    async with AsyncSessionLocal() as db:
        moments = (
            (
                await db.execute(
                    select(ComputeAllocationEvent.at).where(
                        ComputeAllocationEvent.allocation_id == dead.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(set(moments)) == 2
    assert dead.id not in fake.secrets
    assert survivor.id in fake.secrets
    revoked = (
        await real_session.execute(
            select(MachineCredential.revoked_at).where(MachineCredential.machine_id == dead.id)
        )
    ).scalar_one()
    assert revoked is not None

    second = await _pass(fake, now + timedelta(minutes=1))
    assert (second.moved, second.edges) == (0, [])
    assert await _edges(dead.id) == [("ready", "lost"), ("lost", "released")]

    async with AsyncSessionLocal() as db:
        stored = await db.get(WorkspaceObject, chat.id)
        assert stored is not None
        assert (
            await placement.bound_machine_state(
                db,
                org_team_id=org_admin.org_id,
                machine_id=chat_service.chat_spec_of(stored).machine_id,
            )
            == "none"
        )
        await placement.rebind_if_stranded(
            db, chat=stored, ctx=_ctx(org_admin), org_team_id=org_admin.org_id
        )
        await db.commit()
    assert await _bound(chat.id) == str(survivor.id)


# --------------------------------------------------------------------------- #
# heartbeat stale while the provider says running
# --------------------------------------------------------------------------- #


async def test_a_hung_daemon_reads_unreachable_takes_nothing_and_is_not_replaced(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    now = datetime.now(UTC)
    hung = await _node(
        real_session,
        platform_admin,
        fake,
        state="ready",
        phase="running",
        beat=now - timedelta(minutes=5),
        chats=1,
    )
    waiting = await _chat_on(real_session, org_admin, hung)

    # The admin page: the lifecycle is still ready, the liveness is not.
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    detail = await client.get(f"{MACHINES}/{hung.id}")
    assert detail.status_code == 200, detail.text
    assert (detail.json()["state"], detail.json()["liveness"]) == ("ready", "unreachable")

    # Placement gives it nothing new.
    async with AsyncSessionLocal() as db:
        assert await placement.place_for_org(db, org_team_id=org_admin.org_id) is None

    # The reconcile believes the provider: nothing is failed, lost or rebuilt.
    summary = await _pass(fake, now)
    assert summary.edges == []
    assert (await _row(hung.id)).state == "ready"
    assert len(fake.nodes) == 1

    # Beats resume: the box is ready again and its chat never moved.
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, hung.id)
        assert alloc is not None
        await machines.heartbeat(db, alloc, ctx=_ctx(platform_admin), now=datetime.now(UTC))
    async with AsyncSessionLocal() as db:
        assert (await placement.place_for_org(db, org_team_id=org_admin.org_id)) is not None
    assert await _bound(waiting.id) == str(hung.id)
    assert await _edges(hung.id) == []
    provisioned = (
        await real_session.execute(
            select(func.count())
            .select_from(ComputeAllocation)
            .where(
                ComputeAllocation.origin == "provisioned",
                ComputeAllocation.state.not_in(("released", "failed")),
            )
        )
    ).scalar_one()
    assert provisioned == 1


# --------------------------------------------------------------------------- #
# drain with chats in flight
# --------------------------------------------------------------------------- #


async def test_a_drained_box_places_nothing_holds_its_chats_and_forces_them_off_in_order(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    now = datetime.now(UTC)
    mt = await make_machine_type(real_session, provider=EC2)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    made = await client.post(
        f"{MACHINES}/provision",
        json={
            "provider": "ec2",
            "machine_type_code": mt.provider_type_id,
            "storage_gb": 50,
            "tenancy": "pool",
            "name": "leaving",
        },
    )
    assert made.status_code == 202, made.text
    box_id = UUID(made.json()["id"])
    async with AsyncSessionLocal() as db:
        box = await db.get(ComputeAllocation, box_id)
        assert box is not None
        transition(db, box, "bootstrapping")
        transition(db, box, "ready")
        box.last_heartbeat_at = now
        box.chats_served = 2
        await db.commit()
    other = await _node(
        real_session, platform_admin, fake, state="ready", phase="running", beat=now
    )
    first = await _chat_on(real_session, org_admin, await _row(box_id))

    drained = await client.post(f"{MACHINES}/{box_id}/drain", json={"reason": "resize"})
    assert drained.status_code == 200, drained.text

    # No new chat lands on it, and it stays draining while it holds two.
    async with AsyncSessionLocal() as db:
        target = await placement.place_for_org(db, org_team_id=org_admin.org_id)
        assert target is not None and target.id == other.id
    summary = await _pass(fake, now + timedelta(minutes=1))
    assert (await _row(box_id)).state == "draining"
    assert ("draining", "releasing") not in summary.edges

    refused = await client.post(f"{MACHINES}/{box_id}/terminate", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "machine_has_chats"
    assert "2 chats" in refused.json()["error"]["message"]
    assert (await _row(box_id)).state == "draining"

    forced = await client.post(f"{MACHINES}/{box_id}/terminate", json={"force": True})
    assert forced.status_code == 202, forced.text
    assert await _bound(first.id) == str(other.id)
    assert await _edges(box_id) == [
        ("pending", "provisioning"),
        ("provisioning", "bootstrapping"),
        ("bootstrapping", "ready"),
        ("ready", "draining"),
        ("draining", "releasing"),
        # The provider confirmed the terminate, so it finished in the request.
        ("releasing", "released"),
    ]


# --------------------------------------------------------------------------- #
# provisioning that never boots
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "phase", "after"),
    [
        pytest.param("provisioning", "starting", timedelta(minutes=10, seconds=1), id="never-runs"),
        pytest.param(
            "bootstrapping", "running", timedelta(minutes=15, seconds=1), id="never-claims"
        ),
    ],
)
async def test_a_dedicated_box_that_never_boots_fails_and_its_org_can_be_given_another(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
    state: str,
    phase: str,
    after: timedelta,
) -> None:
    mt = await make_machine_type(real_session, provider=EC2)
    body = {
        "provider": "ec2",
        "machine_type_code": mt.provider_type_id,
        "storage_gb": 50,
        "tenancy": "dedicated",
        "org_id": str(org_admin.org_id),
        "name": "theirs",
    }
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    made = await client.post(f"{MACHINES}/provision", json=body)
    assert made.status_code == 202, made.text
    box_id = UUID(made.json()["id"])
    pod = made.json()["provider_machine_id"]
    fake.script(pod, phase)  # type: ignore[arg-type]
    started = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        box = await db.get(ComputeAllocation, box_id)
        assert box is not None
        if state == "bootstrapping":
            transition(db, box, "bootstrapping", now=started)
        else:
            box.state_changed_at = started
        await db.commit()

    await _pass(fake, started + after)

    failed = await _row(box_id)
    assert failed.state == "failed"
    assert (await fake.describe(pod)).phase == "gone"  # the terminate was made
    assert box_id not in fake.secrets
    history = await _edges(box_id)
    assert history[0] == ("pending", "provisioning")
    assert history[-1] == (state, "failed")

    again = await client.post(f"{MACHINES}/provision", json=body)
    assert again.status_code == 202, again.text
    assert again.json()["dedicated_org"]["id"] == str(org_admin.org_id)
    assert await _edges(box_id) == history


async def test_a_dedicated_box_that_is_still_live_keeps_its_org(
    client: AsyncClient,
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    fake: FakeNodeProvider,
) -> None:
    """The negative half: replacing an assignment is only for a box that is
    finished. One still coming up keeps the org, whatever the admin clicks."""
    mt = await make_machine_type(real_session, provider=EC2)
    body = {
        "provider": "ec2",
        "machine_type_code": mt.provider_type_id,
        "storage_gb": 50,
        "tenancy": "dedicated",
        "org_id": str(org_admin.org_id),
    }
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.post(f"{MACHINES}/provision", json=body)).status_code == 202
    again = await client.post(f"{MACHINES}/provision", json=body)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "org_has_machine"


# --------------------------------------------------------------------------- #
# a provider API that errors or times out
# --------------------------------------------------------------------------- #


class _Flaky(FakeNodeProvider):
    """Answers for every machine but the ones in ``down``."""

    down: set[str]

    async def describe(self, machine_id: str) -> NodeDescription:
        if machine_id in self.down:
            raise ComputeProviderError("RunPod request failed: ReadTimeout")
        return await super().describe(machine_id)


async def test_a_provider_that_cannot_answer_changes_nothing_and_says_so_once(
    real_session: AsyncSession, platform_admin: OrgWithAdmin
) -> None:
    now = datetime.now(UTC)
    fake = _Flaky(kind=KIND)
    fake.down = set()
    silent = await _node(real_session, platform_admin, fake, state="ready", phase="running")
    unsure = await _node(real_session, platform_admin, fake, state="ready", phase="unknown")
    gone = await _node(real_session, platform_admin, fake, state="ready", phase="gone")
    fake.down.add(silent.provider_machine_id)
    errors = ProviderErrorLog()

    with capture_logs() as logs:
        for minute in range(5):
            summary = await _pass(fake, now + timedelta(minutes=minute), errors=errors)
            assert summary.provider_errors == 1

    assert (await _row(silent.id)).state == "ready"
    assert (await _row(unsure.id)).state == "ready"
    assert await _edges(silent.id) == []
    assert await _edges(unsure.id) == []
    # The pass went on past the machine it could not ask.
    assert (await _row(gone.id)).state == "released"
    warned = [e for e in logs if e["event"] == "compute.nodes.provider_error"]
    assert [e["allocation_id"] for e in warned] == [str(silent.id)]

    # It answers again: said once, and a later outage is news again.
    fake.down.clear()
    with capture_logs() as logs:
        await _pass(fake, now + timedelta(minutes=6), errors=errors)
        await _pass(fake, now + timedelta(minutes=7), errors=errors)
        fake.down.add(silent.provider_machine_id)
        await _pass(fake, now + timedelta(minutes=8), errors=errors)
    events = [e["event"] for e in logs if e.get("allocation_id") == str(silent.id)]
    assert events == ["compute.nodes.provider_answered", "compute.nodes.provider_error"]


async def _no_stranded_leftovers() -> None:
    """Tombstone every live chat in this database (a tombstone leaves the
    placement queue). A box coming up serves the most recently active stranded
    chats up to its room, and the chats earlier tests left behind would take
    the room a test is measuring."""
    from alkera_core.models import WorkspaceObject
    from sqlalchemy import update

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.type == "chat", WorkspaceObject.deleted_at == 0)
            .values(deleted_at=datetime.now(UTC).timestamp())
        )
        await db.commit()


async def test_a_chat_waiting_on_a_lost_box_is_placed_on_the_next_heartbeat_of_a_survivor(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The reader already asked and is waiting on the answer the lost box was
    writing, so no next message will place the chat. The survivor was up all
    along and has no ready transition of its own to come — until the pass that
    lost the other box gives it one. The reachability sweep in between must not
    spend it."""
    # The survivor's room is measured against the chats bound to it, and its
    # first beat below offers it every stranded chat this database holds; the
    # test is about the ONE chat that waits, so the leftovers leave the queue.
    await _no_stranded_leftovers()
    now = datetime.now(UTC)
    fake = FakeNodeProvider(kind=KIND)
    dead = await _node(
        real_session, platform_admin, fake, state="ready", phase="gone", beat=now, chats=1
    )
    survivor = await _node(real_session, platform_admin, fake, state="ready", phase="running")
    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, survivor.id)
        assert alloc is not None
        await machines.heartbeat(db, alloc, ctx=_ctx(platform_admin), now=now)
    waiting = await _chat_on(real_session, org_admin, dead)

    await _pass(fake, now)
    async with AsyncSessionLocal() as db:
        await sweep_reachability(db, now=now + timedelta(seconds=5))
    assert await _bound(waiting.id) == str(dead.id)

    async with AsyncSessionLocal() as db:
        alloc = await db.get(ComputeAllocation, survivor.id)
        assert alloc is not None
        await machines.heartbeat(
            db, alloc, ctx=_ctx(platform_admin), now=now + timedelta(seconds=15)
        )
    assert await _bound(waiting.id) == str(survivor.id)
