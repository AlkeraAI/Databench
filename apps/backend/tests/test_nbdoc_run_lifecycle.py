"""A notebook run's life on the platform, and how every answer names who.

Through the real routes on real Postgres and real Files (the live document is
the in-memory stand-in of ``test_nbdoc_routes``):

* a run the platform recorded moves through the statuses the engine reports
  for it (its ``answer``, then ``run.*`` events), never backwards, with its
  kernel and when it started and ended;
* a run the box began (an agent's, a widget's) is recorded from its
  ``run.queued``, attributed to the agent and the person it acts for only
  when the box serves that chat;
* a request the box missed while its worker restarted is sent again under
  the same id, and a run it never answers ends rather than waits forever;
* an environment id with slashes reaches the box whole, and an event
  sequence past what the outbox keeps is a 422, never a 500;
* every name an answer carries is a person's name, "<agent> for <name>" or
  the product's name, never a raw id.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core import brand
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, User, WorkspaceObject
from alkera_core.notebooks.models import NotebookEdit, NotebookKernel, NotebookRun
from alkera_core.schemas.realtime.machine import NotebookRequestOp
from backend.services.notebooks.feed import MAX_EVENT_SEQ
from backend.services.notebooks.names import FORMER_MEMBER, Names, usable
from backend.services.notebooks.transport import KernelHost
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import login
from tests.test_nbdoc_routes import (
    CELL_A,
    CELL_B,
    Box,
    Rig,
    _holder,
    _kernel_batch,
    _snapshot,
    rig,  # noqa: F401
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


# -- helpers ------------------------------------------------------------------------


@pytest.fixture
async def nb(rig: Rig) -> Rig:  # noqa: F811 - pytest names a fixture's input by its name
    """The notebook routes' rig (``test_nbdoc_routes``)."""
    return rig


async def _post(
    nb: Rig, box: Box, kernel_id: str, events: list[dict[str, Any]], *, state: str | None = None
) -> Any:
    return await box.client.post(
        nb.url("/events"), json={"kernel_id": kernel_id, "state": state, "events": events}
    )


async def _run(run_id: str) -> NotebookRun:
    async with AsyncSessionLocal() as db:
        found = (
            await db.execute(select(NotebookRun).where(NotebookRun.engine_run_id == run_id))
        ).scalar_one()
        return found


async def _runs(nb: Rig) -> list[NotebookRun]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(NotebookRun)
            .where(NotebookRun.item_id == nb.fw.node_id)
            .order_by(NotebookRun.created_at)
        )
        return list(rows.scalars())


async def _start_run(nb: Rig, client: AsyncClient) -> str:
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    answer = await client.post(nb.url("/runs"), json={"target": {"kind": "cells", "ids": [CELL_B]}})
    assert answer.status_code == 200, answer.text
    run_id: str = answer.json()["run_id"]
    return run_id


async def _bind(db: AsyncSession, chat_id: str, machine_id: str | None) -> None:
    """Bind the chat to ``machine_id`` the way placement does."""
    chat = await db.get(WorkspaceObject, uuid.UUID(chat_id))
    assert chat is not None
    spec = dict(chat.spec or {})
    spec["machine_id"] = machine_id
    await db.execute(update(WorkspaceObject).where(WorkspaceObject.id == chat.id).values(spec=spec))
    await db.commit()


def _name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip()


def _kernel() -> str:
    return f"krn_{uuid.uuid4().hex[:12]}"


# -- a run the platform recorded ------------------------------------------------------


