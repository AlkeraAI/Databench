"""``GET /api/v1/notebooks/{drive}/{item}/editor``: where the notebook editor
opens a notebook, or a new notebook in a folder.

The editor runs only in a chat's workspace pane, so the answer is a chat: the
chat whose own folder holds the node, or in a workspace's folder the chat a
wake of that workspace goes through. Where no kernel can run (outside every
chat and workspace folder) there is none, and a reader the chat policy does
not let send is named none either, since the editor is opened to edit and run.
"""

from __future__ import annotations

import uuid

import pytest
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from backend.services.workspaces import workspace_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.file_world import FileWorld, file_world

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

NOTEBOOK = b"import marimo\napp = marimo.App()\n"


async def _node(db: AsyncSession, node_id: uuid.UUID | str) -> FileNode:
    node = await db.get(FileNode, uuid.UUID(str(node_id)))
    assert node is not None
    return node


def _url(node: FileNode) -> str:
    return f"/api/v1/notebooks/{node.drive_id}/{node.id}/editor"


async def _chat_world(db: AsyncSession, org: OrgWithAdmin) -> FileWorld:
    return await file_world(db, org, content=NOTEBOOK, name="analysis.alknb.py")


@pytest.mark.parametrize(
    ("who", "named"),
    [
        pytest.param("owner", True, id="owner"),
        pytest.param("writer", True, id="writer-may-send"),
        pytest.param("reader", False, id="reader-may-not-send"),
    ],
)
async def test_a_notebook_in_a_chat_s_folder_opens_in_that_chat(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    client: AsyncClient,
    who: str,
    named: bool,
) -> None:
    fw = await _chat_world(real_session, org_admin)
    person = getattr(fw.world, who)
    await login(client, person.user.email, person.password)
    notebook = await _node(real_session, fw.node_id)

    answer = await client.get(_url(notebook))

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"chat_id": fw.world.ref.doc_id if named else None}


async def test_the_folder_a_notebook_would_be_created_in_answers_the_same_chat(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    fw = await _chat_world(real_session, org_admin)
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    notebook = await _node(real_session, fw.node_id)
    assert notebook.parent_id is not None
    folder = await _node(real_session, notebook.parent_id)

    answer = await client.get(_url(folder))

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"chat_id": fw.world.ref.doc_id}


async def test_a_folder_outside_every_chat_and_workspace_opens_nowhere(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    """The folder that holds the chat's own folder is in no chat: a notebook
    there has no kernel, so the editor has nowhere to open it."""
    fw = await _chat_world(real_session, org_admin)
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    node = await _node(real_session, fw.node_id)
    chat_folder: FileNode | None = None
    while node.parent_id is not None:
        node = await _node(real_session, node.parent_id)
        if str(node.target_object_id) == fw.world.ref.doc_id:
            chat_folder = node
            break
    assert chat_folder is not None and chat_folder.parent_id is not None
    outside = await _node(real_session, chat_folder.parent_id)

    answer = await client.get(_url(outside))

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"chat_id": None}


async def test_a_notebook_in_a_workspace_opens_in_the_chat_its_wake_goes_through(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    workspace, _made = await workspace_service.create_project(
        real_session, owner=owner, org_id=org_admin.org_id, title="Analysis", client_id=None
    )
    await real_session.commit()
    fw = await file_world(
        real_session, org_admin, content=NOTEBOOK, name="analysis.alknb.py", workspace=workspace
    )
    chat = await real_session.get(WorkspaceObject, uuid.UUID(fw.world.ref.doc_id))
    assert chat is not None
    await login(client, fw.world.owner.user.email, fw.world.owner.password)
    notebook = await _node(real_session, fw.node_id)

    answer = await client.get(_url(notebook))

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"chat_id": str(chat.id)}


async def test_a_stranger_is_answered_the_opaque_not_found(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> None:
    fw = await _chat_world(real_session, org_admin)
    stranger = fw.world.stranger
    await login(client, stranger.user.email, stranger.password)
    notebook = await _node(real_session, fw.node_id)

    answer = await client.get(_url(notebook))

    assert answer.status_code == 404, answer.text
