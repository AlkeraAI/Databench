"""A person's own box in Files: its owner's chat folders, and nobody else's.

The Files decider admits a box to a chat's folder by the chat's binding. A
personal box runs only its person's private chats, so the binding is not enough
for it: a colleague's chat folder is a stranger's 404 on every Files door (the
item, its children, the lease) whether the colleague's chat is bound to the box
or to nothing, and so is the folder of a workspace that owns one. Its owner's
chat folder, bound to it, is reachable exactly as a platform box reaches the
chats bound to it.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin
from tests.files._live_holder import MockHolder
from tests.test_personal_box import Box, Person, _create, _member, _personal_box

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


async def _folder(chat_id: str) -> FileNode:
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


async def _bind(chat_id: str, machine: str | None) -> None:
    async with AsyncSessionLocal() as session:
        if machine is None:
            await session.execute(
                text("UPDATE workspace_objects SET spec = spec - 'machine_id' WHERE id = :id"),
                {"id": uuid.UUID(chat_id)},
            )
        else:
            await session.execute(
                text(
                    "UPDATE workspace_objects SET spec = jsonb_set(spec, '{machine_id}', "
                    "to_jsonb(CAST(:machine AS text))) WHERE id = :id"
                ),
                {"id": uuid.UUID(chat_id), "machine": machine},
            )
        await session.commit()


class Setup:
    def __init__(self, box: Box, client: AsyncClient, owner: Person, colleague: Person) -> None:
        self.box = box
        self.client = client
        self.owner = owner
        self.colleague = colleague

    def item(self, node: FileNode) -> str:
        return f"{BASE}/drives/{node.drive_id}/items/{node.id}"


@pytest_asyncio.fixture
async def setup(client: AsyncClient, org_admin: OrgWithAdmin) -> AsyncIterator[Setup]:
    owner, colleague = await _member(org_admin.org_id), await _member(org_admin.org_id)
    box = await _personal_box(client, owner)
    client.headers.update(box.headers)
    yield Setup(box, client, owner, colleague)


async def test_the_box_reaches_its_owners_chat_folder(
    setup: Setup, real_session: AsyncSession
) -> None:
    mine = await _create(setup.owner)
    assert mine["machine_id"] == setup.box.machine_id
    node = await _folder(mine["id"])
    read = await setup.client.get(setup.item(node))
    assert read.status_code == 200, read.text
    assert read.json()["capabilities"]["can_lease"] is True
    holder = MockHolder(setup.client, node.drive_id, node.id, machine=setup.box.machine_id)
    taken = await holder.take(real_session, _idem)
    assert taken.status_code == 200, taken.text


@pytest.mark.parametrize("bound", ["to-the-box", "to-nothing"])
async def test_a_colleagues_chat_folder_is_a_strangers_on_every_door(
    setup: Setup, real_session: AsyncSession, bound: str
) -> None:
    theirs = await _create(setup.colleague)
    await _bind(theirs["id"], setup.box.machine_id if bound == "to-the-box" else None)
    node = await _folder(theirs["id"])

    read = await setup.client.get(setup.item(node))
    assert read.status_code == 404, read.text
    children = await setup.client.get(f"{setup.item(node)}/children")
    assert children.status_code == 404, children.text
    holder = MockHolder(setup.client, node.drive_id, node.id, machine=setup.box.machine_id)
    taken = await holder.take(real_session, _idem)
    assert taken.status_code == 404, taken.text
    drive = await setup.client.get(f"{BASE}/drives", params={"chatId": theirs["id"]})
    assert drive.status_code == 404, drive.text


async def test_the_owners_chat_folder_is_its_own_but_a_colleagues_next_to_it_is_not(
    setup: Setup,
) -> None:
    """The control: the same box, the same drive listing, one folder each."""
    mine = await _create(setup.owner)
    theirs = await _create(setup.colleague)
    await _bind(theirs["id"], setup.box.machine_id)
    mine_node, their_node = await _folder(mine["id"]), await _folder(theirs["id"])
    assert (await setup.client.get(setup.item(mine_node))).status_code == 200
    assert (await setup.client.get(setup.item(their_node))).status_code == 404


async def test_a_content_url_the_box_minted_redeems_as_the_box_and_its_owner(
    setup: Setup, org_admin: OrgWithAdmin
) -> None:
    """The content origin has no credential, so it rebuilds the minter from
    the signed claim. For a personal box it must rebuild the owner too, or the
    redemption would be decided as a box that serves every chat bound to it."""
    from alkera_core.authz.principal import ActingContext
    from alkera_core.files import signed_urls
    from alkera_core.models import MachineCredential
    from backend.api.routes.files.content_serve import _Redeemer, _with_personal_owner

    async with AsyncSessionLocal() as session:
        credential = (
            await session.execute(
                select(MachineCredential).where(
                    MachineCredential.machine_id == uuid.UUID(setup.box.machine_id)
                )
            )
        ).scalar_one()
        claim = signed_urls.ContentClaim(
            org_id=org_admin.org_id,
            user_id=None,
            session_id=None,
            machine_id=setup.box.machine_id,
            credential_id=credential.id,
        )
        bare = _Redeemer(
            claim=claim,
            context=ActingContext.for_machine(
                machine_id=uuid.UUID(setup.box.machine_id),
                credential_id=credential.id,
                org_id=org_admin.org_id,
                label="",
            ),
        )
        rebuilt = await _with_personal_owner(session, bare)
    assert rebuilt is not None
    assert rebuilt.context.personal_owner_id == setup.owner.id
    assert rebuilt.context.serves(org_admin.org_id)
