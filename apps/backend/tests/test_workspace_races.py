"""Concurrent and repeated requests against workspaces: one outcome each.

Deleting a workspace serializes against filing a chat into it, so a chat is
either ended with the workspace or refused, never left alive in a folder that
went to the trash. Concurrent creates with one ``client_id`` all get the one
object it made, and a replay that differs from the request that made it is
told so. Titles are stored as they read: trimmed, never empty, and free of
the controls that make a folder name read as something else.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.schemas.objects.workspace_api import CONTROL_IN_TITLE
from backend.services import workspaces
from backend.services.chats import chat_service
from httpx import AsyncClient
from sqlalchemy import text
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

WORKSPACES = "/api/v1/workspaces"
CHATS = "/api/v1/chats"


@contextmanager
def projects_allowed() -> Iterator[None]:
    """Project workspaces are refused while a workspace holds one chat; a test
    that needs one as a fixture makes it with the flag on for that request."""
    previous = settings.workspaces_multi_chat
    settings.workspaces_multi_chat = True
    try:
        yield
    finally:
        settings.workspaces_multi_chat = previous


@pytest.fixture
def multi_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    yield


async def _waiting_on_a_lock(deadline_s: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    while loop.time() < end:
        async with AsyncSessionLocal() as db:
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    pytest.fail("the second request never queued behind the first")


async def _project(client: AsyncClient, title: str = "Pricing") -> dict[str, Any]:
    with projects_allowed():
        made = await client.post(WORKSPACES, json={"title": title})
    assert made.status_code == 201, made.text
    body: dict[str, Any] = made.json()
    return body


async def _chat_in(db: Any, owner: User, workspace: WorkspaceObject) -> WorkspaceObject:
    chat, _ = await chat_service.create_chat(
        db,
        owner=owner,
        org_id=owner.home_org_team_id,
        title="Racing",
        client_id=None,
        machine_id=None,
        machine_status="none",
        workspace=workspace,
    )
    return chat


@pytest.mark.usefixtures("multi_chat")
async def test_a_chat_filed_before_the_delete_commits_is_ended_with_the_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client)

    async with AsyncSessionLocal() as filing, AsyncSessionLocal() as ending:
        owner = await filing.get(User, org_admin.admin_id)
        workspace = await filing.get(WorkspaceObject, UUID(made["id"]))
        to_end = await ending.get(WorkspaceObject, UUID(made["id"]))
        assert owner is not None and workspace is not None and to_end is not None
        chat = await _chat_in(filing, owner, workspace)

        ended = asyncio.create_task(workspaces.end(ending, workspace=to_end, actor=None))
        await _waiting_on_a_lock()
        await filing.commit()
        gone = await asyncio.wait_for(ended, 10.0)
        await ending.commit()

    assert gone is not None
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, chat.id)
        assert row is not None and row.deleted_at != 0, "the chat ended with its workspace"


@pytest.mark.usefixtures("multi_chat")
async def test_a_box_report_racing_a_workspace_delete_never_deadlocks(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A box's publisher-state report locks the chat and then its workspace;
    deleting the workspace locks the workspace and then each chat. Caught
    between the two, the report skips its sandbox note instead of waiting,
    so neither request is chosen as a deadlock victim: the report commits,
    and the delete then ends the chat with its workspace."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client, "Busy")
    machine = str(uuid.uuid4())
    async with AsyncSessionLocal() as setup:
        owner = await setup.get(User, org_admin.admin_id)
        workspace = await setup.get(WorkspaceObject, UUID(made["id"]))
        assert owner is not None and workspace is not None
        chat = await _chat_in(setup, owner, workspace)
        await setup.execute(
            text(
                "UPDATE workspace_objects SET spec = spec || "
                "jsonb_build_object('machine_id', CAST(:m AS text)) WHERE id = :id"
            ),
            {"m": machine, "id": chat.id},
        )
        await setup.commit()
    ctx = ActingContext.for_user(
        user_id=org_admin.admin_id, org_id=org_admin.org_id, email=org_admin.admin_email
    )
    report = SimpleNamespace(workspace_sandbox="awake", workspace_memory_mb=None)

    async with AsyncSessionLocal() as reporting, AsyncSessionLocal() as ending:
        reported = await reporting.get(WorkspaceObject, chat.id)
        to_end = await ending.get(WorkspaceObject, UUID(made["id"]))
        assert reported is not None and to_end is not None
        reported = await chat_service.set_publisher_state(
            reporting, chat=reported, state="publishing"
        )
        ended = asyncio.create_task(workspaces.end(ending, workspace=to_end, actor=None))
        await _waiting_on_a_lock()
        written = await asyncio.wait_for(
            workspaces.record_sandbox_report(reporting, chat=reported, ctx=ctx, report=report),
            10.0,
        )
        await reporting.commit()
        gone = await asyncio.wait_for(ended, 10.0)
        await ending.commit()

    assert written is None, "the report waited on the workspace the delete holds"
    assert gone is not None
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, chat.id)
        assert row is not None and row.deleted_at != 0, "the chat ended with its workspace"


@pytest.mark.usefixtures("multi_chat")
async def test_a_chat_filed_after_the_delete_took_the_workspace_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client)

    async with AsyncSessionLocal() as filing, AsyncSessionLocal() as ending:
        owner = await filing.get(User, org_admin.admin_id)
        workspace = await filing.get(WorkspaceObject, UUID(made["id"]))
        to_end = await ending.get(WorkspaceObject, UUID(made["id"]))
        assert owner is not None and workspace is not None and to_end is not None
        assert await workspaces.end(ending, workspace=to_end, actor=None) is not None

        filed = asyncio.create_task(_chat_in(filing, owner, workspace))
        await _waiting_on_a_lock()
        await ending.commit()
        with pytest.raises(workspaces.WorkspaceGoneError):
            await asyncio.wait_for(filed, 10.0)
        await filing.rollback()


@pytest.mark.usefixtures("multi_chat")
async def test_a_second_delete_of_the_same_workspace_ends_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client)

    first, second = await asyncio.gather(
        client.delete(f"{WORKSPACES}/{made['id']}"), client.delete(f"{WORKSPACES}/{made['id']}")
    )

    assert sorted([first.status_code, second.status_code]) == [204, 404]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(WORKSPACES, {"title": "Race"}, id="workspaces"),
        pytest.param(CHATS, {"title": "Race"}, id="chats"),
    ],
)
async def test_concurrent_creates_with_one_client_id_all_get_the_one_object(
    client: AsyncClient, org_admin: OrgWithAdmin, path: str, body: dict[str, Any]
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"race-{uuid.uuid4()}"

    with projects_allowed():
        answers = await asyncio.gather(
            *(client.post(path, json={**body, "client_id": client_id}) for _ in range(6))
        )

    assert [answer.status_code for answer in answers] == [201] * 6, [a.text for a in answers]
    assert len({answer.json()["id"] for answer in answers}) == 1


@pytest.mark.usefixtures("multi_chat")
async def test_a_chat_replay_naming_another_workspace_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first, second = await _project(client, "A"), await _project(client, "B")
    client_id = f"chat-{uuid.uuid4()}"
    made = await client.post(CHATS, json={"client_id": client_id, "workspace_id": first["id"]})
    assert made.status_code == 201, made.text

    again = await client.post(CHATS, json={"client_id": client_id, "workspace_id": first["id"]})
    elsewhere = await client.post(
        CHATS, json={"client_id": client_id, "workspace_id": second["id"]}
    )

    assert again.status_code == 201 and again.json()["id"] == made.json()["id"]
    assert elsewhere.status_code == 409, elsewhere.text
    assert elsewhere.json()["error"]["code"] == "client_id_in_use"


async def test_a_workspace_replay_with_another_title_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"ws-{uuid.uuid4()}"
    with projects_allowed():
        made = await client.post(WORKSPACES, json={"title": "Pricing", "client_id": client_id})
        assert made.status_code == 201, made.text
        again = await client.post(WORKSPACES, json={"title": "Pricing", "client_id": client_id})
        other = await client.post(WORKSPACES, json={"title": "Churn", "client_id": client_id})

    assert again.status_code == 201 and again.json()["id"] == made.json()["id"]
    assert other.status_code == 409, other.text
    assert other.json()["error"]["code"] == "client_id_in_use"


@pytest.mark.parametrize("title", ["   ", "‮", " ⁦⁩ "], ids=["blank", "rlo", "isolates"])
async def test_a_title_with_nothing_visible_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, title: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    with projects_allowed():
        refused = await client.post(WORKSPACES, json={"title": title})
    assert refused.status_code == 422, refused.text


async def test_a_title_is_stored_trimmed_and_without_bidi_controls(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client, "  ‮gnp.exe ")

    assert made["title"] == "gnp.exe"
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, UUID(made["files_node_id"]))
    assert node is not None
    assert "‮" not in bytes(node.name).decode()
    assert bytes(node.name).decode().startswith("gnp.exe")


_CONTROL_TITLES = [
    pytest.param("Q3\tplan", id="tab"),
    pytest.param("Q3\nplan", id="newline"),
    pytest.param("Q3 plan\n", id="a-trailing-newline"),
    pytest.param("Q3\rplan", id="carriage-return"),
    pytest.param("Q3\x7fplan", id="delete"),
    pytest.param("Q3\x01plan", id="c0-control"),
    pytest.param("Q3\x85plan", id="c1-next-line"),
]


@pytest.mark.parametrize("title", _CONTROL_TITLES)
async def test_a_title_carrying_a_control_character_is_refused_on_create(
    client: AsyncClient, org_admin: OrgWithAdmin, title: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    with projects_allowed():
        refused = await client.post(WORKSPACES, json={"title": title})
    assert refused.status_code == 422, refused.text
    assert CONTROL_IN_TITLE in refused.text


@pytest.mark.parametrize("title", _CONTROL_TITLES)
async def test_a_rename_to_a_title_carrying_a_control_character_changes_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, title: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client, "Pricing")

    refused = await client.patch(
        f"{WORKSPACES}/{made['id']}", json={"title": title, "expected_version": made["version"]}
    )

    assert refused.status_code == 422, refused.text
    assert CONTROL_IN_TITLE in refused.text
    after = await client.get(f"{WORKSPACES}/{made['id']}")
    assert after.json()["title"] == "Pricing"


async def test_a_title_with_inner_spaces_and_right_to_left_text_is_kept(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The negative twin: only control characters are refused."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client, "Q3  plan مرحبا")
    assert made["title"] == "Q3  plan مرحبا"


@pytest.mark.usefixtures("multi_chat")
async def test_a_workspaces_chats_are_read_a_bounded_page_at_a_time(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made, other = await _project(client, "Paged"), await _project(client, "Other")
    started = []
    for n in range(3):
        chat = await client.post(CHATS, json={"title": f"c{n}", "workspace_id": made["id"]})
        assert chat.status_code == 201, chat.text
        started.append(chat.json()["id"])
    path = f"{WORKSPACES}/{made['id']}/chats"

    first = (await client.get(path, params={"limit": 2})).json()
    second = (await client.get(path, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    foreign = await client.get(
        f"{WORKSPACES}/{other['id']}/chats", params={"cursor": first["next_cursor"]}
    )

    assert [item["id"] for item in first["items"]] == started[:2]
    assert [item["id"] for item in second["items"]] == started[2:]
    assert second["next_cursor"] is None
    assert foreign.status_code == 422, "a cursor from another workspace's listing is refused"


async def test_a_workspace_of_one_whose_chat_an_older_build_ended_is_not_listed(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    chat = (await client.post(CHATS, json={"title": "Ended elsewhere"})).json()
    async with AsyncSessionLocal() as db:
        # The previous build's delete: the chat row tombstoned, its workspace left.
        await db.execute(
            text("UPDATE workspace_objects SET deleted_at = 1 WHERE id = :id"),
            {"id": UUID(chat["id"])},
        )
        await db.commit()

    listed = (await client.get(WORKSPACES)).json()["items"]

    assert chat["workspace_id"] not in [item["id"] for item in listed]


async def test_a_client_id_a_deleted_workspace_held_is_retired(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"ws-{uuid.uuid4()}"
    with projects_allowed():
        made = await client.post(WORKSPACES, json={"title": "Once", "client_id": client_id})
        assert (await client.delete(f"{WORKSPACES}/{made.json()['id']}")).status_code == 204
        again = await client.post(WORKSPACES, json={"title": "Once", "client_id": client_id})

    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "client_id_retired"


async def _trash(node_id: str, *, purge: bool = False) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        if purge:
            # What the purge window leaves: the folder, and everything in it, gone.
            await db.execute(
                text(
                    "UPDATE file_nodes SET trashed_at = now(), target_object_id = NULL, "
                    "subtype = NULL WHERE id = :id OR path_ids ~ CAST(:below AS lquery)"
                ),
                {"id": UUID(node_id), "below": f"*.{node_id.replace('-', '_')}.*"},
            )
        else:
            await db.execute(
                text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"),
                {"id": UUID(node_id)},
            )
        await db.commit()


@pytest.mark.usefixtures("multi_chat")
@pytest.mark.parametrize("purge", [False, True], ids=["trashed", "purged"])
async def test_a_main_workspace_whose_folder_went_is_given_a_new_one(
    client: AsyncClient, org_admin: OrgWithAdmin, purge: bool
) -> None:
    """The main workspace is where a chat lands when nobody named a place, and
    it cannot be deleted: a folder of it that was trashed, or purged after the
    trash window, is made again rather than every new chat being refused."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    main = (await client.get(f"{WORKSPACES}/main")).json()
    await _trash(main["files_node_id"], purge=purge)

    started = await client.post(CHATS, json={"title": "Has a home"})
    again = (await client.get(f"{WORKSPACES}/main")).json()

    assert started.status_code == 201, started.text
    assert started.json()["workspace_id"] == main["id"]
    assert again["id"] == main["id"]
    assert again["files_node_id"] not in (None, main["files_node_id"])
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        chat_node = await db.get(FileNode, UUID(started.json()["files_node_id"]))
        assert chat_node is not None
        records = await db.get(FileNode, chat_node.parent_id)
        assert records is not None and bytes(records.name) == b".chats"
        assert str(records.parent_id) == again["files_node_id"]


