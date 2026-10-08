"""Where a chat runs once orgs hold machines of their own.

The order: a workspace pinned to an org machine runs there and only there
(running, stopped, starting, waiting for hardware or failed alike) when the
machine is the chat's org's, not deleted, and the chat's owner may use it; an
org's own registered box; the org pool, for an Enterprise org or a self-hosted
deployment only, read at placement time; the shared pool, unless the org said
its chats wait for its own machines.

Every case is driven through the real placement service against real
Postgres. The negative cases are the point: a pin naming another org's
machine binds nothing of that org's, an unpinned chat never lands on an
assigned machine, and a pinned chat is never taken by the pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.compute import (
    DEDICATED_TENANCY,
    POOL_TENANCY,
    ComputeAllocation,
)
from alkera_core.models.org_machines import OrgMachine
from backend.services.compute import placement
from backend.services.org import teams as team_service
from backend.services.workspaces import workspace_service
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._lock_waits import backend_pid, until_waiting_on
from tests._org_machine_helpers import (
    make_org_machine,
    make_team,
    pin_workspace,
    set_fallback,
)
from tests.conftest import OrgWithAdmin, make_member, make_org_enterprise

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

Plan = Literal["self_serve", "enterprise", "self_hosted"]
Pin = Literal[
    "none",
    "running",
    "stopped",
    "failed",
    "waiting",
    "starting",
    "deleted",
    "not_in_audience",
    "foreign",
]
OrgPool = Literal["running", "stopped", "assigned_only", "none"]
Outcome = Literal["pin", "org_pool", "shared", "nothing"]


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The shared pool is global: a pool box another test left live would
    serve this test's org."""
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy.in_((POOL_TENANCY, DEDICATED_TENANCY)))
        .values(state="released")
    )
    await real_session.commit()


@pytest.fixture
def saas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "self_hosted", False)
    yield


async def _shared_box(session: AsyncSession, operator: OrgWithAdmin) -> ComputeAllocation:
    mt = await make_machine_type(session, provider="ec2")
    now = datetime.now(UTC)
    box = ComputeAllocation(
        user_id=operator.admin_id,
        org_team_id=operator.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        origin="provisioned",
        name=f"pool-{uuid4().hex[:6]}",
        tenancy=POOL_TENANCY,
        sandbox="gvisor",
        capacity=6,
        state="ready",
        provider_machine_id=f"i-{uuid4().hex[:8]}",
        created_at=now,
        ready_at=now,
        state_changed_at=now,
        last_heartbeat_at=now,
        capabilities_json=[BoxCapability.WORKSPACES],
    )
    session.add(box)
    await session.commit()
    await hold_with_credential(session, box)
    return box


async def _member(session: AsyncSession, org: OrgWithAdmin) -> User:
    user, _ = await make_member(session, org_id=org.org_id, verified=True)
    return user


def _ctx(user: User, org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=user.id, org_id=org.org_id, email=user.email)


async def _plan(
    session: AsyncSession, org: OrgWithAdmin, plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "self_hosted", plan == "self_hosted")
    if plan == "enterprise":
        await make_org_enterprise(org.org_id)


