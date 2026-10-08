"""Where a chat of a shared workspace is placed.

Every chat of a workspace that owns a folder runs in one sandbox under one
lease on that folder, so its chats belong on one box, and only on a box that
said it can run a workspace: a box on the previous build would serve the chat
without its shared files, silently, so it is never bound one (the chat routes
then refuse the chat with a reason). A chat in a workspace of one is placed
exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alkera_core.authz import ActingContext
from alkera_core.compute.provider import EC2
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.compute import POOL_TENANCY, ComputeAllocation
from backend.services.compute import placement
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests.conftest import OrgWithAdmin

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

WORKSPACES = "workspaces"


@pytest.fixture(autouse=True)
async def _quiet_platform_boxes(real_session: AsyncSession) -> None:
    """The pool is global: a pool box another test left live would serve
    this test's org, so every one is released first."""
    await real_session.execute(
        update(ComputeAllocation)
        .where(ComputeAllocation.tenancy == POOL_TENANCY)
        .values(state="released")
    )
    await real_session.commit()


async def _pool_box(
    session: AsyncSession,
    *,
    operator: OrgWithAdmin,
    name: str,
    chats_served: int,
    capabilities: list[str] | None,
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
        provider_machine_id=f"pod-{name}",
        last_heartbeat_at=datetime.now(UTC),
        capabilities_json=capabilities,
    )
    session.add(alloc)
    await session.commit()
    await hold_with_credential(session, alloc)
    return alloc


async def _place(
    session: AsyncSession,
    org: OrgWithAdmin,
    *,
    prefer: UUID | None = None,
    capability: str | None = None,
) -> str | None:
    ctx = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)
    binding = await placement.resolve_machine_for(
        session,
        ctx=ctx,
        org_team_id=org.org_id,
        purpose="chat",
        prefer=prefer,
        capability=capability,
    )
    return None if binding is None else binding.name


async def _workspace(session: AsyncSession, org: OrgWithAdmin, *, layout: str) -> UUID:
    row = WorkspaceObject(
        org_team_id=org.org_id,
        logical_id=f"ws-{layout}-{datetime.now(UTC).timestamp()}",
        type="workspace",
        title="Pricing",
        owner_user_id=org.admin_id,
        visibility_scope="private",
        spec={"layout": layout},
    )
    session.add(row)
    await session.commit()
    return UUID(str(row.id))


async def _chat_in(
    session: AsyncSession, org: OrgWithAdmin, workspace: UUID, *, machine: str | None
) -> UUID:
    spec: dict[str, str] = {"workspace_id": str(workspace)}
    if machine is not None:
        spec["machine_id"] = machine
    row = WorkspaceObject(
        org_team_id=org.org_id,
        logical_id=f"chat-{datetime.now(UTC).timestamp()}",
        type="chat",
        title="Sibling",
        owner_user_id=org.admin_id,
        visibility_scope="private",
        spec=spec,
    )
    session.add(row)
    await session.commit()
    return UUID(str(row.id))


