"""Waking a workspace whose own machine is gone.

A workspace pinned to an org machine wakes there: running, it is served
there; stopped, the wake starts it. When the machine is deleted, or the
chat's owner was taken out of its audience, nothing lands the workspace
somewhere else unasked. A person opening it is answered
``machine_unavailable`` with the choices they may pick from, the default
placement first and preselected when it serves the org; any other waker (a
message, an agent, Slack) moves it to the default placement and the workspace
records the move, which its machine row shows. When nothing serves the org by
default, nothing moves and nothing wakes.

Driven through the real wake route, placement and delete service against real
Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.box_contract import BoxCapability
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.compute import DEDICATED_TENANCY, POOL_TENANCY, ComputeAllocation
from alkera_core.models.org_machines import OrgMachine, OrgMachineAudience
from backend.services.compute import placement
from backend.services.workspaces import workspace_service
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import make_org_machine, pin_workspace, set_fallback
from tests.conftest import OrgWithAdmin, app_client, login, make_member, make_org_enterprise

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]


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


@pytest.fixture(autouse=True)
def _saas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
        capabilities_json=[BoxCapability.WORKSPACES.value],
    )
    session.add(box)
    await session.commit()
    await hold_with_credential(session, box)
    return box


class Scene:
    """A member's project workspace pinned to an org machine, with one chat in
    it bound to that machine and slept there."""

    def __init__(
        self,
        owner: User,
        password: str,
        machine: OrgMachine,
        alloc: ComputeAllocation,
        workspace: WorkspaceObject,
        chat: WorkspaceObject,
    ) -> None:
        self.owner = owner
        self.password = password
        self.machine = machine
        self.alloc = alloc
        self.workspace = workspace
        self.chat = chat

    def ctx(self, org: OrgWithAdmin) -> ActingContext:
        return ActingContext.for_user(
            user_id=self.owner.id, org_id=org.org_id, email=self.owner.email
        )


async def _scene(
    session: AsyncSession, org: OrgWithAdmin, *, look: str = "running", name: str = "GPU box"
) -> Scene:
    owner, password = await make_member(session, org_id=org.org_id, verified=True)
    assert password is not None
    machine, alloc = await make_org_machine(
        session,
        org_id=org.org_id,
        operator_id=org.admin_id,
        look=look,  # type: ignore[arg-type]
        name=name,
    )
    workspace, _ = await workspace_service.create_project(
        session, owner=owner, org_id=org.org_id, title="Project", client_id=None
    )
    await session.commit()
    await pin_workspace(session, workspace.id, machine.id)
    chat = WorkspaceObject(
        org_team_id=org.org_id,
        logical_id=f"chat-{uuid4().hex[:10]}",
        namespace="workspace",
        type="chat",
        title="chat",
        version=1,
        status="ready",
        spec={
            "machine_id": str(alloc.id),
            "machine_status": "ready",
            "workspace_id": str(workspace.id),
            "mirror_state": "asleep",
            "last_seq": 0,
        },
        owner_user_id=owner.id,
        visibility_scope="private",
    )
    session.add(chat)
    await session.commit()
    return Scene(owner, password, machine, alloc, workspace, chat)


async def _delete_machine(session: AsyncSession, scene: Scene, org: OrgWithAdmin) -> None:
    """Delete the machine the way the delete route does, and let the reconcile
    release its allocation."""
    from alkera_core.compute.org_machines import request_delete

    machine = await session.get(OrgMachine, scene.machine.id)
    assert machine is not None
    admin = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)
    await request_delete(session, machine, actor=admin.audit_dict())
    await session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.id == scene.alloc.id)
        .values(state="released")
    )
    await session.commit()


async def _spec(object_id: UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        row = await session.get(WorkspaceObject, object_id)
        assert row is not None
        return dict(row.spec)


async def _open(scene: Scene) -> dict[str, Any]:
    async with app_client() as theirs:
        await login(theirs, scene.owner.email, scene.password)
        woke = await theirs.post(f"/api/v1/chats/{scene.chat.id}/wake")
        assert woke.status_code == 200, woke.text
        return dict(woke.json())


async def _machine_row(scene: Scene) -> dict[str, Any]:
    async with app_client() as theirs:
        await login(theirs, scene.owner.email, scene.password)
        read = await theirs.get(f"/api/v1/workspaces/{scene.workspace.id}/machine")
        assert read.status_code == 200, read.text
        return dict(read.json())


async def _message(session: AsyncSession, scene: Scene, org: OrgWithAdmin) -> None:
    """The placement a message (or Slack, or an agent) runs before it sends."""
    chat = await session.get(WorkspaceObject, scene.chat.id, populate_existing=True)
    assert chat is not None
    await placement.rebind_if_stranded(
        session, chat=chat, ctx=scene.ctx(org), org_team_id=org.org_id
    )
    await session.commit()


# --------------------------------------------------------------------------- #
# the workspace's own machine still serves
# --------------------------------------------------------------------------- #


async def test_opening_wakes_on_the_same_running_machine(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin)
    answer = await _open(scene)
    assert answer == {"outcome": "waking", "machine_unavailable": None}
    spec = await _spec(scene.chat.id)
    assert spec["machine_id"] == str(scene.alloc.id), "never moved off its own machine"
    assert spec.get("wake_requested_at") is not None


class _StartingProvider:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start(self, machine_id: str) -> None:
        self.started.append(machine_id)


async def test_a_stopped_machine_is_started_and_the_chat_stays_on_it(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live shared box does not take the workspace while its own machine is
    merely stopped: the wake starts that machine instead."""
    from backend.services.compute import provisioning

    fake = _StartingProvider()
    monkeypatch.setattr(provisioning, "make_node_provider", lambda _kind, _config: fake)
    await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin, look="stopped")
    await _message(real_session, scene, org_admin)
    assert (await _spec(scene.chat.id))["machine_id"] == str(scene.alloc.id)
    await real_session.refresh(scene.machine)
    assert scene.machine.desired_power == "on"
    assert fake.started == [scene.alloc.provider_machine_id]