@pytest.mark.usefixtures("multi_chat")
async def test_a_main_workspace_whose_records_folder_was_trashed_gets_it_back(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    main = (await client.get(f"{WORKSPACES}/main")).json()
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        records = (
            await db.execute(
                text("SELECT id FROM file_nodes WHERE parent_id = :p AND name = '.chats'"),
                {"p": UUID(main["files_node_id"])},
            )
        ).scalar_one()
    await _trash(str(records))

    started = await client.post(CHATS, json={"title": "Filed again"})

    assert started.status_code == 201, started.text
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        chat_node = await db.get(FileNode, UUID(started.json()["files_node_id"]))
        assert chat_node is not None
        parent = await db.get(FileNode, chat_node.parent_id)
        assert parent is not None and parent.id != records and bytes(parent.name) == b".chats"
        assert str(parent.parent_id) == main["files_node_id"]


@pytest.mark.usefixtures("multi_chat")
async def test_a_chat_has_nowhere_to_go_while_a_project_workspaces_folder_is_trashed(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A project workspace can be deleted, so its trashed folder is the
    owner's to restore; nothing makes it again behind them."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client, "Shelved")
    await _trash(made["files_node_id"])

    refused = await client.post(CHATS, json={"title": "Homeless", "workspace_id": made["id"]})

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "workspace_folder_missing"


async def test_the_objects_route_does_not_rename_a_workspace(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A workspace of one is renamed with its chat, on the workspace route; the
    generic objects route refuses rather than renaming half of it."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    made = await _project(client, "Named")

    refused = await client.put(
        f"/api/v1/objects/{made['id']}", json={"title": "Renamed", "expected_version": 1}
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "title_not_editable_here"
    assert (await client.get(f"{WORKSPACES}/{made['id']}")).json()["title"] == "Named"
