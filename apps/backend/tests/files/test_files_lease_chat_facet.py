"""A lease a box holds for a chat names the chat on every item under it.

A person browsing a chat's folder in Files is looking at a folder the chat is
writing, and the page has to say which conversation holds it and link there for
the people who may open it. So the lease facet carries the chat's id, its
current title, and whether THIS caller may open the chat, through the real
item and listing routes, on the leased folder and on everything inside it.

The permission is the chat policy's own READ answer and nothing else. The case
that pins that is a colleague shared ONE folder inside the chat: they read the
folder, they are told the chat's title, and they are given no link, until the
chat itself is shared with them. Nothing here goes through ``enforce()`` (a
read model is not a door), so no decision row is asserted.

The same pass names the two parties to the lease. A box registers itself by
allocation id and holds its own lease, so the raw facet names the machine and
the holder with the SAME uuid — which is what a status line built from it used
to read as. ``machine_name`` is the allocation's own name and ``holder_name``
is the person's, when there is a person; an id that names no allocation of this
org is left unnamed rather than resolved against somebody else's machine.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from _files_kit import node_etag
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.principal import ActingContext
from alkera_core.files import acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_READER
from alkera_core.files.lease_snapshots import CHAT_FOLDER_SUBTYPE
from alkera_core.files.objects_bridge import CHAT_TYPE
from backend.services.org import teams as team_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.conftest import app_client, login
from tests.files._boxes import bind_chat, registered_box

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"
TITLE = "Q3 warehouse spike"
MACHINE_NAME = "alkera-demo-box"


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


@dataclass(frozen=True)
class LeasedChat:
    """A chat folder a box is running, with two folders and a file inside."""

    drive_id: uuid.UUID
    chat_id: uuid.UUID
    folder: Any
    scratch: Any
    notes: Any
    report: Any


@pytest_asyncio.fixture
async def box(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> AsyncIterator[tuple[AsyncClient, str]]:
    """The org admin's box: a proven workspace machine on a box credential."""
    token, machine_id = await registered_box(
        real_session,
        user_id=files_org.org.admin_id,
        email=files_org.org.admin_email,
        org_id=files_org.org.org_id,
    )
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    ) as client:
        yield client, machine_id


async def _take_lease(
    client: AsyncClient,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    idem: Any,
    session: AsyncSession,
    *,
    machine: str,
    purpose: str,
) -> None:
    granted = await client.post(
        f"{_item(drive_id, node_id)}/lease",
        json={"instanceId": f"{machine}:chat-a", "machineId": machine, "purpose": purpose},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )
    assert granted.status_code == 200, granted.text


@pytest_asyncio.fixture
async def leased_chat(
    fx: FilesFixtures,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    idem: Any,
) -> LeasedChat:
    """The chat as the bridge files it — a folder stamped with the object's type,
    pointing at the chat row — with the box holding it under the ``chat`` purpose."""
    box_client, machine_id = box
    drive = await fx.drive()
    folder = await fx.node(
        b"Kickoff.alkerachat", kind="folder", subtype=CHAT_TYPE, parent=await fx.home()
    )
    scratch = await fx.node(b"scratch", kind="folder", parent=folder)
    notes = await fx.node(b"notes", kind="folder", parent=scratch)
    report = await fx.node(b"report.csv", parent=notes)
    await bind_chat(fx, real_session, folder, machine=machine_id, title=TITLE)
    chat_id = (
        await real_session.execute(
            text("SELECT target_object_id FROM file_nodes WHERE id = :n"), {"n": folder.id}
        )
    ).scalar_one()
    await _take_lease(
        box_client, drive.id, folder.id, idem, real_session, machine=machine_id, purpose="chat"
    )
    return LeasedChat(
        drive_id=drive.id,
        chat_id=uuid.UUID(str(chat_id)),
        folder=folder,
        scratch=scratch,
        notes=notes,
        report=report,
    )


async def _lease_of(client: AsyncClient, drive_id: uuid.UUID, node_id: uuid.UUID) -> dict[str, Any]:
    response = await client.get(_item(drive_id, node_id))
    assert response.status_code == 200, response.text
    facet = response.json()["lease"]
    assert facet is not None, "the node is under the lease"
    return dict(facet)


