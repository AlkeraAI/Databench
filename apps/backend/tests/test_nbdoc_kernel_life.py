"""A notebook kernel's record, and what a run the platform ended tells the
people on the notebook, through the real routes on real Postgres.

* ``notebook_kernels`` follows the box's kernel events: the state the batch
  names (or, when it names none, the last ``kernel.state`` / ``kernel.exited``
  it carries), the environment the kernel runs in, and one kernel at a time:
  a kernel that comes up ends every other the notebook had, whether or not
  their exit was heard (the box's channel for events of no kernel included);
* a run the platform ends ``refused`` on its own record (the box never
  answered, or answered with a refusal) reaches every socket on the
  notebook's channel as a ``run.finished`` naming the run and why.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.events import HubEvent
from alkera_core.models import EventOutbox
from alkera_core.notebooks.models import NotebookKernel
from alkera_core.notebooks.runs import PLATFORM_EVENTS_KEY
from alkera_notebook.engine.models import KernelInfo
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import login
from tests.test_nbdoc_channel import CHANNEL, ITEM, ORG, _batch, _ev, _wire
from tests.test_nbdoc_routes import (  # noqa: F401
    Box,
    Rig,
    _holder,
    _notebook_events,
    _snapshot,
    rig,
)
from tests.test_nbdoc_run_lifecycle import Restarting, _run, _start_run

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on")]


@pytest.fixture
async def nb(rig: Rig) -> Rig:  # noqa: F811 - pytest names a fixture's input by its name
    """The notebook routes' rig (``test_nbdoc_routes``)."""
    return rig


async def _post(
    nb: Rig, box: Box, kernel_id: str, events: list[dict[str, Any]], *, state: str | None
) -> None:
    answer = await box.client.post(
        nb.url("/events"), json={"kernel_id": kernel_id, "state": state, "events": events}
    )
    assert answer.status_code == 200, answer.text


async def _kernel(kernel_id: str) -> NotebookKernel:
    async with AsyncSessionLocal() as db:
        found = await db.get(NotebookKernel, kernel_id)
        assert found is not None
        return found


async def _status(nb: Rig, client: AsyncClient) -> KernelInfo:
    await login(client, nb.fw.world.reader.user.email, nb.fw.world.reader.password)
    answer = await client.post(nb.url("/kernel"), json={"action": "status"})
    assert answer.status_code == 200, answer.text
    return KernelInfo.model_validate(answer.json()["kernel"])


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _state(seq: int, state: str, **rest: Any) -> dict[str, Any]:
    return {"seq": seq, "type": "kernel.state", "state": state, **rest}


# -- the kernel's state -----------------------------------------------------------------


