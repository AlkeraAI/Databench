"""What a box writes into a chat is the chat owner's, and never shows a machine id.

A box speaks on its own machine credential: no user behind it, and an id that
names a machine. Every write it makes lands in the folder of a chat bound to
it, and those bytes were already charged to whoever owns that folder. The node
and its versions are credited to that person too, so the Files panel's Owner
column, the "Owned by me" filter and the versions dialog read the person a
reader knows.

What each case pins:

* every write door a box uses (a create, the tree report, a content push)
  records the chat's owner, and the listing names them;
* the version the box pushed is authored by the owner, and still marked as the
  machine's;
* a row a box wrote before this, still recorded as the machine, lists as
  "Agent", never as the id;
* crediting the owner grants nothing: a colleague still gets the opaque 404 on
  the file and the box still may not share it.

Both kinds of box are covered: a person's own box, and an org's dedicated box
running a member's chat.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, make_member
from tests.files._boxes import credential_box
from tests.files._live_holder import MockHolder
from tests.test_personal_box import Person, _create, _personal_box

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

BASE = "/api/v1/files"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from alkera_core.config import settings
    from backend.api.routes.compute.machines import heartbeat_decision_sink
    from backend.services.identity.device_authorization import _user_code_limiter

    monkeypatch.setattr(settings, "compute_heartbeat_ready_seconds", 600)
    _user_code_limiter.reset()
    heartbeat_decision_sink().clear()


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": secrets.token_hex(8)}


async def _named_member(org_id: uuid.UUID, first: str, last: str) -> Person:
    async with AsyncSessionLocal() as session:
        user, password = await make_member(
            session, org_id=org_id, verified=True, first_name=first, last_name=last
        )
    assert password is not None
    return Person(user, password)


async def _chat_folder(chat_id: str) -> FileNode:
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.target_object_id == uuid.UUID(chat_id),
                    FileNode.subtype == "chat",
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        session.expunge(node)
        return node


async def _bind(chat_id: str, machine: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
            ),
            {"id": uuid.UUID(chat_id), "machine": machine},
        )
        await session.commit()


async def _child(parent_id: uuid.UUID, name: str) -> FileNode:
    async with AsyncSessionLocal() as session:
        node = (
            await session.execute(
                select(FileNode).where(
                    FileNode.parent_id == parent_id,
                    FileNode.name == name.encode(),
                    FileNode.trashed_at.is_(None),
                )
            )
        ).scalar_one()
        session.expunge(node)
        return node


@dataclass
class Running:
    """A chat its owner made, bound to a box that holds its folder's lease."""

    box: AsyncClient
    machine_id: str
    owner: Person
    owner_client: AsyncClient
    colleague: Person
    folder: FileNode
    holder: MockHolder

    def item(self, node_id: uuid.UUID) -> str:
        return f"{BASE}/drives/{self.folder.drive_id}/items/{node_id}"


async def _personal(client: AsyncClient, org_id: uuid.UUID) -> tuple[Person, AsyncClient, str]:
    owner = await _named_member(org_id, "Ana", "Ruiz")
    box = await _personal_box(client, owner)
    client.headers.update(box.headers)
    return owner, client, box.machine_id


async def _dedicated(
    client: AsyncClient, org_admin: OrgWithAdmin, session: AsyncSession
) -> tuple[Person, AsyncClient, str]:
    owner = await _named_member(org_admin.org_id, "Ana", "Ruiz")
    raw, machine_id = await credential_box(
        session, org_id=org_admin.org_id, user_id=org_admin.admin_id
    )
    client.headers.update({"Authorization": f"Bearer {raw}"})
    return owner, client, machine_id


