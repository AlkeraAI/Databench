"""A shared workspace runs on the box that holds its lease.

Every chat of a workspace that owns a folder runs in one sandbox under one
``workspace`` lease on that folder, and while one box holds that lease every
other box is refused the folder. So the lease holder, while it answers, is
the one box that can serve any chat of the workspace: a new member is placed
there, a member bound to another box that still answers is moved there on its
next message, and a box coming up leaves the workspace's stranded members to
it. A holder that has stopped answering decides nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.provider import EC2
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.models import FileLease, User, WorkspaceObject
from alkera_core.models.compute import POOL_TENANCY, ComputeAllocation
from backend.services.compute import placement
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests.conftest import OrgWithAdmin

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

WORKSPACES = "workspaces"
Holder = Literal["answering", "silent", "released"]


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The pool is global: a pool box another test left live would serve this
    test's org, so every one is released first."""
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy == POOL_TENANCY)
        .values(state="released")
    )
    await real_session.commit()


async def _box(
    session: AsyncSession, operator: OrgWithAdmin, name: str, *, chats_served: int = 0
) -> ComputeAllocation:
    machine_type = await make_machine_type(session, provider=EC2)
    alloc = ComputeAllocation(
        user_id=operator.admin_id,
        org_team_id=operator.org_id,
        machine_type_id=machine_type.id,
        lifecycle="workspace",
        name=name,
        tenancy=POOL_TENANCY,
        sandbox="gvisor",
        capacity=10,
        chats_served=chats_served,
        state="ready",
        provider_machine_id=f"pod-{name}-{uuid.uuid4().hex[:6]}",
        last_heartbeat_at=datetime.now(UTC),
        capabilities_json=[WORKSPACES],
    )
    session.add(alloc)
    await session.commit()
    await hold_with_credential(session, alloc)
    return alloc


async def _workspace(session: AsyncSession, fx: FilesFixtures) -> tuple[uuid.UUID, uuid.UUID]:
    """A native workspace and its folder, laid out as the product files it."""
    row = WorkspaceObject(
        org_team_id=fx.org_team_id,
        logical_id=uuid.uuid4().hex,
        type="workspace",
        title="Pricing",
        owner_user_id=fx.actor_id,
        visibility_scope="private",
        spec={"layout": "native"},
    )
    session.add(row)
    await session.commit()
    folder = await fx.node(
        b"Pricing.alkeraworkspace",
        kind="folder",
        subtype=WORKSPACE_TYPE,
        target_object_id=row.id,
        parent=await fx.home(),
    )
    return uuid.UUID(str(row.id)), folder.id


async def _member(
    session: AsyncSession,
    fx: FilesFixtures,
    workspace: uuid.UUID,
    *,
    machine: ComputeAllocation | None,
    age: timedelta = timedelta(0),
) -> WorkspaceObject:
    spec = {"workspace_id": str(workspace)}
    if machine is not None:
        spec["machine_id"] = str(machine.id)
    row = WorkspaceObject(
        org_team_id=fx.org_team_id,
        logical_id=uuid.uuid4().hex,
        type="chat",
        title="Member",
        owner_user_id=fx.actor_id,
        visibility_scope="private",
        spec=spec,
    )
    session.add(row)
    await session.commit()
    # Stamped into the future so this test's chats are the most recently
    # active in a database that holds every other test's leftovers.
    await session.execute(
        update(WorkspaceObject)
        .where(WorkspaceObject.id == row.id)
        .values(updated_at=datetime.now(UTC) + timedelta(days=1) - age)
    )
    await session.commit()
    return row


async def _lease(
    session: AsyncSession,
    fx: FilesFixtures,
    folder: uuid.UUID,
    holder: ComputeAllocation,
    *,
    state: Holder = "answering",
) -> None:
    """``holder`` takes the workspace lease on ``folder``. ``silent`` is a
    holder that stopped heartbeating with its lease still live; ``released``
    a lease the holder gave back."""
    now = datetime.now(UTC)
    session.add(
        FileLease(
            node_id=folder,
            org_team_id=fx.org_team_id,
            epoch=1,
            holder_principal_kind="user",
            holder_kind="machine",
            holder_principal_id=holder.id,
            holder_instance_id=f"{holder.id}:ws",
            machine_id=str(holder.id),
            purpose="workspace",
            expires_at=now + timedelta(minutes=10),
            released_at=now if state == "released" else None,
        )
    )
    if state == "silent":
        holder.last_heartbeat_at = now - timedelta(hours=1)
    await session.commit()


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _bound_to(session: AsyncSession, chat: WorkspaceObject) -> str | None:
    await session.refresh(chat)
    return (chat.spec or {}).get("machine_id")


