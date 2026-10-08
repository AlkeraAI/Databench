"""A reference the agent wrote, resolved against the chat's real Files.

The chats are made through the real route, so each has the folder and working
folder (``scratch/``) a real chat has; the files under them are seeded where
the agent would have written them.
"""

from __future__ import annotations

import uuid

import pytest
from _files_kit import FilesFixtures, FilesOrgFixture
from alkera_core.authz.principal import ActingContext
from alkera_core.files.objects_bridge import CHAT_SANDBOX_FOLDER
from alkera_core.models.files.tree import FileNode
from backend.services.files.chat_paths import ChatFile, chat_path, resolve_chat_file
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("files_on")

assert CHAT_SANDBOX_FOLDER is not None


async def _chat(client: AsyncClient) -> tuple[uuid.UUID, uuid.UUID]:
    """A chat through the real route: its id and its folder's node id."""
    response = await client.post("/api/v1/chats", json={"title": "Survey"})
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["id"]), uuid.UUID(body["files_node_id"])


async def _child(session: AsyncSession, parent: uuid.UUID, name: bytes) -> FileNode:
    found = (
        await session.execute(
            select(FileNode).where(FileNode.parent_id == parent, FileNode.name == name)
        )
    ).scalar_one()
    return found


class ChatTree:
    """One chat's folder with a few files seeded where the agent writes."""

    def __init__(self, chat_id: uuid.UUID, nodes: dict[str, uuid.UUID]) -> None:
        self.chat_id = chat_id
        self.nodes = nodes


async def _tree(client: AsyncClient, session: AsyncSession, fx: FilesFixtures) -> ChatTree:
    chat_id, chat_node = await _chat(client)
    chat = await fx.folder(chat_node)
    working = await _child(session, chat_node, CHAT_SANDBOX_FOLDER)
    charts = await fx.node(b"charts", kind="folder", parent=working)
    nodes = {
        "charts": charts.id,
        "charts/responses.png": (await fx.node(b"responses.png", parent=charts)).id,
        "plot_responses.py": (await fx.node(b"plot_responses.py", parent=working)).id,
        "my chart.png": (await fx.node(b"my chart.png", parent=working)).id,
        "records.md": (await fx.node(b"records.md", parent=chat)).id,
    }
    return ChatTree(chat_id, nodes)


async def _resolve(
    session: AsyncSession, org: FilesOrgFixture, chat_id: uuid.UUID, target: str
) -> ChatFile | None:
    path = chat_path(target, chat_id=str(chat_id))
    if path is None:
        return None
    ctx = ActingContext.for_user(user_id=org.org.admin_id, org_id=org.org.org_id, email="")
    return await resolve_chat_file(
        session, ctx=ctx, org_team_id=org.org.org_id, chat_id=chat_id, path=path
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("charts/responses.png", "charts/responses.png", id="relative-nested"),
        pytest.param("./plot_responses.py", "plot_responses.py", id="dot-slash"),
        pytest.param("my%20chart.png", "my chart.png", id="url-encoded-space"),
        pytest.param("records.md", "records.md", id="beside-the-working-folder"),
        pytest.param(
            "/opt/alkera-work/.alkera/chats/{chat}/scratch/charts/responses.png",
            "charts/responses.png",
            id="absolute-box-path",
        ),
    ],
)
async def test_a_reference_resolves_to_the_file_the_agent_wrote(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    target: str,
    expected: str,
) -> None:
    tree = await _tree(files_client, real_session, fx)
    found = await _resolve(real_session, files_org, tree.chat_id, target.format(chat=tree.chat_id))
    assert found is not None
    assert found.node_id == tree.nodes[expected]
    assert found.name == expected.rsplit("/", 1)[-1]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("charts/missing.png", id="nonexistent"),
        pytest.param("charts", id="a-folder-is-not-a-file"),
        pytest.param("../records.md", id="outside-the-folder"),
        pytest.param("/etc/passwd", id="system-path"),
    ],
)
async def test_a_reference_to_nothing_in_this_chat_resolves_to_nothing(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
    target: str,
) -> None:
    tree = await _tree(files_client, real_session, fx)
    assert await _resolve(real_session, files_org, tree.chat_id, target) is None


async def test_the_same_path_in_another_chat_is_never_this_chats_file(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """Two chats each hold ``charts/responses.png``; each resolves to its own,
    and a box path naming the other chat resolves to nothing."""
    first = await _tree(files_client, real_session, fx)
    second = await _tree(files_client, real_session, fx)
    mine = await _resolve(real_session, files_org, first.chat_id, "charts/responses.png")
    theirs = await _resolve(real_session, files_org, second.chat_id, "charts/responses.png")
    assert mine is not None and theirs is not None
    assert mine.node_id == first.nodes["charts/responses.png"]
    assert theirs.node_id == second.nodes["charts/responses.png"]
    crossing = f"/opt/alkera-work/.alkera/chats/{second.chat_id}/scratch/charts/responses.png"
    assert await _resolve(real_session, files_org, first.chat_id, crossing) is None


async def test_a_trashed_file_resolves_to_nothing(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    from datetime import UTC, datetime

    tree = await _tree(files_client, real_session, fx)
    node = await real_session.get(FileNode, tree.nodes["plot_responses.py"])
    assert node is not None
    node.trashed_at = datetime.now(UTC)
    await real_session.commit()
    assert await _resolve(real_session, files_org, tree.chat_id, "plot_responses.py") is None


async def test_a_chat_of_another_org_resolves_to_nothing(
    files_client: AsyncClient,
    real_session: AsyncSession,
    fx: FilesFixtures,
    files_org: FilesOrgFixture,
) -> None:
    """The repo is scoped to the org the caller names: another org's scope
    cannot see this chat's folder at all."""
    tree = await _tree(files_client, real_session, fx)
    path = chat_path("charts/responses.png", chat_id=str(tree.chat_id))
    assert path is not None
    stranger = uuid.uuid4()
    ctx = ActingContext.for_user(user_id=files_org.org.admin_id, org_id=stranger, email="")
    assert (
        await resolve_chat_file(
            real_session, ctx=ctx, org_team_id=stranger, chat_id=tree.chat_id, path=path
        )
        is None
    )
