"""The box's notebooks: one engine per workspace, shared by the chats' agents
and the backend's requests, with the kernel's events relayed to the backend.

Real engines with real kernels (local subprocesses), real files on disk and
the real machine-request schema; the backend's events route is the boundary,
recorded by :class:`Sink`. The folders the box holds are stood in by
:class:`Folders` (the custody behind them is the cloud layer's, covered by
its own suites; :class:`HeldFolders` is pinned against a held folder's shape
below).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.cloud.machine_requests import MachineRequests
from alkera_cli.files.mount import fence_headers
from alkera_cli.harness.sandbox_scope import SandboxScope, own_scope
from alkera_cli.notebooks.box import (
    ENGINE_CHANNEL_PREFIX,
    SERVED_OPS,
    BoxFolder,
    BoxNotebooks,
    HeldFolders,
)
from alkera_cli.notebooks.box_compose import BoxNotebookSlot
from alkera_cli.notebooks.engine_host import WorkspaceTenancy
from alkera_cli.notebooks.store_loro import Located, agent_actor_id
from alkera_core.schemas.realtime.machine import NOTEBOOK_REQUEST_OPS, NotebookMachineRequest
from alkera_notebook.engine.engine import NotebookEngine
from alkera_notebook.envs import LocalCommandRunner, LocalEnvRegistry
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.outputs.state import TABLE_MIME
from alkera_notebook.tools.engine_adapter import local_engine
from alkera_notebook.tools.models import InsertCellOp, RunCells, SetSettingOp
from alkera_notebook.tools.port import ActorRef, NotebookToolError

CHAT = "0f1e2d3c-4b5a-4968-8776-655443322110"
OTHER_CHAT = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
WS = "ws:w1"
LEASE = "6b0e1c4f-0000-4000-8000-00000000aaaa"
DRIVE = "6b0e1c4f-0000-4000-8000-00000000dddd"
NOTEBOOK = "analysis/weekly.alknb.py"
AGENT = ActorRef(kind="agent", id=agent_actor_id(CHAT), display_name="Agent")
PERSON = {"kind": "person", "id": "user:ada", "display_name": "Ada"}


# -- the rig -----------------------------------------------------------------------


def folder_of(key: str, root: Path, *, lease: str = LEASE, step: str = "") -> BoxFolder:
    """A held folder whose drive rows are the files on its disk."""

    def node_at(relative: str) -> str | None:
        if not (root / relative).is_file():
            return None
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}/{relative}"))

    return BoxFolder(
        key=key,
        root=root,
        drive_id=DRIVE,
        lease_node_id=lease,
        step=step,
        headers=fence_headers(3, "instance-1"),
        node_at=node_at,
    )


@dataclass
class Folders:
    held: dict[str, BoxFolder] = field(default_factory=dict)

    def by_key(self, key: str) -> BoxFolder | None:
        return self.held.get(key)

    def by_lease(self, lease_node_id: str) -> BoxFolder | None:
        return next((f for f in self.held.values() if f.lease_node_id == lease_node_id), None)


@dataclass
class Post:
    where: Located
    kernel_id: str
    state: str | None
    events: list[dict[str, Any]]


@dataclass
class Sink:
    """The backend's events route, as the box reaches it: every batch encoded
    as the box's HTTP client encodes a body (strict JSON, no ``NaN``), so a
    value the wire cannot carry fails here as it fails on a real box."""

    posts: list[Post] = field(default_factory=list)

    async def post(
        self,
        where: Located,
        *,
        kernel_id: str,
        state: str | None,
        events: Sequence[Mapping[str, Any]],
    ) -> None:
        wire = json.dumps(
            {"kernel_id": kernel_id, "state": state, "events": list(events)},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.posts.append(Post(where, kernel_id, state, json.loads(wire)["events"]))

    def answers(self, request_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events()
            if e.get("type") == "answer" and e.get("request_id") == str(request_id)
        ]

    def events(self) -> list[dict[str, Any]]:
        return [e for p in self.posts for e in p.events]

    def answer(self, request_id: uuid.UUID) -> dict[str, Any] | None:
        return next(
            (
                e
                for e in self.events()
                if e.get("type") == "answer" and e.get("request_id") == str(request_id)
            ),
            None,
        )

    async def until(self, found: Callable[[], Any]) -> Any:
        """``found()`` once it is truthy, waiting up to a minute (a kernel
        starts and runs in that)."""
        async with asyncio.timeout(60.0):
            while True:
                value = found()
                if value:
                    return value
                await asyncio.sleep(0.05)


@dataclass
class Rig:
    box: BoxNotebooks
    sink: Sink
    folders: Folders
    engines: list[NotebookEngine]
    root: Path

    def request(
        self, op: str, body: dict[str, Any] | None = None, *, path: str = NOTEBOOK
    ) -> NotebookMachineRequest:
        folder = self.folders.by_lease(LEASE)
        assert folder is not None
        node = folder.node_at(path) or str(uuid.uuid4())
        return NotebookMachineRequest(
            request_id=uuid.uuid4(),
            drive_id=uuid.UUID(DRIVE),
            item_id=uuid.UUID(node),
            op=op,  # type: ignore[arg-type]
            body=body or {},
            lease_node_id=uuid.UUID(LEASE),
            path=f"{folder.step}/{path}".strip("/"),
        )


def make_box(
    tmp_path: Path,
    folders: Folders,
    sink: Sink,
    *,
    scope: Callable[[str], SandboxScope] = own_scope,
    envs: Callable[[WorkspaceTenancy], Any] | None = None,
) -> tuple[BoxNotebooks, list[NotebookEngine]]:
    engines: list[NotebookEngine] = []

    def engine_for(tenancy: WorkspaceTenancy) -> NotebookEngine:
        engine = local_engine(
            tenancy.folder,
            data_root=tmp_path / "data" / str(len(engines)),
            # The test's own interpreter unless a test builds environments:
            # the rig's notebooks show pandas frames without building one.
            envs=StaticEnvRegistry() if envs is None else envs(tenancy),
        )
        engines.append(engine)
        return engine

    async def locate(key: str, path: str) -> Located:
        folder = folders.by_key(key)
        assert folder is not None
        node = folder.node_at(path)
        assert node is not None
        return Located(drive_id=folder.drive_id, item_id=node, headers=folder.headers)

    box = BoxNotebooks(
        folders=folders,
        engine_for=engine_for,
        events=sink,
        locate=locate,
        tenancy_for=lambda key, root: WorkspaceTenancy(key, root, tmp_path / "envs"),
        scope=scope,
    )
    return box, engines


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    root = tmp_path / "chat" / "files"
    root.mkdir(parents=True)
    folders = Folders({CHAT: folder_of(CHAT, root)})
    sink = Sink()
    box, engines = make_box(tmp_path, folders, sink)
    try:
        yield Rig(box, sink, folders, engines, root)
    finally:
        await box.close()


async def _agent_notebook(rig: Rig, cells: list[str]) -> list[str]:
    """The chat's agent creates a notebook through its tools' host; its
    cells' ids in order."""
    host = rig.box.host_factory(rig.root, AGENT)
    port = await host.create(
        NOTEBOOK, [InsertCellOp(source=src, name=f"c{i}") for i, src in enumerate(cells)], {}
    )
    view = await port.read(None, include_source=True, include_outputs=False)
    return [c.id for c in view.cells]


# -- one engine per workspace, shared --------------------------------------------------