@pytest.mark.parametrize(
    ("plan", "pin", "pool", "fallback", "outcome"),
    [
        pytest.param("self_serve", "running", "none", True, "pin", id="running-pin-binds"),
        pytest.param("self_serve", "stopped", "none", True, "pin", id="stopped-pin-binds-to-wake"),
        pytest.param("self_serve", "failed", "none", True, "pin", id="failed-pin-holds-the-chat"),
        pytest.param("self_serve", "waiting", "none", True, "pin", id="pin-waiting-for-hardware"),
        pytest.param("self_serve", "starting", "none", True, "pin", id="starting-pin-binds"),
        pytest.param("self_serve", "deleted", "none", True, "shared", id="deleted-pin-is-no-pin"),
        pytest.param(
            "self_serve", "not_in_audience", "none", True, "shared", id="owner-left-the-audience"
        ),
        pytest.param("self_serve", "foreign", "none", True, "shared", id="foreign-pin-is-no-pin"),
        pytest.param(
            "self_serve", "none", "running", True, "shared", id="self-serve-skips-its-pool-machine"
        ),
        pytest.param("enterprise", "none", "running", True, "org_pool", id="enterprise-org-pool"),
        pytest.param(
            "enterprise", "none", "stopped", False, "org_pool", id="enterprise-wakes-stopped-pool"
        ),
        pytest.param("enterprise", "none", "none", True, "shared", id="fallback-to-shared"),
        pytest.param("enterprise", "none", "none", False, "nothing", id="no-fallback-waits"),
        pytest.param("enterprise", "running", "running", True, "pin", id="pin-beats-org-pool"),
        pytest.param("enterprise", "deleted", "running", True, "org_pool", id="deleted-pin-pool"),
        pytest.param(
            "enterprise", "not_in_audience", "none", False, "nothing", id="unusable-pin-waits"
        ),
        pytest.param("enterprise", "failed", "none", True, "pin", id="failed-pin-never-falls-back"),
        pytest.param(
            "enterprise", "none", "assigned_only", True, "shared", id="assigned-never-takes-regular"
        ),
        pytest.param(
            "enterprise", "foreign", "running", True, "org_pool", id="foreign-pin-own-pool"
        ),
        pytest.param("self_hosted", "none", "running", True, "org_pool", id="self-hosted-pool"),
        pytest.param("self_hosted", "none", "none", False, "nothing", id="self-hosted-no-fallback"),
    ],
)
async def test_placement_decision_table(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    plan: Plan,
    pin: Pin,
    pool: OrgPool,
    fallback: bool,
    outcome: Outcome,
) -> None:
    await _plan(real_session, org_admin, plan, monkeypatch)
    owner = await _member(real_session, org_admin)
    shared = await _shared_box(real_session, platform_admin)
    if not fallback:
        await set_fallback(real_session, org_id=org_admin.org_id, fallback=False)
    pool_alloc: ComputeAllocation | None = None
    if pool != "none":
        _, pool_alloc = await make_org_machine(
            real_session,
            org_id=org_admin.org_id,
            operator_id=org_admin.admin_id,
            look="stopped" if pool == "stopped" else "running",
            use_mode="assigned" if pool == "assigned_only" else "pool",
        )
    pin_id: UUID | None = None
    pin_alloc: ComputeAllocation | None = None
    if pin == "foreign":
        other_org, other_admin = await team_service.create_org_with_admin(
            real_session,
            org_name=f"Other {uuid4().hex[:6]}",
            admin_email=f"other-{uuid4().hex[:8]}@example.com",
            admin_first_name="Other",
            admin_last_name="Admin",
            admin_password="other-pass-12345",
        )
        await real_session.commit()
        foreign, pin_alloc = await make_org_machine(
            real_session, org_id=other_org.id, operator_id=other_admin.id, look="running"
        )
        pin_id = foreign.id
    elif pin != "none":
        look = {"not_in_audience": "running"}.get(pin, pin)
        audience = [("user", org_admin.admin_id)] if pin == "not_in_audience" else [("org",)]
        machine, pin_alloc = await make_org_machine(
            real_session,
            org_id=org_admin.org_id,
            operator_id=org_admin.admin_id,
            look=look,  # type: ignore[arg-type]
            audience=audience,  # type: ignore[arg-type]
        )
        pin_id = machine.id

    binding = await placement.resolve_machine_for(
        real_session,
        ctx=_ctx(owner, org_admin),
        org_team_id=org_admin.org_id,
        purpose="chat",
        owner_user_id=owner.id,
        workspace_pin=pin_id,
    )

    expected: UUID | None = {
        "pin": pin_alloc.id if pin_alloc is not None and pin != "foreign" else None,
        "org_pool": pool_alloc.id if pool_alloc is not None else None,
        "shared": shared.id,
        "nothing": None,
    }[outcome]
    assert (binding.machine_id if binding is not None else None) == expected
    if pin == "foreign" and pin_alloc is not None:
        assert binding is None or binding.machine_id != pin_alloc.id
    if outcome == "pin":
        assert binding is not None and binding.pinned
        assert binding.wake is (pin == "stopped")


async def test_the_least_loaded_running_pool_machine_takes_the_chat_and_a_held_chat_stays(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _plan(real_session, org_admin, "enterprise", monkeypatch)
    owner = await _member(real_session, org_admin)
    _, busy = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        use_mode="pool",
        chats_served=4,
    )
    _, quiet = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        use_mode="pool",
        chats_served=1,
    )
    ctx = _ctx(owner, org_admin)
    first = await placement.resolve_machine_for(
        real_session, ctx=ctx, org_team_id=org_admin.org_id, purpose="chat"
    )
    kept = await placement.resolve_machine_for(
        real_session, ctx=ctx, org_team_id=org_admin.org_id, purpose="chat", prefer=busy.id
    )
    assert first is not None and first.machine_id == quiet.id
    assert kept is not None and kept.machine_id == busy.id


