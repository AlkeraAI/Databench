"""The notebook routes against real Postgres and real Files.

The live document's sandbox work (the Loro notebook type and the format code)
is the CRDT lane's and is covered by its own suites; here it is replaced by a
small in-memory document (:class:`FakeDocs`) so the routes' own contract is
what is pinned: who is admitted (Files rungs, the lease fence, the
``notebook.run`` policy and its decision row), what is recorded (edits, runs,
kernels), what a writer is told (notices), what reaches the box (the
transport) and what a forged kernel event can reach (nothing).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core import brand
from alkera_core.authz import ActingContext
from alkera_core.authz.headers import agent_headers
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.events.types import EventType
from alkera_core.files import acl as files_acl
from alkera_core.files.authz.grants import Principal
from alkera_core.files.authz.ladder import ROLE_WRITER
from alkera_core.models import EventOutbox, WorkspaceObject
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.models import NotebookEdit, NotebookKernel, NotebookRun
from alkera_core.schemas.realtime.machine import NotebookRequestOp
from alkera_notebook.engine.models import KernelInfo, NotebookOpsResult, NotebookView
from backend.authz import decide_on_record
from backend.services.crdt.errors import CrdtError
from backend.services.crdt.notebook_peers import NotebookOpError
from backend.services.crdt.notebook_type import author_display
from backend.services.crdt.registry import DocRef
from backend.services.notebooks import app as notebook_app
from backend.services.notebooks.carets import CaretBoard
from backend.services.notebooks.feed import NotebookFeed
from backend.services.notebooks.service import NotebookService
from backend.services.notebooks.transport import KernelHost
from backend.services.org import teams as team_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, fastapi_app, login
from tests.crdt.file_world import FileWorld, file_world
from tests.files._boxes import registered_box
from tests.files._live_holder import MockHolder

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]

CELL_A = "a1b2c3d4e5"
CELL_B = "f6g7h8j9k0"
NOTEBOOK = b"import marimo\napp = marimo.App()\n"


# -- the doubles -----------------------------------------------------------------


@dataclass(frozen=True)
class _Applied:
    token: str
    repeat: bool
    cells: list[dict[str, Any]]
    created: list[str]
    touched: list[str]
    notices: list[dict[str, Any]]
    caret: bytes | None
    peer: int


@dataclass(frozen=True)
class _View:
    token: str
    covered: bool
    view: dict[str, Any]


@dataclass
class FakeDocs:
    """A notebook document of two cells, edited in memory. A batch that names
    an unknown cell is refused like the lane refuses it."""

    cells: dict[str, str] = field(default_factory=lambda: {CELL_A: "x = 1", CELL_B: "y = x"})
    #: The format's analysis of the head, when a test gives the stand-in one.
    graph: dict[str, Any] | None = None
    #: What the document refuses the next batch with, when a test says.
    refuse: Exception | None = None
    version: int = 1
    applied: list[dict[str, Any]] = field(default_factory=list)
    submits: dict[str, _Applied] = field(default_factory=dict)
    holder_told: list[DocRef] = field(default_factory=list)

    def holder_wrote(self, ref: DocRef) -> None:
        self.holder_told.append(ref)

    async def apply(
        self,
        ref: DocRef,
        *,
        ops: list[dict[str, Any]],
        base_token: str | None,
        submit_id: str | None,
        agent_id: str | None,
        author: Any,
        actor_key: str | None = None,
    ) -> _Applied:
        if self.refuse is not None:
            raise self.refuse
        if submit_id is not None and submit_id in self.submits:
            first = self.submits[submit_id]
            return _Applied(**{**first.__dict__, "repeat": True})
        touched: list[str] = []
        for index, op in enumerate(ops):
            cell = op.get("cell_id")
            if cell not in self.cells:
                raise NotebookOpError(index, "cell_not_found", f"no cell {cell}")
            touched.append(cell)
        for op in ops:
            self.cells[op["cell_id"]] = op.get("source", self.cells[op["cell_id"]])
        self.version += 1
        self.applied.append(
            {"agent_id": agent_id, "author": author, "ops": ops, "actor_key": actor_key}
        )
        result = _Applied(
            token=f"1.v{self.version}",
            repeat=False,
            cells=[
                {"id": c, "index": i, "kind": "python", "name": "_"}
                for i, c in enumerate(self.cells)
                if c in touched
            ],
            created=[],
            touched=touched,
            notices=[],
            caret=None,
            peer=5000,
        )
        if submit_id is not None:
            self.submits[submit_id] = result
        return result

    def graph_at(self, ref: DocRef, token: str) -> dict[str, Any] | None:
        # The document's analysis is the CRDT lane's (its own tests); this
        # stand-in has one only when a test sets it.
        return self.graph if token == f"1.v{self.version}" else None

    async def view(
        self, ref: DocRef, *, frontier: str | None = None, wait_seconds: float = 0.0
    ) -> _View:
        return _View(
            token=f"1.v{self.version}",
            covered=frontier is None or frontier <= f"1.v{self.version}",
            view={
                "format": "1.0",
                "settings": {"reactivity": "autorun"},
                "cells": [
                    {"id": c, "index": i, "kind": "python", "name": "_", "source": s}
                    for i, (c, s) in enumerate(self.cells.items())
                ],
            },
        )


@dataclass
class RecordingTransport:
    """Records every request; for an op named in ``answers`` the engine
    answers it, as a box does by posting an ``answer`` event (which every
    replica's feed hears)."""

    sent: list[tuple[KernelHost, str, dict[str, Any]]] = field(default_factory=list)
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    feed: NotebookFeed | None = None
    _seq: int = 0

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Any,
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self.sent.append((host, op, dict(body)))
        request_id = request_id or uuid.uuid4()
        answer = self.answers.get(op)
        if answer is not None and self.feed is not None:
            self._seq += 1
            self.feed.observe(
                _kernel_batch(
                    host.org_id,
                    host.item_id,
                    [{"seq": self._seq, "type": "answer", "request_id": str(request_id), **answer}],
                )
            )
        return request_id


def _kernel_batch(
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    events: list[dict[str, Any]],
    kernel_id: str = "k-test",
) -> HubEvent:
    channel = f"nb:{item_id}"
    return HubEvent(
        lane="durable",
        org_id=org_id,
        type=EventType.NOTEBOOK_EVENT.value,
        entity="notebook",
        entity_id=channel,
        version=0,
        visibility="org",
        payload={"kernel_id": kernel_id, "state": None, "events": events},
        channel=channel,
    )


@dataclass
class Rig:
    fw: FileWorld
    docs: FakeDocs
    transport: RecordingTransport
    service: NotebookService
    drive_id: uuid.UUID

    def url(self, tail: str = "") -> str:
        return f"/api/v1/notebooks/{self.drive_id}/{self.fw.node_id}{tail}"


@pytest.fixture
async def rig(real_session: AsyncSession, org_admin: OrgWithAdmin) -> AsyncIterator[Rig]:
    from tests.conftest import fastapi_app as app

    fw = await file_world(real_session, org_admin, content=NOTEBOOK, name="analysis.alknb.py")
    node = await real_session.get(FileNode, fw.node_id)
    assert node is not None
    docs = FakeDocs()
    feed = NotebookFeed()
    # The engine takes every run it is sent (a test about what the box makes
    # of a run says so itself): the run route's delivery is then answered.
    transport = RecordingTransport(feed=feed, answers={"run": {"result": {"status": "queued"}}})
    service = NotebookService(
        transport=transport,
        feed=feed,
        carets=CaretBoard(),
        decide=decide_on_record,
        docs=docs,
    )
    setattr(app.state, notebook_app.STATE_ATTR, service)
    try:
        yield Rig(fw, docs, transport, service, uuid.UUID(str(node.drive_id)))
    finally:
        await service.close()
        setattr(app.state, notebook_app.STATE_ATTR, None)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


@dataclass
class Box:
    """The box holding the chat's folder: a registered machine, speaking on
    its own client, under the lease it took."""

    client: AsyncClient
    holder: MockHolder
    machine_id: str

    @property
    def fence(self) -> dict[str, str]:
        return self.holder.fence


async def _holder(
    db: AsyncSession, client: AsyncClient, fw: FileWorld, *, mount: bool = False
) -> Box:
    """The chat's box, holding its folder: as a chat's box (taking what
    people write there) or as a mount (taking nothing)."""
    owner = fw.world.owner.user
    token, machine_id = await registered_box(
        db, user_id=owner.id, email=owner.email, org_id=fw.world.org_id
    )
    await db.commit()
    folder = (
        await db.execute(
            select(FileNode).where(FileNode.target_object_id == uuid.UUID(fw.world.ref.doc_id))
        )
    ).scalar_one()
    box_client = AsyncClient(
        transport=ASGITransport(app=fastapi_app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}", **agent_headers(machine_id)},
    )
    holder = MockHolder(box_client, folder.drive_id, folder.id, machine=machine_id)
    if mount:
        taken = await holder.take(db, _idem, purpose="mount")
    else:
        taken = await holder.take(db, _idem, purpose="chat", inbound=True, live=True)
    assert taken.status_code == 200, taken.text
    return Box(box_client, holder, machine_id)


def _edit(cell: str, source: str) -> dict[str, Any]:
    return {"op": "replace", "cell_id": cell, "source": source}


async def _edits(rig: Rig) -> list[NotebookEdit]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(NotebookEdit)
            .where(NotebookEdit.item_id == rig.fw.node_id)
            .order_by(NotebookEdit.id)
        )
        return list(rows.scalars())


