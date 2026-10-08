"""Deleting a chat ends the lease its box holds on the chat's folder.

The folder of a deleted chat lands in its owner's Trash. The box running the
chat held that folder's lease, and nothing ended it: the delete trashes the
folder without the fence (the subject of the lease is what is going away), the
box's beats went on renewing it for as long as the box kept the chat — and a box
that never heard about the delete kept it for ever. "Delete forever" then
answered "Someone has this folder for local use" for a folder nobody could open.

Driven through the real routes: the chat delete, the box's per-lease and
batched beats and its re-take, the owner's "Delete forever" and "Empty trash".
The box is the org admin's own, on the admin's session with a proven machine
assertion — the shape an org box on its operator's session has, and the one
whose beats the owner's own rung on the folder kept admitting.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from _files_kit import node_etag
from alkera_core.authz.headers import agent_headers
from alkera_core.files.objects_bridge import CHAT_TYPE
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._suite_app import app as fastapi_app
from tests.files._boxes import bind_chat, registered_box

if TYPE_CHECKING:  # the fixtures live in a conftest, which is not an importable package
    from .conftest import FilesFixtures, FilesOrgFixture

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

PREFIX = "/api/v1/files"
#: The trash routes name a resource with no etag of its own; the header is
#: required and not compared.
PRECONDITION = {"If-Match": "0"}


def _item(drive_id: uuid.UUID, node_id: uuid.UUID) -> str:
    return f"{PREFIX}/drives/{drive_id}/items/{node_id}"


@dataclass(frozen=True)
class RunningChat:
    """A chat whose folder the box holds under the ``chat`` purpose."""

    drive_id: uuid.UUID
    chat_id: uuid.UUID
    folder: Any
    epoch: int
    instance: str


@pytest_asyncio.fixture
async def box(
    real_session: AsyncSession, files_org: FilesOrgFixture
) -> AsyncIterator[tuple[AsyncClient, str]]:
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


async def _take(
    client: AsyncClient,
    session: AsyncSession,
    idem: Any,
    drive_id: uuid.UUID,
    node_id: uuid.UUID,
    *,
    instance: str,
    machine: str,
) -> Any:
    return await client.post(
        f"{_item(drive_id, node_id)}/lease",
        json={"instanceId": instance, "machineId": machine, "purpose": "chat"},
        headers={**idem(), "If-Match": await node_etag(session, node_id)},
    )


@pytest_asyncio.fixture
async def running_chat(
    fx: FilesFixtures, real_session: AsyncSession, box: tuple[AsyncClient, str], idem: Any
) -> RunningChat:
    box_client, machine_id = box
    drive = await fx.drive()
    folder = await fx.node(
        b"Kickoff.alkerachat", kind="folder", subtype=CHAT_TYPE, parent=await fx.home()
    )
    await fx.node(b"notes.md", parent=folder)
    await bind_chat(fx, real_session, folder, machine=machine_id)
    chat_id = (
        await real_session.execute(
            text("SELECT target_object_id FROM file_nodes WHERE id = :n"), {"n": folder.id}
        )
    ).scalar_one()
    instance = f"{machine_id}:chat"
    granted = await _take(
        box_client, real_session, idem, drive.id, folder.id, instance=instance, machine=machine_id
    )
    assert granted.status_code == 200, granted.text
    return RunningChat(
        drive_id=drive.id,
        chat_id=uuid.UUID(str(chat_id)),
        folder=folder,
        epoch=int(granted.json()["epoch"]),
        instance=instance,
    )


async def _lease(session: AsyncSession, node_id: uuid.UUID) -> Any:
    row = (
        await session.execute(
            text(
                "SELECT expires_at, released_at IS NULL AND reaped_at IS NULL "
                "AND expires_at > now() AS live FROM file_leases WHERE node_id = :n"
            ),
            {"n": node_id},
        )
    ).one()
    await session.commit()
    return row


async def _left_live(session: AsyncSession, node_id: uuid.UUID) -> None:
    """The row a chat delete from before this change left behind: live, a TTL
    ahead of it, on a folder in the trash."""
    await session.execute(
        text(
            "UPDATE file_leases SET released_at = NULL, grantable_after = now(), "
            "expires_at = now() + interval '1 hour' WHERE node_id = :n"
        ),
        {"n": node_id},
    )
    await session.commit()


async def _delete_chat(client: AsyncClient, chat: RunningChat) -> None:
    deleted = await client.delete(f"/api/v1/chats/{chat.chat_id}")
    assert deleted.status_code == 204, deleted.text


async def _gone(session: AsyncSession, node_id: uuid.UUID) -> bool:
    found = (
        await session.execute(text("SELECT 1 FROM file_nodes WHERE id = :n"), {"n": node_id})
    ).first()
    await session.commit()
    return found is None


async def test_deleting_the_chat_ends_its_folder_lease(
    files_client: AsyncClient, real_session: AsyncSession, running_chat: RunningChat
) -> None:
    assert (await _lease(real_session, running_chat.folder.id)).live

    await _delete_chat(files_client, running_chat)

    assert not (await _lease(real_session, running_chat.folder.id)).live


async def test_the_box_beat_on_a_deleted_chats_lease_is_refused_and_moves_nothing(
    files_client: AsyncClient,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    running_chat: RunningChat,
) -> None:
    """Both beats a box sends, the per-lease one and the batch, are fenced — the
    answer that makes the box ask for the folder again, and learn it is gone."""
    box_client, _machine = box
    await _delete_chat(files_client, running_chat)
    before = (await _lease(real_session, running_chat.folder.id)).expires_at

    single = await box_client.post(
        f"{_item(running_chat.drive_id, running_chat.folder.id)}/lease/heartbeat",
        json={"epoch": running_chat.epoch, "instanceId": running_chat.instance},
    )
    batched = await box_client.post(
        f"{PREFIX}/drives/{running_chat.drive_id}/leases/heartbeat",
        json={
            "leases": [
                {
                    "nodeId": str(running_chat.folder.id),
                    "epoch": running_chat.epoch,
                    "instanceId": running_chat.instance,
                    "synced": True,
                }
            ]
        },
    )

    assert single.status_code == 409, single.text
    assert single.json()["code"] == "files.lease_fenced"
    assert batched.status_code == 200, batched.text
    assert [v["verdict"] for v in batched.json()["leases"]] == ["superseded"]
    after = await _lease(real_session, running_chat.folder.id)
    assert not after.live
    assert after.expires_at == before


async def test_a_box_that_kept_beating_a_deleted_chats_lease_is_fenced(
    files_client: AsyncClient,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    running_chat: RunningChat,
) -> None:
    """The lease a delete from before this change left live — the one the box
    in the report was still renewing. Its next beat is fenced, not renewed."""
    box_client, _machine = box
    await _delete_chat(files_client, running_chat)
    await _left_live(real_session, running_chat.folder.id)
    before = (await _lease(real_session, running_chat.folder.id)).expires_at

    beat = await box_client.post(
        f"{_item(running_chat.drive_id, running_chat.folder.id)}/lease/heartbeat",
        json={"epoch": running_chat.epoch, "instanceId": running_chat.instance},
    )

    assert beat.status_code == 409, beat.text
    assert beat.json()["code"] == "files.lease_fenced"
    assert (await _lease(real_session, running_chat.folder.id)).expires_at == before


async def test_the_box_cannot_take_a_deleted_chats_folder_again(
    files_client: AsyncClient,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    running_chat: RunningChat,
    idem: Any,
) -> None:
    """A fenced box asks for the folder again. For a deleted chat the answer is
    ``files.trashed`` — the one it drops the chat on — never a fresh grant."""
    box_client, machine = box
    await _delete_chat(files_client, running_chat)

    retake = await _take(
        box_client,
        real_session,
        idem,
        running_chat.drive_id,
        running_chat.folder.id,
        instance=running_chat.instance,
        machine=machine,
    )

    assert retake.status_code == 409, retake.text
    assert retake.json()["code"] == "files.trashed"
    assert not (await _lease(real_session, running_chat.folder.id)).live


@pytest.mark.parametrize("left_live", [False, True], ids=["ended-at-delete", "left-live"])
async def test_delete_forever_removes_a_deleted_chats_folder(
    files_client: AsyncClient,
    real_session: AsyncSession,
    running_chat: RunningChat,
    idem: Any,
    left_live: bool,
) -> None:
    await _delete_chat(files_client, running_chat)
    if left_live:
        await _left_live(real_session, running_chat.folder.id)

    purged = await files_client.delete(
        f"{_item(running_chat.drive_id, running_chat.folder.id)}?permanent=true",
        headers={**idem(), "If-Match": await node_etag(real_session, running_chat.folder.id)},
    )

    assert purged.status_code == 204, purged.text
    assert await _gone(real_session, running_chat.folder.id)


@pytest.mark.parametrize("left_live", [False, True], ids=["ended-at-delete", "left-live"])
async def test_emptying_the_trash_removes_a_deleted_chats_folder(
    files_client: AsyncClient,
    real_session: AsyncSession,
    running_chat: RunningChat,
    idem: Any,
    left_live: bool,
) -> None:
    await _delete_chat(files_client, running_chat)
    if left_live:
        await _left_live(real_session, running_chat.folder.id)

    emptied = await files_client.post(
        f"{PREFIX}/drives/{running_chat.drive_id}/trash/empty",
        json={},
        headers={**idem(), **PRECONDITION},
    )

    assert emptied.status_code == 200, emptied.text
    assert emptied.json() == {"removed": 1, "skipped": 0, "skippedReasons": []}
    assert await _gone(real_session, running_chat.folder.id)


async def test_a_running_chats_folder_still_refuses_the_owners_delete_and_keeps_its_beat(
    files_client: AsyncClient,
    real_session: AsyncSession,
    box: tuple[AsyncClient, str],
    running_chat: RunningChat,
    idem: Any,
) -> None:
    """No regression on a folder that is NOT in the trash: the owner's trash and
    purge are refused while the box holds it, and the box's beat renews."""
    box_client, _machine = box
    headers = {"If-Match": await node_etag(real_session, running_chat.folder.id)}

    trashed = await files_client.delete(
        _item(running_chat.drive_id, running_chat.folder.id), headers={**idem(), **headers}
    )
    purged = await files_client.delete(
        f"{_item(running_chat.drive_id, running_chat.folder.id)}?permanent=true",
        headers={**idem(), **headers},
    )
    beat = await box_client.post(
        f"{_item(running_chat.drive_id, running_chat.folder.id)}/lease/heartbeat",
        json={"epoch": running_chat.epoch, "instanceId": running_chat.instance},
    )

    assert (trashed.status_code, trashed.json()["code"]) == (409, "files.leased")
    assert (purged.status_code, purged.json()["code"]) == (409, "files.leased")
    assert beat.status_code == 200, beat.text
    assert (await _lease(real_session, running_chat.folder.id)).live
    assert not await _gone(real_session, running_chat.folder.id)