async def test_the_agent_and_a_person_s_request_share_one_kernel(rig: Rig) -> None:
    """The agent defines a value in the notebook's kernel; a person's run that
    the backend carried to the box reads it, in the same kernel, under the run
    id the backend recorded, and the kernel's events reach the backend."""
    first, second = await _agent_notebook(rig, ["x = 41\nx", "y = x + 1\ny"])
    host = rig.box.host_factory(rig.root, AGENT)
    port = await host.open(NOTEBOOK)
    ran = await port.run(RunCells(ids=[first]), confirm_expensive=True)
    assert ran.run_id is not None
    await port.wait(ran.run_id, 60.0)

    request = rig.request(
        "run",
        {
            "run_id": "backend-run-1",
            "target": {"kind": "cells", "ids": [second]},
            "requested_by": PERSON,
        },
    )
    await rig.box.dispatch(request)
    answer = rig.sink.answer(request.request_id)
    assert answer is not None and answer["result"]["run_id"] == "backend-run-1"

    finished = await rig.sink.until(
        lambda: [
            e
            for e in rig.sink.events()
            if e.get("type") == "run.finished" and e.get("run_id") == "backend-run-1"
        ]
    )
    assert finished[0]["status"] == "ok"
    outputs = [
        e for e in rig.sink.events() if e.get("type") == "cell.output" and e["cell_id"] == second
    ]
    assert outputs and "42" in json.dumps(outputs[-1]["output"])
    assert len(rig.engines) == 1, "the agent and the request reached one engine"
    queued = next(
        e
        for e in rig.sink.events()
        if e.get("type") == "run.queued" and e.get("run_id") == "backend-run-1"
    )
    assert queued["requested_by"]["id"] == "user:ada"


async def test_the_chat_s_agent_runs_for_the_chat_s_person(rig: Rig) -> None:
    """A run the chat's agent starts names the person it acts for, so it
    reads "<agent> for Ada", never a bare agent; a chat whose person the
    box does not know runs as the agent alone."""
    from alkera_notebook.actors import ActingFor

    ada = ActingFor(id="user:ada", display_name="Ada")
    rig.box.use_people(lambda chat: ada if chat == CHAT else None)
    (cell,) = await _agent_notebook(rig, ["1 + 1"])
    port = await rig.box.host_factory(rig.root, AGENT).open(NOTEBOOK)
    ran = await port.run(RunCells(ids=[cell]), confirm_expensive=True)
    assert ran.run_id is not None
    queued = await rig.sink.until(
        lambda: [
            e
            for e in rig.sink.events()
            if e.get("type") == "run.queued" and e.get("run_id") == ran.run_id
        ]
    )
    who = queued[0]["requested_by"]
    assert who["kind"] == "agent" and who["acting_for"] == {"id": "user:ada", "display_name": "Ada"}


async def test_a_requester_the_backend_names_as_acting_for_a_person_runs_for_them(
    rig: Rig,
) -> None:
    (cell,) = await _agent_notebook(rig, ["2"])
    agent = {
        "kind": "agent",
        "id": "agent:other-chat",
        "display_name": "Agent",
        "acting_for": {"id": "user:bo", "display_name": "Bo"},
    }
    request = rig.request(
        "run", {"run_id": "r-bo", "target": {"kind": "cells", "ids": [cell]}, "requested_by": agent}
    )
    await rig.box.dispatch(request)
    queued = await rig.sink.until(
        lambda: [
            e
            for e in rig.sink.events()
            if e.get("type") == "run.queued" and e.get("run_id") == "r-bo"
        ]
    )
    assert queued[0]["requested_by"]["acting_for"] == {"id": "user:bo", "display_name": "Bo"}
    # A person is never named by an id when the backend gave no name.
    nameless = rig.request(
        "run",
        {
            "run_id": "r-anon",
            "target": {"kind": "cells", "ids": [cell]},
            "requested_by": {"kind": "person", "id": "user:cy"},
        },
    )
    await rig.box.dispatch(nameless)
    anon = await rig.sink.until(
        lambda: [
            e
            for e in rig.sink.events()
            if e.get("type") == "run.queued" and e.get("run_id") == "r-anon"
        ]
    )
    assert anon[0]["requested_by"]["display_name"] == ""


async def test_events_are_numbered_per_kernel_and_posted_where_the_notebook_is(rig: Rig) -> None:
    """Each kernel's events carry their own increasing sequence, every post
    names the notebook's drive and node under the folder's fence, and nothing
    is posted under a kernel id while no kernel runs except under the
    notebook's engine channel, reported ``absent``."""
    (cell,) = await _agent_notebook(rig, ["1 + 1"])
    request = rig.request("run", {"run_id": "r-1", "target": {"kind": "cells", "ids": [cell]}})
    await rig.box.dispatch(request)
    await rig.sink.until(lambda: any(e.get("type") == "run.finished" for e in rig.sink.events()))
    folder = rig.folders.by_key(CHAT)
    assert folder is not None
    by_kernel: dict[str, list[int]] = {}
    for post in rig.sink.posts:
        assert post.where.item_id == folder.node_at(NOTEBOOK)
        assert dict(post.where.headers) == dict(folder.headers)
        for event in post.events:
            assert event["kernel_id"] == post.kernel_id
            by_kernel.setdefault(post.kernel_id, []).append(event["seq"])
        if post.kernel_id.startswith(ENGINE_CHANNEL_PREFIX):
            assert post.state in ("absent", "starting")
    for seqs in by_kernel.values():
        assert seqs == list(range(1, len(seqs) + 1))
    kernels = [k for k in by_kernel if not k.startswith(ENGINE_CHANNEL_PREFIX)]
    assert len(kernels) == 1
    kernel_states = [p.state for p in rig.sink.posts if p.kernel_id == kernels[0]]
    assert "idle" in kernel_states


# -- every request is answered ---------------------------------------------------------


async def test_every_op_the_backend_sends_is_served() -> None:
    """The ops the backend's transport may send and the ops a box serves are
    the same set: a new op cannot go unanswered by omission."""
    assert set(NOTEBOOK_REQUEST_OPS) == set(SERVED_OPS)


@pytest.mark.parametrize(
    ("op", "body", "check"),
    [
        pytest.param(
            "envs",
            {},
            lambda r: r["current"]["env_id"] and isinstance(r["envs"], list),
            id="envs",
        ),
        pytest.param(
            "kernel",
            {"action": "status"},
            lambda r: r["state"] == "absent",
            id="kernel-status-before-any-run",
        ),
        pytest.param("snapshot", {}, lambda r: r == {}, id="snapshot"),
    ],
)
async def test_a_request_is_answered_with_its_result(
    rig: Rig, op: str, body: dict[str, Any], check: Callable[[dict[str, Any]], Any]
) -> None:
    await _agent_notebook(rig, ["x = 1"])
    request = rig.request(op, body)
    await rig.box.dispatch(request)
    answer = rig.sink.answer(request.request_id)
    assert answer is not None and "error" not in answer, answer
    assert check(answer["result"])


async def test_a_snapshot_carries_the_notebook_s_whole_view(rig: Rig) -> None:
    (cell,) = await _agent_notebook(rig, ["x = 1"])
    request = rig.request("snapshot")
    await rig.box.dispatch(request)
    snapshots = [e for e in rig.sink.events() if e.get("type") == "snapshot"]
    assert snapshots and [c["id"] for c in snapshots[-1]["view"]["cells"]] == [cell]
    assert snapshots[-1]["frames"] == {}


