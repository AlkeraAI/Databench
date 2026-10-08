"""A workspace lease: one holder for a workspace's whole folder.

A box that runs every chat of a workspace in one sandbox holds the workspace's
``.alkeraworkspace`` folder under one ``workspace`` lease. Three claims, each
driven through the real fence against real lease rows a real
``LeaseService.acquire`` wrote:

* the lease takes what people drop into the shared ``files/`` tree (it is
  what every chat in the workspace and every person works in) and nothing
  else: the folder itself, the ``.chats/`` records and a decoy folder named
  like a chat's working directory stay refused;
* another holder can neither nest a chat lease under it nor take it over a
  chat lease somebody else holds, which is what keeps a box on the previous
  build, serving one chat of the workspace the old way, from writing beside
  the box that holds the workspace (and the reverse);
* the holder's own chat leases nest under it, and each write is fenced by the
  lease that really covers it: a chat's records by the chat's lease, the
  shared tree by the workspace's.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.leases import (
    InboundAdmission,
    LeaseConflict,
    LeaseContext,
    LeaseService,
    fenced_write_for,
)
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio

BOX = "box-workspace"
MACHINE = "runner-7"


def _ctx(org: FilesOrg, principal_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(principal_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class Workspace:
    """A workspace folder as plain ids (a refused call expires ORM rows)."""

    def __init__(self, nodes: dict[str, Any]) -> None:
        self.ids = {name: node.id for name, node in nodes.items()}

    def id(self, name: str) -> uuid.UUID:
        return self.ids[name]


async def _workspace(repo: FilesRepo, files_factory: FilesFactory) -> Workspace:
    """A workspace folder shaped the way the product mints one: ``files/``
    (the shared tree), ``.chats/`` holding one chat's folder with its own
    working directory and records, and a decoy ``scratch/`` at the top that a
    member could have made while nobody held the folder. What makes a folder a
    workspace or a chat is the object behind it, so those columns are set."""
    drive = await files_factory.drive()
    nodes = await files_factory.tree(
        "ws/ ws/files/ ws/files/notes.md ws/files/data/ ws/files/data/raw.csv "
        "ws/.chats/ ws/.chats/c1/ ws/.chats/c1/scratch/ ws/.chats/c1/manifest.json "
        "ws/scratch/",
        drive=drive,
    )
    async with repo.transaction():
        for name, subtype in (("ws", "workspace"), ("ws/.chats/c1", "chat")):
            await repo.session.execute(
                text(
                    "UPDATE file_nodes SET subtype = :subtype, target_object_id = :object "
                    "WHERE id = :node"
                ),
                {"subtype": subtype, "object": uuid.uuid4(), "node": nodes[name].id},
            )
    return Workspace(nodes)


async def _acquire(
    repo: FilesRepo,
    org: FilesOrg,
    clock: FakeClock,
    node: uuid.UUID,
    *,
    purpose: str,
    instance: str = BOX,
    principal: uuid.UUID | None = None,
) -> Any:
    async with repo.transaction():
        return await LeaseService(repo, _ctx(org, principal), clock).acquire(
            NodeId(node), instance_id=instance, machine_id=MACHINE, purpose=purpose
        )


async def _fence(
    repo: FilesRepo,
    ws: Workspace,
    name: str,
    *,
    lease: LeaseContext | None = None,
    into: bool = False,
) -> Any:
    async with repo.transaction():
        node = await repo.node(NodeId(ws.id(name)))
        assert node is not None
        return await fenced_write_for(repo, node, lease, into=into)


@pytest.mark.parametrize(
    ("target", "into"),
    [
        pytest.param("ws/files/notes.md", False, id="a-file-in-the-shared-tree"),
        pytest.param("ws/files/data", False, id="a-folder-under-it"),
        pytest.param("ws/files/data/raw.csv", False, id="a-file-deeper-down"),
        pytest.param("ws/files", True, id="a-new-child-of-the-shared-tree"),
        pytest.param("ws/files/data", True, id="a-new-child-deeper-down"),
    ],
)
async def test_a_workspace_lease_admits_a_write_into_the_shared_tree_by_default(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
    into: bool,
) -> None:
    ws = await _workspace(repo, files_factory)
    grant = await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    assert grant.accepts_inbound is True
    admission = await _fence(repo, ws, target, into=into)
    assert isinstance(admission, InboundAdmission)
    assert admission.epoch == grant.epoch


@pytest.mark.parametrize(
    ("target", "into"),
    [
        pytest.param("ws", True, id="a-new-child-of-the-workspace-folder"),
        pytest.param("ws", False, id="the-workspace-folder-itself"),
        pytest.param("ws/files", False, id="the-shared-tree-itself"),
    ],
)
async def test_a_workspace_lease_refuses_changes_to_its_folder_and_shared_tree(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
    into: bool,
) -> None:
    ws = await _workspace(repo, files_factory)
    await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, ws, target, into=into)
    assert refused.value.code == "files.leased"


@pytest.mark.parametrize(
    ("target", "into"),
    [
        pytest.param("ws/.chats", True, id="a-new-chat-folder-in-the-records"),
        pytest.param("ws/.chats/c1/manifest.json", False, id="a-sleeping-chats-records"),
        pytest.param("ws/scratch", True, id="a-folder-beside-the-shared-tree"),
    ],
)
async def test_a_workspace_lease_does_not_cover_what_is_outside_its_shared_tree(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
    into: bool,
) -> None:
    """The box writes only the shared tree under its workspace lease; a chat's
    records move under that chat's own lease. So the workspace lease fences
    nothing else, and the server creating a new chat's record folder while
    the box holds the workspace (a second chat starting while the first is
    awake) is an ordinary write."""
    ws = await _workspace(repo, files_factory)
    await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    assert await _fence(repo, ws, target, into=into) is None


async def test_a_new_chat_starts_beside_an_awake_sibling_whose_records_stay_fenced(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    ws = await _workspace(repo, files_factory)
    await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    await _acquire(
        repo, files_org, clock, ws.id("ws/.chats/c1"), purpose="chat", instance=f"{BOX}:c1"
    )

    assert await _fence(repo, ws, "ws/.chats", into=True) is None
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, ws, "ws/.chats/c1/manifest.json")
    assert refused.value.code == "files.leased"


async def test_a_mount_over_the_same_folder_admits_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The purpose decides the default, not the folder: the same workspace held
    as a desktop mount owns its tree outright and takes no inbound write."""
    ws = await _workspace(repo, files_factory)
    grant = await _acquire(repo, files_org, clock, ws.id("ws"), purpose="mount")
    assert grant.accepts_inbound is False
    with pytest.raises(LeaseConflict):
        await _fence(repo, ws, "ws/files", into=True)