async def test_a_starting_pool_machine_is_preferred_to_waking_a_stopped_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _plan(real_session, org_admin, "enterprise", monkeypatch)
    _, stopped = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        use_mode="pool",
        look="stopped",
    )
    starting_machine, starting = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        use_mode="pool",
        look="running",
    )
    # Registered and never beaten: it is coming up.
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.id == starting.id)
        .values(last_heartbeat_at=None)
    )
    await real_session.commit()
    chosen = await placement.place_for_org(real_session, org_team_id=org_admin.org_id)
    assert chosen is not None and chosen.id == starting.id != stopped.id
    assert starting_machine.use_mode == "pool"


async def test_a_team_audience_admits_members_of_its_sub_teams(
    real_session: AsyncSession, org_admin: OrgWithAdmin, saas: None
) -> None:
    team = await make_team(real_session, org_id=org_admin.org_id)
    child = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Child", parent_team_id=team.id
    )
    await real_session.commit()
    inside, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    outside, _ = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    from backend.services.org import memberships as membership_service

    await membership_service.add_member(real_session, team_id=child.id, user_id=inside.id)
    await real_session.commit()
    machine, alloc = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=[("team", team.id)],
    )
    for user, admitted in ((inside, True), (outside, False)):
        binding = await placement.resolve_machine_for(
            real_session,
            ctx=_ctx(user, org_admin),
            org_team_id=org_admin.org_id,
            purpose="chat",
            owner_user_id=user.id,
            workspace_pin=machine.id,
        )
        assert (binding is not None and binding.machine_id == alloc.id) is admitted


async def test_a_new_chat_in_a_pinned_workspace_runs_on_the_pin(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    saas: None,
) -> None:
    """The pin is read off the workspace's own spec, Main included."""
    owner = await _member(real_session, org_admin)
    await _shared_box(real_session, platform_admin)
    machine, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    main, _ = await workspace_service.ensure_main(
        real_session, owner=owner, org_id=org_admin.org_id
    )
    await real_session.commit()
    ctx = _ctx(owner, org_admin)
    unpinned = await placement.place_new_chat(
        real_session, ctx=ctx, user=owner, mode=None, workspace=main
    )
    await pin_workspace(real_session, main.id, machine.id)
    pinned = await placement.place_new_chat(
        real_session, ctx=ctx, user=owner, mode=None, workspace=main
    )
    assert unpinned is not None and unpinned.machine_id != alloc.id
    assert pinned is not None and pinned.machine_id == alloc.id


async def _chat(
    session: AsyncSession,
    *,
    org_id: UUID,
    owner_id: UUID,
    workspace: WorkspaceObject | None,
    machine_id: UUID | None = None,
) -> WorkspaceObject:
    chat = WorkspaceObject(
        org_team_id=org_id,
        logical_id=f"chat-{uuid4().hex[:10]}",
        namespace="workspace",
        type="chat",
        title="chat",
        version=1,
        status="ready",
        spec={
            "machine_id": str(machine_id) if machine_id else None,
            "machine_status": "ready" if machine_id else "none",
            "workspace_id": str(workspace.id) if workspace is not None else None,
            "last_seq": 0,
        },
        owner_user_id=owner_id,
        visibility_scope="private",
    )
    session.add(chat)
    await session.commit()
    return chat


async def _bound_to(session: AsyncSession, chat: WorkspaceObject) -> str | None:
    spec = (
        await session.execute(
            select(WorkspaceObject.spec)
            .where(WorkspaceObject.id == chat.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return (spec or {}).get("machine_id")


async def _project(session: AsyncSession, owner: User, org: OrgWithAdmin) -> WorkspaceObject:
    workspace, _ = await workspace_service.create_project(
        session, owner=owner, org_id=org.org_id, title="Project", client_id=None
    )
    await session.commit()
    return workspace


async def test_an_assigned_machine_coming_up_takes_only_the_chats_pinned_to_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, saas: None
) -> None:
    owner = await _member(real_session, org_admin)
    machine, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    pinned_ws = await _project(real_session, owner, org_admin)
    await pin_workspace(real_session, pinned_ws.id, machine.id)
    plain_ws = await _project(real_session, owner, org_admin)
    pinned = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=pinned_ws
    )
    unpinned = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=plain_ws
    )
    moved = await placement.bind_stranded_chats_platform(real_session, alloc=alloc, actor=None)
    await real_session.commit()
    assert {chat.id for chat in moved} == {pinned.id}
    assert await _bound_to(real_session, pinned) == str(alloc.id)
    assert await _bound_to(real_session, unpinned) is None