async def test_an_unpinned_workspace_wakes_on_the_shared_pool(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    shared = await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin)
    await pin_workspace(real_session, scene.workspace.id, None)
    await real_session.execute(
        update(WorkspaceObject)
        .where(WorkspaceObject.id == scene.chat.id)
        .values(spec=WorkspaceObject.spec.op("||")({"machine_id": str(shared.id)}))
    )
    await real_session.commit()
    answer = await _open(scene)
    assert answer == {"outcome": "waking", "machine_unavailable": None}
    assert (await _spec(scene.chat.id))["machine_id"] == str(shared.id)


# --------------------------------------------------------------------------- #
# the machine is gone
# --------------------------------------------------------------------------- #


async def test_a_person_opening_a_workspace_whose_machine_was_deleted_is_asked(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin, name="Old GPU")
    await _delete_machine(real_session, scene, org_admin)

    answer = await _open(scene)

    assert answer["outcome"] == "machine_unavailable"
    held = answer["machine_unavailable"]
    assert held["workspace_id"] == str(scene.workspace.id)
    assert held["lost"] == {
        "org_machine_id": str(scene.machine.id),
        "name": "Old GPU",
        "reason": "deleted",
        "fell_back": False,
    }
    assert held["preselect_default"] is True
    assert [(c["kind"], c["name"], c["org_machine_id"]) for c in held["choices"]] == [
        ("shared", "Standard", None)
    ]
    spec = await _spec(scene.chat.id)
    assert spec["machine_id"] == str(scene.alloc.id), "nothing moved before the person chose"
    assert spec.get("wake_intent_at") is None, "a held wake does not throttle the next one"
    row = await _machine_row(scene)
    assert row["lost"]["reason"] == "deleted" and row["lost"]["fell_back"] is False