async def test_another_holder_cannot_nest_a_chat_lease_under_a_workspace_lease(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    ws = await _workspace(repo, files_factory)
    await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    with pytest.raises(LeaseConflict) as refused:
        await _acquire(
            repo,
            files_org,
            clock,
            ws.id("ws/.chats/c1"),
            purpose="chat",
            instance="old-box",
            principal=uuid.uuid4(),
        )
    assert refused.value.code == "files.leased"


async def test_a_workspace_lease_waits_out_a_chat_lease_another_holder_has(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A box on the previous build holding one chat of the workspace keeps it:
    the box that wants the whole folder is refused until that lease is gone."""
    ws = await _workspace(repo, files_factory)
    await _acquire(
        repo,
        files_org,
        clock,
        ws.id("ws/.chats/c1"),
        purpose="chat",
        instance="old-box",
        principal=uuid.uuid4(),
    )
    with pytest.raises(LeaseConflict) as refused:
        await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    assert refused.value.code == "files.leased"


async def test_the_holders_chat_leases_nest_and_each_write_is_fenced_by_its_own_lease(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    ws = await _workspace(repo, files_factory)
    workspace = await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    chat = await _acquire(
        repo, files_org, clock, ws.id("ws/.chats/c1"), purpose="chat", instance=f"{BOX}:c1"
    )
    ctx = _ctx(files_org)
    under_workspace = held_by(ctx, workspace.epoch, BOX)
    under_chat = held_by(ctx, chat.epoch, f"{BOX}:c1")
    # Any chat's agent writes into the shared tree under the workspace's lease.
    landed = await _fence(repo, ws, "ws/files/notes.md", lease=under_workspace)
    assert landed is not None and not isinstance(landed, InboundAdmission)
    assert int(landed.epoch) == workspace.epoch
    # A chat's records are the chat lease's to write.
    landed = await _fence(repo, ws, "ws/.chats/c1/manifest.json", lease=under_chat)
    assert int(landed.epoch) == chat.epoch
    # And neither lease writes the other's bytes. Both leases are live, so the
    # refusal says the path is not under the lease named, never that the lease
    # is gone: a box told "fenced" drops a folder it still holds.
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, ws, "ws/files/notes.md", lease=under_chat)
    assert refused.value.code == "files.lease_mismatch"
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, ws, "ws/.chats/c1/manifest.json", lease=under_workspace)
    assert refused.value.code == "files.lease_mismatch"


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("ws/scratch", id="beside-the-shared-tree"),
        pytest.param("ws/.chats/c1/manifest.json", id="a-chats-records"),
    ],
)
async def test_the_workspace_holder_writing_outside_its_shared_tree_is_told_not_fenced(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
) -> None:
    """The box's workspace push was refused ``lease_fenced`` for a path its
    workspace lease does not cover while that lease was alive and beating,
    read it as the lease lost, and dropped the folder: nothing it wrote after
    reached the drive. A live claim that does not cover the path is a
    mismatch; only a claim that is no longer live is fenced."""
    ws = await _workspace(repo, files_factory)
    workspace = await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")
    mine = held_by(_ctx(files_org), workspace.epoch, BOX)
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, ws, target, lease=mine, into=target == "ws/scratch")
    assert refused.value.code == "files.lease_mismatch"

    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET released_at = now() WHERE node_id = :node"),
            {"node": ws.id("ws")},
        )
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, ws, "ws/files/notes.md", lease=mine)
    assert refused.value.code == "files.lease_fenced", "a released lease is fenced"


async def test_a_box_retakes_its_own_workspace_over_the_chat_leases_it_kept(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A box that restarted still holds its members' chat leases, and its
    workspace lease has lapsed or is being taken again. Refusing the box its
    own workspace until those chat leases lapsed kept it off the shared tree
    for their whole TTL and stopped every member it was resuming. The grant
    passes the box's own chat leases, which nest under it as they always do;
    each lease still fences only its own bytes afterwards."""
    ws = await _workspace(repo, files_factory)
    chat = await _acquire(
        repo, files_org, clock, ws.id("ws/.chats/c1"), purpose="chat", instance=f"{BOX}:c1"
    )

    workspace = await _acquire(repo, files_org, clock, ws.id("ws"), purpose="workspace")

    ctx = _ctx(files_org)
    landed = await _fence(repo, ws, "ws/files/notes.md", lease=held_by(ctx, workspace.epoch, BOX))
    assert int(landed.epoch) == workspace.epoch
    landed = await _fence(
        repo, ws, "ws/.chats/c1/manifest.json", lease=held_by(ctx, chat.epoch, f"{BOX}:c1")
    )
    assert int(landed.epoch) == chat.epoch


@pytest.mark.parametrize(
    ("purpose", "other_principal"),
    [
        pytest.param("mount", False, id="a-mount-over-its-own-chat-lease"),
        pytest.param("box", False, id="a-box-lease-over-its-own-chat-lease"),
        pytest.param("workspace", True, id="another-boxs-workspace-over-it"),
    ],
)
async def test_nothing_else_is_granted_over_a_live_chat_lease(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    purpose: str,
    other_principal: bool,
) -> None:
    """Only the workspace grant of the box that holds the chat lease passes
    it: any other purpose, or the same purpose from anyone else, would put a
    second holder on the chat's records."""
    ws = await _workspace(repo, files_factory)
    await _acquire(
        repo, files_org, clock, ws.id("ws/.chats/c1"), purpose="chat", instance=f"{BOX}:c1"
    )
    with pytest.raises(LeaseConflict) as refused:
        await _acquire(
            repo,
            files_org,
            clock,
            ws.id("ws"),
            purpose=purpose,
            principal=uuid.uuid4() if other_principal else None,
        )
    assert refused.value.code == "files.leased"