async def test_the_shared_pool_never_takes_a_pinned_chat_while_its_machine_is_down(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    saas: None,
) -> None:
    owner = await _member(real_session, org_admin)
    machine, stopped = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )
    pinned_ws = await _project(real_session, owner, org_admin)
    await pin_workspace(real_session, pinned_ws.id, machine.id)
    plain_ws = await _project(real_session, owner, org_admin)
    pinned = await _chat(
        real_session,
        org_id=org_admin.org_id,
        owner_id=owner.id,
        workspace=pinned_ws,
        machine_id=stopped.id,
    )
    unpinned = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=plain_ws
    )
    shared = await _shared_box(real_session, platform_admin)
    moved = await placement.bind_stranded_chats_platform(real_session, alloc=shared, actor=None)
    await real_session.commit()
    assert pinned.id not in {chat.id for chat in moved}
    assert await _bound_to(real_session, pinned) == str(stopped.id)
    assert await _bound_to(real_session, unpinned) == str(shared.id)


async def test_an_org_pool_machine_coming_up_takes_the_orgs_unpinned_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _plan(real_session, org_admin, "enterprise", monkeypatch)
    owner = await _member(real_session, org_admin)
    _, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, use_mode="pool"
    )
    plain_ws = await _project(real_session, owner, org_admin)
    unpinned = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=plain_ws
    )
    await placement.bind_stranded_chats_platform(real_session, alloc=alloc, actor=None)
    await real_session.commit()
    assert await _bound_to(real_session, unpinned) == str(alloc.id)


async def test_an_org_pool_machine_of_a_self_serve_org_takes_no_unpinned_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, saas: None
) -> None:
    owner = await _member(real_session, org_admin)
    _, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, use_mode="pool"
    )
    plain_ws = await _project(real_session, owner, org_admin)
    unpinned = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=plain_ws
    )
    moved = await placement.bind_stranded_chats_platform(real_session, alloc=alloc, actor=None)
    assert moved == []
    assert await _bound_to(real_session, unpinned) is None


async def test_another_orgs_chat_with_a_forged_pin_is_never_bound_to_this_orgs_machine(
    real_session: AsyncSession, org_admin: OrgWithAdmin, saas: None
) -> None:
    machine, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"Other {uuid4().hex[:6]}",
        admin_email=f"other-{uuid4().hex[:8]}@example.com",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    stranger_ws, _ = await workspace_service.create_project(
        real_session, owner=other_admin, org_id=other_org.id, title="Theirs", client_id=None
    )
    await real_session.commit()
    await pin_workspace(real_session, stranger_ws.id, machine.id)
    stranger = await _chat(
        real_session, org_id=other_org.id, owner_id=other_admin.id, workspace=stranger_ws
    )
    moved = await placement.bind_stranded_chats_platform(real_session, alloc=alloc, actor=None)
    ctx = ActingContext.for_user(
        user_id=other_admin.id, org_id=other_org.id, email=other_admin.email
    )
    placed = await placement.resolve_machine_for(
        real_session,
        ctx=ctx,
        org_team_id=other_org.id,
        purpose="chat",
        owner_user_id=other_admin.id,
        workspace_pin=machine.id,
    )
    assert stranger.id not in {chat.id for chat in moved}
    assert await _bound_to(real_session, stranger) is None
    assert placed is None or placed.machine_id != alloc.id


async def test_a_message_moves_a_stranded_chat_onto_its_workspaces_pin(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    saas: None,
) -> None:
    owner = await _member(real_session, org_admin)
    machine, alloc = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    await _shared_box(real_session, platform_admin)
    ws = await _project(real_session, owner, org_admin)
    await pin_workspace(real_session, ws.id, machine.id)
    dead = uuid4()
    chat = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=ws, machine_id=dead
    )
    await placement.rebind_if_stranded(
        real_session, chat=chat, ctx=_ctx(owner, org_admin), org_team_id=org_admin.org_id
    )
    await real_session.commit()
    assert await _bound_to(real_session, chat) == str(alloc.id)