@pytest.mark.parametrize(
    ("finished", "status", "reason"),
    [
        pytest.param({"status": "ok"}, "ok", None, id="ok"),
        pytest.param({"status": "error"}, "error", None, id="error"),
        pytest.param(
            {"status": "interrupted", "reason": "interrupt"},
            "interrupted",
            "interrupt",
            id="interrupted",
        ),
        pytest.param(
            {"status": "kernel_restarted", "reason": "out_of_memory"},
            "kernel_restarted",
            "out_of_memory",
            id="kernel-restarted",
        ),
        pytest.param(
            {"status": "refused", "reason": "upstream_being_edited"},
            "refused",
            "upstream_being_edited",
            id="refused-at-dequeue",
        ),
    ],
)
async def test_a_run_follows_the_engine_from_queued_to_how_it_ended(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    finished: dict[str, Any],
    status: str,
    reason: str | None,
) -> None:
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    kernel_id = _kernel()

    queued = await _run(run_id)
    assert (queued.status, queued.kernel_id, queued.started_at, queued.finished_at) == (
        "queued",
        None,
        None,
        None,
    )
    posted = await _post(
        nb,
        box,
        kernel_id,
        [
            {"seq": 1, "type": "answer", "request_id": run_id, "result": {"status": "queued"}},
            {"seq": 2, "type": "run.queued", "run_id": run_id, "trigger": "run"},
            {"seq": 3, "type": "run.started", "run_id": run_id, "plan": []},
        ],
    )
    assert posted.status_code == 200, posted.text
    running = await _run(run_id)
    assert (running.status, running.kernel_id) == ("running", kernel_id)
    assert running.started_at is not None and running.finished_at is None

    posted = await _post(
        nb, box, kernel_id, [{"seq": 4, "type": "run.finished", "run_id": run_id, **finished}]
    )
    assert posted.status_code == 200, posted.text
    ended = await _run(run_id)
    assert (ended.status, ended.reason, ended.kernel_id) == (status, reason, kernel_id)
    assert ended.finished_at is not None and ended.started_at is not None
    assert ended.started_at <= ended.finished_at