async def test_each_settled_kernel_posts_a_snapshot_with_every_cell_s_run_state(
    rig: Rig,
) -> None:
    """The backend's view and a socket joining the channel start from the
    latest snapshot, so the box posts the notebook's whole state each time
    the kernel settles idle: after runs of different cells, the last snapshot
    posted carries every cell's status, outputs and last run with its times,
    not just the cell of the run that came first."""
    cells = await _agent_notebook(rig, ["a = 1\nprint('one')", "b = a + 1\nb", "print('three')"])
    for n, cell in enumerate(cells):
        run_id = f"r-settle-{n}"
        await rig.box.dispatch(
            rig.request(
                "run",
                {
                    "run_id": run_id,
                    "target": {"kind": "cells", "ids": [cell]},
                    "requested_by": PERSON,
                },
            )
        )
        await rig.sink.until(
            lambda run_id=run_id: any(
                e.get("type") == "run.finished" and e.get("run_id") == run_id
                for e in rig.sink.events()
            )
        )

    def settled() -> dict[str, Any] | None:
        events = rig.sink.events()
        last_finish = max(i for i, e in enumerate(events) if e.get("type") == "run.finished")
        after = [e for e in events[last_finish:] if e.get("type") == "snapshot"]
        return after[-1] if after else None

    snapshot = await rig.sink.until(settled)
    by_id = {c["id"]: c for c in snapshot["view"]["cells"]}
    for n, cell in enumerate(cells):
        state = by_id[cell]
        assert state["status"] == "fresh", state
        assert state["outputs"], state
        last = state["last_run"]
        assert last["run_id"] == f"r-settle-{n}"
        assert last["started_at"] is not None and last["finished_at"] is not None
    assert "one" in json.dumps(by_id[cells[0]]["outputs"])
    assert "2" in json.dumps(by_id[cells[1]]["outputs"])
    assert "three" in json.dumps(by_id[cells[2]]["outputs"])
    assert snapshot["view"]["kernel"]["queue"] == []


@pytest.mark.parametrize(
    ("op", "body", "code"),
    [
        pytest.param("kernel", {"action": "explode"}, "invalid_request", id="unknown-action"),
        pytest.param(
            "frame_attach",
            {"frame_id": "f" * 32, "model_ids": ["m1"]},
            "kernel_unavailable",
            id="frame-with-no-kernel",
        ),
        pytest.param("table", {"cell_id": "nope"}, "not_found", id="table-of-no-cell"),
        pytest.param("frame_detach", {}, "invalid_request", id="detach-naming-nothing"),
        pytest.param(
            "run", {"target": {"kind": "nothing"}}, "invalid_request", id="run-unknown-target"
        ),
    ],
)
async def test_a_request_the_engine_refuses_is_answered_with_the_error(
    rig: Rig, op: str, body: dict[str, Any], code: str
) -> None:
    await _agent_notebook(rig, ["x = 1"])
    request = rig.request(op, body)
    await rig.box.dispatch(request)
    answer = rig.sink.answer(request.request_id)
    assert answer is not None and "result" not in answer
    assert answer["error"]["code"] == code


async def test_a_request_for_a_notebook_the_box_does_not_hold_is_answered_at_once(
    rig: Rig,
) -> None:
    """The backend waits for an answer: a request naming a lease this box
    does not hold is still answered, under the notebook's engine channel, so
    the person is told at once. It is said as a folder the box does not hold
    (the backend wakes its chat, or sends it again), never as a notebook that
    is not there."""
    request = rig.request("envs").model_copy(update={"lease_node_id": uuid.uuid4()})
    await rig.box.dispatch(request)
    answer = rig.sink.answer(request.request_id)
    assert answer is not None and answer["error"]["code"] == "folder_not_held"
    (post,) = rig.sink.posts
    assert post.kernel_id.startswith(ENGINE_CHANNEL_PREFIX) and post.state == "absent"


async def test_a_request_for_a_folder_the_box_has_not_taken_yet_is_served_once_it_has(
    rig: Rig,
) -> None:
    """The backend sends a request only to the machine the folder's lease
    names, so a box that has not taken that folder is about to (a restarted
    or woken box takes its folders seconds after its worker is ready). It
    answers ``folder_not_held`` at once and serves the same request when the
    backend sends it again after the take, instead of refusing a run it
    would have run."""
    await _agent_notebook(rig, ["x = 1"])
    request = rig.request("envs")
    held = rig.folders.held.pop(CHAT)
    await rig.box.dispatch(request)
    answer = rig.sink.answer(request.request_id)
    assert answer is not None and answer["error"]["code"] == "folder_not_held"

    rig.folders.held[CHAT] = held
    await rig.box.dispatch(request)
    served = rig.sink.answers(request.request_id)
    assert len(served) == 2 and "result" in served[-1], served


async def test_a_path_outside_the_tree_the_box_works_in_is_answered_not_found(
    rig: Rig,
) -> None:
    escaped = rig.request("envs").model_copy(update={"path": "../outside.alknb.py"})
    await rig.box.dispatch(escaped)
    assert rig.sink.answer(escaped.request_id)["error"]["code"] == "not_found"  # type: ignore[index]


async def test_answers_with_no_notebook_open_count_up_from_one_on_a_channel_of_their_own(
    rig: Rig, tmp_path: Path
) -> None:
    """The backend keeps each kernel's number in an int32 column and drops
    an event not past the last one it took: the box numbers each channel's
    answers 1, 2, 3, and a restarted box answers on a channel of its own, so
    its first answer is not dropped behind numbers an earlier one used."""
    unheld, item = uuid.uuid4(), uuid.uuid4()
    requests = [
        rig.request("envs").model_copy(update={"lease_node_id": unheld, "item_id": item})
        for _ in range(3)
    ]
    for request in requests:
        await rig.box.dispatch(request)
    channels = {post.kernel_id for post in rig.sink.posts}
    assert len(channels) == 1 and next(iter(channels)).startswith(ENGINE_CHANNEL_PREFIX)
    assert [e["seq"] for e in rig.sink.events()] == [1, 2, 3]

    restarted_sink = Sink()
    restarted, _ = make_box(tmp_path / "again", rig.folders, restarted_sink)
    try:
        again = rig.request("envs").model_copy(update={"lease_node_id": unheld, "item_id": item})
        await restarted.dispatch(again)
    finally:
        await restarted.close()
    (post,) = restarted_sink.posts
    assert post.kernel_id not in channels and post.events[0]["seq"] == 1


async def test_a_request_delivered_twice_is_served_once(rig: Rig) -> None:
    (cell,) = await _agent_notebook(rig, ["1"])
    request = rig.request("run", {"run_id": "twice-1", "target": {"kind": "cells", "ids": [cell]}})
    await rig.box.dispatch(request)
    await rig.box.dispatch(request)
    await rig.sink.until(lambda: any(e.get("type") == "run.finished" for e in rig.sink.events()))
    answers = [
        e
        for e in rig.sink.events()
        if e.get("type") == "answer" and e["request_id"] == str(request.request_id)
    ]
    queued = [e for e in rig.sink.events() if e.get("type") == "run.queued"]
    assert len(answers) == 1 and len(queued) == 1


async def test_a_notebook_s_requests_are_served_in_the_order_they_arrived(rig: Rig) -> None:
    """Frames, widget messages and runs keep their order: a later request
    waits for the one before it, even when it would finish first."""
    await _agent_notebook(rig, ["x = 1"])
    first, second = rig.request("envs"), rig.request("kernel", {"action": "status"})
    tasks = [rig.box.dispatch(first), rig.box.dispatch(second)]
    await asyncio.gather(*tasks)
    order = [e["request_id"] for e in rig.sink.events() if e.get("type") == "answer"]
    assert order == [str(first.request_id), str(second.request_id)]


async def _sleeping_run(rig: Rig) -> str:
    """A run of a cell that sleeps two minutes, once it is under way."""
    (cell,) = await _agent_notebook(rig, ["import time\ntime.sleep(120)"])
    await rig.box.dispatch(
        rig.request("run", {"run_id": "sleeper", "target": {"kind": "cells", "ids": [cell]}})
    )
    await rig.sink.until(
        lambda: any(
            e.get("type") == "cell.status" and e.get("status") == "running"
            for e in rig.sink.events()
        )
    )
    return cell


def _run_ended(rig: Rig) -> dict[str, Any] | None:
    return next((e for e in rig.sink.events() if e.get("type") == "run.finished"), None)


