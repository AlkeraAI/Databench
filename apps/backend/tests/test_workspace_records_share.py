"""A share that would reach one chat of a workspace of several is refused on
the node's whole path, not on the chat's own folder alone.

The chats of a native workspace keep their folders under the workspace's
``.chats`` records folder. A grant on that folder, or on anything inside one
chat's folder, reaches the chats below it through the drive's inheritance
exactly as a grant on the chat would, so each is answered with the same
"share the workspace instead" refusal, and no grant is written. The workspace's
own folder and its shared tree are shared as before, and a chat that is a
workspace of one keeps being shared through anything in it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_WRITER
from alkera_core.files.objects_bridge import WORKSPACE_CHATS_FOLDER, WORKSPACE_FILES_FOLDER
from alkera_core.models import User
from alkera_core.models.files.acl import FileShare
from alkera_core.models.files.tree import FileNode
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@pytest.fixture
def multi_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    yield


async def _node(node_id: str | UUID) -> FileNode:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, UUID(str(node_id)))
        assert node is not None
        return node


async def _child(parent_id: str | UUID, name: bytes | None = None) -> FileNode:
    """A live child of ``parent_id``: the one called ``name``, or the first."""
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        stmt = select(FileNode).where(
            FileNode.parent_id == UUID(str(parent_id)), FileNode.trashed_at.is_(None)
        )
        if name is not None:
            stmt = stmt.where(FileNode.name == name)
        child = (await db.execute(stmt.order_by(FileNode.name).limit(1))).scalar_one_or_none()
        assert child is not None, f"no child {name!r} under {parent_id}"
        return child


async def _grants_on(node_id: str | UUID) -> int:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        return int(
            await db.scalar(
                select(func.count())
                .select_from(FileShare)
                .where(FileShare.node_id == UUID(str(node_id)), FileShare.revoked_at.is_(None))
            )
            or 0
        )


async def _share(client: AsyncClient, drive_id: str, node_id: str, member: User) -> Any:
    """Share a node through the Files permissions route, as the dialog does."""
    etag = (await _node(node_id)).etag
    return await client.post(
        f"/api/v1/files/drives/{drive_id}/items/{node_id}/permissions",
        json={"principal": {"kind": "user", "id": str(member.id)}, "role": ROLE_WRITER},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(etag)},
    )


async def _member(real_session: AsyncSession, org_admin: OrgWithAdmin) -> User:
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    return member


async def _in_a_chat_folder(client: AsyncClient, chat: dict[str, Any]) -> str:
    """A folder made inside the chat's own folder, through the Files route."""
    made = await client.post(
        f"/api/v1/files/drives/{chat['files_drive_id']}/items/{chat['files_node_id']}/children",
        json={"name": "notes", "folder": {}},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


#: Which node of a workspace of several a share names, as a function of the
#: workspace's folder and one of its chats.
_REFUSED = ("records_folder", "inside_a_chat_folder", "the_chat_folder")


@pytest.mark.usefixtures("multi_chat")
@pytest.mark.parametrize("where", [pytest.param(where, id=where) for where in _REFUSED])
async def test_a_share_that_reaches_a_chat_of_a_workspace_of_several_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession, where: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "In main"})).json()
    main = (await client.get("/api/v1/workspaces/main")).json()
    assert chat["files_node_id"] and main["files_node_id"]
    member = await _member(real_session, org_admin)
    if where == "records_folder":
        target = str((await _child(main["files_node_id"], WORKSPACE_CHATS_FOLDER)).id)
    elif where == "inside_a_chat_folder":
        target = await _in_a_chat_folder(client, chat)
    else:
        target = chat["files_node_id"]

    refused = await _share(client, chat["files_drive_id"], target, member)

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert (body["code"], body["message"]) == (
        "files.share_the_workspace",
        "Share the workspace instead.",
    )
    assert await _grants_on(target) == 0


@pytest.mark.usefixtures("multi_chat")
@pytest.mark.parametrize(
    "where",
    [
        pytest.param("workspace_folder", id="workspace_folder"),
        pytest.param("shared_tree", id="shared_tree"),
    ],
)
async def test_the_workspace_and_its_shared_tree_are_shared_as_before(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession, where: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    main = (await client.get("/api/v1/workspaces/main")).json()
    member = await _member(real_session, org_admin)
    target = (
        main["files_node_id"]
        if where == "workspace_folder"
        else str((await _child(main["files_node_id"], WORKSPACE_FILES_FOLDER)).id)
    )

    granted = await _share(client, main["files_drive_id"], target, member)

    assert granted.status_code == 201, granted.text
    assert await _grants_on(target) == 1


async def test_inside_a_chat_that_is_a_workspace_of_one_is_shared_as_before(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "workspaces_multi_chat", False)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post("/api/v1/chats", json={"title": "Alone"})).json()
    member = await _member(real_session, org_admin)
    inside = await _in_a_chat_folder(client, chat)

    granted = await _share(client, chat["files_drive_id"], inside, member)

    assert granted.status_code == 201, granted.text