async def _colleague(files_org: FilesOrgFixture) -> AsyncClient:
    """A second logged-in client for the org's member, so the admin's cookie
    jar on ``files_client`` is left alone."""
    client = app_client()
    return await login(client, files_org.member.email, files_org.member_password)


async def _grant_reader(fx: FilesFixtures, files_org: FilesOrgFixture, node: Any) -> None:
    async with fx.repo.transaction():
        await acl.grant(
            fx.repo,
            ActingContext.for_user(
                user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
            ),
            node,
            Principal(kind="user", id=files_org.member.id),
            ROLE_READER,
        )


async def test_the_subtype_the_snapshot_reads_is_the_one_the_bridge_stamps() -> None:
    """The lease query recognises a chat's folder by the subtype the bridge
    writes on it. The two spellings live in modules that cannot import each
    other, so this is what keeps them one word."""
    assert CHAT_FOLDER_SUBTYPE == CHAT_TYPE


async def test_the_chats_owner_is_named_the_chat_and_may_open_it_everywhere_under_the_lease(
    files_client: AsyncClient, leased_chat: LeasedChat
) -> None:
    """On the chat folder, on the working directory, on a folder inside it and on
    the file at the bottom: one chat, one title, and the link for its owner."""
    expected = {"chat_id": str(leased_chat.chat_id), "chat_title": TITLE, "can_open_chat": True}
    for node in (leased_chat.folder, leased_chat.scratch, leased_chat.notes, leased_chat.report):
        facet = await _lease_of(files_client, leased_chat.drive_id, node.id)
        assert {key: facet[key] for key in expected} == expected, node.name_display
        assert facet["purpose"] == "chat"


async def test_a_listing_names_the_chat_on_every_row(
    files_client: AsyncClient, leased_chat: LeasedChat
) -> None:
    """The listing route resolves the page once and every row carries it, the
    way titles and owner names ride a page."""
    response = await files_client.get(
        f"{_item(leased_chat.drive_id, leased_chat.scratch.id)}/children"
    )
    assert response.status_code == 200, response.text
    rows = response.json()["value"]
    assert [row["name"] for row in rows] == ["notes"]
    facet = rows[0]["lease"]
    assert facet["chat_id"] == str(leased_chat.chat_id)
    assert facet["chat_title"] == TITLE
    assert facet["can_open_chat"] is True


async def test_a_reader_shared_one_folder_inside_is_told_the_chat_and_given_no_link(
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    leased_chat: LeasedChat,
) -> None:
    """Reading the folder is not opening the chat. The colleague holds a rung on
    ``notes`` alone: the folder answers, the facet names the chat, and the
    permission is false, because no rung reaches the chat's own node. Sharing
    the chat is what turns it true, on the very same read."""
    await _grant_reader(fx, files_org, leased_chat.notes)
    colleague = await _colleague(files_org)
    try:
        facet = await _lease_of(colleague, leased_chat.drive_id, leased_chat.notes.id)
        assert facet["chat_id"] == str(leased_chat.chat_id)
        assert facet["chat_title"] == TITLE
        assert facet["can_open_chat"] is False
        # The chat folder itself is not theirs to read, which is the fact the
        # permission above reflects rather than the folder they were shared.
        refused = await colleague.get(_item(leased_chat.drive_id, leased_chat.folder.id))
        assert refused.status_code == 404, refused.text

        # The share dialog's grant: a rung on the chat's node.
        await _grant_reader(fx, files_org, leased_chat.folder)
        shared = await _lease_of(colleague, leased_chat.drive_id, leased_chat.notes.id)
        assert shared["can_open_chat"] is True
        assert shared["chat_title"] == TITLE
    finally:
        await colleague.aclose()