@pytest.mark.parametrize("action", ["interrupt", "interrupt_all", "shutdown"])
async def test_stopping_a_kernel_overtakes_the_request_the_box_is_still_serving(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A request ahead of it in the notebook's order is still being served
    (it could be a restart waiting on a kernel, or a run reading a slow
    document): the stop reaches the kernel anyway."""
    from alkera_cli.notebooks import box as box_module

    await _sleeping_run(rig)
    release = asyncio.Event()

    async def stuck(relay: Any, body: dict[str, Any]) -> dict[str, Any]:
        await release.wait()
        return {}

    monkeypatch.setitem(box_module._OPS, "envs", stuck)
    ahead = rig.box.dispatch(rig.request("envs"))
    stop = rig.request("kernel", {"action": action, "requested_by": PERSON})
    await asyncio.wait_for(rig.box.dispatch(stop), 30)
    ended = await rig.sink.until(lambda: _run_ended(rig))
    assert ended["status"] in ("interrupted", "kernel_restarted"), ended
    assert rig.sink.answer(stop.request_id)["result"] is not None  # type: ignore[index]
    assert not ahead.done()
    release.set()
    await ahead


async def test_a_request_that_does_not_stop_the_kernel_keeps_its_place(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only stopping overtakes. A status read (or a restart, or a run) asked
    after a request still being served waits for it."""
    from alkera_cli.notebooks import box as box_module

    await _agent_notebook(rig, ["x = 1"])
    release = asyncio.Event()

    async def stuck(relay: Any, body: dict[str, Any]) -> dict[str, Any]:
        await release.wait()
        return {}

    monkeypatch.setitem(box_module._OPS, "envs", stuck)
    ahead = rig.box.dispatch(rig.request("envs"))
    later = rig.request("kernel", {"action": "status"})
    behind = rig.box.dispatch(later)
    await asyncio.sleep(0.3)
    assert not behind.done() and rig.sink.answer(later.request_id) is None
    release.set()
    await asyncio.gather(ahead, behind)
    assert rig.sink.answer(later.request_id) is not None


async def test_an_answer_the_backend_is_slow_to_take_does_not_hold_the_next_request(
    tmp_path: Path,
) -> None:
    """The backend stalls on one answer's post. The notebook's next request
    is served meanwhile: its effect (the run starting) does not wait for the
    stalled post."""
    root = tmp_path / "chat" / "files"
    root.mkdir(parents=True)
    folders = Folders({CHAT: folder_of(CHAT, root)})
    stalled = asyncio.Event()
    release = asyncio.Event()

    class StallingSink(Sink):
        async def post(
            self,
            where: Located,
            *,
            kernel_id: str,
            state: str | None,
            events: Sequence[Mapping[str, Any]],
        ) -> None:
            if any(e.get("type") == "answer" for e in events) and not release.is_set():
                stalled.set()
                await release.wait()
            await super().post(where, kernel_id=kernel_id, state=state, events=events)

    sink = StallingSink()
    box, engines = make_box(tmp_path, folders, sink)
    rig = Rig(box, sink, folders, engines, root)
    try:
        (cell,) = await _agent_notebook(rig, ["x = 2"])
        first = box.dispatch(rig.request("kernel", {"action": "status"}))
        await asyncio.wait_for(stalled.wait(), 30)
        run = rig.request("run", {"run_id": "after", "target": {"kind": "cells", "ids": [cell]}})
        second = box.dispatch(run)
        (session,) = engines[0].sessions

        await sink.until(lambda: session._cell_status(cell) == "fresh")
        assert not first.done(), "the first answer's post is still stalled"
        release.set()
        await asyncio.gather(first, second)
        assert sink.answer(run.request_id) is not None
    finally:
        release.set()
        await box.close()


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")


@needs_uv
@pytest.mark.parametrize(
    ("can_edit", "expected"),
    [
        pytest.param(True, "removed", id="the-backend-says-they-may-edit"),
        pytest.param(False, "forbidden", id="the-backend-says-they-may-not"),
        pytest.param(None, "forbidden", id="the-backend-says-nothing"),
    ],
)
async def test_a_person_changes_a_notebooks_own_packages_as_far_as_the_backend_allows(
    tmp_path: Path, can_edit: bool | None, expected: str
) -> None:
    """The notebook's PEP 723 block is its environment's spec, so changing it
    is an edit: the box lets a requester make it exactly when the request
    says the backend decided they may edit the notebook."""
    root = tmp_path / "chat" / "files"
    root.mkdir(parents=True)
    folders = Folders({CHAT: folder_of(CHAT, root)})
    sink = Sink()

    def envs(tenancy: WorkspaceTenancy) -> LocalEnvRegistry:
        return LocalEnvRegistry(
            tenancy.folder,
            tmp_path / "envs",
            runner=LocalCommandRunner(),
            python=sys.executable,
            uv=shutil.which("uv") or "uv",
        )

    box, engines = make_box(tmp_path, folders, sink, envs=envs)
    rig = Rig(box, sink, folders, engines, root)
    try:
        host = box.host_factory(root, AGENT)
        port = await host.create(NOTEBOOK, [InsertCellOp(source="1")], {"env": "script"})
        await port.apply(
            [SetSettingOp(key="header", value='# /// script\n# dependencies = ["tinypkg"]\n# ///')],
            None,
        )
        notebook_file = root / NOTEBOOK
        assert '"tinypkg"' in notebook_file.read_text(encoding="utf-8")
        who = dict(PERSON)
        if can_edit is not None:
            who["can_edit"] = can_edit
        request = rig.request(
            "env_action", {"action": "remove", "packages": ["tinypkg"], "requested_by": who}
        )
        await box.dispatch(request)
        answer = await sink.until(lambda: sink.answer(request.request_id))
        if expected == "removed":
            assert "error" not in answer, answer
            assert "tinypkg" not in notebook_file.read_text(encoding="utf-8")
        else:
            assert answer["error"]["code"] == "forbidden"
            assert "needs edit rights" in answer["error"]["message"]
            assert '"tinypkg"' in notebook_file.read_text(encoding="utf-8")
    finally:
        await box.close()


# -- the agents' host factory -----------------------------------------------------------


async def test_a_workspace_member_reaches_its_workspace_s_engine(tmp_path: Path) -> None:
    """Two chats in one workspace (their sandbox scope's tree is the
    workspace's) open the same engine; a chat on its own gets its own."""
    ws_root = tmp_path / "ws" / "files"
    own_root = tmp_path / "own" / "files"
    ws_root.mkdir(parents=True)
    own_root.mkdir(parents=True)
    scopes = {
        CHAT: SandboxScope(session_id=CHAT, tree=WS, container=CHAT),
        OTHER_CHAT: SandboxScope(session_id=OTHER_CHAT, tree=WS, container=OTHER_CHAT),
    }
    lone = "5c4b3a29-1807-4f6e-9d8c-7b6a59483726"
    folders = Folders(
        {WS: folder_of(WS, ws_root), lone: folder_of(lone, own_root, lease=str(uuid.uuid4()))}
    )
    box, engines = make_box(
        tmp_path, folders, Sink(), scope=lambda s: scopes.get(s) or own_scope(s)
    )
    try:
        mine = box.host_factory(ws_root, AGENT)
        theirs = box.host_factory(
            ws_root, ActorRef(kind="agent", id=agent_actor_id(OTHER_CHAT), display_name="a")
        )
        alone = box.host_factory(
            own_root, ActorRef(kind="agent", id=agent_actor_id(lone), display_name="a")
        )
        await mine.create(NOTEBOOK, [InsertCellOp(source="x = 1")], {})
        assert await theirs.notebooks() == [NOTEBOOK]
        assert await alone.notebooks() == []
        assert len(engines) == 2
    finally:
        await box.close()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("not-held", id="a-chat-whose-folder-this-box-does-not-hold"),
        pytest.param("elsewhere", id="a-chat-working-outside-the-folder"),
        pytest.param("person", id="an-actor-that-is-no-chat-s-agent"),
    ],
)
async def test_a_chat_without_its_folder_here_has_no_notebooks(rig: Rig, case: str) -> None:
    actor, root = AGENT, rig.root
    if case == "not-held":
        actor = ActorRef(kind="agent", id=agent_actor_id(OTHER_CHAT), display_name="a")
    elif case == "elsewhere":
        root = rig.root.parent
    else:
        actor = ActorRef(kind="person", id="user:ada", display_name="Ada")
    with pytest.raises(NotebookToolError) as raised:
        rig.box.host_factory(root, actor)
    assert raised.value.code == "unavailable"
    assert rig.engines == []


