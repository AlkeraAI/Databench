"""Where a chat's files live, as the wire says it.

A chat is a folder that wraps its working directory together with its own
records, and "View files" on a chat opens the working directory — never the
folder around it. The client is not left to find that node by name: the name
is the one thing a person is never shown, so the chat's own facet names the
node, on a single read and on every listing row alike, resolved through the
bridge's one lookup. A chat with no working directory names none, and a folder
a chat merely carries — an ``outputs/`` from an older layout — is an ordinary
folder that never becomes the answer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from alkera_core.files.objects_bridge import CHAT_SANDBOX_FOLDER
from alkera_core.models.files.tree import FileNode
from alkera_core.models.user import User
from backend.services.files.items import CHAT_FILES_NODE_KEY
from backend.services.objects import object_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/files"

assert CHAT_SANDBOX_FOLDER is not None, "these cases are about the child a chat is born with"
WORKING = CHAT_SANDBOX_FOLDER.decode("utf-8")


async def _drive_id(client: AsyncClient) -> str:
    response = await client.get(f"{BASE}/drives")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _chat_node(client: AsyncClient, title: str = "Warehouse spike") -> str:
    """A chat created through the real route, as its Files node id."""
    response = await client.post("/api/v1/chats", json={"title": title})
    assert response.status_code == 201, response.text
    node_id = response.json()["files_node_id"]
    assert node_id, "a chat created with Files on names its node"
    return str(node_id)


async def _item(client: AsyncClient, drive: str, node_id: str) -> dict[str, Any]:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _rows(client: AsyncClient, drive: str, node_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"{BASE}/drives/{drive}/items/{node_id}/children")
    assert response.status_code == 200, response.text
    return [dict(row) for row in response.json()["value"]]


async def _named(client: AsyncClient, drive: str, node_id: str, name: str) -> dict[str, Any]:
    rows = {str(row["name"]): row for row in await _rows(client, drive, node_id)}
    assert name in rows, sorted(rows)
    return rows[name]


async def _folder_under(
    client: AsyncClient, drive: str, parent: str, name: str, idem: Callable[[], dict[str, str]]
) -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/drives/{drive}/items/{parent}/children",
        json={"kind": "folder", "name": name},
        headers=idem(),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _files_target(item: dict[str, Any]) -> str | None:
    facet = item.get("object") or {}
    named = (facet.get("metadata") or {}).get(CHAT_FILES_NODE_KEY)
    return str(named) if named else None


async def test_a_chat_names_the_node_its_files_live_at_on_a_read_and_on_a_listing(
    files_client: AsyncClient, files_on: None
) -> None:
    """The single read and the listing row agree, and both name the working
    directory the chat was born with — not the chat itself, and not a name."""
    chat_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    working = await _named(files_client, drive, chat_id, WORKING)

    chat = await _item(files_client, drive, chat_id)
    assert _files_target(chat) == str(working["id"])
    assert _files_target(chat) != chat_id

    listed = await _named(files_client, drive, str(chat["parentId"]), str(chat["name"]))
    assert _files_target(listed) == str(working["id"]), "the page's batched lookup agrees"
    # The working directory is an ordinary folder on the wire: it IS nothing
    # else, so a client walks into it rather than opening it as a page.
    assert working.get("object") is None
    assert working["kind"] == "folder"


async def test_a_chat_with_no_working_directory_names_none(
    files_client: AsyncClient, files_on: None, idem: Callable[[], dict[str, str]]
) -> None:
    """Trashed, the working directory is gone, and the chat says so by naming
    nothing — a client then opens the chat folder itself. A folder that merely
    shares the layout of an older chat does not step in as the answer."""
    chat_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    working = await _named(files_client, drive, chat_id, WORKING)

    trashed = await files_client.delete(
        f"{BASE}/drives/{drive}/items/{working['id']}",
        headers={**idem(), "If-Match": str(working["etag"])},
    )
    assert trashed.status_code == 200, trashed.text

    assert _files_target(await _item(files_client, drive, chat_id)) is None
    await _folder_under(files_client, drive, chat_id, "outputs", idem)
    assert _files_target(await _item(files_client, drive, chat_id)) is None


async def test_a_legacy_outputs_folder_is_listed_as_a_plain_folder_and_never_the_target(
    files_client: AsyncClient, files_on: None, idem: Callable[[], dict[str, str]]
) -> None:
    """A chat created under the older layout keeps its ``outputs/``: browsable,
    downloadable, and beside the working directory rather than in place of it."""
    chat_id = await _chat_node(files_client)
    drive = await _drive_id(files_client)
    legacy = await _folder_under(files_client, drive, chat_id, "outputs", idem)

    rows = {str(row["name"]): row for row in await _rows(files_client, drive, chat_id)}
    assert set(rows) == {WORKING, "outputs"}
    assert rows["outputs"]["capabilities"]["can_download"] is True
    assert rows["outputs"].get("object") is None

    chat = await _item(files_client, drive, chat_id)
    assert _files_target(chat) == str(rows[WORKING]["id"])
    assert _files_target(chat) != str(legacy["id"])


async def test_a_template_names_the_node_its_files_live_at_like_a_chat(
    files_client: AsyncClient, files_on: None, real_session: AsyncSession, files_org: Any
) -> None:
    """A template owns a working directory for the same reason a chat does —
    it is what a new chat copies from — so "view files" on one opens that
    directory, not the folder wrapping it with the template's own README."""
    owner = (
        await real_session.execute(select(User).where(User.id == files_org.org.admin_id))
    ).scalar_one()
    template, created = await object_service.create_object(
        real_session,
        owner=owner,
        org_id=owner.home_org_team_id,
        type="chat_template",
        title="Warehouse starter",
        spec={"brief": "Start here"},
    )
    assert created
    await real_session.commit()
    node_id = (
        await real_session.execute(
            select(FileNode.id).where(
                FileNode.target_object_id == template.id,
                FileNode.subtype == "chat_template",
                FileNode.trashed_at.is_(None),
            )
        )
    ).scalar_one()

    drive = await _drive_id(files_client)
    working = await _named(files_client, drive, str(node_id), WORKING)
    item = await _item(files_client, drive, str(node_id))
    assert _files_target(item) == str(working["id"])
    assert _files_target(item) != str(node_id)

    listed = await _named(files_client, drive, str(item["parentId"]), str(item["name"]))
    assert _files_target(listed) == str(working["id"]), "the page's batched lookup agrees"