async def test_a_deleted_chat_keeps_its_id_and_loses_its_title_and_its_link(
    files_client: AsyncClient, leased_chat: LeasedChat, real_session: AsyncSession
) -> None:
    """The id is the lease's; the title and the permission are the chat row's,
    and a row that is gone yields neither, whoever is asking."""
    await real_session.execute(
        text("UPDATE workspace_objects SET deleted_at = 1 WHERE id = :c"),
        {"c": leased_chat.chat_id},
    )
    await real_session.commit()
    facet = await _lease_of(files_client, leased_chat.drive_id, leased_chat.notes.id)
    assert facet["chat_id"] == str(leased_chat.chat_id)
    assert facet["chat_title"] == ""
    assert facet["can_open_chat"] is False


async def test_a_lease_on_a_plain_folder_names_no_chat_whatever_its_purpose(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    idem: Any,
) -> None:
    """A mount on somebody's laptop, and a ``chat`` lease a box took on a folder
    that is not a chat's: the node decides, not the purpose word."""
    box_client, machine_id = box
    drive = await fx.drive()
    mounted = await fx.node(b"project", kind="folder")
    borrowed = await fx.node(b"not-a-chat", kind="folder")
    await _take_lease(
        files_client, drive.id, mounted.id, idem, real_session, machine="laptop", purpose="mount"
    )
    await _take_lease(
        box_client, drive.id, borrowed.id, idem, real_session, machine=machine_id, purpose="chat"
    )
    for node in (mounted, borrowed):
        facet = await _lease_of(files_client, drive.id, node.id)
        assert facet["chat_id"] is None, node.name_display
        assert facet["chat_title"] == ""
        assert facet["can_open_chat"] is False


async def _name_machine(session: AsyncSession, machine_id: str, name: str) -> None:
    """Give the allocation the name its owner typed. A box that was never named
    keeps the empty string the column defaults to."""
    await session.execute(
        text("UPDATE compute_allocations SET name = :name WHERE id = :machine"),
        {"name": name, "machine": machine_id},
    )
    await session.commit()


async def test_the_lease_names_the_box_rather_than_repeating_its_id(
    files_client: AsyncClient,
    leased_chat: LeasedChat,
    box: tuple[AsyncClient, str],
    real_session: AsyncSession,
) -> None:
    """On the chat folder and everything under it: the machine's own name, the
    id it is fenced on left exactly as it was, and no person invented for a
    holder that is a machine."""
    _box_client, machine_id = box
    await _name_machine(real_session, machine_id, MACHINE_NAME)
    for node in (leased_chat.folder, leased_chat.scratch, leased_chat.notes, leased_chat.report):
        facet = await _lease_of(files_client, leased_chat.drive_id, node.id)
        assert facet["machine_name"] == MACHINE_NAME, node.name_display
        # The name is added beside the id, never over it: the id is what the
        # server fences a write on and what a holder sends back.
        assert facet["machine"] == machine_id
        # The holder IS the box. There is no person behind it, and none is
        # borrowed from the user who provisioned it.
        assert facet["holder"] == machine_id
        assert facet["holder_name"] == ""


async def test_a_box_nobody_named_is_left_unnamed(
    files_client: AsyncClient, leased_chat: LeasedChat
) -> None:
    """The allocation's name is empty until somebody sets one, and the facet
    says so rather than falling back to the id it is trying to replace."""
    facet = await _lease_of(files_client, leased_chat.drive_id, leased_chat.notes.id)
    assert facet["machine_name"] == ""


async def test_a_member_shared_one_folder_inside_is_told_the_machines_name(
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    leased_chat: LeasedChat,
    box: tuple[AsyncClient, str],
    real_session: AsyncSession,
) -> None:
    """The name rides the listing route as well as the item route, and it is
    not the admin's to see alone: anyone who may read a row under the lease is
    told which machine holds it."""
    _box_client, machine_id = box
    await _name_machine(real_session, machine_id, MACHINE_NAME)
    await _grant_reader(fx, files_org, leased_chat.notes)
    colleague = await _colleague(files_org)
    try:
        facet = await _lease_of(colleague, leased_chat.drive_id, leased_chat.notes.id)
        assert facet["machine_name"] == MACHINE_NAME

        listed = await colleague.get(
            _item(leased_chat.drive_id, leased_chat.notes.id) + "/children"
        )
        assert listed.status_code == 200, listed.text
        rows = listed.json()["value"]
        assert [row["name"] for row in rows] == ["report.csv"]
        assert rows[0]["lease"]["machine_name"] == MACHINE_NAME
    finally:
        await colleague.aclose()


