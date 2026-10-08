"""What a chat that is AWAKE still takes from the person watching it.

A lease refuses every writer but its holder, which is right for a mount and
wrong for a chat: the person is looking at the folder the agent is working in,
and dropping a file into it has to work while the agent is still running. So a
lease may declare that it accepts inbound writes, and the drive then admits a
narrow set of them and records each one for the holder to apply.

These tests drive the fence itself against real Postgres and real lease rows a
real ``LeaseService.acquire`` wrote, because the whole claim is about what the
fence answers — a hand-built lease row would prove only that the SELECT matches
the INSERT beside it. The matrix is what admission must NOT cover: the chat
node, the working directory as a target, and the chat's own records stay
refused even while the lease says it takes inbound writes.
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
    is_final_push,
    is_hand_back,
)
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio

INSTANCE = "box-1"
MACHINE = "runner-7"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _held(org: FilesOrg, epoch: int, *, final: bool = False) -> LeaseContext:
    """The holder's own fencing context: the pair AND the principal behind it,
    which is what the route builds and what the lease row is matched on."""
    return held_by(_ctx(org), epoch, INSTANCE, final=final)


class Chat:
    """A leased chat folder, as plain ids.

    Ids rather than rows: a refused call rolls the session back, which expires
    every ORM instance the factory handed over, and a later attribute read off
    one of those is an error rather than a value.
    """

    def __init__(self, nodes: dict[str, Any], epoch: int) -> None:
        self.ids = {name: node.id for name, node in nodes.items()}
        self.epoch = epoch

    def id(self, name: str) -> uuid.UUID:
        return self.ids[name]


async def _chat(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    inbound: bool,
) -> Chat:
    """A leased chat folder shaped the way the product mints one.

    ``chat/`` holds ``scratch/`` (the working directory the agent writes in),
    ``manifest.json`` (a record) and a ``.runtime/`` directory. What makes the
    folder a chat is the object behind it, never its name, so the two columns
    the chat lookup reads are the two this sets.
    """
    drive = await files_factory.drive()
    nodes = await files_factory.tree(
        "chat/ chat/scratch/ chat/scratch/report.html chat/scratch/assets/ "
        "chat/scratch/assets/logo.png chat/manifest.json chat/.runtime/ "
        "chat/.runtime/state.json elsewhere.txt",
        drive=drive,
    )
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET subtype = 'chat', target_object_id = :object "
                "WHERE id = :node"
            ),
            {"object": uuid.uuid4(), "node": nodes["chat"].id},
        )
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(nodes["chat"].id), instance_id=INSTANCE, machine_id=MACHINE, purpose="chat"
        )
    async with repo.transaction():
        # The acquire route learns this from the body; the fence only reads the
        # column, so the column is what the test sets.
        await repo.session.execute(
            text("UPDATE file_leases SET accepts_inbound = :inbound WHERE node_id = :node"),
            {"inbound": inbound, "node": nodes["chat"].id},
        )
    return Chat(nodes, grant.epoch)


async def _fence(
    repo: FilesRepo,
    chat: Chat,
    name: str,
    *,
    lease: LeaseContext | None = None,
    into: bool = False,
) -> Any:
    async with repo.transaction():
        node = await repo.node(NodeId(chat.id(name)))
        assert node is not None
        return await fenced_write_for(repo, node, lease, into=into)


# -- what an awake chat admits ------------------------------------------------


@pytest.mark.parametrize(
    ("target", "into"),
    [
        pytest.param("chat/scratch/report.html", False, id="a-file-in-the-working-directory"),
        pytest.param("chat/scratch/assets", False, id="a-folder-under-it"),
        pytest.param("chat/scratch/assets/logo.png", False, id="a-file-deeper-down"),
        pytest.param("chat/scratch", True, id="a-new-child-of-the-working-directory"),
        pytest.param("chat/scratch/assets", True, id="a-new-child-deeper-down"),
    ],
)
async def test_an_awake_chat_admits_a_write_inside_its_working_directory(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
    into: bool,
) -> None:
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    admission = await _fence(repo, chat, target, into=into)
    assert isinstance(admission, InboundAdmission)
    assert admission.epoch == chat.epoch


@pytest.mark.parametrize(
    ("target", "into"),
    [
        pytest.param("chat", True, id="a-new-child-of-the-chat-folder"),
        pytest.param("chat", False, id="the-chat-folder-itself"),
        pytest.param("chat/scratch", False, id="the-working-directory-itself"),
        pytest.param("chat/manifest.json", False, id="the-transcript-manifest"),
        pytest.param("chat/.runtime", False, id="the-runtime-directory"),
        pytest.param("chat/.runtime", True, id="a-new-child-of-the-runtime-directory"),
        pytest.param("chat/.runtime/state.json", False, id="a-file-inside-the-runtime"),
    ],
)
async def test_an_awake_chat_still_refuses_everything_outside_the_working_directory(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
    into: bool,
) -> None:
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, chat, target, into=into)
    assert refused.value.code == "files.leased"


@pytest.mark.parametrize(
    ("target", "into"),
    [
        pytest.param("chat/scratch/report.html", False, id="a-file-in-the-working-directory"),
        pytest.param("chat/scratch", True, id="a-new-child-of-the-working-directory"),
        pytest.param("chat", True, id="a-new-child-of-the-chat-folder"),
    ],
)
async def test_a_lease_that_does_not_accept_inbound_refuses_the_same_writes(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
    into: bool,
) -> None:
    """The column is the whole difference: nothing about the shape of the tree
    lets a write in on its own."""
    chat = await _chat(repo, files_factory, files_org, clock, inbound=False)
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, chat, target, into=into)
    assert refused.value.code == "files.leased"


async def test_an_ordinary_leased_folder_admits_nothing(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A mounted folder that is not a chat has no working directory, so a lease
    that says it accepts inbound writes still takes none — the folder named
    ``scratch`` under it is an ordinary folder, not a sandbox."""
    drive = await files_factory.drive()
    nodes = await files_factory.tree("team/ team/scratch/ team/scratch/notes.md", drive=drive)
    async with repo.transaction():
        await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(nodes["team"].id), instance_id=INSTANCE, machine_id=MACHINE
        )
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET accepts_inbound = true WHERE node_id = :node"),
            {"node": nodes["team"].id},
        )
    target_id = nodes["team/scratch/notes.md"].id
    async with repo.transaction():
        node = await repo.node(NodeId(target_id))
        assert node is not None
        with pytest.raises(LeaseConflict) as refused:
            await fenced_write_for(repo, node, None)
    assert refused.value.code == "files.leased"


