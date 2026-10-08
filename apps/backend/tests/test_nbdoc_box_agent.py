"""An agent's notebook edit made on the box reaches the file, as its person.

The engine runs on the box that holds the chat's folder, so the agent's
notebook tools edit the live document through the box: the box applies the
batch under the fence of its lease, naming the chat whose agent made it. The
batch is then the agent's, written for the chat's person, and the write back
of the document to the ``.alknb.py`` file is attributed to that person. A
batch the box applies without naming a chat has no person behind it, and the
write back has nobody to save as.

Real Postgres, real Files, the real CRDT lane and its sandbox workers, the
real route; the box is a registered machine holding the folder's lease.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core import brand
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.models import CrdtUpdate, User, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.models import NotebookEdit
from alkera_core.schemas.realtime import LiveTextRequest
from alkera_core.schemas.realtime.machine import NotebookMachineRequest, parse_machine_request
from backend.authz import decide_on_record
from backend.services.chats import chat_service
from backend.services.crdt.docs import CrdtDocs
from backend.services.crdt.switch import write_back_org
from backend.services.notebooks import app as notebook_app
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from backend.services.org import settings as org_settings_service
from backend.services.workspaces import workspace_service
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import FileWorld, drive_text, head_version
from tests.crdt.test_nbdoc_real_format import FULL
from tests.crdt.test_nbdoc_session import WEEKLY, notebook_world
from tests.test_nbdoc_routes import Box, RecordingTransport, _holder, _notebook_events

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

AGENT_SOURCE = "weekly = orders  # the agent's edit"


@dataclass
class Live:
    fw: FileWorld
    docs: CrdtDocs
    drive_id: uuid.UUID

    @property
    def chat_id(self) -> str:
        return self.fw.world.ref.doc_id

    def url(self, tail: str = "") -> str:
        return f"/api/v1/notebooks/{self.drive_id}/{self.fw.node_id}{tail}"


async def _live(
    db: AsyncSession,
    org: OrgWithAdmin,
    *,
    sessions: bool,
    workspace: WorkspaceObject | None = None,
) -> AsyncIterator[Live]:
    from tests.conftest import fastapi_app as app

    fw = await notebook_world(db, org, FULL, workspace=workspace)
    node = await db.get(FileNode, fw.node_id)
    assert node is not None
    async with lane_docs() as docs:
        docs.run_sessions = sessions
        feed = NotebookFeed()
        service = NotebookService(
            transport=RecordingTransport(feed=feed),
            feed=feed,
            carets=CaretBoard(),
            decide=decide_on_record,
            docs=docs.notebooks,
        )
        setattr(app.state, notebook_app.STATE_ATTR, service)
        try:
            yield Live(fw, docs, uuid.UUID(str(node.drive_id)))
        finally:
            setattr(app.state, notebook_app.STATE_ATTR, None)


@pytest.fixture
async def live(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Live]:
    """The lane with its sessions held still: each write back is run here."""
    async for found in _live(real_session, org_admin, sessions=False):
        yield found


async def _project(db: AsyncSession, org: OrgWithAdmin, title: str) -> WorkspaceObject:
    owner = await db.get(User, org.admin_id)
    assert owner is not None
    workspace, _made = await workspace_service.create_project(
        db, owner=owner, org_id=org.org_id, title=title, client_id=None
    )
    await db.commit()
    return workspace


@pytest.fixture
async def live_in_workspace(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> AsyncIterator[Live]:
    """The lane, its notebook in a chat started inside a project workspace."""
    workspace = await _project(real_session, org_admin, "Analysis")
    async for found in _live(real_session, org_admin, sessions=False, workspace=workspace):
        yield found


@pytest.fixture
async def live_sessions(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Live]:
    """The lane running its sessions, as it does in the backend."""
    async for found in _live(real_session, org_admin, sessions=True):
        yield found


async def _bind(db: AsyncSession, chat_id: str, machine_id: str | None) -> None:
    """Bind the chat to ``machine_id`` the way placement does: its spec names it."""
    chat = await db.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    spec = dict(chat.spec or {})
    spec["machine_id"] = machine_id
    await db.execute(update(WorkspaceObject).where(WorkspaceObject.id == chat.id).values(spec=spec))
    await db.commit()


async def _box_for_chat(db: AsyncSession, client: AsyncClient, live: Live) -> Box:
    box = await _holder(db, client, live.fw)
    await _bind(db, live.chat_id, box.machine_id)
    return box


def _agent_batch(chat_id: str | None, submit: str = "box-agent-0001") -> dict[str, Any]:
    body: dict[str, Any] = {
        "ops": [{"op": "replace", "cell_id": WEEKLY, "source": AGENT_SOURCE}],
        "submit_id": submit,
    }
    if chat_id is not None:
        body["agent_chat_id"] = chat_id
    return body


async def _edits(db: AsyncSession, live: Live) -> list[NotebookEdit]:
    rows = await db.execute(
        select(NotebookEdit)
        .where(NotebookEdit.item_id == live.fw.node_id)
        .order_by(NotebookEdit.id)
    )
    return list(rows.scalars())


async def _authors(db: AsyncSession, live: Live) -> set[uuid.UUID | None]:
    rows = await db.execute(
        select(CrdtUpdate.author_user_id).where(
            CrdtUpdate.doc_type == "notebook", CrdtUpdate.doc_id == live.fw.ref.doc_id
        )
    )
    return set(rows.scalars())


async def test_an_agent_only_edit_is_written_back_to_the_file_as_the_person_it_acts_for(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """Only the agent edited (through the box): the batch is recorded as the
    chat's agent acting for its owner, and the write back lands on the drive
    as a version the owner wrote, holding the agent's edit."""
    owner = live.fw.world.owner.user
    box = await _box_for_chat(real_session, client, live)
    answer = await box.client.post(
        live.url("/ops"), json=_agent_batch(live.chat_id), headers=box.fence
    )
    assert answer.status_code == 200, answer.text

    edits = await _edits(real_session, live)
    assert [(e.actor_key, e.actor_kind, e.user_id, e.agent_id) for e in edits] == [
        (f"agent:{live.chat_id}", "agent", owner.id, live.chat_id)
    ]
    assert (
        edits[0].actor_display == f"{brand.agent_name()} for {owner.first_name} {owner.last_name}"
    )
    assert await _authors(real_session, live) == {owner.id}

    settled = await live.docs.sessions.write_back(real_session, live.fw.ref)
    await real_session.commit()
    assert settled.outcome == "written"
    assert AGENT_SOURCE in await drive_text(real_session, live.fw)
    assert (await head_version(real_session, live.fw)).created_by == owner.id