async def _notebook_events(org_id: uuid.UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == EventType.NOTEBOOK_EVENT.value,
            )
            .order_by(EventOutbox.id)
        )
        return list(rows.scalars())


# -- the document peer route ---------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "status"),
    [
        pytest.param("writer", 200, id="can-edit-writes"),
        pytest.param("reader", 403, id="can-view-is-refused"),
        pytest.param("stranger", 404, id="a-stranger-learns-nothing"),
    ],
)
async def test_ops_take_files_write(rig: Rig, client: AsyncClient, who: str, status: int) -> None:
    person = getattr(rig.fw.world, who)
    await login(client, person.user.email, person.password)
    answer = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 2")]})
    assert answer.status_code == status, answer.text
    assert (rig.docs.cells[CELL_A] == "x = 2") is (status == 200)
    if status == 200:
        body = answer.json()
        assert body["repeat"] is False and body["cells"][0]["id"] == CELL_A
        # A person's operations never pass the document type's socket hook:
        # the route records them, once, as that person.
        assert [(e.cell_id, e.actor_key, e.actor_kind) for e in await _edits(rig)] == [
            (CELL_A, f"user:{person.user.id}", "person")
        ]
        (call,) = rig.docs.applied
        assert call["agent_id"] is None and call["author"].id == person.user.id
        # The document keeps one Loro peer per writer: the route names it.
        assert call["actor_key"] == f"user:{person.user.id}"


async def test_an_op_the_document_refuses_names_its_index(rig: Rig, client: AsyncClient) -> None:
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(
        rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 3"), _edit("zzzzzzzzzz", "q")]}
    )
    assert answer.status_code == 422
    error = answer.json()["error"]
    assert (error["code"], error["message"], error["details"]) == (
        "cell_not_found",
        "no cell zzzzzzzzzz",
        {"op_index": 1},
    )


async def test_the_box_writes_under_its_fence_as_its_machine_and_is_recorded_once(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    box = await _holder(real_session, client, rig.fw)
    body = {"ops": [_edit(CELL_B, "y = x + 1")], "submit_id": "box-submit-0001"}
    first = await box.client.post(rig.url("/ops"), json=body, headers=box.fence)
    again = await box.client.post(rig.url("/ops"), json=body, headers=box.fence)
    stale = await box.client.post(
        rig.url("/ops"), json=body, headers=box.holder.stale_fence(epoch=0)
    )
    assert first.status_code == 200, first.text
    assert again.status_code == 200 and again.json()["repeat"] is True
    assert stale.status_code == 409
    assert [call["agent_id"] for call in rig.docs.applied] == [box.machine_id]
    (edit,) = await _edits(rig)
    assert (edit.cell_id, edit.actor_key, edit.edits) == (CELL_B, f"machine:{box.machine_id}", 1)


async def test_a_person_cannot_write_past_a_lease_that_takes_no_inbound_writes(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, rig.fw, mount=True)
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 9")]})
    assert answer.status_code == 409
    assert rig.docs.applied == []


async def test_a_cell_another_actor_edited_lately_is_named_until_the_window_passes(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """The box's agent edits a cell; a person editing it within 15 s is told
    who, and once the window passes is told nothing."""
    from backend.services.notebooks.service import NOTICE_WINDOW

    box = await _holder(real_session, client, rig.fw)
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    first_at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    async def box_edit(at: datetime) -> None:
        rig.service.clock = lambda: at
        answer = await box.client.post(
            rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 4")]}, headers=box.fence
        )
        assert answer.status_code == 200, answer.text

    async def person_edit(at: datetime) -> list[dict[str, Any]]:
        rig.service.clock = lambda: at
        answer = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 5")]})
        assert answer.status_code == 200, answer.text
        notices: list[dict[str, Any]] = answer.json()["notices"]
        return notices

    await box_edit(first_at)
    inside = await person_edit(first_at + NOTICE_WINDOW - timedelta(seconds=1))
    await box_edit(first_at + NOTICE_WINDOW)
    outside = await person_edit(first_at + 2 * NOTICE_WINDOW + timedelta(seconds=1))
    # The box writing on its own (no chat named) is the platform.
    assert [(n["cell_id"], n["kind"], n["by"]) for n in inside] == [
        (CELL_A, "concurrent_edit", brand.product_name())
    ]
    assert outside == []
    edits = {e.actor_key: e.edits for e in await _edits(rig)}
    assert edits == {
        f"machine:{box.machine_id}": 2,
        f"user:{rig.fw.world.writer.user.id}": 2,
    }


# -- runs and the notebook.run decision ------------------------------------------------


async def _decisions(org_id: uuid.UUID) -> list[EventOutbox]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox)
            .where(EventOutbox.org_id == org_id, EventOutbox.type == "authz.decision")
            .order_by(EventOutbox.id)
        )
        return [row for row in rows.scalars() if row.payload.get("policy") == "notebook.run"]


@pytest.mark.parametrize(
    ("who", "status", "effect", "reason"),
    [
        pytest.param("writer", 200, "allow", "can_edit", id="can-edit-runs"),
        pytest.param("commenter", 403, "deny", "needs_can_edit", id="can-comment-is-refused"),
        pytest.param("reader", 403, "deny", "needs_can_edit", id="can-view-is-refused"),
    ],
)
async def test_a_run_is_decided_by_notebook_run_and_recorded(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    who: str,
    status: int,
    effect: str,
    reason: str,
) -> None:
    box = await _holder(real_session, client, rig.fw)
    person = getattr(rig.fw.world, who)
    await login(client, person.user.email, person.password)
    answer = await client.post(
        rig.url("/runs"),
        json={"target": {"kind": "cells", "ids": [CELL_B]}, "frontier": "1.v1"},
    )
    assert answer.status_code == status, answer.text
    (decision,) = await _decisions(rig.fw.world.org_id)
    assert (decision.payload["effect"], decision.payload["reason"]) == (effect, reason)
    async with AsyncSessionLocal() as db:
        runs = list(
            (
                await db.execute(select(NotebookRun).where(NotebookRun.item_id == rig.fw.node_id))
            ).scalars()
        )
    if status != 200:
        assert "notebook.run_refused" in answer.text
        assert runs == [] and rig.transport.sent == []
        return
    body = answer.json()
    assert body["status"] == "queued" and body["submitted"] == {CELL_B: "y = x"}
    (run,) = runs
    assert run.requested_by_user_id == person.user.id and run.trigger == "run"
    assert run.submitted["cells"][CELL_B]["bytes"] == len(b"y = x")
    (host, op, sent) = rig.transport.sent[0]
    assert (host.machine_id, op, sent["run_id"]) == (box.machine_id, "run", body["run_id"])
    assert "y = x" not in str(sent)