async def test_putting_a_workspace_away_closes_its_engine_and_the_next_use_makes_one(
    rig: Rig,
) -> None:
    await _agent_notebook(rig, ["x = 1"])
    await rig.box.dispatch(rig.request("snapshot"))
    (engine,) = rig.engines
    await rig.box.put_away(CHAT)
    with pytest.raises(Exception, match="closed"):
        await engine.open(NOTEBOOK)
    host = rig.box.host_factory(rig.root, AGENT)
    await host.open(NOTEBOOK)
    assert len(rig.engines) == 2


# -- the slot the runtime and the machine channel hold ---------------------------------


async def test_the_slot_refuses_until_started_then_serves_the_box(rig: Rig) -> None:
    composed: list[Any] = []

    def compose(custody: Any, headers: Any, api_url: str) -> BoxNotebooks:
        composed.append((custody, api_url))
        return rig.box

    slot = BoxNotebookSlot(compose=compose)
    with pytest.raises(NotebookToolError):
        slot(rig.root, AGENT)
    slot.dispatch({"t": "machine.request", "kind": "notebook"})  # malformed: ignored
    assert slot.start(object(), dict, "http://api") is rig.box
    assert slot.start(object(), dict, "http://api") is rig.box
    assert len(composed) == 1
    host = slot(rig.root, AGENT)
    await host.create(NOTEBOOK, [InsertCellOp(source="x = 1")], {})
    request = rig.request("envs")
    slot.dispatch(request.model_dump(mode="json"))
    await rig.sink.until(lambda: rig.sink.answer(request.request_id))


async def test_the_machine_channel_hands_a_notebook_request_over_and_acks_nothing() -> None:
    handed: list[Mapping[str, Any]] = []

    class NoFolders:
        def held_by_lease_node(self, lease_node_id: str) -> None:
            return None

        def push(self, chat_id: str) -> None:
            return None

    requests = MachineRequests(NoFolders(), notebooks=handed.append)
    frame = {"t": "machine.request", "kind": "notebook", "request_id": str(uuid.uuid4())}
    assert requests.handle(frame) is None
    assert handed == [frame]
    assert MachineRequests(NoFolders()).handle(frame) is None


# -- the folders a box holds --------------------------------------------------------------


@dataclass
class _Record:
    drive_id: str = DRIVE
    node_id: str = LEASE
    epoch: int = 7
    instance_id: str = "inst-9"


@dataclass
class _Api:
    rows: dict[str, str]

    def resolve(self, paths: Sequence[str]) -> dict[str, str]:
        return {p: self.rows[p] for p in paths if p in self.rows}


@dataclass
class _Sync:
    root: Path
    api: _Api
    fenced: bool = False


@dataclass
class _Held:
    chat_id: str
    root: Path
    record: _Record
    live: _Sync | None


class _Custody:
    def __init__(self, held: _Held) -> None:
        self._held = held

    def held(self, chat_id: str) -> Any:
        return self._held if chat_id == self._held.chat_id else None

    def held_by_lease_node(self, lease_node_id: str) -> Any:
        return self._held if lease_node_id == self._held.record.node_id else None


def test_a_held_folder_is_its_live_tree_under_its_lease_s_fence(tmp_path: Path) -> None:
    sync = _Sync(tmp_path / "chat" / "files", _Api({NOTEBOOK: "node-1"}))
    held = _Held(CHAT, tmp_path / "chat", _Record(), sync)
    folders = HeldFolders(_Custody(held))  # type: ignore[arg-type]
    found = folders.by_key(CHAT)
    by_lease = folders.by_lease(LEASE)
    assert found is not None and by_lease is not None and by_lease.key == found.key == CHAT
    assert (found.root, found.drive_id, found.lease_node_id, found.step) == (
        sync.root,
        DRIVE,
        LEASE,
        "files",
    )
    assert dict(found.headers) == dict(fence_headers(7, "inst-9"))
    assert found.node_at(NOTEBOOK) == "node-1" and found.node_at("absent.alknb.py") is None
    assert folders.by_key(OTHER_CHAT) is None and folders.by_lease(str(uuid.uuid4())) is None


def test_a_folder_whose_live_sync_has_not_started_holds_no_notebooks(tmp_path: Path) -> None:
    held = _Held(CHAT, tmp_path / "chat", _Record(), None)
    assert HeldFolders(_Custody(held)).by_key(CHAT) is None  # type: ignore[arg-type]


def test_a_folder_whose_fence_closed_is_still_held_and_reads_paused(tmp_path: Path) -> None:
    """Missed heartbeats pause the folder; they do not make it gone."""
    sync = _Sync(tmp_path / "chat" / "files", _Api({}), fenced=True)
    folders = HeldFolders(_Custody(_Held(CHAT, tmp_path / "chat", _Record(), sync)))  # type: ignore[arg-type]
    found = folders.by_key(CHAT)
    assert found is not None and found.paused
    assert folders.by_lease(LEASE) is not None
    sync.fenced = False
    assert folders.by_key(CHAT).paused is False  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("step", "lease_path", "expected"),
    [
        pytest.param("files", "files/a/b.alknb.py", "a/b.alknb.py", id="under-the-step"),
        pytest.param("", "a/b.alknb.py", "a/b.alknb.py", id="no-step"),
        pytest.param("files", "other/b.alknb.py", None, id="beside-the-tree"),
        pytest.param("files", "files", None, id="the-tree-itself"),
        pytest.param("files", "files/../x.alknb.py", None, id="climbing-out"),
        pytest.param("", "/", None, id="nothing"),
    ],
)
def test_a_path_the_drive_names_is_spelled_under_the_tree(
    tmp_path: Path, step: str, lease_path: str, expected: str | None
) -> None:
    assert folder_of(CHAT, tmp_path, step=step).under_root(lease_path) == expected


# -- frames, widget messages and tables ------------------------------------------------

ANN = uuid.UUID("a0000000-0000-4000-8000-00000000a001")
SLIDER = "import alkera.ui as ui\nslider = ui.slider(0, 10, value=5)\nslider"
DOUBLED = "doubled = slider.value * 2\ndoubled"


def frame_id_for(owner: uuid.UUID) -> str:
    """A frame id as the backend mints one for ``owner`` (its first part is
    the owner's id; the box reads nothing else of it)."""
    return f"{owner.hex}.any.{'0' * 16}.{uuid.uuid4().hex}"


async def _ran(rig: Rig, cells: list[str]) -> list[str]:
    ids = await _agent_notebook(rig, cells)
    request = rig.request("run", {"run_id": "setup-1", "target": {"kind": "all"}})
    await rig.box.dispatch(request)
    await rig.sink.until(
        lambda: any(
            e.get("type") == "run.finished" and e.get("run_id") == "setup-1"
            for e in rig.sink.events()
        )
    )
    return ids


