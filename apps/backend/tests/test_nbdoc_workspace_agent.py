"""A chat's agent edits the notebooks of its own workspace, and no other's.

A chat in a workspace works in the workspace's shared ``files/`` tree, and the
box serving the workspace holds the workspace folder's lease. When the box
applies a batch the chat's agent made on a notebook there, the backend asks
whether the chat is in the workspace holding the notebook. The chat names its
workspace by the workspace object's id; the notebook is held by the workspace
folder, a Files node with an id of its own. These cases pin that the answer
is read in one id space: the chat's own workspace (the person's Main, or a
project) is admitted, and a chat of another workspace is refused.

Real Postgres, real Files, the real CRDT lane, the real route; the box is a
registered machine holding the workspace folder's lease.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core.authz.headers import agent_headers
from alkera_core.authz.principal import ActingContext
from alkera_core.files.content import ContentService
from alkera_core.files.ids import NodeId
from alkera_core.files.objects_bridge import CHAT_TYPE, WORKSPACE_TYPE
from alkera_core.models import WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.models import NotebookEdit, NotebookRun
from backend.authz import decide_on_record
from backend.services.files.context import build_files_context
from backend.services.notebooks import app as notebook_app
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, fastapi_app
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.test_nbdoc_real_format import FULL
from tests.crdt.test_nbdoc_session import WEEKLY
from tests.files._boxes import registered_box
from tests.files._files_kit import FilesFixtures
from tests.files._live_holder import MockHolder
from tests.test_nbdoc_routes import RecordingTransport

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

AGENT_SOURCE = "weekly = orders  # the agent's edit"


@dataclass(frozen=True)
class Workspace:
    """A native workspace laid out as the product lays it out: its folder
    holding ``files/`` and ``.chats/``, and one chat filed under ``.chats/``."""

    object_id: uuid.UUID
    folder_id: uuid.UUID
    files_id: uuid.UUID
    chat_id: uuid.UUID


@dataclass
class Rig:
    fx: FilesFixtures
    drive_id: uuid.UUID
    main: Workspace
    project: Workspace
    notebooks: dict[str, uuid.UUID]
    box: AsyncClient
    machine_id: str


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _object(db: AsyncSession, org: OrgWithAdmin, **columns: Any) -> uuid.UUID:
    row = WorkspaceObject(
        org_team_id=org.org_id,
        logical_id=uuid.uuid4().hex,
        owner_user_id=org.admin_id,
        visibility_scope="private",
        **columns,
    )
    db.add(row)
    await db.flush()
    return uuid.UUID(str(row.id))


async def _workspace(
    db: AsyncSession, org: OrgWithAdmin, fx: FilesFixtures, *, name: str, kind: str, machine: str
) -> Workspace:
    object_id = await _object(
        db, org, type="workspace", title=name, spec={"layout": "native", "kind": kind}
    )
    folder = await fx.node(
        f"{name}.alkeraworkspace".encode(),
        kind="folder",
        subtype=WORKSPACE_TYPE,
        target_object_id=object_id,
        parent=await fx.home(),
    )
    files = await fx.node(b"files", kind="folder", parent=folder)
    chats = await fx.node(b".chats", kind="folder", parent=folder)
    chat_id = await _object(
        db,
        org,
        type="chat",
        title=f"{name} chat",
        spec={"machine_id": machine, "workspace_id": str(object_id), "mirror_state": "awake"},
    )
    await fx.node(
        f"{name} chat.alkerachat".encode(),
        kind="folder",
        subtype=CHAT_TYPE,
        target_object_id=chat_id,
        parent=chats,
    )
    await db.commit()
    return Workspace(object_id, uuid.UUID(str(folder.id)), uuid.UUID(str(files.id)), chat_id)


async def _notebook(
    db: AsyncSession, org: OrgWithAdmin, fx: FilesFixtures, parent_id: uuid.UUID
) -> uuid.UUID:
    """A notebook in the shared tree, its bytes put the way any upload lands."""
    node = await fx.node(b"weekly.alknb.py", parent=await fx.folder(parent_id))
    ctx = ActingContext.for_user(user_id=org.admin_id, org_id=org.org_id, email=org.admin_email)
    context = await build_files_context(db, ctx)
    service = ContentService(context.repo, context.ctx, context.clock, context.store)
    data = FULL.encode()

    async def body() -> AsyncIterator[bytes]:
        yield data

    await db.refresh(node)
    await service.put_version(NodeId(node.id), body(), size_declared=len(data), if_match=node.etag)
    await db.commit()
    return uuid.UUID(str(node.id))


@pytest.fixture
async def rig(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Rig]:
    db = real_session
    fx = FilesFixtures(db, org_admin.org_id, org_admin.admin_id)
    drive = await fx.drive()
    token, machine_id = await registered_box(
        db, user_id=org_admin.admin_id, email=org_admin.admin_email, org_id=org_admin.org_id
    )
    await db.commit()
    main = await _workspace(db, org_admin, fx, name="Main", kind="main", machine=machine_id)
    project = await _workspace(
        db, org_admin, fx, name="Pricing", kind="project", machine=machine_id
    )
    notebooks = {
        "main": await _notebook(db, org_admin, fx, main.files_id),
        "project": await _notebook(db, org_admin, fx, project.files_id),
    }
    async with (
        AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
        ) as box,
        lane_docs() as docs,
    ):
        docs.run_sessions = False
        feed = NotebookFeed()
        service = NotebookService(
            transport=RecordingTransport(feed=feed),
            feed=feed,
            carets=CaretBoard(),
            decide=decide_on_record,
            docs=docs.notebooks,
        )
        setattr(fastapi_app.state, notebook_app.STATE_ATTR, service)
        try:
            yield Rig(fx, uuid.UUID(str(drive.id)), main, project, notebooks, box, machine_id)
        finally:
            setattr(fastapi_app.state, notebook_app.STATE_ATTR, None)


async def _hold(db: AsyncSession, rig: Rig, workspace: Workspace) -> MockHolder:
    holder = MockHolder(
        rig.box,
        rig.drive_id,
        workspace.folder_id,
        instance=f"{rig.machine_id}:ws:{workspace.object_id}",
        machine=rig.machine_id,
    )
    taken = await holder.take(db, _idem, purpose="workspace", inbound=True, live=True)
    assert taken.status_code == 200, taken.text
    return holder


async def _agent_ops(rig: Rig, holder: MockHolder, notebook: uuid.UUID, chat_id: uuid.UUID) -> Any:
    return await rig.box.post(
        f"/api/v1/notebooks/{rig.drive_id}/{notebook}/ops",
        json={
            "ops": [{"op": "replace", "cell_id": WEEKLY, "source": AGENT_SOURCE}],
            "submit_id": f"ws-agent-{uuid.uuid4().hex[:8]}",
            "agent_chat_id": str(chat_id),
        },
        headers=holder.fence,
    )


async def _edits(db: AsyncSession, notebook: uuid.UUID) -> list[NotebookEdit]:
    rows = await db.execute(select(NotebookEdit).where(NotebookEdit.item_id == notebook))
    return list(rows.scalars())


@pytest.mark.parametrize("where", ["main", "project"])
async def test_a_chat_s_agent_edits_a_notebook_in_its_own_workspace(
    real_session: AsyncSession, rig: Rig, org_admin: OrgWithAdmin, where: str
) -> None:
    """The workspace folder's node id is not the workspace's id: the chat is
    admitted because it names the workspace the folder stands for."""
    workspace = rig.main if where == "main" else rig.project
    folder = await real_session.get(FileNode, workspace.folder_id)
    assert folder is not None and folder.id != workspace.object_id
    holder = await _hold(real_session, rig, workspace)

    answer = await _agent_ops(rig, holder, rig.notebooks[where], workspace.chat_id)

    assert answer.status_code == 200, answer.text
    edits = await _edits(real_session, rig.notebooks[where])
    assert [(e.actor_key, e.actor_kind, e.user_id) for e in edits] == [
        (f"agent:{workspace.chat_id}", "agent", org_admin.admin_id)
    ]


@pytest.mark.parametrize("where", ["main", "project"])
async def test_a_chat_of_another_workspace_is_refused(
    real_session: AsyncSession, rig: Rig, where: str
) -> None:
    """Both chats are served by the same box, which holds the notebook's
    workspace: only the chat in that workspace speaks for its notebooks."""
    workspace, other = (rig.main, rig.project) if where == "main" else (rig.project, rig.main)
    holder = await _hold(real_session, rig, workspace)

    answer = await _agent_ops(rig, holder, rig.notebooks[where], other.chat_id)

    assert answer.status_code == 403, answer.text
    # The body also carries the error envelope, whose trace id differs per request.
    body = answer.json()
    assert (body["code"], body["message"]) == (
        "notebook.agent_chat_refused",
        "the chat is not in the notebook's workspace",
    )
    assert await _edits(real_session, rig.notebooks[where]) == []


async def _agent_run(rig: Rig, holder: MockHolder, notebook: uuid.UUID, chat_id: uuid.UUID) -> str:
    """The box reports a run the chat's agent asked for; its engine run id."""
    engine_id = f"run_{uuid.uuid4().hex[:16]}"
    posted = await rig.box.post(
        f"/api/v1/notebooks/{rig.drive_id}/{notebook}/events",
        json={
            "kernel_id": f"krn_{uuid.uuid4().hex[:12]}",
            "state": None,
            "events": [
                {
                    "seq": 1,
                    "type": "run.queued",
                    "run_id": engine_id,
                    "trigger": "run",
                    "position": 1,
                    "requested_by": {
                        "kind": "agent",
                        "id": f"agent:{chat_id}",
                        "display_name": "x",
                    },
                },
                {"seq": 2, "type": "run.finished", "run_id": engine_id, "status": "ok"},
            ],
        },
        headers=holder.fence,
    )
    assert posted.status_code == 200, posted.text
    return engine_id


