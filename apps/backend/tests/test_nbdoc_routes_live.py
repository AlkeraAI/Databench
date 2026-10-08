"""The notebook routes on the real CRDT lane: real Postgres, Files, sandbox
workers and the format API behind the HTTP surface (the route suite beside
this one stands the lane in with an in-memory document).

What is pinned: an operation batch end to end (its result, its refusal as an
``OpErrorBody``), and an editor handing over typing that never reached the
document before its history restarted. (A run's wait for the requester's
frontier is pinned on the lane itself, in ``test_nbdoc_session``.)"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from alkera_core.models.files.tree import FileNode
from backend.authz import decide_on_record
from backend.services.crdt.docs import CrdtDocs
from backend.services.notebooks import app as notebook_app
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from httpx import AsyncClient
from loro import ExportMode
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, login
from tests.crdt.crdt_world import crdt_docs as lane_docs
from tests.crdt.file_world import FileWorld
from tests.crdt.test_nbdoc_real_format import FULL
from tests.crdt.test_nbdoc_session import (
    ORDERS,
    WEEKLY,
    Tab,
    _open,
    _restart,
    notebook_world,
)
from tests.test_nbdoc_routes import RecordingTransport

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@dataclass
class Live:
    fw: FileWorld
    docs: CrdtDocs
    drive_id: uuid.UUID

    def url(self, tail: str = "") -> str:
        return f"/api/v1/notebooks/{self.drive_id}/{self.fw.node_id}{tail}"


@pytest.fixture
async def live(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Live]:
    from tests.conftest import fastapi_app as app

    fw = await notebook_world(real_session, org_admin, FULL)
    node = await real_session.get(FileNode, fw.node_id)
    assert node is not None
    async with lane_docs() as docs:
        docs.run_sessions = False
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


def _source(view: dict[str, object], cell_id: str) -> str:
    cells = view["cells"]
    assert isinstance(cells, list)
    return str(next(c for c in cells if c["id"] == cell_id)["source"])


async def test_an_operation_batch_runs_end_to_end_on_the_live_document(
    live: Live, client: AsyncClient
) -> None:
    owner = live.fw.world.owner
    await login(client, owner.user.email, owner.password)
    read = (await client.get(live.url())).json()
    answer = await client.post(
        live.url("/ops"),
        json={
            "ops": [
                {"op": "replace", "cell_id": ORDERS, "source": "SELECT 1"},
                {"op": "insert", "source": "z = 1", "after": WEEKLY, "name": "z"},
            ],
            "base_token": read["token"],
            "submit_id": "live-route-0001",
        },
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    (created,) = body["created"]
    assert [c["id"] for c in body["cells"]] == [ORDERS, created]
    after = (await client.get(live.url())).json()
    assert after["token"] == body["token"]
    assert _source(after, ORDERS) == "SELECT 1"
    assert [c["id"] for c in after["cells"]][-1] == created


async def test_a_refused_operation_answers_its_index_and_code(
    live: Live, client: AsyncClient
) -> None:
    owner = live.fw.world.owner
    await login(client, owner.user.email, owner.password)
    answer = await client.post(
        live.url("/ops"),
        json={
            "ops": [
                {"op": "replace", "cell_id": ORDERS, "source": "SELECT 2"},
                {"op": "edit", "cell_id": WEEKLY, "edits": [{"old": "absent", "new": "x"}]},
            ]
        },
    )
    assert answer.status_code == 422
    assert {k: answer.json()[k] for k in ("code", "op_index")} == {
        "code": "edit_not_found",
        "op_index": 1,
    }


async def test_unsent_typing_is_handed_over_after_a_history_restart(
    real_session: AsyncSession, live: Live, client: AsyncClient
) -> None:
    owner = live.fw.world.owner
    slow = Tab(5000)
    await _open(live.docs, real_session, live.fw, owner, slow)
    sent = slow.doc.oplog_vv
    slow.type(WEEKLY, 0, "# unsent\n")
    unsent = bytes(slow.doc.export(ExportMode.Updates(sent)))
    await _restart(live.docs, live.fw)
    await login(client, owner.user.email, owner.password)
    answer = await client.post(
        live.url("/rebase"), json={"epoch": 1, "update": base64.b64encode(unsent).decode()}
    )
    assert answer.status_code == 200, answer.text
    after = (await client.get(live.url())).json()
    assert after["token"] == answer.json()["token"]
    assert _source(after, WEEKLY).startswith("# unsent\n")


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        pytest.param(
            {"epoch": 7, "update": "AAAA"}, 409, "crdt_rebase_unavailable", id="no-kept-state"
        ),
        pytest.param(
            {"epoch": 1, "update": "not base64!"}, 422, "notebook.bad_update", id="not-base64"
        ),
    ],
)
async def test_a_hand_over_that_cannot_be_carried_is_refused(
    live: Live, client: AsyncClient, body: dict[str, object], status: int, code: str
) -> None:
    owner = live.fw.world.owner
    await login(client, owner.user.email, owner.password)
    await client.get(live.url())
    answer = await client.post(live.url("/rebase"), json=body)
    assert (answer.status_code, answer.json().get("code")) == (status, code)


async def test_a_reader_cannot_hand_over_typing(live: Live, client: AsyncClient) -> None:
    reader = live.fw.world.reader
    await login(client, reader.user.email, reader.password)
    answer = await client.post(live.url("/rebase"), json={"epoch": 1, "update": "AAAA"})
    assert answer.status_code == 403
