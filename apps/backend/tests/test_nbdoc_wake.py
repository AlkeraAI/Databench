"""A notebook whose chat sleeps wakes it, the way opening the chat does.

A notebook in a chat's folder runs on the box that holds the folder, and a box
holds it only while it serves the chat. When the chat sleeps (it handed its
lease back, or its box restarted and has not taken it again), a request that
needs the kernel wakes the chat through the same wake opening the chat asks
for and is answered ``notebook.waking`` with the wait, so the notebook can
ask again once the box holds the folder; nothing reaches a box and no run is
recorded meanwhile. Only someone who may run the notebook wakes it, and only
through a chat they may send in: anyone else gets the answer they always got,
and nothing is woken.

Real Postgres, real Files, the real routes and the real chat wake; the box is
a registered machine with a grant, beating; the transport records what would
reach it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from alkera_core.authz import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.models import NotebookRun
from alkera_core.schemas.realtime.machine import NOTEBOOK_FOLDER_NOT_HELD
from backend.authz import decide_on_record
from backend.services.notebooks import app as notebook_app
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, fastapi_app, login, mint_cli_token
from tests.crdt.file_world import FileWorld, file_world
from tests.files._live_holder import MockHolder
from tests.test_nbdoc_routes import NOTEBOOK, FakeDocs, RecordingTransport

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

WAKING = "notebook.waking"


@dataclass
class Rig:
    fw: FileWorld
    transport: RecordingTransport
    drive_id: uuid.UUID
    machine_id: str
    box: AsyncClient

    @property
    def chat_id(self) -> uuid.UUID:
        return uuid.UUID(self.fw.world.ref.doc_id)

    def url(self, tail: str = "") -> str:
        return f"/api/v1/notebooks/{self.drive_id}/{self.fw.node_id}{tail}"


async def _ready_machine(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session: AsyncSession
) -> tuple[str, dict[str, str]]:
    """A box registered for the org under a grant, beating: ready."""
    from tests._compute_helpers import make_grant, make_machine_type

    machine_type = await make_machine_type(real_session)
    await make_grant(real_session, org_team_id=org_admin.org_id, machine_type_id=machine_type.id)
    token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    registered = await client.post(
        "/api/v1/machines/register",
        json={
            "provider": "runpod",
            "provider_pod_id": f"nb-wake-{uuid.uuid4().hex[:8]}",
            "name": "demo-box",
            "machine_type_code": machine_type.provider_type_id,
        },
        headers={"Authorization": f"Bearer {token}", **agent_headers("booting")},
    )
    assert registered.status_code == 201, registered.text
    machine_id = str(registered.json()["id"])
    box = {"Authorization": f"Bearer {token}", **agent_headers(machine_id)}
    beat = await client.post(f"/api/v1/machines/{machine_id}/heartbeat", headers=box)
    assert beat.status_code in (200, 204), beat.text
    return machine_id, box


@pytest.fixture
async def rig(
    real_session: AsyncSession, org_admin: OrgWithAdmin, client: AsyncClient
) -> AsyncIterator[Rig]:
    fw = await file_world(real_session, org_admin, content=NOTEBOOK, name="analysis.alknb.py")
    node = await real_session.get(FileNode, fw.node_id)
    assert node is not None
    machine_id, headers = await _ready_machine(client, org_admin, real_session)
    feed = NotebookFeed()
    transport = RecordingTransport(feed=feed, answers={"run": {"result": {"status": "queued"}}})
    service = NotebookService(
        transport=transport,
        feed=feed,
        carets=CaretBoard(),
        decide=decide_on_record,
        docs=FakeDocs(),
        # A box that keeps saying it does not hold the folder is sent the
        # request again until this wait runs out: short, so the case that
        # says so ends in a moment.
        answer_seconds=1.0,
        resend_seconds=0.1,
        ready_seconds=1.0,
    )
    setattr(fastapi_app.state, notebook_app.STATE_ATTR, service)
    async with AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://test", headers=headers
    ) as box:
        try:
            yield Rig(fw, transport, uuid.UUID(str(node.drive_id)), machine_id, box)
        finally:
            await service.close()
            setattr(fastapi_app.state, notebook_app.STATE_ATTR, None)


async def _bind(rig: Rig, *, mirror_state: str) -> None:
    """The chat bound to the rig's box, which last said it holds the chat's
    session (``awake``) or closed it (``asleep``)."""
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, rig.chat_id)
        assert chat is not None
        chat.spec = {
            **chat.spec,
            "machine_id": rig.machine_id,
            "machine_status": "ready",
            "mirror_state": mirror_state,
        }
        await session.commit()


async def _hold(real_session: AsyncSession, rig: Rig) -> None:
    """The box takes the chat's folder's lease, as it does serving the chat
    (and still names it after a restart, before it serves the chat again)."""
    folder = (
        await real_session.execute(select(FileNode).where(FileNode.target_object_id == rig.chat_id))
    ).scalar_one()
    holder = MockHolder(rig.box, folder.drive_id, folder.id, machine=rig.machine_id)
    taken = await holder.take(
        real_session,
        lambda: {"Idempotency-Key": uuid.uuid4().hex},
        purpose="chat",
        inbound=True,
        live=True,
    )
    assert taken.status_code == 200, taken.text


async def _wake_stamp(rig: Rig) -> Any:
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, rig.chat_id)
        assert chat is not None
        return chat.spec.get("wake_requested_at")


async def _runs(rig: Rig) -> list[NotebookRun]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(NotebookRun).where(NotebookRun.item_id == rig.fw.node_id)
        )
        return list(rows.scalars())


#: Each request that needs the notebook's machine: (method, path, body).
NEEDS_THE_MACHINE = [
    pytest.param("POST", "/runs", {"target": {"kind": "all"}}, id="run-all"),
    pytest.param("POST", "/runs", {"target": {"kind": "stale"}}, id="run-stale"),
    pytest.param("POST", "/kernel", {"action": "restart"}, id="restart-the-kernel"),
    pytest.param("POST", "/outputs/clear", {"cell_ids": None}, id="clear-outputs"),
    pytest.param("POST", "/env/install", {"packages": ["polars"]}, id="install"),
    pytest.param("GET", "/envs", None, id="load-the-environments"),
]


async def _ask(client: AsyncClient, rig: Rig, method: str, path: str, body: Any) -> Any:
    if method == "GET":
        return await client.get(rig.url(path))
    return await client.post(rig.url(path), json=body)


@pytest.mark.parametrize(("method", "path", "body"), NEEDS_THE_MACHINE)
@pytest.mark.parametrize("lease", ["handed-back", "named-after-a-restart"])
async def test_a_request_on_a_sleeping_chat_s_notebook_wakes_the_chat_and_waits(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    method: str,
    path: str,
    body: Any,
    lease: str,
) -> None:
    """The chat sleeps: whether it handed its folder back or its box still
    names the lease after restarting, the request wakes the chat and is
    answered with the wait. Nothing reaches the box and nothing is recorded,
    so asking again once the box holds the folder runs it once."""
    await _bind(rig, mirror_state="asleep")
    if lease == "named-after-a-restart":
        await _hold(real_session, rig)
    owner = rig.fw.world.owner
    await login(client, owner.user.email, owner.password)
    assert await _wake_stamp(rig) is None

    answer = await _ask(client, rig, method, path, body)

    assert answer.status_code == 503, answer.text
    assert (answer.json()["code"], answer.json()["message"]) == (WAKING, "Waking the machine…")
    assert answer.headers["Retry-After"] == "3"
    assert await _wake_stamp(rig) is not None, "the chat's wake is stamped for its box"
    read = (await client.get(f"/api/v1/chats/{rig.chat_id}")).json()
    assert read["session_state"] == "waking", "the chat page shows the same wake"
    assert rig.transport.sent == []
    assert await _runs(rig) == []


async def test_once_the_box_holds_the_folder_the_same_run_goes_through(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _bind(rig, mirror_state="asleep")
    owner = rig.fw.world.owner
    await login(client, owner.user.email, owner.password)
    body = {"target": {"kind": "all"}, "client_run_id": "client-run-wake-1"}
    first = await client.post(rig.url("/runs"), json=body)
    assert first.status_code == 503, first.text

    # The box takes the chat: it holds the folder and says the chat is awake.
    await _hold(real_session, rig)
    await _bind(rig, mirror_state="awake")
    again = await client.post(rig.url("/runs"), json=body)

    assert again.status_code == 200, again.text
    assert again.json()["status"] == "queued"
    (run,) = await _runs(rig)
    assert run.client_run_id == "client-run-wake-1"
    assert [op for _host, op, _sent in rig.transport.sent] == ["run"]


async def test_a_notebook_whose_chat_is_up_runs_with_no_wake(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _bind(rig, mirror_state="awake")
    await _hold(real_session, rig)
    owner = rig.fw.world.owner
    await login(client, owner.user.email, owner.password)

    answer = await client.post(rig.url("/runs"), json={"target": {"kind": "all"}})

    assert answer.status_code == 200, answer.text
    assert await _wake_stamp(rig) is None
    assert [op for _host, op, _sent in rig.transport.sent] == ["run"]


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "code"),
    [
        pytest.param(
            "POST",
            "/runs",
            {"target": {"kind": "all"}},
            403,
            "notebook.run_refused",
            id="a-run-is-refused-as-ever",
        ),
        pytest.param(
            "GET", "/envs", None, 409, "notebook.no_machine", id="loading-envs-is-answered-as-ever"
        ),
    ],
)
async def test_someone_who_may_not_run_the_notebook_wakes_nothing(
    rig: Rig, client: AsyncClient, method: str, path: str, body: Any, status: int, code: str
) -> None:
    """A Can view reader may read the notebook and run nothing: their
    request is answered as it always was, and the chat stays asleep."""
    await _bind(rig, mirror_state="asleep")
    reader = rig.fw.world.reader
    await login(client, reader.user.email, reader.password)

    answer = await _ask(client, rig, method, path, body)

    assert answer.status_code == status, answer.text
    assert code in answer.text
    assert await _wake_stamp(rig) is None
    assert rig.transport.sent == []


async def test_stopping_the_kernel_of_a_sleeping_chat_wakes_nothing(
    rig: Rig, client: AsyncClient
) -> None:
    """A sleeping chat runs no kernel: shutting one down is not a reason to
    start the machine."""
    await _bind(rig, mirror_state="asleep")
    owner = rig.fw.world.owner
    await login(client, owner.user.email, owner.password)

    answer = await client.post(rig.url("/kernel"), json={"action": "shutdown"})

    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == "notebook.no_machine"
    assert await _wake_stamp(rig) is None


@pytest.mark.parametrize(
    ("code", "status", "answered"),
    [
        pytest.param(NOTEBOOK_FOLDER_NOT_HELD, 503, WAKING, id="folder-not-held-waits-on-a-wake"),
        pytest.param("not_found", 409, "not_found", id="any-other-refusal-is-said-as-ever"),
    ],
)
async def test_a_box_that_says_it_does_not_hold_the_folder_is_waited_on(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    code: str,
    status: int,
    answered: str,
) -> None:
    """The platform last heard the chat is up, but its box answers that it
    does not hold the folder (it restarted and has not taken the chat yet):
    the caller is told to wait, not that the notebook is gone."""
    await _bind(rig, mirror_state="awake")
    await _hold(real_session, rig)
    rig.transport.answers["envs"] = {"error": {"code": code, "message": "not here"}}
    owner = rig.fw.world.owner
    await login(client, owner.user.email, owner.password)

    answer = await client.get(rig.url("/envs"))

    assert answer.status_code == status, answer.text
    assert answer.json()["code"] == answered