async def test_a_box_batch_that_names_no_chat_is_saved_as_the_box(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """The same edit applied as the bare machine is the machine's: no person
    in the session wrote it, so the write back has nobody to name. It is
    saved as the box that holds the folder, under its fence, exactly as the
    box's own upload of the file would land (the write back's holder
    fallback), stamped with its machine."""
    box = await _box_for_chat(real_session, client, live)
    answer = await box.client.post(live.url("/ops"), json=_agent_batch(None), headers=box.fence)
    assert answer.status_code == 200, answer.text
    (edit,) = await _edits(real_session, live)
    assert (edit.actor_key, edit.user_id) == (f"machine:{box.machine_id}", None)
    assert await _authors(real_session, live) == {None}

    settled = await live.docs.sessions.write_back(real_session, live.fw.ref)
    await real_session.commit()
    assert settled.outcome == "written"
    assert AGENT_SOURCE in await drive_text(real_session, live.fw)
    version = await head_version(real_session, live.fw)
    assert (version.version_metadata or {}).get("machine_id") == box.machine_id
    assert version.source == "document_snapshot"


async def test_a_bare_box_batch_is_not_saved_once_the_box_lets_go_of_the_folder(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """The holder's write back is the box's own upload, made only under a lease
    it holds now: once the box has let go of the folder, its bare batch has
    nobody to save as and saving is refused, the drive untouched."""
    box = await _box_for_chat(real_session, client, live)
    answer = await box.client.post(live.url("/ops"), json=_agent_batch(None), headers=box.fence)
    assert answer.status_code == 200, answer.text
    await real_session.execute(
        text("UPDATE file_leases SET released_at = now() WHERE node_id = :node"),
        {"node": box.holder.node_id},
    )
    await real_session.commit()

    settled = await live.docs.sessions.write_back(real_session, live.fw.ref)
    await real_session.commit()
    assert settled.outcome == "refused"
    assert AGENT_SOURCE not in await drive_text(real_session, live.fw)


async def test_an_agent_edits_a_notebook_in_its_chats_workspace(
    real_session: AsyncSession, live_in_workspace: Live, client: AsyncClient
) -> None:
    """A chat started in a workspace belongs to it: its agent's batch on a
    notebook the workspace holds is the agent's, written for the chat's
    person."""
    live = live_in_workspace
    owner = live.fw.world.owner.user
    box = await _box_for_chat(real_session, client, live)
    answer = await box.client.post(
        live.url("/ops"), json=_agent_batch(live.chat_id), headers=box.fence
    )
    assert answer.status_code == 200, answer.text
    edits = await _edits(real_session, live)
    assert [(e.actor_key, e.actor_kind, e.user_id) for e in edits] == [
        (f"agent:{live.chat_id}", "agent", owner.id)
    ]


async def test_a_chat_of_another_workspace_cannot_write_the_notebook(
    real_session: AsyncSession,
    live_in_workspace: Live,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
) -> None:
    """The same box serves a chat started in a second workspace: naming that
    chat for a notebook the first workspace holds is refused whole."""
    live = live_in_workspace
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    box = await _box_for_chat(real_session, client, live)
    elsewhere = await _project(real_session, org_admin, "Elsewhere")
    other, _made = await chat_service.create_chat(
        real_session,
        owner=owner,
        title="Another",
        client_id=None,
        machine_id=box.machine_id,
        machine_status="ready",
        org_id=org_admin.org_id,
        workspace=elsewhere,
    )
    await real_session.commit()
    answer = await box.client.post(
        live.url("/ops"), json=_agent_batch(str(other.id)), headers=box.fence
    )
    assert answer.status_code == 403, answer.text
    assert answer.json()["code"] == "notebook.agent_chat_refused"
    assert await _edits(real_session, live) == []


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("bound-elsewhere", id="a-chat-another-machine-serves"),
        pytest.param("unbound", id="a-chat-no-machine-serves"),
        pytest.param("unknown", id="a-chat-that-does-not-exist"),
        pytest.param("deleted", id="a-deleted-chat"),
        pytest.param("not-a-chat", id="an-id-that-is-not-a-chat"),
    ],
)
async def test_a_box_cannot_write_for_a_chat_it_does_not_serve(
    real_session: AsyncSession, live: Live, client: AsyncClient, case: str
) -> None:
    """The chat a box names must be one bound to that very box; any other
    claim is refused whole (403) and writes nothing, never downgraded to the
    machine's own edit."""
    box = await _holder(real_session, client, live.fw)
    chat_id = live.chat_id
    if case == "bound-elsewhere":
        await _bind(real_session, chat_id, str(uuid.uuid4()))
    elif case == "unbound":
        await _bind(real_session, chat_id, None)
    elif case == "unknown":
        chat_id = str(uuid.uuid4())
    elif case == "deleted":
        await _bind(real_session, chat_id, box.machine_id)
        await real_session.execute(
            update(WorkspaceObject)
            .where(WorkspaceObject.id == uuid.UUID(chat_id))
            .values(deleted_at=1.0)
        )
        await real_session.commit()
    elif case == "not-a-chat":
        chat_id = str(live.fw.node_id)
    answer = await box.client.post(live.url("/ops"), json=_agent_batch(chat_id), headers=box.fence)
    assert answer.status_code == 403, answer.text
    assert answer.json()["code"] == "notebook.agent_chat_refused"
    assert await _edits(real_session, live) == []
    view = (await box.client.get(live.url())).json()
    assert AGENT_SOURCE not in str(view["cells"])