async def test_a_workspace_chat_goes_to_a_box_that_can_run_workspaces(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _pool_box(
        real_session, operator=platform_admin, name="old-roomy", chats_served=0, capabilities=None
    )
    await _pool_box(
        real_session,
        operator=platform_admin,
        name="new-busier",
        chats_served=5,
        capabilities=[WORKSPACES],
    )
    assert await _place(real_session, org_admin, capability=WORKSPACES) == "new-busier"
    # Asked nothing of the box, a chat is spread by room as it always was.
    assert await _place(real_session, org_admin) == "old-roomy"


async def test_with_no_box_that_can_a_workspace_chat_is_placed_nowhere(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    await _pool_box(
        real_session, operator=platform_admin, name="old-only", chats_served=0, capabilities=[]
    )
    assert await _place(real_session, org_admin, capability=WORKSPACES) is None
    # The same box still takes every chat that asks nothing new of it.
    assert await _place(real_session, org_admin) == "old-only"


async def test_the_box_the_workspace_is_on_outranks_a_less_loaded_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    holding = await _pool_box(
        real_session,
        operator=platform_admin,
        name="holding",
        chats_served=9,
        capabilities=[WORKSPACES],
    )
    await _pool_box(
        real_session,
        operator=platform_admin,
        name="able",
        chats_served=0,
        capabilities=[WORKSPACES],
    )
    assert (
        await _place(real_session, org_admin, prefer=holding.id, capability=WORKSPACES) == "holding"
    )


async def test_a_native_workspace_prefers_the_box_its_siblings_are_on(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    workspace = await _workspace(real_session, org_admin, layout="native")
    machine = "0b0b0b0b-0000-4000-8000-000000000001"
    sibling = await _chat_in(real_session, org_admin, workspace, machine=machine)

    prefer, capability = await placement.workspace_placement(real_session, workspace)
    assert (prefer, capability) == (UUID(machine), WORKSPACES)
    # The chat being placed is not its own sibling.
    prefer, capability = await placement.workspace_placement(
        real_session, str(workspace), exclude=sibling
    )
    assert (prefer, capability) == (None, WORKSPACES)


async def test_a_workspace_of_one_asks_nothing_new_of_placement(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    workspace = await _workspace(real_session, org_admin, layout="adopted")
    await _chat_in(
        real_session, org_admin, workspace, machine="0b0b0b0b-0000-4000-8000-000000000002"
    )
    assert await placement.workspace_placement(real_session, workspace) == (None, None)
    assert await placement.workspace_placement(real_session, None) == (None, None)
    assert await placement.workspace_placement(real_session, "not-a-uuid") == (None, None)


async def test_an_incapable_preferred_box_does_not_shadow_a_capable_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The box a workspace's siblings are on is preferred only among the boxes
    that can run a workspace: an older box there would be refused the chat
    while a box that can stands ready, so the chat goes to that box."""
    old = await _pool_box(
        real_session, operator=platform_admin, name="old-holds-it", chats_served=0, capabilities=[]
    )
    await _pool_box(
        real_session,
        operator=platform_admin,
        name="able-busier",
        chats_served=5,
        capabilities=[WORKSPACES],
    )
    assert await _place(real_session, org_admin, prefer=old.id, capability=WORKSPACES) == (
        "able-busier"
    )
    # Asked nothing of the box, the preferred box still wins.
    assert await _place(real_session, org_admin, prefer=old.id) == "old-holds-it"


async def test_a_new_workspace_chat_binds_when_the_preferred_box_cannot_run_one(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """The same rule through the create path: a sibling on an older box does
    not make a new chat of the workspace unplaceable while a box that can run
    a workspace is up."""
    old = await _pool_box(
        real_session, operator=platform_admin, name="old-sibling", chats_served=0, capabilities=[]
    )
    await _pool_box(
        real_session,
        operator=platform_admin,
        name="able",
        chats_served=3,
        capabilities=[WORKSPACES],
    )
    workspace = await _workspace(real_session, org_admin, layout="native")
    await _chat_in(real_session, org_admin, workspace, machine=str(old.id))
    owner = await real_session.get(User, org_admin.admin_id)
    binding = await placement.place_new_chat(
        real_session,
        ctx=_ctx(org_admin),
        user=owner,
        mode=None,
        workspace=await real_session.get(WorkspaceObject, workspace),
    )
    assert binding is not None and binding.name == "able"


# --------------------------------------------------------------------------- #
# a box coming up, and a workspace's stranded chats
# --------------------------------------------------------------------------- #


def _ctx(org: OrgWithAdmin) -> ActingContext:
    return ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)


async def _stranded_in(
    session: AsyncSession, org: OrgWithAdmin, workspace: UUID, count: int
) -> list[UUID]:
    """``count`` chats of the workspace bound to nothing, the most recently
    active in the database (a box coming up serves those first, and this
    database holds every other test's leftovers)."""
    chats = [await _chat_in(session, org, workspace, machine=None) for _ in range(count)]
    base = datetime.now(UTC) + timedelta(days=1)
    for index, chat in enumerate(chats):
        await session.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id == chat)
            .values(updated_at=base - timedelta(milliseconds=index))
        )
    await session.commit()
    return chats


async def _bound_to(session: AsyncSession, chat: UUID) -> str | None:
    row = await session.get(WorkspaceObject, chat, populate_existing=True)
    assert row is not None
    return (row.spec or {}).get("machine_id")


async def test_a_box_that_cannot_run_a_workspace_takes_none_of_its_stranded_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A box on the previous build (or one whose first beat has not restated
    what it can do) would run each chat alone with no shared tree: it takes
    none of a workspace's chats, and a chat of no workspace still moves."""
    workspace = await _workspace(real_session, org_admin, layout="native")
    members = await _stranded_in(real_session, org_admin, workspace, 2)
    old = await _pool_box(
        real_session, operator=platform_admin, name="old-up", chats_served=0, capabilities=None
    )
    moved = await placement.bind_stranded_chats_platform(real_session, alloc=old, actor=None)
    await real_session.commit()
    assert not {chat.id for chat in moved} & set(members)
    for member in members:
        assert await _bound_to(real_session, member) is None


@pytest.mark.parametrize(
    ("served", "moved_count"),
    [
        pytest.param(9, 0, id="room-for-one-moves-neither"),
        pytest.param(8, 2, id="room-for-two-moves-both"),
    ],
)
async def test_a_workspaces_stranded_chats_move_together_or_not_at_all(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    served: int,
    moved_count: int,
) -> None:
    """Only one box can hold a workspace's lease, so splitting its chats
    across two boxes leaves the second box's members refused. A box with room
    for one of two stranded siblings takes neither; with room for both it
    takes both."""
    workspace = await _workspace(real_session, org_admin, layout="native")
    members = await _stranded_in(real_session, org_admin, workspace, 2)
    box = await _pool_box(
        real_session,
        operator=platform_admin,
        name=f"able-{served}",
        chats_served=served,
        capabilities=[WORKSPACES],
    )
    await placement.bind_stranded_chats_platform(real_session, alloc=box, actor=None)
    await real_session.commit()
    bound = [await _bound_to(real_session, member) for member in members]
    assert bound.count(str(box.id)) == moved_count
    assert bound.count(None) == 2 - moved_count


async def test_a_workspaces_stranded_chats_stay_off_a_box_while_another_runs_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    """A sibling still served on another box is where the workspace runs: a
    box coming up does not take the stranded member away from it, though
    first placement would put the org's next chat there (it is the less
    loaded box). The member's next message joins the sibling instead."""
    home = await _pool_box(
        real_session,
        operator=platform_admin,
        name="home",
        chats_served=5,
        capabilities=[WORKSPACES],
    )
    workspace = await _workspace(real_session, org_admin, layout="native")
    await _chat_in(real_session, org_admin, workspace, machine=str(home.id))
    (member,) = await _stranded_in(real_session, org_admin, workspace, 1)
    newcomer = await _pool_box(
        real_session,
        operator=platform_admin,
        name="newcomer",
        chats_served=0,
        capabilities=[WORKSPACES],
    )
    await placement.bind_stranded_chats_platform(real_session, alloc=newcomer, actor=None)
    await real_session.commit()
    assert await _bound_to(real_session, member) is None
    row = await real_session.get(WorkspaceObject, member)
    assert row is not None
    await placement.rebind_if_stranded(
        real_session, chat=row, ctx=_ctx(org_admin), org_team_id=org_admin.org_id
    )
    await real_session.commit()
    assert await _bound_to(real_session, member) == str(home.id)