async def test_a_pinned_chat_on_a_failed_machine_is_not_moved_to_the_pool(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    saas: None,
) -> None:
    owner = await _member(real_session, org_admin)
    machine, failed = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="failed"
    )
    await _shared_box(real_session, platform_admin)
    ws = await _project(real_session, owner, org_admin)
    await pin_workspace(real_session, ws.id, machine.id)
    chat = await _chat(
        real_session,
        org_id=org_admin.org_id,
        owner_id=owner.id,
        workspace=ws,
        machine_id=failed.id,
    )
    await placement.rebind_if_stranded(
        real_session, chat=chat, ctx=_ctx(owner, org_admin), org_team_id=org_admin.org_id
    )
    await real_session.commit()
    assert await _bound_to(real_session, chat) == str(failed.id)


@pytest.mark.parametrize(
    ("state", "action"),
    [
        pytest.param("running", "bind", id="running"),
        pytest.param("unreachable", "bind", id="unreachable"),
        pytest.param("starting", "bind", id="starting"),
        pytest.param("stopping", "wake", id="stopping"),
        pytest.param("stopped", "wake", id="stopped"),
        pytest.param("waiting_for_hardware", "hold", id="waiting"),
        pytest.param("failed", "hold", id="failed"),
        pytest.param("deleted", None, id="deleted"),
    ],
)
def test_pin_action_covers_every_org_machine_state(state: str, action: str | None) -> None:
    assert placement.pin_action(state) == action  # type: ignore[arg-type]


def test_choose_org_pool_prefers_running_then_starting_then_stopped() -> None:
    now = datetime.now(UTC)

    def alloc(state: str, *, beat: bool, served: int = 0) -> ComputeAllocation:
        return ComputeAllocation(
            id=uuid4(),
            state=state,
            last_heartbeat_at=now if beat else None,
            chats_served=served,
            capacity=6,
        )

    stopped = alloc("asleep", beat=False)
    starting = alloc("ready", beat=False)
    busy = alloc("ready", beat=True, served=3)
    idle = alloc("ready", beat=True, served=0)
    assert placement.choose_org_pool([stopped, starting, busy, idle], now=now) is idle
    assert (
        placement.choose_org_pool([stopped, starting, busy, idle], prefer=busy.id, now=now) is busy
    )
    assert placement.choose_org_pool([stopped, starting], now=now) is starting
    assert placement.choose_org_pool([stopped], now=now) is stopped
    assert placement.choose_org_pool([], now=now) is None


@pytest.mark.parametrize(
    ("stop_reason", "woken"),
    [
        pytest.param("idle", True, id="stopped-idle-is-woken"),
        pytest.param("", True, id="slept-with-no-reason-is-woken"),
        pytest.param("user", False, id="stopped-by-a-manager-stays-off"),
        pytest.param("credits", False, id="out-of-credit-stays-off"),
        pytest.param("cap", False, id="at-its-cap-stays-off"),
    ],
)
async def test_a_stopped_pool_machine_is_woken_only_when_it_stopped_on_its_own(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str,
    woken: bool,
) -> None:
    await _plan(real_session, org_admin, "enterprise", monkeypatch)
    owner = await _member(real_session, org_admin)
    shared = await _shared_box(real_session, platform_admin)
    machine, stopped = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        use_mode="pool",
        look="stopped",
    )
    machine.stop_reason = stop_reason
    await real_session.commit()
    binding = await placement.resolve_machine_for(
        real_session, ctx=_ctx(owner, org_admin), org_team_id=org_admin.org_id, purpose="chat"
    )
    assert binding is not None
    assert binding.machine_id == (stopped.id if woken else shared.id)


class _StartingProvider:
    """Starts whatever it is asked to, or refuses every start."""

    def __init__(self, *, refuse: bool) -> None:
        self.refuse = refuse
        self.started: list[str] = []

    async def start(self, machine_id: str) -> None:
        if self.refuse:
            from alkera_core.compute.provider import ComputeProviderError

            raise ComputeProviderError("no free GPU on the host", status_code=500)
        self.started.append(machine_id)