async def test_can_edit_on_the_notebook_alone_does_not_run_its_folder(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """The kernel binds the whole folder its machine holds. Someone given Can
    edit on one notebook, and nothing on the folder around it, edits that
    file and runs nothing: a run would hand them the rest of the tree."""
    await _holder(real_session, client, rig.fw)
    owner, outsider = rig.fw.world.owner.user, rig.fw.world.stranger
    ctx = ActingContext.for_user(user_id=owner.id, org_id=owner.home_org_team_id, email=owner.email)
    async with team_service.files_transaction(real_session, ctx) as repo:
        node = await repo.session.get(FileNode, rig.fw.node_id)
        assert node is not None
        await files_acl.grant(
            repo, ctx, node, Principal(kind="user", id=outsider.user.id), ROLE_WRITER
        )
    await real_session.commit()
    await login(client, outsider.user.email, outsider.password)
    answer = await client.post(rig.url("/runs"), json={"target": {"kind": "all"}})
    assert answer.status_code == 403, answer.text
    assert "notebook.run_refused" in answer.text
    (decision,) = await _decisions(rig.fw.world.org_id)
    assert (decision.payload["effect"], decision.payload["reason"]) == (
        "deny",
        "needs_can_edit_on_folder",
    )
    assert rig.transport.sent == []


async def test_a_person_outside_the_workspace_never_reaches_its_connections(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """Sharing a workspace shares its owner's connections, personal ones
    included: a kernel in it runs on them for whoever the workspace is shared
    with (the box's half is pinned in ``test_notebook_sql_nbsqw``). Someone it
    is not shared with runs nothing there, so no statement of theirs ever
    reaches those connections: the run is the opaque not-found and nothing is
    sent to the box."""
    await _holder(real_session, client, rig.fw)
    stranger = rig.fw.world.stranger
    await login(client, stranger.user.email, stranger.password)
    answer = await client.post(
        rig.url("/runs"),
        json={"target": {"kind": "cells", "ids": [CELL_B]}, "frontier": "1.v1"},
    )
    assert answer.status_code == 404, answer.text
    assert rig.transport.sent == []
    async with AsyncSessionLocal() as db:
        runs = (
            await db.execute(select(NotebookRun).where(NotebookRun.item_id == rig.fw.node_id))
        ).scalars()
        assert list(runs) == []


async def test_a_run_with_no_machine_holding_the_folder_is_a_conflict(
    rig: Rig, client: AsyncClient
) -> None:
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(rig.url("/runs"), json={"target": {"kind": "all"}})
    assert answer.status_code == 409
    assert answer.json()["code"] == "notebook.no_machine"
    assert rig.transport.sent == []


async def test_a_retried_run_is_answered_from_its_record(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, rig.fw)
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    body = {"target": {"kind": "all"}, "client_run_id": "client-run-0001"}
    first = await client.post(rig.url("/runs"), json=body)
    second = await client.post(rig.url("/runs"), json=body)
    assert first.status_code == second.status_code == 200
    assert second.json()["repeat"] is True and second.json()["run_id"] == first.json()["run_id"]
    assert len(rig.transport.sent) == 1


# -- kernel events from the box ----------------------------------------------------------


async def _post_events(rig: Rig, box: Box, kernel_id: str, events: list[dict[str, Any]]) -> Any:
    return await box.client.post(
        rig.url("/events"), json={"kernel_id": kernel_id, "state": "idle", "events": events}
    )


async def test_an_output_too_large_for_the_event_log_arrives_as_a_marker_not_a_500(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """The owner's kernel showed a 7.3 MB image: the batch carrying it was a
    500 (the outbox caps a row at 2 MB) and every later event of that kernel
    was lost with it. The output now arrives as a marker that says it was too
    large, and the events after it arrive too."""
    import base64

    from alkera_core.notebooks.limits import TOO_LARGE_MIME

    box = await _holder(real_session, client, rig.fw)
    kernel_id = f"kernel-{uuid.uuid4().hex[:8]}"
    image = base64.b64encode(b"\x89PNG" + b"\x00" * 5_500_000).decode("ascii")
    events = [
        {"seq": 1, "type": "kernel.state", "state": "busy"},
        {
            "seq": 2,
            "type": "cell.output",
            "cell_id": CELL_A,
            "run_id": "r1",
            "output": {"image/png": image, "text/plain": "<Figure>"},
        },
        {
            "seq": 3,
            "type": "cell.output",
            "cell_id": CELL_B,
            "run_id": "r1",
            "output": {"text/plain": "after"},
        },
    ]
    posted = await _post_events(rig, box, kernel_id, events)
    assert posted.status_code == 200, posted.text
    rows = await _notebook_events(rig.fw.world.org_id)
    carried = [e for row in rows for e in (row.payload or {}).get("events", [])]
    by_seq = {e["seq"]: e for e in carried if e.get("kernel_id", kernel_id) == kernel_id}
    assert sorted(by_seq) == [1, 2, 3]
    assert TOO_LARGE_MIME in by_seq[2]["output"] and "image/png" not in by_seq[2]["output"]
    assert by_seq[3]["output"] == {"text/plain": "after"}


async def test_events_bind_a_kernel_to_the_holder_and_a_forged_one_goes_nowhere(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    org_admin: OrgWithAdmin,
) -> None:
    """The first batch binds the kernel to the folder's holder; a retried
    batch adds nothing; a batch naming a kernel bound to another org's
    notebook is refused as not found and announces nothing."""
    other_org, _ = await team_service.create_org_with_admin(
        real_session,
        org_name=f"other-{uuid.uuid4().hex[:8]}",
        admin_email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        admin_first_name="Other",
        admin_last_name="Admin",
        admin_password="other-pass-12345",
    )
    await real_session.commit()
    box = await _holder(real_session, client, rig.fw)
    foreign_id = f"kernel-b-{uuid.uuid4().hex[:8]}"
    kernel_id = f"kernel-a-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        # Another org's kernel, bound to the same machine id: the org half of
        # the binding is what refuses it.
        db.add(
            NotebookKernel(
                kernel_id=foreign_id,
                org_id=other_org.id,
                drive_id=uuid.uuid4(),
                item_id=rig.fw.node_id,
                machine_id=box.machine_id,
                state="idle",
                seq=0,
            )
        )
        await db.commit()
    events = [{"seq": 1, "type": "kernel.state"}, {"seq": 2, "type": "cell.status"}]
    first = await _post_events(rig, box, kernel_id, events)
    again = await _post_events(rig, box, kernel_id, events)
    forged = await _post_events(rig, box, foreign_id, events)
    assert first.status_code == 200, first.text
    assert first.json() == {"kernel_id": kernel_id, "accepted": 2, "seq": 2}
    assert again.json()["accepted"] == 0
    assert forged.status_code == 404
    announced = await _notebook_events(org_admin.org_id)
    assert [row.entity_id for row in announced] == [f"nb:{rig.fw.node_id}"] * 2
    assert [len(row.payload["events"]) for row in announced] == [2, 0]
    assert await _notebook_events(other_org.id) == []
    async with AsyncSessionLocal() as db:
        kernel = await db.get(NotebookKernel, kernel_id)
        foreign = await db.get(NotebookKernel, foreign_id)
    assert kernel is not None and (kernel.machine_id, kernel.seq) == (box.machine_id, 2)
    assert foreign is not None and foreign.seq == 0


async def test_a_person_cannot_post_kernel_events(rig: Rig, client: AsyncClient) -> None:
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(
        rig.url("/events"), json={"kernel_id": "k", "events": [{"seq": 1, "type": "x"}]}
    )
    assert answer.status_code == 404


# -- the view's presence --------------------------------------------------------------


def _caret_event(rig: Rig, peer: int, cell: str, who: str) -> Any:
    from alkera_core.events import HubEvent

    channel = f"doc:notebook:{rig.fw.node_id}"
    return HubEvent(
        lane="ephemeral",
        org_id=rig.fw.world.org_id,
        type="doc.crdt_ephemeral",
        entity="doc",
        entity_id=channel,
        version=0,
        visibility="org",
        payload={
            "envelope": {"payload": {"loro_peer": peer, "user_id": "u", "display_name": who}},
            "caret_cell": cell,
        },
        channel=channel,
    )


async def test_the_view_names_who_is_in_each_cell_now(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """Presence is people's carets and, for an actor that publishes no caret
    (here the machine), its edits within 15 s, one entry per actor and cell
    at its newest; an edit older than the window and a caret in a cell the
    document no longer has are left out."""
    box = await _holder(real_session, client, rig.fw)
    start = datetime.now(UTC)
    rig.service.clock = lambda: start - timedelta(seconds=40)
    await box.client.post(
        rig.url("/ops"), json={"ops": [_edit(CELL_B, "y = 1")]}, headers=box.fence
    )
    rig.service.clock = lambda: start - timedelta(seconds=5)
    await box.client.post(
        rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 1")]}, headers=box.fence
    )
    rig.service.clock = lambda: start - timedelta(seconds=2)
    await box.client.post(
        rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 2")]}, headers=box.fence
    )
    rig.service.clock = lambda: start
    rig.service.carets.observe(_caret_event(rig, 2000, CELL_B, "Bo"))
    rig.service.carets.observe(_caret_event(rig, 2001, "zzzzzzzzzz", "Cy"))
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.get(rig.url())
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [c["id"] for c in body["cells"]] == [CELL_A, CELL_B]
    seen = [(p["who"], p["kind"], p["cell_id"]) for p in body["presence"]]
    assert seen == [("Bo", "person", CELL_B), (brand.product_name(), "system", CELL_A)]
    assert [p["caret"] for p in body["presence"]] == [True, False]
    (agent,) = [p for p in body["presence"] if p["who"] == brand.product_name()]
    assert datetime.fromisoformat(agent["at"]) == start - timedelta(seconds=2)


async def _chat_on(db: AsyncSession, rig: Rig, box: Box) -> str:
    """Bind the rig's chat to the box the way placement does, so the box may
    apply batches its agent made; the chat's id."""
    chat_id = rig.fw.world.ref.doc_id
    chat = await db.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    await db.execute(
        update(WorkspaceObject)
        .where(WorkspaceObject.id == chat.id)
        .values(spec={**(chat.spec or {}), "machine_id": box.machine_id})
    )
    await db.commit()
    return chat_id


def _presence_events(rows: list[EventOutbox]) -> list[list[dict[str, Any]]]:
    """Each ``presence`` event the platform announced, in order."""
    return [
        event["presence"]
        for row in rows
        for event in row.payload.get("platform_events") or []
        if event.get("type") == "presence"
    ]


def _placed(presence: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    return [(p["actor_id"], p["cell_id"], p["expires_in"]) for p in presence]


async def test_an_agent_is_one_presence_announced_on_each_edit_and_gone_when_it_lapses(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """An agent editing through its chat's box is one actor wherever it
    edits: each batch announces the whole presence list on the notebook's
    channel, every entry naming the chat's agent and how long it still
    holds (15 s from the edit). The view says the same, and once the window
    has passed the agent is in no cell."""
    box = await _holder(real_session, client, rig.fw)
    chat_id = await _chat_on(real_session, rig, box)
    agent = f"agent:{chat_id}"
    start = datetime.now(UTC)
    for second, cell in [(0, CELL_A), (1, CELL_B), (2, CELL_A)]:
        rig.service.clock = lambda second=second: start + timedelta(seconds=second)
        answer = await box.client.post(
            rig.url("/ops"),
            json={"ops": [_edit(cell, f"v = {second}")], "agent_chat_id": chat_id},
            headers=box.fence,
        )
        assert answer.status_code == 200, answer.text

    announced = _presence_events(await _notebook_events(rig.fw.world.org_id))
    assert [_placed(one) for one in announced] == [
        [(agent, CELL_A, 15.0)],
        [(agent, CELL_B, 15.0), (agent, CELL_A, 14.0)],
        [(agent, CELL_A, 15.0), (agent, CELL_B, 14.0)],
    ]
    assert {p["kind"] for one in announced for p in one} == {"agent"}

    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    rig.service.clock = lambda: start + timedelta(seconds=16.5)
    late = await client.get(rig.url())
    assert late.status_code == 200, late.text
    assert _placed(late.json()["presence"]) == [(agent, CELL_A, 0.5)]
    rig.service.clock = lambda: start + timedelta(seconds=17.5)
    assert (await client.get(rig.url())).json()["presence"] == []


async def test_a_persons_edit_announces_no_presence(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """A person's edits claim no cell (their caret does), so the ops route
    announces nothing for them."""
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 9")]})
    assert answer.status_code == 200, answer.text
    assert _presence_events(await _notebook_events(rig.fw.world.org_id)) == []


# -- output frames, tables, environments, widget modules (rev 3.5) ----------------------


def _effects(rows: list[EventOutbox]) -> list[tuple[str, str]]:
    return [(row.payload["effect"], row.payload["reason"]) for row in rows]


@pytest.mark.parametrize(
    ("who", "status", "decided"),
    [
        pytest.param("writer", 200, [("allow", "can_edit")], id="can-edit-attaches"),
        pytest.param("commenter", 403, [("deny", "needs_can_edit")], id="can-comment-is-refused"),
        pytest.param("reader", 403, [("deny", "needs_can_edit")], id="can-view-is-refused"),
        pytest.param("stranger", 404, [], id="a-stranger-is-refused-before-the-policy"),
    ],
)
async def test_a_frame_is_attached_only_by_someone_who_may_run(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    who: str,
    status: int,
    decided: list[tuple[str, str]],
) -> None:
    """A frame is where widget messages are sent from: attaching one is
    decided by ``notebook.run`` and recorded either way; a refused attach
    never reaches the hub."""
    from backend.services.notebooks import frames

    await _holder(real_session, client, rig.fw)
    rig.transport.answers["frame_attach"] = {
        "result": {"opens": [{"comm_id": "w1", "data": {"state": {"value": 1}}}]}
    }
    person = getattr(rig.fw.world, who)
    await login(client, person.user.email, person.password)
    answer = await client.post(
        rig.url("/frames"), json={"output_id": "out-1", "model_ids": ["w1"], "peer_id": "p:7"}
    )
    assert answer.status_code == status, answer.text
    assert _effects(await _decisions(rig.fw.world.org_id)) == decided
    attached = [body for _, op, body in rig.transport.sent if op == "frame_attach"]
    if status != 200:
        if status == 403:
            assert answer.json()["error"]["code"] == "notebook.run_refused"
        assert attached == []
        return
    body = answer.json()
    assert body["opens"] == [{"comm_id": "w1", "data": {"state": {"value": 1}}}]
    owned = frames.verify(body["frame_id"], item_id=rig.fw.node_id)
    assert owned is not None and owned.owner == person.user.id
    assert frames.reaches(body["frame_id"], user_id=person.user.id, peer_id="p:7")
    assert [(b["frame_id"], b["output_id"]) for b in attached] == [(body["frame_id"], "out-1")]
    # The box names the person a widget-first action is theirs.
    assert attached[0]["requested_by"] == {
        "kind": "person",
        "id": f"user:{person.user.id}",
        "display_name": author_display(person.user),
    }


async def test_a_frame_is_not_attached_while_the_lease_refuses_writes(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, rig.fw, mount=True)
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(rig.url("/frames"), json={"output_id": "out-1"})
    assert answer.status_code == 403, answer.text
    assert _effects(await _decisions(rig.fw.world.org_id)) == [("deny", "lease_refuses_writes")]
    assert all(op != "frame_attach" for _, op, _ in rig.transport.sent)


@pytest.mark.parametrize(
    ("who", "frame", "status", "decided"),
    [
        pytest.param("writer", "mine", 200, [("allow", "can_edit")], id="own-frame-is-carried"),
        # The policy allowed, the frame did not: the allow rides the refused
        # request's transaction and is rolled back with it.
        pytest.param("writer", "theirs", 404, [], id="another-person-s-frame"),
        pytest.param("writer", "elsewhere", 404, [], id="a-frame-of-another-notebook"),
        pytest.param("writer", "forged", 404, [], id="a-forged-frame"),
        # A non-runner is refused by the policy before any frame is looked at:
        # its own valid frame and someone else's are answered alike.
        pytest.param(
            "reader", "mine", 403, [("deny", "needs_can_edit")], id="non-runner-own-frame"
        ),
        pytest.param(
            "reader", "theirs", 403, [("deny", "needs_can_edit")], id="non-runner-foreign-frame"
        ),
    ],
)
async def test_a_widget_message_goes_only_through_the_sender_s_own_frame(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    who: str,
    frame: str,
    status: int,
    decided: list[tuple[str, str]],
) -> None:
    from backend.services.notebooks import frames

    await _holder(real_session, client, rig.fw)
    person = getattr(rig.fw.world, who)
    owner = rig.fw.world.owner.user.id
    minted = {
        "mine": frames.mint(owner=person.user.id, item_id=rig.fw.node_id, peer_id=None),
        "theirs": frames.mint(owner=owner, item_id=rig.fw.node_id, peer_id=None),
        "elsewhere": frames.mint(owner=person.user.id, item_id=uuid.uuid4(), peer_id=None),
    }
    forged = minted["theirs"].split(".")
    forged[0] = person.user.id.hex  # the owner's signature, re-labelled as the sender's
    minted["forged"] = ".".join(forged)
    await login(client, person.user.email, person.password)
    message = {"comm_id": "w1", "msg_id": "m1", "content": {"method": "update"}}
    answer = await client.post(rig.url("/comm"), json={**message, "frame_id": minted[frame]})
    assert answer.status_code == status, answer.text
    assert _effects(await _decisions(rig.fw.world.org_id)) == decided
    comms = [body for _, op, body in rig.transport.sent if op == "comm"]
    if status == 200:
        assert [(c["frame_id"], c["comm_id"]) for c in comms] == [(minted[frame], "w1")]
        assert comms[0]["requested_by"]["id"] == f"user:{person.user.id}"
    else:
        assert comms == []


async def test_a_widget_message_names_a_frame(rig: Rig, client: AsyncClient) -> None:
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    message = {"comm_id": "w1", "msg_id": "m1", "content": {"method": "update"}}
    assert (await client.post(rig.url("/comm"), json=message)).status_code == 422
    assert rig.transport.sent == []


async def test_a_frame_is_detached_only_by_its_owner(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    from backend.services.notebooks import frames

    await _holder(real_session, client, rig.fw)
    writer = rig.fw.world.writer.user
    mine = frames.mint(owner=writer.id, item_id=rig.fw.node_id, peer_id=None)
    theirs = frames.mint(owner=rig.fw.world.owner.user.id, item_id=rig.fw.node_id, peer_id=None)
    await login(client, writer.email, rig.fw.world.writer.password)
    assert (await client.delete(rig.url(f"/frames/{theirs}"))).status_code == 404
    assert (await client.delete(rig.url(f"/frames/{mine}"))).status_code == 204
    detached = [body for _, op, body in rig.transport.sent if op == "frame_detach"]
    assert detached == [
        {
            "frame_id": mine,
            "requested_by": {
                "kind": "person",
                "id": f"user:{writer.id}",
                "display_name": author_display(writer),
            },
        }
    ]


async def test_a_person_who_may_no_longer_run_still_detaches_their_frame(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """Detaching is cleanup of one's own frame, not an act through the
    kernel: a reader drops a frame minted for them, undecided by
    ``notebook.run``."""
    from backend.services.notebooks import frames

    await _holder(real_session, client, rig.fw)
    reader = rig.fw.world.reader
    mine = frames.mint(owner=reader.user.id, item_id=rig.fw.node_id, peer_id=None)
    await login(client, reader.user.email, reader.password)
    assert (await client.delete(rig.url(f"/frames/{mine}"))).status_code == 204
    assert [body for _, op, body in rig.transport.sent if op == "frame_detach"] == [
        {
            "frame_id": mine,
            "requested_by": {
                "kind": "person",
                "id": f"user:{reader.user.id}",
                "display_name": author_display(reader.user),
            },
        }
    ]
    assert await _decisions(rig.fw.world.org_id) == []


async def test_a_socket_leaving_detaches_its_frames_as_its_person(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """A socket that left drops the frames narrowed to it, the request naming
    the person whose frames they are."""
    from backend.services.notebooks import frames

    await _holder(real_session, client, rig.fw)
    writer = rig.fw.world.writer.user
    await rig.service.detach_frames(
        org_id=rig.fw.world.org_id, item_id=rig.fw.node_id, user=writer, peer_id="p:7"
    )
    assert [body for _, op, body in rig.transport.sent if op == "frame_detach"] == [
        {
            "prefix": frames.owner_prefix(owner=writer.id, peer_id="p:7"),
            "requested_by": {
                "kind": "person",
                "id": f"user:{writer.id}",
                "display_name": author_display(writer),
            },
        }
    ]


#: Every route that acts through the kernel: (path, body, what reaches the box).
_KERNEL_ROUTES = [
    pytest.param("/runs", {"target": {"kind": "cells", "ids": [CELL_B]}}, "run", id="run"),
    *(
        pytest.param("/kernel", {"action": action}, "kernel", id=f"kernel-{action}")
        for action in ("interrupt", "interrupt_all", "restart", "shutdown")
    ),
    pytest.param("/outputs/clear", {"cell_ids": [CELL_B]}, "outputs_clear", id="outputs-clear"),
    pytest.param("/outputs/clear", {}, "outputs_clear", id="outputs-clear-all"),
    pytest.param("/env/install", {"packages": ["polars"]}, "env_install", id="env-install"),
    pytest.param(
        "/comm",
        {"comm_id": "w1", "msg_id": "m1", "content": {"method": "update"}},
        "comm",
        id="comm",
    ),
    pytest.param("/frames", {"output_id": "out-1"}, "frame_attach", id="frame-attach"),
]


@pytest.mark.parametrize(("path", "body", "op"), _KERNEL_ROUTES)
@pytest.mark.parametrize(
    ("who", "status", "decided"),
    [
        pytest.param("writer", 200, [("allow", "can_edit")], id="can-edit"),
        pytest.param("reader", 403, [("deny", "needs_can_edit")], id="can-view"),
    ],
)
async def test_every_act_through_the_kernel_is_decided_by_notebook_run(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    path: str,
    body: dict[str, Any],
    op: str,
    who: str,
    status: int,
    decided: list[tuple[str, str]],
) -> None:
    from backend.services.notebooks import frames

    box = await _holder(real_session, client, rig.fw)
    rig.transport.answers["frame_attach"] = {"result": {"opens": []}}
    person = getattr(rig.fw.world, who)
    if path == "/comm":
        body = {
            **body,
            "frame_id": frames.mint(owner=person.user.id, item_id=rig.fw.node_id, peer_id=None),
        }
    await login(client, person.user.email, person.password)
    answer = await client.post(rig.url(path), json=body)
    assert answer.status_code == status, answer.text
    (decision,) = await _decisions(rig.fw.world.org_id)
    assert _effects([decision]) == decided
    assert (decision.payload["action"], decision.payload["path"]) == ("run", rig.url(path))
    assert decision.payload["resource"]["id"] == str(rig.fw.node_id)
    assert decision.payload["attrs"]["rung"] == ("writer" if who == "writer" else "reader")
    reached = [(host.machine_id, sent_op) for host, sent_op, _ in rig.transport.sent]
    if status == 200:
        assert reached == [(box.machine_id, op)]
    else:
        assert answer.json()["error"]["code"] == "notebook.run_refused"
        assert reached == []


async def test_the_kernel_s_state_is_read_without_a_run_decision(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, rig.fw)
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.post(rig.url("/kernel"), json={"action": "status"})
    assert answer.status_code == 200, answer.text
    assert await _decisions(rig.fw.world.org_id) == []
    assert rig.transport.sent == []


@pytest.mark.parametrize(
    ("answer", "status", "code"),
    [
        pytest.param(
            {"error": {"code": "no_such_output", "message": "gone"}},
            409,
            "no_such_output",
            id="the-hub-refuses",
        ),
        pytest.param(None, 503, "notebook.kernel_silent", id="the-hub-is-silent"),
    ],
)
async def test_a_frame_the_hub_refuses_or_never_answers_is_not_attached(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    answer: dict[str, Any] | None,
    status: int,
    code: str,
) -> None:
    await _holder(real_session, client, rig.fw)
    rig.service.answer_seconds = 0.2
    if answer is not None:
        rig.transport.answers["frame_attach"] = answer
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    found = await client.post(rig.url("/frames"), json={"output_id": "out-1"})
    assert (found.status_code, found.json()["code"]) == (status, code)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("limit=0", id="no-rows"),
        pytest.param("limit=1001", id="too-many-rows"),
        pytest.param("offset=-1", id="before-the-first-row"),
    ],
)
async def test_a_table_page_out_of_bounds_is_refused_before_the_kernel(
    rig: Rig, client: AsyncClient, query: str
) -> None:
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.get(rig.url(f"/cells/{CELL_B}/table?{query}"))
    assert answer.status_code == 422
    assert rig.transport.sent == []


async def test_a_table_page_is_read_by_the_kernel_for_a_reader(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, rig.fw)
    page = {
        "schema": [{"name": "a", "type": "Int64"}, {"name": "day", "type": "Date"}],
        "rows": [[40, "2026-02-10"]],
        "total_rows": 41,
        "offset": 40,
    }
    rig.transport.answers["table"] = {"result": page}
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.get(
        rig.url(f"/cells/{CELL_B}/table"),
        params={
            "offset": 40,
            "limit": 10,
            "sort": "a:desc,b:asc",
            "filter_sql": "SELECT * FROM frame",
        },
    )
    assert answer.status_code == 200, answer.text
    # The kernel's page as the box answered it, with the limit asked for.
    assert answer.json() == {**page, "limit": 10}
    (_, op, sent) = rig.transport.sent[-1]
    assert (op, sent) == (
        "table",
        {
            "cell_id": CELL_B,
            "offset": 40,
            "limit": 10,
            "sort": [
                {"column": "a", "descending": True},
                {"column": "b", "descending": False},
            ],
            "filter_sql": "SELECT * FROM frame",
        },
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            {
                "error": {
                    "code": "table_not_held",
                    "message": "Run the cell again to page this table.",
                }
            },
            (409, "table_not_held", "Run the cell again to page this table."),
            id="the-kernel-no-longer-holds-the-frame",
        ),
        pytest.param(
            None,
            (503, "notebook.kernel_silent", "The machine did not answer. Try again."),
            id="the-box-is-silent",
        ),
        pytest.param(
            {"result": {"rows": {"columns": [{"name": "a"}], "rows": [[1]]}, "total_rows": 1}},
            (
                409,
                "notebook.table_unreadable",
                "The machine answered a table page this server can't read.",
            ),
            id="a-box-from-before-pages-had-one-shape",
        ),
    ],
)
async def test_a_table_page_that_cannot_be_read_says_what_to_do(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    answer: dict[str, Any] | None,
    expected: tuple[int, str, str],
) -> None:
    """The table shows the refusal's sentence as it is: the box's own when it
    answered, and one a person can act on when it never did."""
    await _holder(real_session, client, rig.fw)
    rig.service.answer_seconds = 0.2
    if answer is not None:
        rig.transport.answers["table"] = answer
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    found = await client.get(rig.url(f"/cells/{CELL_B}/table"), params={"offset": 50, "limit": 50})
    body = found.json()
    assert (found.status_code, body["code"], body["message"]) == expected


@pytest.mark.parametrize(
    "sort",
    [
        pytest.param("a:sideways", id="unknown-direction"),
        pytest.param(":desc", id="no-column"),
        pytest.param(",".join(f"c{n}:asc" for n in range(17)), id="too-many-keys"),
    ],
)
async def test_a_table_sort_that_does_not_read_is_refused_before_the_kernel(
    real_session: AsyncSession, rig: Rig, client: AsyncClient, sort: str
) -> None:
    await _holder(real_session, client, rig.fw)
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.get(rig.url(f"/cells/{CELL_B}/table"), params={"sort": sort})
    assert (answer.status_code, answer.json()["code"]) == (422, "notebook.bad_sort")
    assert all(op != "table" for _, op, _ in rig.transport.sent)


async def test_the_environments_and_their_packages_are_the_engine_s(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, rig.fw)
    env = {
        "env_id": "default",
        "kind": "default",
        "spec_root": ".alkera/envs/default",
        "python": "3.14.0",
        "state": "ready",
        "recorded_in_file": True,
    }
    rig.transport.answers["envs"] = {"result": {"current": env, "envs": [env]}}
    rig.transport.answers["env_packages"] = {
        "result": {"env_id": "default", "packages": [{"name": "polars", "version": "1.9.0"}]}
    }
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    envs = await client.get(rig.url("/envs"))
    packages = await client.get(rig.url("/envs/default/packages"))
    assert envs.status_code == packages.status_code == 200
    assert envs.json()["current"]["env_id"] == "default"
    # The engine's own shapes: ``EnvListing`` and ``EnvPackages``. A box
    # from before environment actions answers without them: they read as
    # none, so the panel offers nothing that box would refuse.
    assert envs.json()["current"] == {
        **env,
        "recorded": "",
        "last_failure": "",
        "allowed_actions": None,
    }
    assert packages.json() == {
        "env_id": "default",
        "packages": [{"name": "polars", "version": "1.9.0"}],
        "requirements": [],
    }
    assert ("env_packages", {"env_id": "default"}) in [(op, b) for _, op, b in rig.transport.sent]


@pytest.mark.parametrize(
    ("answered", "shared"),
    [
        pytest.param({"shared": False}, False, id="a-box-whose-members-keep-their-own"),
        pytest.param({"shared": True}, True, id="a-box-whose-members-share-them"),
        pytest.param({}, True, id="an-older-box-that-does-not-say"),
    ],
)
async def test_the_listing_passes_on_whether_the_box_shares_environments(
    real_session: AsyncSession,
    rig: Rig,
    client: AsyncClient,
    answered: dict[str, bool],
    shared: bool,
) -> None:
    """Whether the workspace's members share its environments is the box's
    word, passed through to the editor; a box that does not say shares them,
    as every box did before it could say otherwise."""
    await _holder(real_session, client, rig.fw)
    env = {
        "env_id": "default",
        "kind": "default",
        "spec_root": ".alkera/envs/default",
        "python": "3.14.0",
        "state": "ready",
        "recorded_in_file": True,
    }
    rig.transport.answers["envs"] = {"result": {"current": env, "envs": [env], **answered}}
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    envs = await client.get(rig.url("/envs"))
    assert envs.status_code == 200, envs.text
    assert envs.json()["shared"] is shared


async def test_a_widget_module_resolves_only_when_offered_or_a_platform_bundle(
    rig: Rig, client: AsyncClient
) -> None:
    entry = "e" * 64
    rig.service.feed.observe(
        _kernel_batch(
            rig.fw.world.org_id,
            rig.fw.node_id,
            [
                {
                    "seq": 1,
                    "type": "widget.asset",
                    "module": "bqplot",
                    "version": "0.12.45",
                    "files": [{"path": "index.js", "sha256": entry}],
                }
            ],
        )
    )
    rig.service.bundle_modules = {("@alkera/ui-widgets", "1.0.0"): "f" * 64}
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)

    async def resolve(module: str, version: str) -> Any:
        return await client.get(
            rig.url("/widget-assets/resolve"), params={"module": module, "version": version}
        )

    offered = await resolve("bqplot", "0.12.45")
    bundle = await resolve("@alkera/ui-widgets", "1.0.0")
    assert (offered.status_code, offered.json()) == (200, {"sha256": entry})
    assert (bundle.status_code, bundle.json()) == (200, {"sha256": "f" * 64})
    assert (await resolve("bqplot", "0.12.44")).status_code == 404
    assert (await resolve("ipyleaflet", "0.19.0")).status_code == 404


async def test_the_view_names_the_output_frame_page_on_the_content_origin(
    rig: Rig, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "files_content_base_url", "https://files.example.test")
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    first = (await client.get(rig.url())).json()["output_frame_url"]
    assert first.startswith("https://files.example.test/c/nb-output/")
    monkeypatch.setattr(settings, "build_id", "another-build")
    second = (await client.get(rig.url())).json()["output_frame_url"]
    assert second != first and len(second.rsplit("/", 1)[1]) == 16


# -- the engine's models on the wire ---------------------------------------------------


async def test_an_ops_result_carries_the_graph_as_the_engine_s_structured_errors(
    rig: Rig, client: AsyncClient
) -> None:
    """The analysis of exactly the result's state is answered as the engine's
    ``GraphSummary``: each error an object naming its code, name and cells
    (never the object's ``str``); without one the graph says not computed."""
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    pending = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 2")]})
    assert pending.status_code == 200, pending.text
    assert pending.json()["graph"] == {"computed": False, "cells": {}, "edges": []}
    rig.docs.graph = {
        "cells": {
            CELL_A: {
                "defs": ["x"],
                "refs": [],
                "errors": [
                    {"code": "multiple_definitions", "name": "x", "cells": [CELL_A, CELL_B]}
                ],
            },
            CELL_B: {"defs": ["x"], "refs": [], "errors": [{"code": "syntax"}]},
        },
        "edges": [[CELL_A, CELL_B]],
    }
    answer = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 3")]})
    assert answer.status_code == 200, answer.text
    result = NotebookOpsResult.model_validate(answer.json())
    assert result.graph.computed is True
    assert result.graph.edges == [(CELL_A, CELL_B)]
    assert [e.model_dump() for e in result.graph.cells[CELL_A].errors] == [
        {"code": "multiple_definitions", "name": "x", "cells": [CELL_A, CELL_B]}
    ]
    assert [e.code for e in result.graph.cells[CELL_B].errors] == ["syntax"]


def _snapshot(kernel_id: str, seq: int, cells: list[dict[str, Any]]) -> dict[str, Any]:
    """A kernel's ``snapshot`` event, as the engine on the box sends it."""
    return {
        "seq": seq,
        "type": "snapshot",
        "kernel_id": kernel_id,
        "view": {
            "path": "analysis.alknb.py",
            "token": "r3.abc",
            "settings": {"reactivity": "autorun"},
            "kernel": {
                "state": "idle",
                "kernel_id": kernel_id,
                "reactivity": "autorun",
                "memory_bytes": 1024,
                "env": {
                    "env_id": "default",
                    "kind": "default",
                    "spec_root": ".alkera/envs/default",
                    "python": "3.14",
                    "state": "ready",
                    "recorded_in_file": True,
                },
                "queue": [],
            },
            "cells": cells,
        },
    }


async def test_the_view_is_the_engine_s_with_each_cell_s_run_state_from_its_kernel(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """The document gives the cells (their text is the live document's, never
    the snapshot's); the current kernel's latest snapshot gives each cell's
    run state and the kernel's environment; a cell it does not report is
    ``not_run``."""
    box = await _holder(real_session, client, rig.fw)
    kernel_id = f"kernel-{uuid.uuid4().hex[:8]}"
    posted = await _post_events(rig, box, kernel_id, [{"seq": 1, "type": "kernel.state"}])
    assert posted.status_code == 200, posted.text
    reported = {
        "id": CELL_A,
        "name": "_",
        "kind": "python",
        "index": 0,
        "status": "fresh",
        "defs": ["x"],
        "source": "x = 'the kernel ran this text'",
        "output": {"kinds": ["text/plain"], "text": "1"},
        "outputs": [{"output_id": "o1", "type": "stream", "name": "stdout", "text": "hello\n"}],
        "last_run": {
            "run_id": "r1",
            "by": {"kind": "person", "id": "user:ann", "display_name": "Ann"},
            "trigger": "run",
        },
    }
    rig.service.feed.observe(
        _kernel_batch(
            rig.fw.world.org_id,
            rig.fw.node_id,
            [_snapshot(kernel_id, 2, [reported])],
            kernel_id=kernel_id,
        )
    )
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.get(rig.url())
    assert answer.status_code == 200, answer.text
    view = NotebookView.model_validate(answer.json())
    assert view.path == "analysis.alknb.py"
    by_id = {cell.id: cell for cell in view.cells}
    assert (by_id[CELL_A].status, by_id[CELL_A].defs, by_id[CELL_A].source) == (
        "fresh",
        ["x"],
        "x = 1",
    )
    assert by_id[CELL_A].output is not None and by_id[CELL_A].output.text == "1"
    assert by_id[CELL_A].last_run is not None and by_id[CELL_A].last_run.run_id == "r1"
    # The outputs themselves reach a client opening the notebook.
    assert [o.model_dump(include={"type", "text"}) for o in by_id[CELL_A].outputs] == [
        {"type": "stream", "text": "hello\n"}
    ]
    assert (by_id[CELL_B].status, by_id[CELL_B].output) == ("not_run", None)
    assert by_id[CELL_B].outputs == []
    assert (view.kernel.kernel_id, view.kernel.state, view.kernel.seq) == (kernel_id, "idle", 1)
    assert view.kernel.env is not None and view.kernel.env.env_id == "default"
    assert view.settings.reactivity == "autorun"


def _ran(cell_id: str, index: int, run_id: str, text: str, at: datetime) -> dict[str, Any]:
    """A cell's run state as a box's settled snapshot reports it."""
    return {
        "id": cell_id,
        "name": "_",
        "kind": "python",
        "index": index,
        "status": "fresh",
        "outputs": [
            {"output_id": f"{cell_id}/s0", "type": "stream", "name": "stdout", "text": text}
        ],
        "last_run": {
            "run_id": run_id,
            "by": {"kind": "person", "id": "user:ann", "display_name": "Ann"},
            "trigger": "run",
            "started_at": at.isoformat(),
            "finished_at": (at + timedelta(seconds=1)).isoformat(),
        },
    }


async def test_after_runs_of_different_cells_the_view_reports_every_cell_s_run(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """The box posts a whole snapshot each time the kernel settles: after a
    first run (cell A) and a later run of cell B, the view reads both cells'
    status, outputs and last run with its times from the latest snapshot,
    never the first one's state where only A had run."""
    box = await _holder(real_session, client, rig.fw)
    kernel_id = f"kernel-{uuid.uuid4().hex[:8]}"
    first_at = datetime(2026, 10, 5, 23, 27, 47, tzinfo=UTC)
    second_at = first_at + timedelta(minutes=10)
    first = _snapshot(kernel_id, 2, [_ran(CELL_A, 0, "r1", "one\n", first_at)])
    run_b = [
        {"seq": 3, "type": "run.started", "run_id": "r2", "plan": []},
        {"seq": 4, "type": "cell.status", "cell_id": CELL_B, "status": "running", "run_id": "r2"},
        {"seq": 5, "type": "run.finished", "run_id": "r2", "status": "ok"},
        {"seq": 6, "type": "kernel.state", "state": "idle"},
    ]
    second = _snapshot(
        kernel_id,
        7,
        [
            _ran(CELL_A, 0, "r1", "one\n", first_at),
            _ran(CELL_B, 1, "r2", "two\n", second_at),
        ],
    )
    batches = [[{"seq": 1, "type": "kernel.state", "state": "idle"}, first], run_b, [second]]
    for batch in batches:
        posted = await _post_events(rig, box, kernel_id, batch)
        assert posted.status_code == 200, posted.text
        rig.service.feed.observe(
            _kernel_batch(rig.fw.world.org_id, rig.fw.node_id, batch, kernel_id=kernel_id)
        )
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.get(rig.url())
    assert answer.status_code == 200, answer.text
    view = NotebookView.model_validate(answer.json())
    by_id = {cell.id: cell for cell in view.cells}
    for cell_id, run_id, text, at in (
        (CELL_A, "r1", "one\n", first_at),
        (CELL_B, "r2", "two\n", second_at),
    ):
        cell = by_id[cell_id]
        assert cell.status == "fresh"
        assert [o.model_dump(include={"text"}) for o in cell.outputs] == [{"text": text}]
        assert cell.last_run is not None and cell.last_run.run_id == run_id
        assert cell.last_run.started_at == at
        assert cell.last_run.finished_at == at + timedelta(seconds=1)
    assert view.kernel.seq == 7


async def test_a_snapshot_of_another_kernel_lends_its_outputs_as_saved_and_no_status(
    real_session: AsyncSession, rig: Rig, client: AsyncClient
) -> None:
    """Statuses are relative to the current kernel: a replica still holding an
    earlier kernel's snapshot shows every cell ``not_run`` and none of that
    kernel's environment. What the cells showed is still shown, as saved
    output, so a restart or a sleep does not blank the notebook."""
    box = await _holder(real_session, client, rig.fw)
    old, new = f"kernel-old-{uuid.uuid4().hex[:6]}", f"kernel-new-{uuid.uuid4().hex[:6]}"
    assert (await _post_events(rig, box, old, [{"seq": 1, "type": "kernel.state"}])).is_success
    rig.service.feed.observe(
        _kernel_batch(
            rig.fw.world.org_id,
            rig.fw.node_id,
            [
                _snapshot(
                    old,
                    2,
                    [
                        {
                            "id": CELL_A,
                            "name": "_",
                            "kind": "python",
                            "index": 0,
                            "status": "fresh",
                            "output": {"kinds": ["text/plain"], "text": "42"},
                            "output_origin": "kernel",
                        }
                    ],
                )
            ],
            kernel_id=old,
        )
    )
    async with AsyncSessionLocal() as db:
        # The box restarted the kernel: the platform's record is of the new one.
        stored = await db.get(NotebookKernel, old)
        assert stored is not None
        db.add(
            NotebookKernel(
                kernel_id=new,
                org_id=stored.org_id,
                drive_id=stored.drive_id,
                item_id=stored.item_id,
                machine_id=stored.machine_id,
                state="starting",
                seq=0,
                started_at=datetime.now(UTC) + timedelta(seconds=1),
                updated_at=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        await db.commit()
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    view = NotebookView.model_validate((await client.get(rig.url())).json())
    assert view.kernel.kernel_id == new and view.kernel.env is None
    assert [cell.status for cell in view.cells] == ["not_run", "not_run"]
    shown = {cell.id: cell for cell in view.cells}
    assert shown[CELL_A].output is not None and shown[CELL_A].output.text == "42"
    assert shown[CELL_A].output_origin == "saved"
    assert [c.output for c in view.cells if c.id != CELL_A] == [None]


async def test_the_kernel_route_answers_the_engine_s_kernel_info(
    rig: Rig, client: AsyncClient
) -> None:
    """With no kernel heard of, the kernel is ``absent`` (never ``null``),
    with the notebook's reactivity."""
    await login(client, rig.fw.world.reader.user.email, rig.fw.world.reader.password)
    answer = await client.post(rig.url("/kernel"), json={"action": "status"})
    assert answer.status_code == 200, answer.text
    kernel = KernelInfo.model_validate(answer.json()["kernel"])
    assert (kernel.state, kernel.kernel_id, kernel.reactivity) == ("absent", None, "autorun")


@pytest.mark.parametrize(
    ("refusal", "status", "body"),
    [
        pytest.param(
            NotebookOpError(2, "cap_exceeded", "a notebook file holds at most 4 MiB"),
            422,
            {
                "type": "urn:alkera:error:cap_exceeded",
                "code": "cap_exceeded",
                "status": 422,
                "message": "a notebook file holds at most 4 MiB",
                "details": {"op_index": 2},
            },
            id="file-cap-names-the-operation",
        ),
        pytest.param(
            CrdtError("doc_full", "the document's history is full; it is being restarted"),
            409,
            {
                "type": "urn:alkera:error:doc_full",
                "code": "doc_full",
                "status": 409,
                "message": "the document's history is full; it is being restarted",
            },
            id="history-cap",
        ),
    ],
)
async def test_a_notebook_at_its_cap_is_a_structured_refusal(
    rig: Rig, client: AsyncClient, refusal: Exception, status: int, body: dict[str, Any]
) -> None:
    """The per-notebook caps (4 MiB of rendered file, 8 MiB of history) reach
    the writer as a code it can act on, never a bare failure."""
    rig.docs.refuse = refusal
    await login(client, rig.fw.world.writer.user.email, rig.fw.world.writer.password)
    answer = await client.post(rig.url("/ops"), json={"ops": [_edit(CELL_A, "x = 2")]})
    error = dict(answer.json()["error"])
    error.pop("trace_id")
    assert (answer.status_code, error) == (status, body)