async def test_a_kernel_coming_up_ends_the_start_the_box_announced_under_no_kernel(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The box says ``starting`` under its channel for events of no kernel,
    then the kernel's own events follow under the kernel's id: the start is
    over, and the notebook's kernel is the one that came up."""
    box = await _holder(real_session, client, nb.fw)
    engine, kernel = _id("engine-"), _id("krn_")
    await _post(nb, box, engine, [_state(1, "starting")], state="starting")
    assert (await _status(nb, client)).state == "starting"

    await _post(nb, box, kernel, [_state(1, "idle")], state="idle")
    start = await _kernel(engine)
    assert (start.state, start.stopped_at is not None) == ("stopped", True)
    came_up = await _kernel(kernel)
    assert (came_up.state, came_up.stopped_at) == ("idle", None)
    status = await _status(nb, client)
    assert (status.kernel_id, status.state) == (kernel, "idle")


async def test_a_restart_s_new_kernel_ends_the_one_it_replaced_even_unheard(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The replaced kernel's exit never reached the platform: it still ends,
    and only the new kernel is the notebook's."""
    box = await _holder(real_session, client, nb.fw)
    old, new = _id("krn_"), _id("krn_")
    await _post(nb, box, old, [_state(1, "idle"), _state(2, "busy")], state="busy")
    await _post(nb, box, new, [_state(1, "starting"), _state(2, "idle")], state="idle")
    replaced = await _kernel(old)
    assert (replaced.state, replaced.stopped_at is not None) == ("stopped", True)
    assert (await _status(nb, client)).kernel_id == new


async def test_a_batch_of_no_running_kernel_ends_no_kernel(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """An answer the box posts with no notebook open goes under a channel of
    its own that says ``absent``: that is no kernel coming up, and the kernel
    that runs keeps running."""
    box = await _holder(real_session, client, nb.fw)
    kernel = _id("krn_")
    await _post(nb, box, kernel, [_state(1, "idle")], state="idle")
    await _post(
        nb,
        box,
        _id("engine-"),
        [{"seq": 1, "type": "answer", "request_id": "r1", "result": {}}],
        state="absent",
    )
    running = await _kernel(kernel)
    assert (running.state, running.stopped_at) == ("idle", None)
    assert (await _status(nb, client)).kernel_id == kernel


@pytest.mark.parametrize(
    ("events", "state"),
    [
        pytest.param([_state(1, "busy")], "busy", id="kernel-state"),
        pytest.param([_state(1, "busy"), _state(2, "idle")], "idle", id="the-last-one"),
        pytest.param(
            [_state(1, "idle"), {"seq": 2, "type": "kernel.exited", "reason": "shutdown"}],
            "stopped",
            id="exited",
        ),
        pytest.param([_state(1, "dead")], "starting", id="unknown-state-ignored"),
        pytest.param(
            [{"seq": 1, "type": "cell.status", "cell_id": "c", "status": "fresh"}],
            "starting",
            id="nothing-said",
        ),
    ],
)
async def test_a_batch_naming_no_state_moves_the_kernel_by_its_kernel_events(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    events: list[dict[str, Any]],
    state: str,
) -> None:
    box = await _holder(real_session, client, nb.fw)
    kernel = _id("krn_")
    await _post(nb, box, kernel, [_state(0, "starting")], state="starting")
    await _post(nb, box, kernel, events, state=None)
    found = await _kernel(kernel)
    assert found.state == state
    assert (found.stopped_at is not None) == (state == "stopped")


@pytest.mark.parametrize(
    ("events", "env_id"),
    [
        pytest.param([_state(1, "idle", env_id="uv_project:.")], "uv_project:.", id="kernel-state"),
        pytest.param([_state(1, "idle")], None, id="kernel-state-without-env"),
        pytest.param("snapshot", "default", id="snapshot-of-this-kernel"),
        pytest.param("other-snapshot", None, id="snapshot-of-another-kernel"),
    ],
)
async def test_the_kernel_s_environment_is_recorded_from_what_its_events_say(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    events: Any,
    env_id: str | None,
) -> None:
    box = await _holder(real_session, client, nb.fw)
    kernel = _id("krn_")
    if events == "snapshot":
        events = [_snapshot(kernel, 1, [])]
    elif events == "other-snapshot":
        events = [_snapshot(_id("krn_"), 1, [])]
    await _post(nb, box, kernel, events, state="idle")
    assert (await _kernel(kernel)).env_id == env_id


async def test_a_later_batch_saying_no_environment_keeps_the_one_recorded(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    box = await _holder(real_session, client, nb.fw)
    kernel = _id("krn_")
    await _post(nb, box, kernel, [_state(1, "idle", env_id="default")], state="idle")
    await _post(nb, box, kernel, [_state(2, "busy")], state="busy")
    assert (await _kernel(kernel)).env_id == "default"


# -- a run the platform ended reaches the channel -------------------------------------------


def _platform_rows(rows: list[EventOutbox]) -> list[EventOutbox]:
    return [row for row in rows if PLATFORM_EVENTS_KEY in row.payload]


async def _heard_on_the_channel(nb: Rig, rows: list[EventOutbox]) -> list[dict[str, Any]]:
    """What a person's socket holding the notebook's channel is sent of
    ``rows``, after its join."""
    wire = _wire()
    await wire.socket.subscribe(f"nb:{nb.fw.node_id}")
    await wire.socket.flush()
    joined = len(wire.nb())
    for row in rows:
        wire.hub.publish(HubEvent.from_outbox(row))
    await wire.pump()
    return wire.nb()[joined:]


async def test_a_run_the_box_never_answers_is_announced_refused_on_the_channel(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    await _holder(real_session, client, nb.fw)
    box = Restarting(nb, missed=10_000, answer={})
    nb.service.transport = box
    nb.service.answer_seconds = 0.3
    nb.service.resend_seconds = 0.05
    run_id = await _start_run(nb, client)
    await nb.service.settle()
    assert (await _run(run_id)).status == "refused"

    rows = _platform_rows(await _notebook_events(nb.fw.world.org_id))
    heard = await _heard_on_the_channel(nb, rows)
    assert heard == [
        {"type": "run.finished", "run_id": run_id, "status": "refused", "reason": "machine_silent"}
    ]
    # Sent as the platform's word, never numbered into a kernel's sequence.
    assert all(isinstance(frame, dict) and "seq" not in frame for frame in heard)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        pytest.param({"error": {"code": "env_build_failed"}}, "env_build_failed", id="error"),
        pytest.param(
            {"result": {"status": "refused", "reason": "kernel_busy"}}, "kernel_busy", id="refused"
        ),
    ],
)
async def test_a_run_the_box_refused_is_announced_on_the_channel(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    answer: dict[str, Any],
    reason: str,
) -> None:
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    await _post(
        nb,
        box,
        _id("engine-"),
        [{"seq": 1, "type": "answer", "request_id": run_id, **answer}],
        state="absent",
    )
    assert ((await _run(run_id)).status, (await _run(run_id)).reason) == ("refused", reason)
    rows = _platform_rows(await _notebook_events(nb.fw.world.org_id))
    assert await _heard_on_the_channel(nb, rows) == [
        {"type": "run.finished", "run_id": run_id, "status": "refused", "reason": reason}
    ]


async def test_a_run_the_box_accepted_is_not_announced_by_the_platform(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The engine announces how an accepted run ends; the platform adds
    nothing of its own."""
    box = await _holder(real_session, client, nb.fw)
    run_id = await _start_run(nb, client)
    await _post(
        nb,
        box,
        _id("krn_"),
        [
            {"seq": 1, "type": "answer", "request_id": run_id, "result": {"status": "queued"}},
            {"seq": 2, "type": "run.finished", "run_id": run_id, "status": "error"},
        ],
        state="idle",
    )
    assert (await _run(run_id)).status == "error"
    assert _platform_rows(await _notebook_events(nb.fw.world.org_id)) == []


async def test_the_platform_s_word_bypasses_the_kernel_cursor_and_no_ring_keeps_it() -> None:
    """A platform event has no sequence: a socket deep in a kernel's sequence
    is still sent it, and a socket joining later is not replayed it."""
    platform = HubEvent(
        lane="durable",
        org_id=ORG,
        type="notebook.event",
        entity="notebook",
        entity_id=CHANNEL,
        version=0,
        visibility="org",
        payload={PLATFORM_EVENTS_KEY: [{"type": "run.finished", "run_id": "r9"}]},
        channel=CHANNEL,
    )
    wire = _wire()
    await wire.socket.subscribe(CHANNEL)
    wire.hub.publish(_batch("k1", _ev(40)))
    wire.hub.publish(platform)
    wire.hub.publish(_batch("k1", _ev(41)))
    await wire.pump()
    assert [(e.get("type"), e.get("seq")) for e in wire.events()] == [
        ("cell.status", 40),
        ("run.finished", None),
        ("cell.status", 41),
    ]
    assert [e["seq"] for e in wire.feed.replay(ITEM).events] == [40, 41]


# -- an install's outcome reaches the channel -----------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            {"result": {"env": {"env_id": "uv_project:pyproject.toml", "state": "ready"}}},
            {"status": "ok", "env_id": "uv_project:pyproject.toml"},
            id="installed",
        ),
        pytest.param(
            {
                "error": {
                    "code": "env_build_failed",
                    "message": "The environment could not be built: add failed (exit 1)",
                }
            },
            {
                "status": "error",
                "code": "env_build_failed",
                "message": "The environment could not be built: add failed (exit 1)",
            },
            id="build-failed",
        ),
        pytest.param(
            None,
            {
                "status": "error",
                "message": "The machine serving this notebook did not answer.",
            },
            id="no-answer",
        ),
    ],
)
async def test_an_install_s_outcome_is_announced_on_the_channel(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    answer: dict[str, Any] | None,
    expected: dict[str, Any],
) -> None:
    """When the box answers an install (or never does), people holding the
    notebook's channel are sent ``env.install`` with the packages asked for
    and the outcome, as the platform's word: no kernel, no sequence."""
    await _holder(real_session, client, nb.fw)
    if answer is not None:
        nb.transport.answers["env_install"] = answer
    nb.service.install_answer_seconds = 0.3
    nb.service.install_resend_seconds = 0.1
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    posted = await client.post(nb.url("/env/install"), json={"packages": ["polars", "six"]})
    assert posted.status_code == 200, posted.text
    await nb.service.settle()
    sent = [body for _, op, body in nb.transport.sent if op == "env_install"]
    assert sent and sent[0]["packages"] == ["polars", "six"]
    rows = _platform_rows(await _notebook_events(nb.fw.world.org_id))
    heard = await _heard_on_the_channel(nb, rows)
    assert heard == [
        {"type": "env.install", "action": "install", "packages": ["polars", "six"], **expected}
    ]


@pytest.mark.parametrize(
    ("action", "packages"),
    [
        pytest.param("build", [], id="build"),
        pytest.param("remove", ["six"], id="remove"),
        pytest.param("cancel", [], id="cancel"),
    ],
)
async def test_an_environment_action_reaches_the_box_with_the_editors_verdict_and_is_announced(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    action: str,
    packages: list[str],
) -> None:
    """A person who may run the notebook builds, prunes or cancels its
    environment: the box is told what, by whom, and whether they may edit
    the notebook (its PEP 723 block is a spec), and the outcome is
    announced on the channel naming the action."""
    await _holder(real_session, client, nb.fw)
    nb.transport.answers["env_action"] = {"result": {"env": {"env_id": "default:x"}}}
    nb.service.install_answer_seconds = 0.3
    nb.service.install_resend_seconds = 0.1
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    posted = await client.post(nb.url(f"/env/{action}"), json={"packages": packages})
    assert posted.status_code == 200, posted.text
    await nb.service.settle()
    (sent,) = [body for _, op, body in nb.transport.sent if op == "env_action"]
    assert sent["action"] == action and sent["packages"] == packages
    assert sent["requested_by"]["can_edit"] is True
    assert sent["requested_by"]["id"] == f"user:{nb.fw.world.writer.user.id}"
    rows = _platform_rows(await _notebook_events(nb.fw.world.org_id))
    heard = await _heard_on_the_channel(nb, rows)
    assert heard == [
        {
            "type": "env.install",
            "action": action,
            "packages": packages,
            "status": "ok",
            "env_id": "default:x",
        }
    ]


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        pytest.param("/env/remove", {"packages": []}, 422, id="remove-names-nothing"),
        pytest.param("/env/upgrade", {}, 422, id="unknown-action"),
        pytest.param("/env/build", {"packages": [""]}, 422, id="empty-name"),
    ],
)
async def test_an_environment_action_the_route_cannot_carry_is_refused(
    real_session: AsyncSession,
    nb: Rig,
    client: AsyncClient,
    path: str,
    body: dict[str, Any],
    status: int,
) -> None:
    await _holder(real_session, client, nb.fw)
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    refused = await client.post(nb.url(path), json=body)
    assert refused.status_code == status, refused.text
    assert [op for _, op, _ in nb.transport.sent if op == "env_action"] == []


async def test_an_install_announces_nothing_before_the_box_answers(
    real_session: AsyncSession, nb: Rig, client: AsyncClient
) -> None:
    """The outcome is the box's: while the install is still building nothing
    is announced."""
    await _holder(real_session, client, nb.fw)
    nb.service.install_answer_seconds = 30.0
    await login(client, nb.fw.world.writer.user.email, nb.fw.world.writer.password)
    posted = await client.post(nb.url("/env/install"), json={"packages": ["polars"]})
    assert posted.status_code == 200, posted.text
    assert _platform_rows(await _notebook_events(nb.fw.world.org_id)) == []
    await nb.service.close()