async def test_a_person_cannot_claim_to_be_a_chat_s_agent(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """Only the folder's holder names the chat it writes for: a person who
    may edit the notebook naming a chat is refused, not recorded as that
    chat's agent."""
    await _box_for_chat(real_session, client, live)
    writer = live.fw.world.writer
    await login(client, writer.user.email, writer.password)
    answer = await client.post(live.url("/ops"), json=_agent_batch(live.chat_id))
    assert answer.status_code == 403, answer.text
    assert answer.json()["code"] == "notebook.agent_chat_refused"
    assert await _edits(real_session, live) == []


async def _live_editing(live: Live, enabled: bool | None) -> None:
    async with AsyncSessionLocal() as db:
        await org_settings_service.set_live_editing(db, live.fw.world.org_id, enabled)
        await db.commit()
    live.docs.switch.forget(live.fw.world.org_id)


async def test_while_live_editing_is_off_no_batch_reaches_the_notebook(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """The switch decides a notebook as it decides a file: while live editing
    is off for the org, an editor cannot open the document, and the box's
    batch for its chat's agent is refused the same way (``crdt_unsupported``,
    told live editing is off), nothing recorded and nothing on the drive.
    Switched back on, the same batch lands."""
    box = await _box_for_chat(real_session, client, live)
    await _live_editing(live, False)
    refused = await box.client.post(
        live.url("/ops"), json=_agent_batch(live.chat_id), headers=box.fence
    )
    assert refused.status_code == 503, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["message"]) == ("crdt_unsupported", "live editing is off")
    assert await _edits(real_session, live) == []
    assert await _authors(real_session, live) == set()

    await _live_editing(live, None)
    answer = await box.client.post(
        live.url("/ops"),
        json=_agent_batch(live.chat_id, submit="box-agent-0002"),
        headers=box.fence,
    )
    assert answer.status_code == 200, answer.text


async def test_switching_live_editing_off_writes_the_notebook_back_first(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """What switching off owes the people who edited a notebook: its unsaved
    edits are written to the ``.alknb.py`` file at once, as the person the
    agent acted for, before anything stops serving it."""
    owner = live.fw.world.owner.user
    box = await _box_for_chat(real_session, client, live)
    answer = await box.client.post(
        live.url("/ops"), json=_agent_batch(live.chat_id), headers=box.fence
    )
    assert answer.status_code == 200, answer.text
    assert AGENT_SOURCE not in await drive_text(real_session, live.fw)

    done = await write_back_org(live.docs, live.fw.world.org_id)
    assert (done.written, done.left) == (1, 0)
    assert AGENT_SOURCE in await drive_text(real_session, live.fw)
    assert (await head_version(real_session, live.fw)).created_by == owner.id


async def test_the_box_is_told_its_own_batch_moved_the_document(
    real_session: AsyncSession, live_sessions: Live, client: AsyncClient
) -> None:
    """The box never read the notebook as text (it edits through operations),
    yet it alone writes the file while it holds the folder: its own batch
    tells it, on its machine channel, which node moved and to what token."""
    live = live_sessions
    published: list[HubEvent] = []

    async def publish(event: HubEvent) -> None:
        published.append(event)

    live.docs.peers.publish = publish
    live.docs.peers.notify_delay = 0.0
    box = await _box_for_chat(real_session, client, live)
    answer = await box.client.post(
        live.url("/ops"), json=_agent_batch(live.chat_id), headers=box.fence
    )
    assert answer.status_code == 200, answer.text
    for _ in range(200):
        if published:
            break
        await asyncio.sleep(0.02)
    notices = [LiveTextRequest.model_validate(e.payload) for e in published]
    assert [e.channel for e in published][:1] == [f"machine:{box.machine_id}"]
    assert notices[0].node_id == live.fw.node_id
    assert notices[0].token == answer.json()["token"]


async def test_a_request_to_the_box_names_where_the_notebook_is_under_its_lease(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    """What the backend asks of the box carries the leased folder and the
    notebook's path under it, and that pair is exactly how the box finds the
    notebook through the Files API (the step down from the folder it holds)."""
    from backend.services.notebooks.service import NotebookService as Service
    from backend.services.notebooks.transport import MachineChannelTransport

    box = await _box_for_chat(real_session, client, live)
    owner = live.fw.world.owner
    await login(client, owner.user.email, owner.password)
    answer = await client.post(live.url("/kernel"), json={"action": "interrupt"})
    assert answer.status_code == 200, answer.text
    from tests.conftest import fastapi_app as app

    service = getattr(app.state, notebook_app.STATE_ATTR)
    assert isinstance(service, Service)
    transport = service.transport
    assert isinstance(transport, RecordingTransport)
    ((host, op, _body),) = transport.sent
    assert op == "kernel"
    assert host.lease_node_id == uuid.UUID(str(box.holder.node_id))
    assert host.path is not None and host.path.endswith("weekly.alknb.py")
    found = await box.client.get(
        f"/api/v1/files/drives/{live.drive_id}/items/{host.lease_node_id}:/{host.path}",
        headers=box.fence,
    )
    assert found.status_code == 200, found.text
    assert found.json()["id"] == str(live.fw.node_id)

    # The machine channel carries both to the box, in the frame it parses.
    request_id = await MachineChannelTransport().send(host, "kernel", {"action": "interrupt"})
    rows = await _notebook_events(live.fw.world.org_id)
    (row,) = [r for r in rows if r.payload.get("request_id") == str(request_id)]
    frame = parse_machine_request(row.payload)
    assert isinstance(frame, NotebookMachineRequest)
    assert (frame.lease_node_id, frame.path) == (host.lease_node_id, host.path)