# -- what admission is NOT ----------------------------------------------------


async def test_an_admitted_write_is_not_a_hand_back_and_stays_quota_bounded(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The ceiling exemption belongs to the machine handing its folder back.

    An admitted write presented no epoch — that is why it needed admitting — so
    it is charged against the drive's ceilings like any other member's write, or
    a full drive would be fillable through any awake chat.
    """
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    admission = await _fence(repo, chat, "chat/scratch", into=True)
    assert is_hand_back(admission, None) is False
    assert is_hand_back(admission, LeaseContext()) is False


async def test_the_holder_presenting_its_fence_gets_the_lease_not_an_admission(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    held = _held(files_org, chat.epoch)
    covering = await _fence(repo, chat, "chat/scratch/report.html", lease=held)
    assert not isinstance(covering, InboundAdmission)
    assert is_hand_back(covering, held) is True


async def test_only_the_push_that_goes_with_the_release_is_exempt_from_the_ceilings(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Holding the folder is not the exemption; handing it back is.

    Every write the live plane makes is fenced by the same epoch as the push
    that ends the lease, so "the holder wrote it" cannot be what lets bytes past
    the drive's ceilings — an awake chat would then write past them all day. The
    holder says which push is the last one, and only that one is exempt; the
    claim on its own, from a caller holding nothing, buys nothing.
    """
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    held = _held(files_org, chat.epoch)
    handing_back = _held(files_org, chat.epoch, final=True)
    covering = await _fence(repo, chat, "chat/scratch/report.html", lease=held)

    assert is_final_push(covering, held) is False
    assert is_final_push(covering, handing_back) is True
    # A write the chat merely admitted is not the holder's, whatever it claims.
    admission = await _fence(repo, chat, "chat/scratch", into=True)
    assert is_final_push(admission, LeaseContext(final=True)) is False
    # ...and neither is a write with no lease over it at all.
    assert is_final_push(None, handing_back) is False


async def test_a_superseded_machine_is_fenced_rather_than_admitted(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A stale epoch is not "no epoch": a machine that was superseded must learn
    it was, not be quietly downgraded to a member dropping a file in."""
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    stale = _held(files_org, chat.epoch - 1)
    with pytest.raises(LeaseConflict) as refused:
        await _fence(repo, chat, "chat/scratch/report.html", lease=stale)
    assert refused.value.code == "files.lease_fenced"


async def test_admission_never_reaches_outside_the_leased_subtree(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """A node outside the chat is not leased at all, so the fence returns
    nothing rather than an admission that would record it on the chat's plane."""
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    assert await _fence(repo, chat, "elsewhere.txt") is None


# -- a trash under the fence --------------------------------------------------


async def _trash(repo: FilesRepo, files_org: FilesOrg, clock: FakeClock, node_id: uuid.UUID) -> Any:
    from alkera_core.files.trash import Trash

    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        assert node is not None
        etag = node.etag
    async with repo.transaction():
        return await Trash(repo, _ctx(files_org), clock).trash(NodeId(node_id), if_match=etag)


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("chat", id="the-chat-node"),
        pytest.param("chat/scratch", id="the-working-directory"),
        pytest.param("chat/manifest.json", id="a-record"),
    ],
)
async def test_an_awake_chat_refuses_a_trash_of_the_mount_it_stands_on(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    target: str,
) -> None:
    """Accepting inbound writes is not consent to be deleted out from under.

    A drop INTO the working directory is the feature; removing the working
    directory, the chat itself or a record is the machine losing the ground it
    is standing on, so those keep the 409 an ordinary mount gives.
    """
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    with pytest.raises(LeaseConflict) as refused:
        await _trash(repo, files_org, clock, chat.id(target))
    assert refused.value.code == "files.leased"


async def test_an_awake_chat_admits_a_trash_inside_its_working_directory(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """And the holder is told, so its next drain unlinks the local copy."""
    chat = await _chat(repo, files_factory, files_org, clock, inbound=True)
    await _trash(repo, files_org, clock, chat.id("chat/scratch/report.html"))

    async with repo.transaction():
        entry = (
            await repo.session.execute(
                text(
                    "SELECT state FROM file_lease_live_entries "
                    "WHERE lease_node_id = :lease AND node_id = :node"
                ),
                {"lease": chat.id("chat"), "node": chat.id("chat/scratch/report.html")},
            )
        ).first()
    assert entry is not None
    assert entry.state == "inbound_delete"


async def test_a_sleeping_chat_refuses_a_trash_inside_its_working_directory(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    chat = await _chat(repo, files_factory, files_org, clock, inbound=False)
    with pytest.raises(LeaseConflict) as refused:
        await _trash(repo, files_org, clock, chat.id("chat/scratch/report.html"))
    assert refused.value.code == "files.leased"