async def test_a_message_moves_a_workspace_whose_machine_was_deleted_to_standard(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    shared = await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin)
    await _delete_machine(real_session, scene, org_admin)

    await _message(real_session, scene, org_admin)

    assert (await _spec(scene.chat.id))["machine_id"] == str(shared.id)
    workspace = await _spec(scene.workspace.id)
    assert workspace["machine_pin"] is None
    assert workspace["fell_back_at"] is not None
    row = await _machine_row(scene)
    assert row["card"]["name"] == "Standard", "the machine row shows where it runs now"
    assert row["lost"] == {
        "org_machine_id": str(scene.machine.id),
        "name": scene.machine.name,
        "reason": "deleted",
        "fell_back": True,
    }
    # Moved once: the next open wakes it where it now runs, nothing asked.
    answer = await _open(scene)
    assert answer["outcome"] != "machine_unavailable"


async def test_with_no_default_placement_the_person_picks_and_nothing_moves(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """An Enterprise org that keeps its chats off the shared pool: the prompt
    lists only the org machines the opener may use, and a message moves
    nothing."""
    await make_org_enterprise(org_admin.org_id)
    await set_fallback(real_session, org_id=org_admin.org_id, fallback=False)
    await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin)
    other, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, name="Spare box"
    )
    await _delete_machine(real_session, scene, org_admin)

    answer = await _open(scene)

    held = answer["machine_unavailable"]
    assert held["preselect_default"] is False
    assert [c["org_machine_id"] for c in held["choices"]] == [str(other.id)]

    await _message(real_session, scene, org_admin)
    assert (await _spec(scene.chat.id))["machine_id"] == str(scene.alloc.id)
    assert (await _spec(scene.workspace.id)).get("fell_back_at") is None


async def test_an_owner_taken_out_of_the_audience_is_asked_too(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin, look="stopped")
    await real_session.execute(
        delete(OrgMachineAudience).where(OrgMachineAudience.org_machine_id == scene.machine.id)
    )
    real_session.add(
        OrgMachineAudience(
            org_team_id=org_admin.org_id,
            org_machine_id=scene.machine.id,
            grantee_kind="user",
            user_id=org_admin.admin_id,
        )
    )
    await real_session.commit()

    answer = await _open(scene)

    held = answer["machine_unavailable"]
    assert held["lost"]["reason"] == "no_access"
    assert str(scene.machine.id) not in [c["org_machine_id"] for c in held["choices"]]
    assert (await _spec(scene.chat.id))["machine_id"] == str(scene.alloc.id)


async def test_a_box_coming_up_does_not_take_a_workspace_waiting_on_a_choice(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    shared = await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin)
    await _delete_machine(real_session, scene, org_admin)

    moved = await placement.bind_stranded_chats_platform(real_session, alloc=shared, actor=None)
    await real_session.commit()

    assert scene.chat.id not in {chat.id for chat in moved}
    assert (await _spec(scene.chat.id))["machine_id"] == str(scene.alloc.id)


async def test_picking_a_machine_clears_the_prompt(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _shared_box(real_session, platform_admin)
    scene = await _scene(real_session, org_admin)
    await _delete_machine(real_session, scene, org_admin)
    async with app_client() as theirs:
        await login(theirs, scene.owner.email, scene.password)
        moved = await theirs.post(
            f"/api/v1/workspaces/{scene.workspace.id}/machine", json={"to_org_machine_id": None}
        )
        assert moved.status_code == 202, moved.text
        read = await theirs.get(f"/api/v1/workspaces/{scene.workspace.id}/machine")
        assert read.json()["lost"] is None
    spec = await _spec(scene.workspace.id)
    assert spec.get("lost_machine_id") is None
    still = (
        await real_session.execute(
            select(OrgMachine.deleted_at).where(OrgMachine.id == scene.machine.id)
        )
    ).scalar_one()
    assert still is not None