@pytest_asyncio.fixture(params=["personal-box", "dedicated-box"])
async def running(
    request: pytest.FixtureRequest,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> AsyncIterator[Running]:
    if request.param == "personal-box":
        owner, box, machine_id = await _personal(client, org_admin.org_id)
    else:
        owner, box, machine_id = await _dedicated(client, org_admin, real_session)
    colleague = await _named_member(org_admin.org_id, "Bo", "Chen")
    chat = await _create(owner)
    await _bind(chat["id"], machine_id)
    folder = await _chat_folder(chat["id"])
    holder = MockHolder(box, folder.drive_id, folder.id, machine=machine_id)
    taken = await holder.take(real_session, _idem, purpose="chat")
    assert taken.status_code == 200, taken.text
    owner_client = await owner.client()
    try:
        yield Running(box, machine_id, owner, owner_client, colleague, folder, holder)
    finally:
        await owner_client.aclose()


async def _box_writes(running: Running, real_session: AsyncSession) -> FileNode:
    """The box makes a folder, reports a file into it and pushes its bytes:
    the three write doors a box uses on a chat's folder."""
    made = await running.box.post(
        f"{running.item(running.folder.id)}/children",
        json={"name": "outputs", "kind": "folder"},
        headers={**_idem(), **running.holder.fence},
    )
    assert made.status_code == 201, made.text
    reported = await running.holder.tree(
        [{"op": "upsert", "path": "outputs/test.txt", "kind": "file", "size": 5}]
    )
    assert reported.status_code == 200, reported.text
    outputs = await _child(running.folder.id, "outputs")
    written = await _child(outputs.id, "test.txt")
    pushed = await running.holder.push(real_session, _idem, written.id, b"hello")
    assert pushed.status_code in (200, 201), pushed.text
    return written


async def test_every_box_write_is_credited_to_the_chats_owner(
    running: Running, real_session: AsyncSession
) -> None:
    written = await _box_writes(running, real_session)
    outputs = await _child(running.folder.id, "outputs")
    owner_id = running.owner.id

    assert outputs.created_by == owner_id
    assert written.created_by == owner_id
    async with AsyncSessionLocal() as session:
        authors = (
            await session.execute(
                select(FileVersion.created_by).where(FileVersion.node_id == written.id)
            )
        ).scalars()
        assert set(authors) == {owner_id}

    listed = await running.owner_client.get(f"{running.item(outputs.id)}/children")
    assert listed.status_code == 200, listed.text
    (row,) = [item for item in listed.json()["value"] if item["name"] == "test.txt"]
    assert row["attrs"]["owner"] == str(owner_id)
    assert row["ownerName"] == "Ana Ruiz"

    mine = await running.owner_client.get(
        f"{running.item(outputs.id)}/children", params={"owner": str(owner_id)}
    )
    assert mine.status_code == 200, mine.text
    assert [item["name"] for item in mine.json()["value"]] == ["test.txt"]


async def test_the_pushed_version_is_the_owners_and_still_the_machines(
    running: Running, real_session: AsyncSession
) -> None:
    written = await _box_writes(running, real_session)

    versions = await running.owner_client.get(f"{running.item(written.id)}/versions")

    assert versions.status_code == 200, versions.text
    (head,) = [row for row in versions.json()["versions"] if row["isHead"]]
    assert head["author"] == "Ana Ruiz"
    assert head["machine"] == running.machine_id


async def test_a_row_a_box_wrote_before_lists_as_agent_never_the_id(
    running: Running, real_session: AsyncSession
) -> None:
    written = await _box_writes(running, real_session)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE file_nodes SET created_by = :machine WHERE id = :node"),
            {"machine": uuid.UUID(running.machine_id), "node": written.id},
        )
        await session.commit()

    read = await running.owner_client.get(running.item(written.id))

    assert read.status_code == 200, read.text
    body: dict[str, Any] = read.json()
    assert body["ownerName"] == "Agent"


async def test_crediting_the_owner_grants_nobody_anything(
    running: Running, real_session: AsyncSession
) -> None:
    written = await _box_writes(running, real_session)

    colleague = await running.colleague.client()
    try:
        refused = await colleague.get(running.item(written.id))
    finally:
        await colleague.aclose()
    assert refused.status_code == 404, refused.text

    as_box = await running.box.get(running.item(written.id))
    assert as_box.status_code == 200, as_box.text
    assert as_box.json()["capabilities"]["can_share"] is False

    shared = await running.box.post(
        f"{running.item(written.id)}/permissions",
        json={"principal": {"kind": "user", "id": str(running.colleague.id)}, "role": "reader"},
        headers={**_idem(), "If-Match": as_box.headers["ETag"]},
    )
    assert shared.status_code in (403, 404), shared.text