async def test_a_person_s_frame_is_theirs_and_their_widget_message_runs_as_them(
    rig: Rig,
) -> None:
    """A frame is attached under the client of the person the backend minted
    it for: its comm-open replays come back in the answer, a widget message
    sent from it is delivered as that person's (the run it causes names
    them), and its own messages reach the backend addressed to the frame."""
    slider_cell, _doubled = await _ran(rig, [SLIDER, DOUBLED])
    frame_id = frame_id_for(ANN)
    attach = rig.request("frame_attach", {"frame_id": frame_id, "output_id": slider_cell})
    await rig.box.dispatch(attach)
    answer = rig.sink.answer(attach.request_id)
    assert answer is not None and "error" not in answer, answer
    (model,) = {o["comm_id"] for o in answer["result"]["opens"]}

    comm = rig.request(
        "comm",
        {
            "frame_id": frame_id,
            "comm_id": model,
            "msg_id": "m-1",
            "content": {"data": {"method": "update", "state": {"value": 7}, "buffer_paths": []}},
            "buffers": [],
            "requested_by": {"kind": "person", "id": f"user:{ANN}", "display_name": "Ann"},
        },
    )
    await rig.box.dispatch(comm)
    assert "error" not in (rig.sink.answer(comm.request_id) or {"error": "none"})
    widget_run = await rig.sink.until(
        lambda: [
            e
            for e in rig.sink.events()
            if e.get("type") == "run.queued" and e.get("trigger") == "widget"
        ]
    )
    assert widget_run[0]["requested_by"]["id"] == f"user:{ANN}"
    to_frame = await rig.sink.until(
        lambda: [
            e
            for e in rig.sink.events()
            if e.get("type") == "frame.message" and e.get("frame_id") == frame_id
        ]
    )
    assert all(isinstance(b, str) for e in to_frame for b in e.get("buffers", []))

    detach = rig.request("frame_detach", {"prefix": ANN.hex})
    await rig.box.dispatch(detach)
    assert rig.sink.answer(detach.request_id)["result"] == {"detached": 1}  # type: ignore[index]


def _queued(rig: Rig, run_id: str | None = None, trigger: str | None = None) -> Any:
    return lambda: [
        e
        for e in rig.sink.events()
        if e.get("type") == "run.queued"
        and (run_id is None or e.get("run_id") == run_id)
        and (trigger is None or e.get("trigger") == trigger)
    ]


@pytest.mark.parametrize(
    "sender",
    [
        pytest.param({"kind": "person", "id": f"user:{ANN}", "display_name": "Ann"}, id="person"),
        pytest.param(
            {
                "kind": "agent",
                "id": "agent:ann-chat",
                "display_name": "Agent",
                "acting_for": {"id": f"user:{ANN}", "display_name": "Ann"},
            },
            id="agent-for-the-owner",
        ),
    ],
)
async def test_a_person_whose_first_action_is_a_widget_move_keeps_their_name(
    rig: Rig, sender: dict[str, Any]
) -> None:
    """The frame is attached with no requester (the backend names only the
    frame), so the owner's client is first made nameless; the widget message
    that names them (or the agent acting for them) gives the run their name,
    and so does every run they ask for after, through the same client."""
    slider_cell, _doubled = await _ran(rig, [SLIDER, DOUBLED])
    frame_id = frame_id_for(ANN)
    attach = rig.request("frame_attach", {"frame_id": frame_id, "output_id": slider_cell})
    await rig.box.dispatch(attach)
    answer = rig.sink.answer(attach.request_id)
    assert answer is not None and "error" not in answer, answer
    (model,) = {o["comm_id"] for o in answer["result"]["opens"]}

    comm = rig.request(
        "comm",
        {
            "frame_id": frame_id,
            "comm_id": model,
            "msg_id": "m-1",
            "content": {"data": {"method": "update", "state": {"value": 3}, "buffer_paths": []}},
            "buffers": [],
            "requested_by": sender,
        },
    )
    await rig.box.dispatch(comm)
    widget_run = await rig.sink.until(_queued(rig, trigger="widget"))
    assert widget_run[0]["requested_by"]["id"] == f"user:{ANN}"
    assert widget_run[0]["requested_by"]["display_name"] == "Ann"

    run = rig.request(
        "run",
        {
            "run_id": "ann-run",
            "target": {"kind": "cells", "ids": [slider_cell]},
            "requested_by": {"kind": "person", "id": f"user:{ANN}", "display_name": "Ann"},
        },
    )
    await rig.box.dispatch(run)
    queued = await rig.sink.until(_queued(rig, run_id="ann-run"))
    assert queued[0]["requested_by"]["display_name"] == "Ann"


async def test_a_requester_s_kept_client_takes_the_name_a_later_request_gives(rig: Rig) -> None:
    """A requester first named with no name is named once a request carries
    one, and a later request with no name does not take it away."""
    (cell,) = await _agent_notebook(rig, ["3"])
    for run_id, who in [
        ("r-1", {"kind": "person", "id": "user:nora"}),
        ("r-2", {"kind": "person", "id": "user:nora", "display_name": "Nora Member"}),
        ("r-3", {"kind": "person", "id": "user:nora"}),
    ]:
        request = rig.request(
            "run",
            {"run_id": run_id, "target": {"kind": "cells", "ids": [cell]}, "requested_by": who},
        )
        await rig.box.dispatch(request)
        # One after another: an identical queued request would join the last.
        await rig.sink.until(
            lambda run_id=run_id: any(
                e.get("type") == "run.finished" and e.get("run_id") == run_id
                for e in rig.sink.events()
            )
        )
    names = [
        (await rig.sink.until(_queued(rig, run_id=r)))[0]["requested_by"]["display_name"]
        for r in ("r-1", "r-2", "r-3")
    ]
    assert names == ["", "Nora Member", "Nora Member"]


async def test_a_message_from_a_frame_nobody_attached_is_refused(rig: Rig) -> None:
    await _ran(rig, [SLIDER, DOUBLED])
    comm = rig.request(
        "comm",
        {
            "frame_id": frame_id_for(ANN),
            "comm_id": "nope",
            "msg_id": "m-1",
            "content": {"data": {"method": "update", "state": {}}},
        },
    )
    await rig.box.dispatch(comm)
    answer = rig.sink.answer(comm.request_id)
    assert answer is not None and "error" in answer


async def test_a_table_page_is_read_from_the_named_frame_and_a_bad_column_is_the_kernel_s_word(
    rig: Rig,
) -> None:
    pytest.importorskip("pandas")
    (cell,) = await _ran(
        rig, ["import pandas as pd\nframe = pd.DataFrame({'a': list(range(30))})\nframe"]
    )
    page = rig.request("table", {"cell_id": cell, "offset": 10, "limit": 5, "sort": []})
    await rig.box.dispatch(page)
    answer = rig.sink.answer(page.request_id)
    assert answer is not None and "error" not in answer, answer
    assert answer["result"]["total_rows"] == 30
    assert answer["result"]["rows"] == [[10], [11], [12], [13], [14]]
    assert answer["result"]["offset"] == 10

    bad = rig.request(
        "table",
        {
            "cell_id": cell,
            "offset": 0,
            "limit": 5,
            "sort": [{"column": "nope", "descending": False}],
        },
    )
    await rig.box.dispatch(bad)
    refused = rig.sink.answer(bad.request_id)
    assert refused is not None and "result" not in refused
    # The kernel's own name for the refusal, passed through untouched (a
    # kernel that tells an unknown column apart says inspect.unknown_column).
    assert refused["error"]["code"] in ("inspect.unknown_column", "inspect.query_failed")
    assert "nope" in refused["error"]["message"]


# -- where a box's notebook SQL comes from ------------------------------------------