@pytest.mark.parametrize(
    ("where", "own"),
    [
        pytest.param("main", True, id="main-own-chat"),
        pytest.param("project", True, id="project-own-chat"),
        pytest.param("main", False, id="main-another-workspace-s-chat"),
    ],
)
async def test_an_agent_s_run_in_a_workspace_notebook_is_recorded_as_the_agent_for_its_person(
    real_session: AsyncSession, rig: Rig, org_admin: OrgWithAdmin, where: str, own: bool
) -> None:
    """A run is attributed through the same workspace check an edit is, so
    the agent of the workspace's own chat is on record acting for its person,
    and a chat of another workspace is not spoken for."""
    workspace, other = (rig.main, rig.project) if where == "main" else (rig.project, rig.main)
    chat = workspace.chat_id if own else other.chat_id
    holder = await _hold(real_session, rig, workspace)

    engine_id = await _agent_run(rig, holder, rig.notebooks[where], chat)

    real_session.expire_all()
    run = (
        await real_session.execute(
            select(NotebookRun).where(NotebookRun.engine_run_id == engine_id)
        )
    ).scalar_one()
    expected = ("agent", org_admin.admin_id, str(chat)) if own else ("system", None, None)
    assert (run.actor_kind, run.requested_by_user_id, run.requested_by_agent) == expected
