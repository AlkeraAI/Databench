"""The org-machine intents against a real database, and the constraints that
keep every org machine inside one org.

Each test commits what it builds (two orgs per test, unique names) so the
database's own triggers and foreign keys are what refuse the cross-org writes.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.compute.org_machines import (
    AdmittedRate,
    create_org_machine,
    new_allocation_for,
    request_delete,
    request_power,
)
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import actor_system
from alkera_core.models import (
    ComputeAllocation,
    ComputeMachineType,
    ComputeOffering,
    EventOutbox,
    OrgMachine,
    OrgMachineAudience,
    Team,
    User,
    WorkspaceMachineMove,
    WorkspaceObject,
)
from alkera_core.schemas.org_machines import AudienceGrant
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ACTOR = actor_system("org-machine-test")
CHECK_VIOLATION = "23514"
FOREIGN_KEY_VIOLATION = "23503"


@dataclass
class Org:
    id: uuid.UUID
    sub_team: uuid.UUID
    user: uuid.UUID


@dataclass
class World:
    a: Org
    b: Org
    machine_type: ComputeMachineType
    offering: ComputeOffering


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def _org(db: AsyncSession) -> Org:
    tag = secrets.token_hex(4)
    root = Team(name=f"om-{tag}", is_root=True)
    db.add(root)
    await db.flush()
    sub = Team(name=f"om-sub-{tag}", parent_team_id=root.id, is_root=False)
    user = User(home_org_team_id=root.id, email=f"om-{tag}@example.com", first_name="O")
    db.add_all([sub, user])
    await db.flush()
    return Org(id=root.id, sub_team=sub.id, user=user.id)


@pytest_asyncio.fixture
async def world(db: AsyncSession) -> World:
    a, b = await _org(db), await _org(db)
    machine_type = ComputeMachineType(
        provider="runpod",
        provider_type_id=f"om-{secrets.token_hex(4)}",
        display_name="Test GPU",
        compute_class="gpu",
        gpu_count=1,
        provider_price_per_minute_nanos=1_000_000,
    )
    db.add(machine_type)
    await db.flush()
    offering = ComputeOffering(
        machine_type_id=machine_type.id,
        name="Test GPU",
        pricing_mode="pass_through",
        markup_bps=2_000,
        storage_gb_default=10,
        storage_gb_max=100,
        storage_rate_per_gb_month_nanos=43_200,
        audience="all",
    )
    db.add(offering)
    await db.commit()
    return World(a=a, b=b, machine_type=machine_type, offering=offering)


async def _machine(
    db: AsyncSession, world: World, *, org: Org | None = None, **fields: Any
) -> OrgMachine:
    org = org or world.a
    values: dict[str, Any] = {
        "org_id": org.id,
        "owner_team_id": org.id,
        "offering": world.offering,
        "machine_type": world.machine_type,
        "name": f"m-{secrets.token_hex(3)}",
        "acquisition": "purchased",
        "free_until": None,
        "use_mode": "assigned",
        "storage_gb": 20,
        "audience": [AudienceGrant(kind="org")],
        "idle_stop_minutes": None,
        "monthly_cap_nanos": None,
        "admitted": AdmittedRate(rate_per_minute_nanos=1_200_000, billing_account_id=None),
        "created_by": org.user,
        "now": NOW,
    }
    values.update(fields)
    om = await create_org_machine(db, **values)
    await db.commit()
    return om


async def _alloc(db: AsyncSession, om: OrgMachine) -> ComputeAllocation:
    assert om.current_allocation_id is not None
    alloc = await db.get(ComputeAllocation, om.current_allocation_id, populate_existing=True)
    assert alloc is not None
    return alloc


def _sqlstate(error: DBAPIError) -> str | None:
    orig = error.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


# ---- create -----------------------------------------------------------------------


async def test_create_writes_the_machine_its_audience_and_a_pending_allocation_of_its_org(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(
        db,
        world,
        audience=[
            AudienceGrant(kind="team", team_id=str(world.a.sub_team)),
            AudienceGrant(kind="user", user_id=str(world.a.user)),
            AudienceGrant(kind="user", user_id=str(world.a.user)),
        ],
    )
    alloc = await _alloc(db, om)
    assert (alloc.state, alloc.lifecycle, alloc.origin, alloc.tenancy) == (
        "pending",
        "workspace",
        "provisioned",
        "dedicated",
    )
    assert (alloc.tenant_org_id, alloc.org_team_id, alloc.org_machine_id) == (
        world.a.id,
        world.a.id,
        om.id,
    )
    # 1_000_000 marked up 20 percent; 20 GB at 43_200 a GB-month is 20 a minute.
    assert alloc.price_per_minute_nanos == 1_200_000
    assert alloc.storage_price_per_minute_nanos == 20
    assert alloc.true_cost_per_minute_nanos == 1_000_000
    assert alloc.true_storage_cost_per_minute_nanos == 20
    assert alloc.storage_gb == 20
    grants = (
        await db.execute(
            select(OrgMachineAudience.grantee_kind).where(
                OrgMachineAudience.org_machine_id == om.id
            )
        )
    ).scalars()
    # The duplicate grant is written once.
    assert sorted(grants) == ["team", "user"]


@pytest.mark.parametrize(
    ("free_until", "rate"),
    [
        pytest.param(NOW + timedelta(days=30), 0, id="granted-within-its-free-period-is-free"),
        pytest.param(NOW - timedelta(seconds=1), 1_200_000, id="granted-past-it-is-billed"),
    ],
)
async def test_a_granted_machine_is_pinned_at_zero_while_free(
    db: AsyncSession, world: World, free_until: datetime, rate: int
) -> None:
    om = await _machine(db, world, acquisition="granted", free_until=free_until)
    alloc = await _alloc(db, om)
    assert alloc.price_per_minute_nanos == rate
    assert alloc.storage_price_per_minute_nanos == (0 if rate == 0 else 20)
    # What it costs us is recorded either way.
    assert alloc.true_cost_per_minute_nanos == 1_000_000


async def test_an_allocation_is_priced_at_the_rate_its_start_was_admitted_at(
    db: AsyncSession, world: World
) -> None:
    """A negotiated grant's rate, not the offering's list price (1,200,000):
    the machine's first allocation and a later one are each made at the rate
    their start was admitted at."""
    om = await _machine(
        db, world, admitted=AdmittedRate(rate_per_minute_nanos=700_000, billing_account_id=None)
    )
    first = await _alloc(db, om)
    second = await new_allocation_for(
        db,
        om,
        offering=world.offering,
        machine_type=world.machine_type,
        user_id=world.a.user,
        admitted=AdmittedRate(rate_per_minute_nanos=650_000, billing_account_id=None),
    )
    assert (first.price_per_minute_nanos, second.price_per_minute_nanos) == (700_000, 650_000)


async def test_a_replacement_allocation_becomes_the_current_one(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(db, world)
    first = om.current_allocation_id
    second = await new_allocation_for(
        db,
        om,
        offering=world.offering,
        machine_type=world.machine_type,
        user_id=world.a.user,
        admitted=AdmittedRate(rate_per_minute_nanos=1_200_000, billing_account_id=None),
    )
    await db.commit()
    assert om.current_allocation_id == second.id != first


async def test_an_audience_team_of_another_org_is_refused(db: AsyncSession, world: World) -> None:
    with pytest.raises(DBAPIError) as refused:
        await _machine(
            db, world, audience=[AudienceGrant(kind="team", team_id=str(world.b.sub_team))]
        )
    await db.rollback()
    assert _sqlstate(refused.value) == CHECK_VIOLATION


# ---- power ------------------------------------------------------------------------


async def _set_state(db: AsyncSession, alloc: ComputeAllocation, state: str) -> None:
    await db.execute(
        update(ComputeAllocation).where(ComputeAllocation.id == alloc.id).values(state=state)
    )
    await db.commit()


async def _last_event(db: AsyncSession, om: OrgMachine) -> EventOutbox:
    row = (
        await db.execute(
            select(EventOutbox)
            .where(EventOutbox.entity_id == str(om.id))
            .order_by(EventOutbox.id.desc())
            .limit(1)
        )
    ).scalar_one()
    return row


async def test_stopping_a_ready_machine_drains_it_with_the_kind_and_deadline(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(db, world)
    alloc = await _alloc(db, om)
    await _set_state(db, alloc, "ready")
    deadline = NOW + timedelta(minutes=5)
    version = om.version
    await request_power(
        db,
        om,
        desired="off",
        reason="credits",
        drain_kind="credits",
        deadline=deadline,
        actor=ACTOR,
        now=NOW,
    )
    await db.commit()
    alloc = await _alloc(db, om)
    assert (alloc.state, alloc.drain_kind, alloc.drain_deadline_at) == (
        "draining",
        "credits",
        deadline,
    )
    assert (om.desired_power, om.stop_reason, om.version) == ("off", "credits", version + 1)
    event = await _last_event(db, om)
    assert event.type == "org_machine.changed"
    assert event.org_id == world.a.id
    # Exactly the three words, and never a price.
    assert event.payload == {"state": "stopping", "step": None, "stop_reason": "credits"}


async def test_a_stop_without_a_deadline_drains_now_as_a_user_stop(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(db, world)
    await _set_state(db, await _alloc(db, om), "ready")
    await request_power(
        db, om, desired="off", reason="user", drain_kind=None, deadline=None, actor=ACTOR, now=NOW
    )
    await db.commit()
    alloc = await _alloc(db, om)
    assert (alloc.drain_kind, alloc.drain_deadline_at) == ("user", NOW)


async def test_stopping_a_machine_still_coming_up_leaves_its_allocation_alone(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(db, world)
    await _set_state(db, await _alloc(db, om), "provisioning")
    await request_power(
        db, om, desired="off", reason="user", drain_kind="user", deadline=None, actor=ACTOR
    )
    await db.commit()
    alloc = await _alloc(db, om)
    assert (alloc.state, alloc.drain_kind) == ("provisioning", None)
    assert om.desired_power == "off"


async def test_starting_an_asleep_machine_records_a_wake_and_no_provider_call(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(db, world)
    await _set_state(db, await _alloc(db, om), "asleep")
    om.desired_power, om.stop_reason = "off", "idle"
    await db.commit()
    await request_power(
        db, om, desired="on", reason="start", drain_kind=None, deadline=None, actor=ACTOR, now=NOW
    )
    await db.commit()
    alloc = await _alloc(db, om)
    assert alloc.state == "asleep"
    assert alloc.wake_requested_at == NOW
    assert alloc.wake_request_json is not None and alloc.wake_request_json["reason"] == "start"
    assert (om.desired_power, om.stop_reason) == ("on", "")
    assert (await _last_event(db, om)).payload == {
        "state": "starting",
        "step": "booting",
        "stop_reason": "",
    }


@pytest.mark.parametrize("reason", ["", "bored"])
async def test_a_stop_needs_a_known_reason(db: AsyncSession, world: World, reason: str) -> None:
    om = await _machine(db, world)
    with pytest.raises(ValueError, match="stop reason"):
        await request_power(
            db, om, desired="off", reason=reason, drain_kind=None, deadline=None, actor=ACTOR
        )
    assert om.desired_power == "on"


# ---- delete -----------------------------------------------------------------------


async def _workspace(db: AsyncSession, org: Org, pin: uuid.UUID | None) -> WorkspaceObject:
    obj = WorkspaceObject(
        org_team_id=org.id,
        logical_id=f"ws-{secrets.token_hex(4)}",
        type="workspace",
        owner_user_id=org.user,
        visibility_scope="org",
        spec={"kind": "project", "machine_pin": str(pin) if pin else None},
    )
    db.add(obj)
    await db.commit()
    return obj


async def test_delete_clears_pins_and_audiences_and_wants_the_machine_off(
    db: AsyncSession, world: World
) -> None:
    om = await _machine(db, world)
    pinned = await _workspace(db, world.a, om.id)
    # The same id written into another org's workspace is that org's business.
    foreign = await _workspace(db, world.b, om.id)
    other = await _workspace(db, world.a, uuid.uuid4())
    await request_delete(db, om, actor=ACTOR, now=NOW)
    await db.commit()

    assert (om.deleted_at, om.desired_power) == (NOW, "off")
    assert (
        await db.execute(
            select(OrgMachineAudience).where(OrgMachineAudience.org_machine_id == om.id)
        )
    ).first() is None
    for obj, pin in ((pinned, None), (foreign, str(om.id)), (other, other.spec["machine_pin"])):
        await db.refresh(obj)
        assert obj.spec["machine_pin"] == pin
    assert pinned.version == 2
    assert (await _last_event(db, om)).payload["state"] == "deleted"


async def test_a_deleted_machine_takes_no_power_request(db: AsyncSession, world: World) -> None:
    om = await _machine(db, world)
    await request_delete(db, om, actor=ACTOR)
    await db.commit()
    with pytest.raises(ValueError, match="deleted"):
        await request_power(
            db, om, desired="on", reason="start", drain_kind=None, deadline=None, actor=ACTOR
        )


# ---- the database keeps each machine in one org --------------------------------------


async def test_an_allocations_tenant_cannot_change_once_set(db: AsyncSession, world: World) -> None:
    om = await _machine(db, world)
    alloc = await _alloc(db, om)
    with pytest.raises(DBAPIError) as refused:
        await db.execute(
            update(ComputeAllocation)
            .where(ComputeAllocation.id == alloc.id)
            .values(tenant_org_id=world.b.id, org_machine_id=None)
        )
    await db.rollback()
    assert _sqlstate(refused.value) == CHECK_VIOLATION


async def test_an_unset_tenant_may_be_set_once(db: AsyncSession, world: World) -> None:
    """The trigger refuses a change, not the first assignment."""
    alloc = ComputeAllocation(
        user_id=world.a.user,
        org_team_id=world.a.id,
        machine_type_id=world.machine_type.id,
    )
    db.add(alloc)
    await db.commit()
    alloc.tenant_org_id = world.a.id
    await db.commit()
    alloc.tenant_org_id = world.b.id
    with pytest.raises(DBAPIError) as refused:
        await db.commit()
    await db.rollback()
    assert _sqlstate(refused.value) == CHECK_VIOLATION


async def test_an_org_machine_cannot_point_at_another_orgs_allocation(
    db: AsyncSession, world: World
) -> None:
    mine = await _machine(db, world)
    theirs = ComputeAllocation(
        user_id=world.b.user,
        org_team_id=world.b.id,
        machine_type_id=world.machine_type.id,
        tenant_org_id=world.b.id,
    )
    db.add(theirs)
    await db.commit()
    with pytest.raises(IntegrityError) as refused:
        await db.execute(
            update(OrgMachine)
            .where(OrgMachine.id == mine.id)
            .values(current_allocation_id=theirs.id)
        )
    await db.rollback()
    assert _sqlstate(refused.value) == FOREIGN_KEY_VIOLATION


async def test_an_allocation_cannot_back_another_orgs_machine(
    db: AsyncSession, world: World
) -> None:
    theirs = await _machine(db, world, org=world.b)
    alloc = ComputeAllocation(
        user_id=world.a.user,
        org_team_id=world.a.id,
        machine_type_id=world.machine_type.id,
        tenant_org_id=world.a.id,
        org_machine_id=theirs.id,
    )
    db.add(alloc)
    with pytest.raises(IntegrityError) as refused:
        await db.commit()
    await db.rollback()
    assert _sqlstate(refused.value) == FOREIGN_KEY_VIOLATION


async def test_an_allocation_naming_an_org_machine_must_name_its_tenant(
    db: AsyncSession, world: World
) -> None:
    """Without the tenant the composite key would not be checked at all."""
    theirs = await _machine(db, world, org=world.b)
    db.add(
        ComputeAllocation(
            user_id=world.a.user,
            org_team_id=world.a.id,
            machine_type_id=world.machine_type.id,
            org_machine_id=theirs.id,
        )
    )
    with pytest.raises(IntegrityError) as refused:
        await db.commit()
    await db.rollback()
    assert _sqlstate(refused.value) == CHECK_VIOLATION


async def test_an_audience_row_cannot_name_another_orgs_machine(
    db: AsyncSession, world: World
) -> None:
    theirs = await _machine(db, world, org=world.b)
    db.add(
        OrgMachineAudience(
            org_team_id=world.a.id,
            org_machine_id=theirs.id,
            grantee_kind="user",
            user_id=world.a.user,
        )
    )
    with pytest.raises(IntegrityError) as refused:
        await db.commit()
    await db.rollback()
    assert _sqlstate(refused.value) == FOREIGN_KEY_VIOLATION


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        pytest.param("workspace", CHECK_VIOLATION, id="a-workspace-of-another-org"),
        pytest.param("target", FOREIGN_KEY_VIOLATION, id="a-target-machine-of-another-org"),
    ],
)
async def test_a_move_stays_inside_its_org(
    db: AsyncSession, world: World, end: str, expected: str
) -> None:
    mine = await _machine(db, world)
    theirs = await _machine(db, world, org=world.b)
    own_workspace = await _workspace(db, world.a, None)
    their_workspace = await _workspace(db, world.b, None)
    db.add(
        WorkspaceMachineMove(
            org_team_id=world.a.id,
            workspace_id=their_workspace.id if end == "workspace" else own_workspace.id,
            to_org_machine_id=theirs.id if end == "target" else mine.id,
        )
    )
    with pytest.raises(DBAPIError) as refused:
        await db.commit()
    await db.rollback()
    assert _sqlstate(refused.value) == expected


async def test_one_active_move_per_workspace(db: AsyncSession, world: World) -> None:
    mine = await _machine(db, world)
    ws = await _workspace(db, world.a, None)
    db.add(WorkspaceMachineMove(org_team_id=world.a.id, workspace_id=ws.id, state="done"))
    db.add(
        WorkspaceMachineMove(org_team_id=world.a.id, workspace_id=ws.id, to_org_machine_id=mine.id)
    )
    await db.commit()
    db.add(WorkspaceMachineMove(org_team_id=world.a.id, workspace_id=ws.id, state="draining"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_live_names_are_unique_per_org_ignoring_case_and_free_again_once_deleted(
    db: AsyncSession, world: World
) -> None:
    first = await _machine(db, world, name="Trainer")
    async with AsyncSessionLocal() as other:
        with pytest.raises(IntegrityError):
            await _machine(other, world, name="trainer")
    # Another org may use the name.
    await _machine(db, world, org=world.b, name="Trainer")
    await request_delete(db, first, actor=ACTOR)
    await db.commit()
    again = await _machine(db, world, name="TRAINER")
    assert again.id != first.id


# ---- who pays -------------------------------------------------------------------------