async def test_a_run_is_sent_under_its_own_id_so_the_box_s_answer_names_it(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    sent_ids: list[uuid.UUID | None] = []
    original = nb.transport.send

    async def send(
        host: KernelHost,
        op: NotebookRequestOp,
        body: Any,
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        if op == "run":
            sent_ids.append(request_id)
        return await original(host, op, body, request_id=request_id)

    nb.transport.send = send  # type: ignore[method-assign]
    await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    assert sent_ids == [uuid.UUID(run_id)]
    assert (await _run(run_id)).engine_run_id == run_id


@pytest.mark.parametrize(
    ("answer", "status", "reason", "ended"),
    [
        pytest.param(
            {"result": {"run_id": "R", "status": "needs_confirmation", "reason": "cost"}},
            "needs_confirmation",
            "cost",
            False,
            id="needs-confirmation",
        ),
        pytest.param(
            {"result": {"run_id": "run_0123456789abcdef", "status": "coalesced"}},
            "coalesced",
            "run_0123456789abcdef",
            True,
            id="coalesced-names-the-run-it-joined",
        ),
        pytest.param(
            {"result": {"run_id": "R", "status": "refused", "reason": "kernel_unavailable"}},
            "refused",
            "kernel_unavailable",
            True,
            id="refused-at-request",
        ),
        pytest.param(
            {"error": {"code": "not_found", "message": "this box does not hold the folder"}},
            "refused",
            "not_found",
            True,
            id="the-box-could-not-serve-it",
        ),
    ],
)
async def test_what_the_engine_made_of_a_run_request_is_the_run_s_status(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    answer: dict[str, Any],
    status: str,
    reason: str,
    ended: bool,
) -> None:
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    if "result" in answer and answer["result"].get("run_id") == "R":
        answer = {"result": {**answer["result"], "run_id": run_id}}
    posted = await _post(
        nb, box, _kernel(), [{"seq": 1, "type": "answer", "request_id": run_id, **answer}]
    )
    assert posted.status_code == 200, posted.text
    run = await _run(run_id)
    assert (run.status, run.reason) == (status, reason)
    assert (run.finished_at is not None) is ended


async def test_a_run_s_status_never_moves_back(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """A late ``queued`` answer does not undo ``running``, and an ended run
    stays ended whatever arrives after."""
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    kernel_id = _kernel()
    await _post(nb, box, kernel_id, [{"seq": 1, "type": "run.started", "run_id": run_id}])
    await _post(
        nb,
        box,
        kernel_id,
        [{"seq": 2, "type": "answer", "request_id": run_id, "result": {"status": "queued"}}],
    )
    assert (await _run(run_id)).status == "running"
    await _post(
        nb, box, kernel_id, [{"seq": 3, "type": "run.finished", "run_id": run_id, "status": "ok"}]
    )
    await _post(
        nb,
        box,
        kernel_id,
        [
            {"seq": 4, "type": "run.started", "run_id": run_id},
            {"seq": 5, "type": "run.finished", "run_id": run_id, "status": "error"},
        ],
    )
    assert (await _run(run_id)).status == "ok"


async def test_another_notebook_s_events_do_not_move_a_run(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """Rows are found by the notebook the batch was posted for: an event
    naming this run's id on a run of another notebook moves nothing here."""
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(NotebookRun)
            .where(NotebookRun.engine_run_id == run_id)
            .values(item_id=uuid.uuid4())
        )
        await db.commit()
    await _post(nb, box, _kernel(), [{"seq": 1, "type": "run.started", "run_id": run_id}])
    assert (await _run(run_id)).status == "queued"


# -- runs the box began ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "kind"),
    [
        pytest.param("agent-of-a-chat-it-serves", "agent", id="agent-for-its-person"),
        pytest.param("agent-of-a-chat-it-does-not-serve", "system", id="agent-chat-elsewhere"),
        pytest.param("person-who-stands", "person", id="a-member-s-widget-run"),
        pytest.param("person-unknown", "system", id="a-person-nobody-knows"),
        pytest.param("nobody", "system", id="the-box-itself"),
    ],
)
async def test_a_run_the_box_began_is_recorded_for_who_it_may_speak_for(
    real_session: AsyncSession, nb: Rig, client: AsyncClient, case: str, kind: str
) -> None:
    box = await _holder(real_session, client, nb.fw)
    chat_id = nb.fw.world.ref.doc_id
    owner = nb.fw.world.owner.user
    writer = nb.fw.world.writer.user
    await _bind(
        real_session,
        chat_id,
        str(uuid.uuid4()) if case == "agent-of-a-chat-it-does-not-serve" else box.machine_id,
    )
    requested_by = {
        "agent-of-a-chat-it-serves": {"kind": "agent", "id": f"agent:{chat_id}"},
        "agent-of-a-chat-it-does-not-serve": {"kind": "agent", "id": f"agent:{chat_id}"},
        "person-who-stands": {"kind": "person", "id": f"user:{writer.id}"},
        "person-unknown": {"kind": "person", "id": f"user:{uuid.uuid4()}"},
        "nobody": {"kind": "system", "id": "box"},
    }[case]
    engine_id = f"run_{uuid.uuid4().hex[:16]}"
    kernel_id = _kernel()
    posted = await _post(
        nb,
        box,
        kernel_id,
        [
            {
                "seq": 1,
                "type": "run.queued",
                "run_id": engine_id,
                "trigger": "widget" if case.startswith("person") else "run",
                "position": 1,
                "requested_by": {**requested_by, "display_name": "whatever the box says"},
            },
            {
                "seq": 2,
                "type": "run.started",
                "run_id": engine_id,
                "plan": [
                    {"cell_id": CELL_A, "reason": "upstream"},
                    {"cell_id": CELL_B, "reason": "target"},
                ],
            },
            {"seq": 3, "type": "run.finished", "run_id": engine_id, "status": "ok"},
        ],
    )
    assert posted.status_code == 200, posted.text
    run = await _run(engine_id)
    assert (run.status, run.actor_kind, run.kernel_id) == ("ok", kind, kernel_id)
    assert run.target == {"kind": "cells", "ids": [CELL_B]}
    expected = {
        "agent": (owner.id, chat_id, f"{brand.agent_name()} for {_name(owner)}"),
        "person": (writer.id, None, _name(writer)),
        "system": (None, None, brand.product_name()),
    }[kind]
    assert (run.requested_by_user_id, run.requested_by_agent, run.actor_display) == expected

    await login(client, nb.fw.world.reader.user.email, nb.fw.world.reader.password)
    activity = await client.get(nb.url("/activity"), params={"since": "2000-01-01T00:00:00Z"})
    assert activity.status_code == 200, activity.text
    (item,) = [i for i in activity.json()["items"] if i["kind"] == "run"]
    assert (item["run_id"], item["status"], item["cell_ids"]) == (engine_id, "ok", [CELL_B])
    assert item["actor"]["display_name"] == expected[2]
    if kind == "agent":
        assert item["actor"]["acting_for"] == {
            "id": f"user:{owner.id}",
            "display_name": _name(owner),
        }
    else:
        assert item["actor"]["acting_for"] is None


async def test_an_agent_run_the_box_began_is_recorded_once(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    box = await _holder(real_session, client, nb.fw)
    chat_id = nb.fw.world.ref.doc_id
    await _bind(real_session, chat_id, box.machine_id)
    queued = {
        "type": "run.queued",
        "run_id": "run_00000000000000aa",
        "trigger": "run",
        "requested_by": {"kind": "agent", "id": f"agent:{chat_id}"},
    }
    kernel_id = _kernel()
    await _post(nb, box, kernel_id, [{"seq": 1, **queued}])
    # The engine restarted its kernel and names the run again on the new one.
    await _post(nb, box, _kernel(), [{"seq": 1, **queued}])
    assert [run.engine_run_id for run in await _runs(nb)] == ["run_00000000000000aa"]


# -- the box away and back ----------------------------------------------------------------


@dataclass
class Restarting:
    """A box whose channel is down for the first ``missed`` requests (its
    worker restarting): those never arrive; the next is answered."""

    nb: Rig
    missed: int
    answer: dict[str, Any]
    sent: list[uuid.UUID] = field(default_factory=list)

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Any,
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        request_id = request_id or uuid.uuid4()
        self.sent.append(request_id)
        self.nb.transport.sent.append((host, op, dict(body)))
        if len(self.sent) > self.missed:
            self.nb.service.feed.observe(
                _kernel_batch(
                    host.org_id,
                    host.item_id,
                    [
                        {
                            "seq": len(self.sent),
                            "type": "answer",
                            "request_id": str(request_id),
                            **self.answer,
                        }
                    ],
                )
            )
        return request_id


async def test_a_frame_attached_while_the_box_restarts_is_attached_once_it_is_back(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    box = Restarting(nb, missed=2, answer={"result": {"opens": []}})
    nb.service.transport = box
    nb.service.answer_seconds = 5.0
    nb.service.resend_seconds = 0.05
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    found = await client.post(nb.url("/frames"), json={"output_id": "out-1"})
    assert found.status_code == 200, found.text
    # The same request, sent again under one id: the box serves it once.
    assert len(box.sent) == 3 and len(set(box.sent)) == 1


async def test_a_box_that_never_comes_back_is_a_retryable_refusal(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    box = Restarting(nb, missed=10_000, answer={})
    nb.service.transport = box
    nb.service.answer_seconds = 0.3
    nb.service.resend_seconds = 0.05
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    found = await client.post(nb.url("/frames"), json={"output_id": "out-1"})
    assert (found.status_code, found.json()["code"]) == (503, "notebook.kernel_silent")
    assert found.headers["Retry-After"] == "2"
    assert len(box.sent) > 1


async def test_a_run_sent_while_the_box_restarts_reaches_it_once_it_is_back(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    box = Restarting(nb, missed=1, answer={"result": {"status": "queued"}})
    nb.service.transport = box
    nb.service.answer_seconds = 5.0
    nb.service.resend_seconds = 0.05
    run_id = await _start_run(nb, client)
    await nb.service.settle()
    assert box.sent == [uuid.UUID(run_id)] * 2
    assert (await _run(run_id)).status == "queued"


async def test_a_run_the_box_never_answers_ends_instead_of_waiting_forever(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    box = Restarting(nb, missed=10_000, answer={})
    nb.service.transport = box
    nb.service.answer_seconds = 0.3
    nb.service.resend_seconds = 0.05
    run_id = await _start_run(nb, client)
    await nb.service.settle()
    run = await _run(run_id)
    assert (run.status, run.reason) == ("refused", "machine_silent")
    assert run.finished_at is not None


NOT_HELD_ANSWER: dict[str, Any] = {
    "error": {"code": "folder_not_held", "message": "this box does not hold the notebook's folder"}
}


@dataclass
class TakingFolder:
    """A box that has just started: its worker is up and answers at once,
    but it answers ``folder_not_held`` until it has taken the notebook's folder,
    which happens after its first ``pending`` sends."""

    nb: Rig
    pending: int
    sent: list[uuid.UUID] = field(default_factory=list)

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Any,
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        request_id = request_id or uuid.uuid4()
        self.sent.append(request_id)
        self.nb.transport.sent.append((host, op, dict(body)))
        answer = (
            NOT_HELD_ANSWER if len(self.sent) <= self.pending else {"result": {"status": "queued"}}
        )
        self.nb.service.feed.observe(
            _kernel_batch(
                host.org_id,
                host.item_id,
                [
                    {
                        "seq": len(self.sent),
                        "type": "answer",
                        "request_id": str(request_id),
                        **answer,
                    }
                ],
            )
        )
        return request_id


async def test_a_run_reaching_a_box_still_taking_the_folder_waits_and_reaches_it(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """A restarted box's worker is ready before it has taken the workspace's
    folder. A run that arrives in between is not refused: the box's
    ``folder_not_held`` keeps it queued and the same request is sent again, past
    the plain answer wait, until the box serves it."""
    await _holder(real_session, client, nb.fw)
    box = TakingFolder(nb, pending=3)
    nb.service.transport = box
    nb.service.answer_seconds = 0.2
    nb.service.resend_seconds = 0.1
    nb.service.ready_seconds = 10.0
    run_id = await _start_run(nb, client)
    await nb.service.settle()
    assert box.sent == [uuid.UUID(run_id)] * 4
    run = await _run(run_id)
    assert (run.status, run.finished_at) == ("queued", None)


async def test_a_box_s_folder_not_held_answer_leaves_the_run_queued(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The answer the box posts is the run's record too: ``folder_not_held`` is
    not a refusal, unlike a box that will not serve the folder."""
    box = await _holder(real_session, client, nb.fw)
    nb.service.answer_seconds = 30.0
    run_id = await _start_run(nb, client)
    posted = await _post(
        nb, box, _kernel(), [{"seq": 1, "type": "answer", "request_id": run_id, **NOT_HELD_ANSWER}]
    )
    assert posted.status_code == 200, posted.text
    run = await _run(run_id)
    assert (run.status, run.reason, run.finished_at) == ("queued", None, None)


async def test_a_box_that_never_takes_the_folder_ends_the_run_folder_not_held(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    box = TakingFolder(nb, pending=10_000)
    nb.service.transport = box
    nb.service.answer_seconds = 0.2
    nb.service.resend_seconds = 0.05
    nb.service.ready_seconds = 0.5
    run_id = await _start_run(nb, client)
    await nb.service.settle()
    run = await _run(run_id)
    assert (run.status, run.reason) == ("refused", "folder_not_held")
    assert run.finished_at is not None and len(box.sent) > 2


async def test_a_request_for_a_folder_the_box_still_does_not_hold_is_a_retryable_refusal(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    nb.service.transport = TakingFolder(nb, pending=10_000)
    nb.service.answer_seconds = 0.2
    nb.service.resend_seconds = 0.05
    nb.service.ready_seconds = 0.4
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    found = await client.post(nb.url("/frames"), json={"output_id": "out-1"})
    assert (found.status_code, found.json()["code"]) == (503, "folder_not_held")
    assert found.headers["Retry-After"] == "2"


async def test_a_run_the_box_speaks_of_after_its_silence_takes_the_engine_s_word(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The platform ended it ``machine_silent`` because no answer came in
    time; a box that then starts it is believed, or the person would see
    "refused" next to the outputs of a run that ran."""
    holder = await _holder(real_session, client, nb.fw)
    nb.service.transport = Restarting(nb, missed=10_000, answer={})
    nb.service.answer_seconds = 0.3
    nb.service.resend_seconds = 0.05
    run_id = await _start_run(nb, client)
    await nb.service.settle()
    assert (await _run(run_id)).reason == "machine_silent"
    kernel_id = _kernel()

    posted = await _post(
        nb, holder, kernel_id, [{"seq": 1, "type": "run.started", "run_id": run_id, "plan": []}]
    )
    assert posted.status_code == 200, posted.text
    running = await _run(run_id)
    assert (running.status, running.reason, running.finished_at) == ("running", None, None)

    posted = await _post(
        nb,
        holder,
        kernel_id,
        [{"seq": 2, "type": "run.finished", "run_id": run_id, "status": "ok"}],
    )
    assert posted.status_code == 200, posted.text
    assert (await _run(run_id)).status == "ok"


@dataclass
class Unreachable:
    """A machine channel the request cannot be written to."""

    async def send(
        self,
        host: KernelHost,
        op: NotebookRequestOp,
        body: Any,
        *,
        request_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        raise ConnectionError("outbox unavailable")


async def test_a_run_whose_request_was_never_sent_ends_at_once(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """Nothing will ever answer it, so it must not sit ``queued``."""
    await _holder(real_session, client, nb.fw)
    nb.service.transport = Unreachable()
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    with contextlib.suppress(ConnectionError):
        answer = await client.post(
            nb.url("/runs"), json={"target": {"kind": "cells", "ids": [CELL_B]}}
        )
        assert answer.status_code >= 500
    (run,) = await _runs(nb)
    assert (run.status, run.reason) == ("refused", "not_sent")
    assert run.finished_at is not None


# -- environments -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelled",
    [
        pytest.param("default:.alkera/envs/default", id="as-the-engine-names-it"),
        pytest.param("default:.alkera%2Fenvs%2Fdefault", id="slashes-encoded"),
    ],
)
async def test_an_environment_named_by_its_spec_root_reaches_the_box_whole(
    real_session: AsyncSession, nb: Rig, client: AsyncClient, spelled: str
) -> None:
    await _holder(real_session, client, nb.fw)
    env_id = "default:.alkera/envs/default"
    nb.transport.answers["env_packages"] = {
        "result": {"env_id": env_id, "packages": [{"name": "polars", "version": "1.9.0"}]}
    }
    await login(client, nb.fw.world.reader.user.email, nb.fw.world.reader.password)
    found = await client.get(nb.url(f"/envs/{spelled}/packages"))
    assert found.status_code == 200, found.text
    assert found.json()["env_id"] == env_id
    assert [b for _, op, b in nb.transport.sent if op == "env_packages"] == [{"env_id": env_id}]


# -- the event sequence ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seq", "status"),
    [
        pytest.param(MAX_EVENT_SEQ, 200, id="the-largest-the-outbox-keeps"),
        pytest.param(MAX_EVENT_SEQ + 1, 422, id="one-past-it"),
        pytest.param(1_759_660_000_000_000, 422, id="a-microsecond-clock"),
        pytest.param(-1, 422, id="below-zero"),
    ],
)
async def test_an_event_sequence_the_outbox_cannot_keep_is_refused_by_name(
    real_session: AsyncSession, nb: Rig, client: AsyncClient, seq: int, status: int
) -> None:
    box = await _holder(real_session, client, nb.fw)
    kernel_id = _kernel()
    posted = await _post(
        nb, box, kernel_id, [{"seq": 1, "type": "kernel.state"}, {"seq": seq, "type": "x"}]
    )
    assert posted.status_code == status, posted.text
    async with AsyncSessionLocal() as db:
        kernel = await db.get(NotebookKernel, kernel_id)
        announced = (
            await db.execute(
                select(EventOutbox).where(EventOutbox.entity_id == f"nb:{nb.fw.node_id}")
            )
        ).scalars()
        rows = list(announced)
    if status == 200:
        assert kernel is not None and kernel.seq == MAX_EVENT_SEQ
        assert [row.version for row in rows] == [MAX_EVENT_SEQ]
        return
    assert posted.json()["code"] == "notebook.seq_out_of_range"
    assert kernel is None and rows == []


# -- names ------------------------------------------------------------------------------------


async def test_the_activity_names_people_now_and_remembers_those_gone(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """A renamed person reads under their new name; a person no longer known
    reads under the name last recorded for them, or as "A former member"
    when nothing usable was; no raw id is ever a name."""
    writer = nb.fw.world.writer.user
    at = datetime.now(UTC)
    gone = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                NotebookEdit(
                    org_id=nb.fw.world.org_id,
                    item_id=nb.fw.node_id,
                    cell_id=CELL_A,
                    actor_key=f"user:{writer.id}",
                    actor_kind="person",
                    user_id=writer.id,
                    actor_display="An old name",
                    first_at=at,
                    last_at=at,
                ),
                NotebookEdit(
                    org_id=nb.fw.world.org_id,
                    item_id=nb.fw.node_id,
                    cell_id=CELL_B,
                    actor_key=f"user:{gone}",
                    actor_kind="person",
                    user_id=None,
                    actor_display="Dee Parted",
                    first_at=at,
                    last_at=at,
                ),
                NotebookEdit(
                    org_id=nb.fw.world.org_id,
                    item_id=nb.fw.node_id,
                    cell_id=CELL_A,
                    actor_key=f"user:{uuid.uuid4()}",
                    actor_kind="person",
                    user_id=None,
                    actor_display=f"user:{gone}",
                    first_at=at,
                    last_at=at,
                ),
            ]
        )
        await db.execute(update(User).where(User.id == writer.id).values(first_name="Renamed"))
        await db.commit()
    await login(client, nb.fw.world.reader.user.email, nb.fw.world.reader.password)
    activity = await client.get(nb.url("/activity"), params={"since": "2000-01-01T00:00:00Z"})
    assert activity.status_code == 200, activity.text
    named = sorted(item["actor"]["display_name"] for item in activity.json()["items"])
    assert named == sorted([f"Renamed {writer.last_name}", "Dee Parted", FORMER_MEMBER])


async def test_a_caret_with_no_name_is_named_by_its_person(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """A caret carries its person's id: the view names them, never
    "someone"."""
    from alkera_core.events import HubEvent

    reader = nb.fw.world.reader.user
    channel = f"doc:notebook:{nb.fw.node_id}"
    nb.service.carets.observe(
        HubEvent(
            lane="ephemeral",
            org_id=nb.fw.world.org_id,
            type="doc.crdt_ephemeral",
            entity="doc",
            entity_id=channel,
            version=0,
            visibility="org",
            payload={
                "envelope": {"payload": {"loro_peer": 3000, "user_id": str(reader.id)}},
                "caret_cell": CELL_B,
            },
            channel=channel,
        )
    )
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    view = await client.get(nb.url())
    assert view.status_code == 200, view.text
    assert [(p["who"], p["cell_id"]) for p in view.json()["presence"]] == [(_name(reader), CELL_B)]
    edited = await client.post(
        nb.url("/ops"), json={"ops": [{"op": "replace", "cell_id": CELL_B, "source": "y = 3"}]}
    )
    assert edited.status_code == 200, edited.text
    assert [(n["kind"], n["by"]) for n in edited.json()["notices"]] == [
        ("caret_present", _name(reader))
    ]


async def test_a_cell_s_last_run_is_named_from_the_run_s_record(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The kernel names a cell's last run by the run's id and its requester's
    raw id; the view names the requester as the record knows them, with when
    the run started and ended."""
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    kernel_id = _kernel()
    await _post(
        nb,
        box,
        kernel_id,
        [
            {"seq": 1, "type": "run.started", "run_id": run_id},
            {"seq": 2, "type": "run.finished", "run_id": run_id, "status": "ok"},
        ],
        state="idle",
    )
    writer = nb.fw.world.writer.user
    reported = {
        "id": CELL_B,
        "name": "_",
        "kind": "python",
        "index": 1,
        "status": "fresh",
        "last_run": {"run_id": run_id, "by": f"user:{writer.id}", "trigger": "run"},
    }
    nb.service.feed.observe(
        _kernel_batch(
            nb.fw.world.org_id,
            nb.fw.node_id,
            [_snapshot(kernel_id, 3, [reported])],
            kernel_id=kernel_id,
        )
    )
    view = await client.get(nb.url())
    assert view.status_code == 200, view.text
    (cell,) = [c for c in view.json()["cells"] if c["id"] == CELL_B]
    by = cell["last_run"]["by"]
    # The engine's attribution is the whole actor: its id stays an id, and the
    # label a reader sees is the person's name, never the id.
    assert by["display_name"] == _name(writer)
    assert by["kind"] == "person" and by["acting_for"] is None
    assert "user:" not in by["display_name"]
    run = await _run(run_id)
    assert cell["last_run"]["finished_at"] is not None
    assert datetime.fromisoformat(cell["last_run"]["finished_at"]) == run.finished_at


# -- the names themselves --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "kept"),
    [
        pytest.param("Ann Lee", "Ann Lee", id="a-name"),
        pytest.param("", None, id="empty"),
        pytest.param("  ", None, id="blank"),
        pytest.param("someone", None, id="a-placeholder"),
        pytest.param(f"user:{uuid.uuid4()}", None, id="a-person-s-raw-id"),
        pytest.param("agent:abc", None, id="an-agent-s-raw-id"),
        pytest.param("machine:m1", None, id="a-machine-s-raw-id"),
        pytest.param(None, None, id="nothing"),
    ],
)
async def test_only_a_recorded_name_that_names_someone_is_kept(
    recorded: str | None, kept: str | None
) -> None:
    assert usable(recorded) == kept


async def test_an_agent_whose_person_is_gone_is_named_from_what_was_recorded(
    real_session: AsyncSession,
) -> None:
    known = Names(real_session)
    await known.load([uuid.uuid4()])
    remembered = known.actor(
        kind="agent",
        actor_key="agent:c",
        user_id=None,
        recorded=f"{brand.agent_name()} for Ann Lee",
    )
    forgotten = known.actor(kind="agent", actor_key="agent:c", user_id=None, recorded="x's agent")
    system = known.actor(kind="system", actor_key="machine:m", user_id=None, recorded="Machine")
    assert (
        remembered.display_name,
        remembered.acting_for and remembered.acting_for.display_name,
    ) == (
        f"{brand.agent_name()} for Ann Lee",
        "Ann Lee",
    )
    assert forgotten.display_name == f"{brand.agent_name()} for {FORMER_MEMBER}"
    assert system.display_name == brand.product_name()


async def test_a_person_is_in_the_cell_their_caret_is_in_and_nowhere_once_it_leaves(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """Presence is where people's carets stand now, not where they typed
    lately: a person who edited a cell and holds no caret in it is not in
    it, a caret names its person as the actor their runs are made under, and
    a cleared caret is gone at once."""
    from alkera_core.events import HubEvent

    reader = nb.fw.world.reader.user
    channel = f"doc:notebook:{nb.fw.node_id}"

    def caret(cell: str | None) -> HubEvent:
        return HubEvent(
            lane="ephemeral",
            org_id=nb.fw.world.org_id,
            type="doc.crdt_ephemeral",
            entity="doc",
            entity_id=channel,
            version=0,
            visibility="org",
            payload={
                "envelope": {"payload": {"loro_peer": 3000, "user_id": str(reader.id)}},
                **({"caret_cell": cell} if cell is not None else {}),
            },
            channel=channel,
        )

    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    edited = await client.post(
        nb.url("/ops"), json={"ops": [{"op": "replace", "cell_id": CELL_A, "source": "x = 3"}]}
    )
    assert edited.status_code == 200, edited.text
    assert (await client.get(nb.url())).json()["presence"] == []

    nb.service.carets.observe(caret(CELL_B))
    presence = (await client.get(nb.url())).json()["presence"]
    assert [(p["actor_id"], p["caret"], p["cell_id"]) for p in presence] == [
        (f"user:{reader.id}", True, CELL_B)
    ]

    nb.service.carets.observe(caret(None))
    assert (await client.get(nb.url())).json()["presence"] == []
