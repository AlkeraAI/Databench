"""The engine reads a notebook's live document a bounded number of times per
run, and never a stale one.

A run used to read the document four times (when asked for and again when
it started: its cells, then who is editing them), each read a path lookup
plus a view over the network; on a busy box a run sat seconds in the queue
behind them. A view is now remembered briefly, but only while the
notebook's follower listens on its channel, and every change signal and
every edit through the store forgets it.

A real engine with a real kernel runs over the store; the backend's view
route is a ``MockTransport`` over a document the test edits, the channel a
scripted :class:`DocSignals`, the clock the store reads pinned by the test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.notebooks.box import BoxFolder, BoxStores
from alkera_cli.notebooks.engine_host import WorkspaceTenancy
from alkera_cli.notebooks.store_loro import DocSignal, Located, LoroDocumentStore
from alkera_notebook.document.ops import EditCell, TextEdit
from alkera_notebook.engine.engine import NotebookEngine
from alkera_notebook.engine.models import Actor, CellsTarget
from alkera_notebook.tools.engine_adapter import local_engine

PATH = "analysis/weekly.alknb.py"
PERSON = Actor(kind="person", id="user:ada", display_name="Ada", can_edit=True, can_run=True)


class Backend:
    """The notebook view and ops routes over a document the test edits; every
    view read and every item it was read for is counted."""

    def __init__(self) -> None:
        self.cells: dict[str, str] = {"c1": "x = 1", "c2": "y = x + 1\ny"}
        self.version = 1
        self.views: list[str] = []
        self.gone: set[str] = set()

    @property
    def token(self) -> str:
        return f"v{self.version}"

    def edit(self, cell: str, source: str) -> None:
        self.cells[cell] = source
        self.version += 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        item = request.url.path.split("/")[5]
        if item in self.gone:
            return httpx.Response(404, json={"code": "not_found", "message": "gone"})
        if request.method == "POST" and request.url.path.endswith("/ops"):
            self.edit("c1", "x = 5")
            return httpx.Response(
                200,
                json={
                    "token": self.token,
                    "repeat": False,
                    "cells": [],
                    "created": [],
                    "notices": [],
                    "graph": {},
                },
            )
        self.views.append(item)
        return httpx.Response(
            200,
            json={
                "token": self.token,
                "cells": [
                    {"id": cid, "kind": "python", "source": src} for cid, src in self.cells.items()
                ],
                "settings": {},
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://backend", transport=httpx.MockTransport(self.handler)
        )


class Channel:
    """The document channel: signals ready, then whatever the test sends;
    ``None`` closes it."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[DocSignal | None] = asyncio.Queue()
        self.ready = asyncio.Event()

    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        yield DocSignal(kind="ready")
        self.ready.set()
        while (signal := await self.queue.get()) is not None:
            yield signal


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _never(_: float) -> None:
    await asyncio.Event().wait()


def _store(
    backend: Backend,
    channel: Channel,
    clock: Clock,
    resolved: list[str] | None = None,
) -> LoroDocumentStore:
    async def resolve(path: str) -> Located:
        if resolved is not None:
            resolved.append(path)
        return Located(drive_id="d1", item_id="i1")

    return LoroDocumentStore(
        http=backend.client(),
        resolve=resolve,
        signals=channel,
        monotonic=clock,
        sleep=_never,
    )


async def _until(found: Callable[[], Any]) -> Any:
    async with asyncio.timeout(30.0):
        while True:
            value = found()
            if value:
                return value
            await asyncio.sleep(0.02)


@pytest.fixture
async def engine_rig(
    tmp_path: Path,
) -> AsyncIterator[tuple[NotebookEngine, Backend, Channel, Clock, list[str]]]:
    backend, channel, clock, resolved = Backend(), Channel(), Clock(), []
    (tmp_path / PATH).parent.mkdir(parents=True)  # the kernel runs beside the notebook
    store = _store(backend, channel, clock, resolved)
    engine = local_engine(tmp_path, data_root=tmp_path / "data", store=store)
    try:
        yield engine, backend, channel, clock, resolved
    finally:
        await engine.close()


async def test_a_run_reads_the_document_once_however_many_times_the_engine_asks(
    engine_rig: tuple[NotebookEngine, Backend, Channel, Clock, list[str]],
) -> None:
    """The notebook sat idle past the freshness window, so the run's first
    read goes to the backend; the engine's other three reads for the run (its
    cells and who edits them, when asked and when started) are served from
    it. Before, each was its own path lookup and view."""
    engine, backend, channel, clock, resolved = engine_rig
    session = await engine.open(PATH)
    client = session.attach(PERSON)
    await asyncio.wait_for(channel.ready.wait(), 30)
    await _until(lambda: backend.views)
    await asyncio.sleep(0.2)  # the follower's catch-up read lands
    clock.now += 60.0
    views, lookups = len(backend.views), len(resolved)

    handle = await client.run(CellsTarget(ids=["c2"]), frontier=backend.token)
    record = await handle.wait(60)

    assert record.status == "ok", (record.reason, record.message)
    assert len(backend.views) - views == 1
    assert len(resolved) - lookups == 1