async def test_a_new_member_goes_to_the_lease_holder_not_the_newest_sibling(
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    platform_admin: OrgWithAdmin,
) -> None:
    """Siblings on two boxes, the lease on the box of the OLDER one: that box
    is the only one the folder is not refused to, so the new chat binds
    there, not where the most recently active sibling happens to be."""
    holder = await _box(real_session, platform_admin, "holder", chats_served=5)
    other = await _box(real_session, platform_admin, "other")
    workspace, folder = await _workspace(real_session, fx)
    await _member(real_session, fx, workspace, machine=holder, age=timedelta(hours=1))
    await _member(real_session, fx, workspace, machine=other)
    await _lease(real_session, fx, folder, holder)

    assert await placement.workspace_placement(real_session, workspace) == (holder.id, WORKSPACES)
    binding = await placement.place_new_chat(
        real_session,
        ctx=_ctx(files_org.org),
        user=await real_session.get(User, files_org.org.admin_id),
        mode=None,
        workspace=await real_session.get(WorkspaceObject, workspace),
    )
    assert binding is not None and binding.machine_id == holder.id


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("silent", id="holder-stopped-beating"),
        pytest.param("released", id="lease-gone"),
    ],
)
async def test_a_lease_that_no_answering_box_holds_decides_nothing(
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    platform_admin: OrgWithAdmin,
    state: Holder,
) -> None:
    """A holder that stopped answering, or a lease given back, says nothing
    about where the workspace runs next: the sibling's box is preferred as
    before, and a member on an answering box stays where it is."""
    holder = await _box(real_session, platform_admin, "holder")
    other = await _box(real_session, platform_admin, "other")
    workspace, folder = await _workspace(real_session, fx)
    member = await _member(real_session, fx, workspace, machine=other)
    await _lease(real_session, fx, folder, holder, state=state)

    assert await placement.workspace_placement(real_session, workspace) == (other.id, WORKSPACES)
    await placement.rebind_if_stranded(
        real_session, chat=member, ctx=_ctx(files_org.org), org_team_id=files_org.org.org_id
    )
    await real_session.commit()
    assert await _bound_to(real_session, member) == str(other.id)


async def test_a_member_on_an_answering_box_moves_to_the_box_holding_its_workspace(
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    platform_admin: OrgWithAdmin,
) -> None:
    """The box serving a member is up, but the workspace was taken by another
    box while it was unreachable: the member's next message moves it to the
    box that holds the workspace, the one box that can serve it now."""
    first = await _box(real_session, platform_admin, "first")
    second = await _box(real_session, platform_admin, "second", chats_served=5)
    workspace, folder = await _workspace(real_session, fx)
    member = await _member(real_session, fx, workspace, machine=first)
    await _lease(real_session, fx, folder, second)

    await placement.rebind_if_stranded(
        real_session, chat=member, ctx=_ctx(files_org.org), org_team_id=files_org.org.org_id
    )
    await real_session.commit()
    assert await _bound_to(real_session, member) == str(second.id)


async def test_a_box_coming_up_leaves_a_workspaces_stranded_member_to_its_lease_holder(
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    platform_admin: OrgWithAdmin,
) -> None:
    """A stranded member of a workspace another answering box holds is not
    taken by a box coming up, though first placement would pick that box
    (it is the less loaded); its next message joins the holder."""
    holder = await _box(real_session, platform_admin, "holder", chats_served=5)
    workspace, folder = await _workspace(real_session, fx)
    member = await _member(real_session, fx, workspace, machine=None)
    await _lease(real_session, fx, folder, holder)
    newcomer = await _box(real_session, platform_admin, "newcomer")

    await placement.bind_stranded_chats_platform(real_session, alloc=newcomer, actor=None)
    await real_session.commit()
    assert await _bound_to(real_session, member) is None
    await placement.rebind_if_stranded(
        real_session, chat=member, ctx=_ctx(files_org.org), org_team_id=files_org.org.org_id
    )
    await real_session.commit()
    assert await _bound_to(real_session, member) == str(holder.id)