@pytest.mark.parametrize(
    "refuse",
    [
        pytest.param(False, id="the-provider-starts-it"),
        pytest.param(True, id="the-provider-refuses-and-the-intent-still-holds"),
    ],
)
async def test_a_message_to_a_stopped_machine_turns_the_orgs_intent_on(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    refuse: bool,
) -> None:
    """A wake by message records that the org wants the machine on, so the
    reconcile converges it up rather than stopping it again."""
    from backend.services.compute import org_machines as org_machine_service
    from backend.services.compute import provisioning

    fake = _StartingProvider(refuse=refuse)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda _kind, _config: fake)
    owner = await _member(real_session, org_admin)
    machine, stopped = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )
    assert machine.desired_power == "off"
    await org_machine_service.wake_for_chat(real_session, stopped.id, ctx=_ctx(owner, org_admin))
    await real_session.commit()
    await real_session.refresh(machine)
    await real_session.refresh(stopped)
    assert (machine.desired_power, machine.stop_reason) == ("on", "")
    if refuse:
        assert stopped.state == "asleep" and stopped.wake_requested_at is not None
    else:
        assert fake.started == [stopped.provider_machine_id]


# --------------------------------------------------------------------------- #
# lock order: a machine's rows before its chats, the org machine before its
# allocation, whoever is waking it
# --------------------------------------------------------------------------- #


async def test_lock_order_a_chat_wake_takes_the_org_machine_before_its_allocation(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop holds the org machine and goes on to its allocation; a message
    waking the same machine at that moment waits for the machine, holding
    nothing, and both finish. The wake used to take the allocation first and
    then wait on the machine, and the stop's step to the allocation closed the
    cycle: one of the two was aborted as a deadlock."""
    from backend.services.compute import org_machines as org_machine_service
    from backend.services.compute import provisioning

    fake = _StartingProvider(refuse=False)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda _kind, _config: fake)
    owner = await _member(real_session, org_admin)
    machine, stopped = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )

    async def wake() -> None:
        async with AsyncSessionLocal() as db:
            await org_machine_service.wake_for_chat(db, stopped.id, ctx=_ctx(owner, org_admin))
            await db.commit()

    async with AsyncSessionLocal() as stop:
        await stop.execute(select(OrgMachine).where(OrgMachine.id == machine.id).with_for_update())
        waking = asyncio.create_task(wake())
        await until_waiting_on(await backend_pid(stop))
        await stop.execute(text("SET LOCAL lock_timeout = '5s'"))
        await stop.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == stopped.id).with_for_update()
        )
        await stop.commit()
    await asyncio.wait_for(waking, timeout=20)
    await real_session.refresh(machine)
    assert machine.desired_power == "on"


async def test_lock_order_a_message_wakes_the_machine_before_it_takes_the_chat(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    saas: None,
) -> None:
    """A sleep holds a machine's allocation and goes on to the chats bound to
    it. A message moving a stranded chat onto that stopped machine used to
    lock the chat (the rebind) and then wait for the allocation (the wake),
    and the sleep's step to the chat closed the cycle. The message now wakes
    the machine first, holding no chat, and both finish."""
    from backend.services.compute import provisioning

    fake = _StartingProvider(refuse=False)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda _kind, _config: fake)
    owner = await _member(real_session, org_admin)
    machine, stopped = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )
    await _shared_box(real_session, platform_admin)
    ws = await _project(real_session, owner, org_admin)
    await pin_workspace(real_session, ws.id, machine.id)
    chat = await _chat(
        real_session, org_id=org_admin.org_id, owner_id=owner.id, workspace=ws, machine_id=uuid4()
    )

    async def send() -> None:
        async with AsyncSessionLocal() as db:
            row = await db.get(WorkspaceObject, chat.id)
            assert row is not None
            await placement.rebind_if_stranded(
                db, chat=row, ctx=_ctx(owner, org_admin), org_team_id=org_admin.org_id
            )
            await db.commit()

    async with AsyncSessionLocal() as sleep:
        await sleep.execute(
            select(ComputeAllocation).where(ComputeAllocation.id == stopped.id).with_for_update()
        )
        sending = asyncio.create_task(send())
        await until_waiting_on(await backend_pid(sleep))
        await sleep.execute(text("SET LOCAL lock_timeout = '5s'"))
        await sleep.execute(
            select(WorkspaceObject).where(WorkspaceObject.id == chat.id).with_for_update()
        )
        await sleep.commit()
    await asyncio.wait_for(sending, timeout=20)
    assert await _bound_to(real_session, chat) == str(stopped.id)
    assert fake.started == [stopped.provider_machine_id]