async def test_a_run_asked_at_a_newer_frontier_runs_the_newer_text(
    engine_rig: tuple[NotebookEngine, Backend, Channel, Clock, list[str]],
) -> None:
    """A person types and runs: the request names the frontier their edit
    made. The remembered view is older; the change signal forgets it, so the
    run waits for and runs the new text instead of the remembered one."""
    engine, backend, channel, _clock, _resolved = engine_rig
    session = await engine.open(PATH)
    client = session.attach(PERSON)
    await asyncio.wait_for(channel.ready.wait(), 30)
    await _until(lambda: backend.views)
    await asyncio.sleep(0.2)
    events = client.queue

    backend.edit("c2", "y = x + 100\ny")
    channel.queue.put_nowait(DocSignal(kind="update", actor_id="user:ada"))
    handle = await client.run(CellsTarget(ids=["c2"]), frontier=backend.token)
    record = await handle.wait(60)

    assert record.status == "ok" and record.frontier == "v2"
    outputs: list[str] = []
    while len(events):
        event = await events.get()
        if event is not None and event.type == "cell.output" and event.cell_id == "c2":
            outputs.append(json.dumps(event.model_dump(mode="json")))
    assert any("101" in o for o in outputs), outputs


async def test_with_no_follower_listening_every_read_goes_to_the_backend() -> None:
    backend, channel, clock = Backend(), Channel(), Clock()
    store = _store(backend, channel, clock)
    await store.load(PATH)
    backend.edit("c1", "x = 2")
    assert (await store.load(PATH)).token == "v2"
    assert (await store.editing(PATH)) == {}
    assert len(backend.views) == 3


async def test_a_view_is_remembered_while_the_channel_listens_and_forgotten_when_it_closes() -> (
    None
):
    backend, channel, clock = Backend(), Channel(), Clock()
    store = _store(backend, channel, clock)
    changes = store.changes(PATH)
    follower = asyncio.ensure_future(changes.__anext__())
    await asyncio.wait_for(channel.ready.wait(), 5)
    await _until(lambda: len(backend.views) >= 2)  # the first read, then at ready
    read = len(backend.views)

    await store.load(PATH)
    await store.snapshot(PATH)
    await store.editing(PATH)
    assert len(backend.views) == read, "served from the view read at ready"

    clock.now += 5.0  # past the freshness window
    await store.load(PATH)
    assert len(backend.views) == read + 1

    channel.queue.put_nowait(None)  # the channel closes; the follower waits to rejoin
    await asyncio.sleep(0.1)
    before = len(backend.views)
    await store.load(PATH)
    await store.load(PATH)
    assert len(backend.views) == before + 2
    follower.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await follower
    await changes.aclose()  # type: ignore[attr-defined]


async def test_a_change_signal_and_the_store_s_own_edit_each_forget_the_view() -> None:
    backend, channel, clock = Backend(), Channel(), Clock()
    store = _store(backend, channel, clock)
    changes = store.changes(PATH)
    follower = asyncio.ensure_future(changes.__anext__())
    await asyncio.wait_for(channel.ready.wait(), 5)
    await _until(lambda: len(backend.views) >= 2)
    assert (await store.load(PATH)).token == "v1"

    # Someone else's edit: the follower reads it on the signal, and the next
    # read answers it.
    backend.edit("c1", "x = 3")
    channel.queue.put_nowait(DocSignal(kind="update", actor_id="user:bo"))
    change = await asyncio.wait_for(follower, 5)
    assert change.token == "v2"
    assert (await store.load(PATH)).token == "v2"

    # The store's own edit: the read right after it is the backend's.
    await store.apply(
        PATH,
        [EditCell(op="edit", cell_id="c1", edits=[TextEdit(old="1", new="5")])],
        None,
        PERSON,
        None,
    )
    assert (await store.load(PATH)).token == "v3"
    await changes.aclose()  # type: ignore[attr-defined]


async def test_the_box_looks_a_path_up_once_and_again_after_the_file_at_it_changed(
    tmp_path: Path,
) -> None:
    """The node at a path is remembered, so a read is not also a lookup; a
    read answered not found forgets it, and the path is looked up again (the
    file replaced at it is read)."""
    backend = Backend()
    lookups: list[str] = []
    nodes = {PATH: str(uuid.uuid4())}

    def node_at(relative: str) -> str | None:
        lookups.append(relative)
        return nodes.get(relative)

    folder = BoxFolder(
        key="chat",
        root=tmp_path,
        drive_id="d1",
        lease_node_id="lease-1",
        step="",
        headers={"X-Fence": "3"},
        node_at=node_at,
    )

    class Folders:
        def by_key(self, key: str) -> BoxFolder | None:
            return folder

        def by_lease(self, lease_node_id: str) -> BoxFolder | None:
            return folder

    stores = BoxStores(http=backend.client(), folders=Folders(), signals=Channel())
    store = stores.store_for(WorkspaceTenancy("chat", tmp_path, tmp_path / "envs"))
    await store.load(PATH)
    await store.load(PATH)
    assert lookups == [PATH]

    old = nodes[PATH]
    nodes[PATH] = str(uuid.uuid4())
    backend.gone.add(old)
    assert (await store.load(PATH)).token == backend.token
    assert lookups == [PATH, PATH]
    assert backend.views[-1] == nodes[PATH]