async def test_a_persons_mount_names_the_person_and_leaves_their_laptop_alone(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A mount is held by a user, not a machine: the holder resolves to their
    name, and the hostname they registered is not an allocation, so there is
    nothing to resolve for the machine and nothing is invented."""
    drive = await fx.drive()
    folder = await fx.node(b"project", kind="folder")
    await _take_lease(
        files_client,
        drive.id,
        folder.id,
        idem,
        real_session,
        machine="dana-macbook",
        purpose="mount",
    )
    facet = await _lease_of(files_client, drive.id, folder.id)
    assert facet["holder_name"] == "Test Admin"
    assert facet["holder"] == "Test Admin"
    assert facet["machine_name"] == ""
    assert facet["machine"] == "dana-macbook"


async def test_a_machine_id_from_another_org_resolves_to_no_name(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """The holder sends the machine id itself, so it can send anything. Another
    org's allocation — a real, named row — must resolve to nothing here, or a
    lease anyone may take would read another tenant's machine names back."""
    other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    _token, their_machine = await registered_box(
        real_session,
        user_id=other_admin.id,
        email=other_admin.email,
        org_id=other_org.id,
    )
    await _name_machine(real_session, their_machine, "their-secret-box")

    drive = await fx.drive()
    folder = await fx.node(b"project", kind="folder")
    await _take_lease(
        files_client,
        drive.id,
        folder.id,
        idem,
        real_session,
        machine=their_machine,
        purpose="mount",
    )
    facet = await _lease_of(files_client, drive.id, folder.id)
    assert facet["machine"] == their_machine
    assert facet["machine_name"] == ""


async def _hold_by(session: AsyncSession, node_id: uuid.UUID, holder_id: uuid.UUID) -> None:
    """Hand the live lease on ``node_id`` to another principal.

    Written straight onto the row because the route will not take a lease for
    somebody else: the case being reproduced is a holder the reader's org never
    admitted, which is what the org scope on the name lookup exists to refuse.
    """
    await session.execute(
        text(
            "UPDATE file_leases SET holder_principal_id = :holder "
            "WHERE node_id = :node AND released_at IS NULL"
        ),
        {"holder": holder_id, "node": node_id},
    )
    await session.commit()


async def test_a_colleague_shared_a_file_is_told_who_has_the_folder_mounted(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A person's own mount is a lease like any other: the member who may read
    a file inside it is told the holder's name, not the uuid the fence is on.

    The holder is not the reader here — the case the box tests never reach,
    because a box is nobody's colleague."""
    drive = await fx.drive()
    folder = await fx.node(b"project", kind="folder")
    inside = await fx.node(b"report.csv", parent=folder)
    await _take_lease(
        files_client,
        drive.id,
        folder.id,
        idem,
        real_session,
        machine="dana-macbook",
        purpose="mount",
    )
    await _grant_reader(fx, files_org, inside)
    colleague = await _colleague(files_org)
    try:
        facet = await _lease_of(colleague, drive.id, inside.id)
        assert facet["holder_name"] == "Test Admin"
        assert facet["mine"] is False
    finally:
        await colleague.aclose()


async def test_a_holder_this_org_never_admitted_is_left_unnamed(
    files_client: AsyncClient,
    fx: FilesFixtures,
    real_session: AsyncSession,
    idem: Any,
) -> None:
    """A holder is a principal written on a lease, not a row the reader was
    handed, so it is named only when this org has them as a member — the same
    answer the members list would give them. Anyone else keeps the id alone,
    which is still what the server fences a write on."""
    _other_org, other_admin = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@test.dev",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()

    drive = await fx.drive()
    folder = await fx.node(b"project", kind="folder")
    await _take_lease(
        files_client,
        drive.id,
        folder.id,
        idem,
        real_session,
        machine="dana-macbook",
        purpose="mount",
    )
    await _hold_by(real_session, folder.id, other_admin.id)

    facet = await _lease_of(files_client, drive.id, folder.id)
    assert facet["holder"] == str(other_admin.id)
    assert facet["holder_name"] == ""


# ---- whether the holder acts for the reader ---------------------------------
#
# A refusal that names "someone" when the folder is held by the reader's own
# chat or box sends them looking for a colleague who does not exist. ``yours``
# is what lets the page say "your chat …" instead.


async def test_the_chats_owner_reads_the_lease_as_their_own_chat(
    files_client: AsyncClient, leased_chat: LeasedChat
) -> None:
    for node in (leased_chat.folder, leased_chat.report):
        facet = await _lease_of(files_client, leased_chat.drive_id, node.id)
        assert facet["yours"] == "chat", node.name_display
        # Not "you": the reader is not the holder, and cannot release it.
        assert facet["mine"] is False


async def test_a_colleagues_chat_on_the_readers_box_reads_as_their_box(
    files_client: AsyncClient,
    files_org: FilesOrgFixture,
    leased_chat: LeasedChat,
    real_session: AsyncSession,
) -> None:
    """An org box runs members' chats on the admin's session. The admin runs the
    box and the chat is the member's: "your box", never "your chat"."""
    await real_session.execute(
        text("UPDATE workspace_objects SET owner_user_id = :u WHERE id = :c"),
        {"u": files_org.member.id, "c": leased_chat.chat_id},
    )
    await real_session.commit()

    facet = await _lease_of(files_client, leased_chat.drive_id, leased_chat.notes.id)

    assert facet["yours"] == "box"


async def test_a_reader_who_neither_owns_the_chat_nor_runs_the_box_reads_none(
    fx: FilesFixtures, files_org: FilesOrgFixture, leased_chat: LeasedChat
) -> None:
    await _grant_reader(fx, files_org, leased_chat.notes)
    colleague = await _colleague(files_org)
    try:
        facet = await _lease_of(colleague, leased_chat.drive_id, leased_chat.notes.id)
    finally:
        await colleague.aclose()

    assert facet["yours"] == "none"


async def test_a_person_holds_their_own_mount_as_you(
    files_client: AsyncClient, fx: FilesFixtures, real_session: AsyncSession, idem: Any
) -> None:
    drive = await fx.drive()
    folder = await fx.node(b"project", kind="folder")
    await _take_lease(
        files_client, drive.id, folder.id, idem, real_session, machine="laptop", purpose="mount"
    )

    facet = await _lease_of(files_client, drive.id, folder.id)

    assert (facet["mine"], facet["yours"]) == (True, "you")


async def test_a_mount_named_after_the_readers_box_does_not_read_as_their_box(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    idem: Any,
) -> None:
    """A person's mount carries a machine name of their own choosing. A member's
    mount that names the admin's box id must not read to the admin as the
    admin's own box: only a box holds its lease as itself."""
    _box_client, machine_id = box
    drive = await fx.drive()
    folder = await fx.node(b"project", kind="folder")
    await _take_lease(
        files_client, drive.id, folder.id, idem, real_session, machine=machine_id, purpose="mount"
    )
    await _hold_by(real_session, folder.id, files_org.member.id)

    facet = await _lease_of(files_client, drive.id, folder.id)

    assert facet["machine"] == machine_id
    assert facet["yours"] == "none"


async def test_the_trash_names_a_rows_holder_the_way_a_listing_does(
    files_client: AsyncClient,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    leased_chat: LeasedChat,
) -> None:
    """A trashed file inside a running chat's folder is still under its lease, and
    the trash is where "Delete forever" meets that lease. The trash listing used
    to carry the raw facet alone, so a refusal there could only say "someone"."""
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.ids import NodeId
    from alkera_core.files.trash import Trash

    ctx = ActingContext.for_user(
        user_id=files_org.org.admin_id, org_id=files_org.org.org_id, email="fixture@test"
    )
    async with fx.repo.transaction():
        # The unfenced trash an object's deletion takes: the file goes to the
        # trash while the chat folder above it stays leased.
        await Trash(fx.repo, ctx, SystemClock()).trash_for_deleted_object(
            NodeId(leased_chat.report.id)
        )
    await fx.repo.session.commit()

    listed = await files_client.get(f"{PREFIX}/drives/{leased_chat.drive_id}/trash")

    assert listed.status_code == 200, listed.text
    [row] = listed.json()["entries"]
    facet = row["item"]["lease"]
    assert (facet["yours"], facet["chat_title"]) == ("chat", TITLE)


# --------------------------------------------------------------------------
# The folder's status: the server's verdict, naming the machine
# --------------------------------------------------------------------------
#
# The browser used to judge, from the lease's raw fields and the chat banner's
# machine read, whether a folder was the machine's live work or the last copy
# it saved, and a surface with no banner (a shared folder, a second member's
# listing) painted "Live" over a machine that had stopped answering.


async def _stream(session: AsyncSession, node_id: uuid.UUID) -> None:
    """Put the lease on the live plane, beating now."""
    await session.execute(
        text(
            "UPDATE file_leases SET live_cadence = CAST(:cadence AS jsonb), heartbeat_at = now(), "
            "last_sync_at = now() WHERE node_id = :node AND released_at IS NULL"
        ),
        {"cadence": '{"heartbeat_seconds": 10}', "node": node_id},
    )
    await session.commit()


async def _checkpoint(session: AsyncSession, node_id: uuid.UUID) -> None:
    """Take the lease off the live plane: the holder saves on a checkpoint."""
    await session.execute(
        text("UPDATE file_leases SET live_cadence = '{}'::jsonb WHERE node_id = :node"),
        {"node": node_id},
    )
    await session.commit()


async def _machine_heard(session: AsyncSession, machine_id: str, at: datetime) -> None:
    """The box is up, and was last heard from at ``at``."""
    await session.execute(
        text(
            "UPDATE compute_allocations SET state = 'ready', last_heartbeat_at = :at "
            "WHERE id = :machine"
        ),
        {"at": at, "machine": machine_id},
    )
    await session.commit()


def _words(facet: dict[str, Any]) -> tuple[str, str, str]:
    status = facet["status"]
    return (status["state"], status["reason_code"], status["sentence"])


async def test_a_checkpoint_lease_reads_as_the_saved_copy_naming_the_box(
    files_client: AsyncClient,
    leased_chat: LeasedChat,
    box: tuple[AsyncClient, str],
    real_session: AsyncSession,
) -> None:
    _client, machine_id = box
    await _name_machine(real_session, machine_id, MACHINE_NAME)
    await _checkpoint(real_session, leased_chat.folder.id)
    facet = await _lease_of(files_client, leased_chat.drive_id, leased_chat.notes.id)
    assert _words(facet) == (
        "saved_copy",
        "",
        "alkera-demo-box holds this folder and saves it from time to time. "
        "These are the files it last saved.",
    )


async def test_a_streaming_lease_reads_live_while_its_box_answers_and_paused_once_it_does_not(
    files_client: AsyncClient,
    leased_chat: LeasedChat,
    box: tuple[AsyncClient, str],
    real_session: AsyncSession,
) -> None:
    _client, machine_id = box
    await _name_machine(real_session, machine_id, MACHINE_NAME)
    await _stream(real_session, leased_chat.folder.id)

    await _machine_heard(real_session, machine_id, datetime.now(UTC))
    live = await _lease_of(files_client, leased_chat.drive_id, leased_chat.report.id)
    assert _words(live) == ("live", "", "alkera-demo-box is writing these files as it works.")

    # The lease still beats (its TTL outlives the box), but the machine has
    # not answered the server for longer than its ready window.
    await _machine_heard(real_session, machine_id, datetime.now(UTC) - timedelta(minutes=20))
    paused = await _lease_of(files_client, leased_chat.drive_id, leased_chat.report.id)
    assert _words(paused) == (
        "sync_paused",
        "machine_unreachable",
        "alkera-demo-box isn't responding. These are the files it last saved.",
    )