def test_a_box_s_notebook_sql_is_every_registered_factory_s_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The box composes its notebooks' SQL from the factories registered on
    the slot, each handed the box's runtime and tool registry, in the order
    they registered; with none registered a notebook resolves no platform
    connection."""
    from alkera_cli.notebooks import sql_slot
    from alkera_core.extensions import ExtensionPoint

    point: ExtensionPoint[sql_slot.NotebookSqlFactory] = ExtensionPoint("test_notebook_sql")
    monkeypatch.setattr(sql_slot, "NOTEBOOK_SQL_PROVIDERS", point)
    runtime, seen = object(), []

    async def tools() -> Any:
        return None

    context = sql_slot.NotebookSqlContext(runtime=runtime, tools=tools)
    assert sql_slot.sql_providers(context) == []

    point = ExtensionPoint("test_notebook_sql")
    monkeypatch.setattr(sql_slot, "NOTEBOOK_SQL_PROVIDERS", point)

    def first(ctx: sql_slot.NotebookSqlContext) -> list[Any]:
        seen.append(("first", ctx.runtime, ctx.tools))
        return ["a", "b"]

    def second(ctx: sql_slot.NotebookSqlContext) -> list[Any]:
        seen.append(("second", ctx.runtime, ctx.tools))
        return ["c"]

    point.register(first)
    point.register(second)
    assert sql_slot.sql_providers(context) == ["a", "b", "c"]
    assert seen == [("first", runtime, tools), ("second", runtime, tools)]


@pytest.fixture
def compiled_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This process as a compiled build sees itself: ``__compiled__`` set, the
    kernel's platform files bundled under the build's directory (two levels
    above the kernel_mount module's own file), and no importable
    ``_alkera_kernel`` or ``alkera`` sources. Returns the bundled tree."""
    import importlib.util

    from alkera_cli.notebooks import kernel_mount

    dist = tmp_path / "alkera.dist"
    bundled = kernel_mount.bundle(dist.joinpath(*kernel_mount.BUNDLED_SUBDIR))
    monkeypatch.setattr(
        kernel_mount, "__file__", str(dist / "alkera_cli" / "notebooks" / "kernel_mount.py")
    )
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", object(), raising=False)
    real_find_spec = importlib.util.find_spec

    def compiled_find_spec(name: str, package: str | None = None) -> Any:
        if name in ("_alkera_kernel", "alkera"):
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", compiled_find_spec)
    return bundled


def assert_mounts_bundle(mount: Path, bundled: Path) -> None:
    assert (mount / "boot.py").read_bytes() == (bundled / "boot.py").read_bytes()
    assert (mount / "_alkera_kernel" / "__init__.py").is_file()
    assert (mount / "public" / "alkera" / "__init__.py").is_file()


async def test_a_compiled_box_s_local_kernels_run_from_the_bundled_platform_files(
    tmp_path: Path, compiled_layout: Path
) -> None:
    """A compiled build has no installed alkera-kernel to link a kernel's
    mount to: the box's local engines mount the files the build bundles."""
    from alkera_cli.notebooks.box_compose import local_engines
    from alkera_notebook.document.file_store import FileDocumentStore

    class Files:
        def store_for(self, tenancy: WorkspaceTenancy) -> FileDocumentStore:
            return FileDocumentStore(str(tenancy.folder))

    root = tmp_path / "ws" / "files"
    root.mkdir(parents=True)
    engine_for = local_engines(Files(), [], data_home=tmp_path / "data")
    engine = engine_for(WorkspaceTenancy(WS, root, tmp_path / "envs"))
    try:
        assert_mounts_bundle(Path(engine.kernel_mount()), compiled_layout)
    finally:
        await engine.close()


async def test_a_compiled_desktop_daemon_s_kernels_run_from_the_bundled_platform_files(
    tmp_path: Path, compiled_layout: Path
) -> None:
    """The desktop daemon's notebook hosts, in a compiled build, mount the
    files the build bundles too."""
    from alkera_cli.notebooks.session import default_host_factory
    from alkera_notebook.tools.engine_adapter import LocalEngines

    factory = default_host_factory()
    assert isinstance(factory, LocalEngines)
    root = tmp_path / "project"
    root.mkdir()
    try:
        engine = factory.workspace(root).engine
        assert_mounts_bundle(Path(engine.kernel_mount()), compiled_layout)
    finally:
        await factory.close()


@pytest.mark.parametrize(
    ("mode", "shared"),
    [
        pytest.param("none", False, id="a-none-box-keeps-each-member-s-own"),
        pytest.param("gvisor", True, id="gvisor-members-share-the-workspace-s"),
    ],
)
async def test_a_box_s_local_engines_say_whether_members_share_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, shared: bool
) -> None:
    """The local engines a box composes report, on their environment listing,
    whether the workspace's members share its environments on this box: on a
    ``none`` box each member keeps its own, so the notebook says so."""
    from alkera_cli.harness import sandbox as sb
    from alkera_cli.notebooks.box_compose import local_engines
    from alkera_notebook.document.file_store import FileDocumentStore

    monkeypatch.setenv(sb.ENV_MODE, mode)

    class Files:
        def store_for(self, tenancy: WorkspaceTenancy) -> FileDocumentStore:
            return FileDocumentStore(str(tenancy.folder))

    root = tmp_path / "ws" / "files"
    root.mkdir(parents=True)
    engine_for = local_engines(Files(), [], data_home=tmp_path / "data")
    engine = engine_for(WorkspaceTenancy(WS, root, tmp_path / "envs"))
    try:
        # What the engine's listing reports (the engine's own tests pin that
        # the listing carries it).
        assert engine.config.shared_envs is shared
    finally:
        await engine.close()


# -- every request the backend waits on is answered, whatever the kernel holds --------

ROWS = 70
DATED_TABLE = (
    "import datetime, decimal\n"
    "import pandas as pd\n"
    "n = %d\n"
    "orders = pd.DataFrame({\n"
    "    'n': list(range(n)),\n"
    "    'day': [datetime.date(2026, 1, 1) + datetime.timedelta(days=i) for i in range(n)],\n"
    "    'amount': [decimal.Decimal(i) / 4 for i in range(n)],\n"
    "    'ratio': [float('nan') if i %% 7 == 0 else i / 2 for i in range(n)],\n"
    "    'raw': [bytes([i]) for i in range(n)],\n"
    "    'big': [2**60 + i for i in range(n)],\n"
    "})\n"
    "orders"
)
ORDER_COLUMNS = ["n", "day", "amount", "ratio", "raw", "big"]


def _table_cell(rows: int) -> str:
    return DATED_TABLE % rows


def _order(i: int) -> list[Any]:
    """Row ``i`` of ``orders`` as a table page carries it, worked out by hand
    (pandas spells a missing float as NaN, so it is null)."""
    day = date(2026, 1, 1) + timedelta(days=i)
    ratio = None if i % 7 == 0 else i / 2
    return [i, day.isoformat(), str(Decimal(i) / 4), ratio, f"0x{i:02x}", str(2**60 + i)]


def _shown_page(sink: Sink, cell: str) -> dict[str, Any]:
    """The table page ``cell``'s output carried to the backend."""

    def find(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if TABLE_MIME in value:
                return dict(value[TABLE_MIME])
            value = list(value.values())
        if isinstance(value, list):
            return next((found for found in map(find, value) if found is not None), None)
        return None

    shown = [
        found for e in sink.events() if e.get("cell_id") == cell and (found := find(e)) is not None
    ]
    assert shown, "no table output was relayed"
    return shown[-1]


async def _restart(rig: Rig) -> None:
    request = rig.request("kernel", {"action": "restart"})
    await rig.box.dispatch(request)
    answer = rig.sink.answer(request.request_id)
    assert answer is not None and "error" not in answer, answer


async def test_a_page_past_the_first_of_a_dated_table_is_answered_with_its_rows(
    rig: Rig,
) -> None:
    """A SQL cell's frame holds dates and decimals; the kernel pages them as
    their Python values. The answer must still reach the backend (a body the
    box cannot encode was dropped whole, answer and all, and the person read
    "did not answer"), carrying them as the first page shows them."""
    pytest.importorskip("pandas")
    (cell,) = await _ran(rig, [_table_cell(ROWS)])
    page = rig.request("table", {"cell_id": cell, "offset": 50, "limit": 50, "sort": []})
    await rig.box.dispatch(page)
    answer = rig.sink.answer(page.request_id)
    assert answer is not None and "error" not in answer, answer
    assert answer["result"] == {
        "schema": _shown_page(rig.sink, cell)["schema"],
        "rows": [_order(i) for i in range(50, ROWS)],
        "total_rows": ROWS,
        "offset": 50,
    }
    assert [c["name"] for c in answer["result"]["schema"]] == ORDER_COLUMNS


async def test_a_table_page_is_the_same_page_however_it_is_reached(rig: Rig) -> None:
    """The first page a table output carries, the page the kernel answers for
    the same rows, and the page cut from the saved output are one page: the
    same keys, the same typed columns and the same encoding of dates,
    decimals, missing values, bytes and integers past a double, so the table
    reads all three the same way."""
    pytest.importorskip("pandas")
    cell, small = await _ran(rig, [_table_cell(ROWS), "orders.head(30)"])
    shown = _shown_page(rig.sink, cell)
    assert shown.pop("source") == {"name": "orders"}
    assert shown == {
        "schema": shown["schema"],
        "rows": [_order(i) for i in range(50)],
        "total_rows": ROWS,
        "offset": 0,
    }
    assert [c["name"] for c in shown["schema"]] == ORDER_COLUMNS

    # The output holds 50 of 70 rows, so this page is the kernel's.
    asked = rig.request("table", {"cell_id": cell, "offset": 0, "limit": 50, "sort": []})
    await rig.box.dispatch(asked)
    from_kernel = rig.sink.answer(asked.request_id)
    assert from_kernel is not None and from_kernel["result"] == shown

    # A table the output holds whole is cut from the output, with no kernel.
    whole = _shown_page(rig.sink, small)
    assert whole["total_rows"] == 30 and len(whole["rows"]) == 30
    stop = rig.request("kernel", {"action": "shutdown"})
    await rig.box.dispatch(stop)
    saved = rig.request("table", {"cell_id": small, "offset": 0, "limit": 50, "sort": []})
    await rig.box.dispatch(saved)
    from_saved = rig.sink.answer(saved.request_id)
    assert from_saved is not None and from_saved["result"] == {
        "schema": shown["schema"],
        "rows": [_order(i) for i in range(30)],
        "total_rows": 30,
        "offset": 0,
    }


async def test_a_table_the_restarted_kernel_no_longer_holds_is_refused_with_what_to_do(
    rig: Rig,
) -> None:
    pytest.importorskip("pandas")
    (cell,) = await _ran(rig, [_table_cell(ROWS)])
    await _restart(rig)
    page = rig.request("table", {"cell_id": cell, "offset": 50, "limit": 50, "sort": []})
    await rig.box.dispatch(page)
    answer = rig.sink.answer(page.request_id)
    assert answer is not None and "result" not in answer, answer
    assert answer["error"] == {
        "code": "table_not_held",
        "message": "Run the cell again to page this table.",
    }


async def test_a_table_whose_output_holds_every_row_pages_without_a_kernel(rig: Rig) -> None:
    """The output saved with the notebook holds every row of a small table,
    so a later page is cut from it even with no kernel to ask; a sort still
    needs the kernel, and says so."""
    pytest.importorskip("pandas")
    (cell,) = await _ran(rig, [_table_cell(30)])
    stop = rig.request("kernel", {"action": "shutdown"})
    await rig.box.dispatch(stop)
    stopped = rig.sink.answer(stop.request_id)
    assert stopped is not None and stopped["result"]["kernel_id"] is None, stopped
    page = rig.request("table", {"cell_id": cell, "offset": 20, "limit": 5, "sort": []})
    await rig.box.dispatch(page)
    answer = rig.sink.answer(page.request_id)
    assert answer is not None and "error" not in answer, answer
    assert answer["result"] == {
        "schema": _shown_page(rig.sink, cell)["schema"],
        "rows": [_order(i) for i in range(20, 25)],
        "total_rows": 30,
        "offset": 20,
    }
    sorted_page = rig.request(
        "table",
        {"cell_id": cell, "offset": 0, "limit": 5, "sort": [{"column": "n", "descending": True}]},
    )
    await rig.box.dispatch(sorted_page)
    refused = rig.sink.answer(sorted_page.request_id)
    assert refused is not None and refused["error"]["code"] == "table_not_held"


FRAME_OF_ADA = f"{uuid.UUID(int=7).hex}.any.{'0' * 16}.{uuid.UUID(int=8).hex}"


def _bodies(cell: str) -> dict[str, dict[str, Any]]:
    """A request body for every op the box serves."""
    return {
        "run": {"run_id": f"r-{uuid.uuid4().hex[:8]}", "target": {"kind": "cells", "ids": [cell]}},
        "kernel": {"action": "status"},
        "outputs_clear": {"cell_ids": ["no-such-cell"]},
        "comm": {"frame_id": FRAME_OF_ADA, "comm_id": "c1", "msg_id": "m1", "content": {}},
        "env_install": {"packages": ["polars"]},
        "env_action": {"action": "remove", "packages": ["polars"]},
        "snapshot": {},
        "frame_attach": {"frame_id": FRAME_OF_ADA, "model_ids": [], "output_id": None},
        "frame_detach": {"frame_id": FRAME_OF_ADA},
        "table": {"cell_id": cell, "offset": 50, "limit": 50, "sort": []},
        "envs": {},
        "env_packages": {"env_id": "no-such-env"},
    }


async def _in_state(rig: Rig, state: str, tmp_path: Path) -> tuple[BoxNotebooks, Sink, str]:
    """The box, its sink and the table cell, with the notebook's kernel as
    ``state`` leaves it."""
    if state == "kernel-absent":
        (cell,) = await _agent_notebook(rig, [_table_cell(ROWS)])
        return rig.box, rig.sink, cell
    (cell,) = await _ran(rig, [_table_cell(ROWS)])
    if state == "kernel-restarted":
        await _restart(rig)
        return rig.box, rig.sink, cell
    # Saved output only: another box process opens the notebook its kernel
    # never ran in, from what was saved beside it.
    await rig.box.close()
    sink = Sink()
    box, _ = make_box(tmp_path / "again", rig.folders, sink)
    return box, sink, cell


@pytest.mark.parametrize("state", ["kernel-absent", "kernel-restarted", "saved-output-only"])
async def test_every_op_is_answered_exactly_once_whatever_the_kernel_holds(
    rig: Rig, tmp_path: Path, state: str
) -> None:
    """The backend waits for one ``answer`` event per request and tells the
    person the machine was silent when none comes. Every op the box serves
    is answered, once, with a result or an error that names a code and says
    what happened, with no kernel, after a restart, and with only the saved
    output: a new op gets a body here or this fails."""
    pytest.importorskip("pandas")
    box, sink, cell = await _in_state(rig, state, tmp_path)
    bodies = _bodies(cell)
    assert set(bodies) == set(SERVED_OPS)
    try:
        for op, body in bodies.items():
            request = rig.request(op, body)
            await box.dispatch(request)
            answers = sink.answers(request.request_id)
            assert len(answers) == 1, (op, answers)
            (answer,) = answers
            if "error" in answer:
                assert answer["error"]["code"] and answer["error"]["message"], (op, answer)
            else:
                assert isinstance(answer["result"], dict), (op, answer)
    finally:
        if box is not rig.box:
            await box.close()
