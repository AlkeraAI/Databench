"""A box whose folder heartbeats stop landing for a while keeps its notebooks.

The live sync fences itself after two missed heartbeats (about 30 s of a
stalled backend). That means "stop reading and writing the drive until a beat
lands", not "the folder is gone": the kernel and everything in it stay, the
document follower waits and joins again, and a request that needs the drive
is refused with a reason a person can act on. Only custody letting go of the
folder ends the notebook's kernel.

The custody is a stand-in whose live sync the test fences and unfences; the
folders, the stores, the document store and the engine (a real kernel) are
the box's own.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.notebooks.box import (
    FOLDER_PAUSED_MESSAGE,
    BoxStores,
    FolderPausedError,
    HeldFolders,
)
from alkera_cli.notebooks.engine_host import WorkspaceTenancy
from alkera_cli.notebooks.store_loro import DocSignal
from alkera_notebook.engine.engine import NotebookEngine
from alkera_notebook.engine.errors import NotFoundError
from alkera_notebook.engine.models import Actor, CellsTarget
from alkera_notebook.tools.engine_adapter import local_engine

KEY = "ws:w1"
LEASE = "6b0e1c4f-0000-4000-8000-00000000aaaa"
DRIVE = "6b0e1c4f-0000-4000-8000-00000000dddd"
NODE = "6b0e1c4f-0000-4000-8000-00000000beef"
PATH = "analysis/weekly.alknb.py"
PERSON = Actor(kind="person", id="user:ada", display_name="Ada", can_edit=True, can_run=True)


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


@dataclass
class Custody:
    """The box's folder custody: one folder, held until ``let_go``."""

    held_folder: _Held | None

    def held(self, chat_id: str) -> Any:
        found = self.held_folder
        return found if found is not None and chat_id == found.chat_id else None

    def held_by_lease_node(self, lease_node_id: str) -> Any:
        found = self.held_folder
        return found if found is not None and lease_node_id == found.record.node_id else None


@dataclass
class Backend:
    """The notebook view route over a document the test edits."""

    cells: dict[str, str] = field(default_factory=lambda: {"c1": "x = 41", "c2": "x + 1"})
    version: int = 1

    def edit(self, cell: str, source: str) -> None:
        self.cells[cell] = source
        self.version += 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "token": f"v{self.version}",
                "cells": [
                    {"id": cid, "kind": "python", "source": src} for cid, src in self.cells.items()
                ],
                "settings": {},
            },
        )


class Channel:
    """The document channel: ready, then whatever the test sends."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[DocSignal] = asyncio.Queue()
        self.ready = asyncio.Event()

    async def watch(self, item_id: str) -> AsyncIterator[DocSignal]:
        yield DocSignal(kind="ready")
        self.ready.set()
        while True:
            yield await self.queue.get()


@dataclass
class Rig:
    engine: NotebookEngine
    stores: BoxStores
    sync: _Sync
    custody: Custody
    backend: Backend
    channel: Channel


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    root = tmp_path / "ws" / "files"
    (root / PATH).parent.mkdir(parents=True)
    sync = _Sync(root, _Api({PATH: NODE}))
    custody = Custody(_Held(KEY, tmp_path / "ws", _Record(), sync))
    backend, channel = Backend(), Channel()
    http = httpx.AsyncClient(
        base_url="http://backend", transport=httpx.MockTransport(backend.handler)
    )
    stores = BoxStores(http=http, folders=HeldFolders(custody), signals=channel)  # type: ignore[arg-type]
    store = stores.store_for(WorkspaceTenancy(KEY, root, tmp_path / "envs"))
    store.follow_retry_initial = store.follow_retry_max = 0.05
    engine = local_engine(root, data_root=tmp_path / "data", store=store)
    try:
        yield Rig(engine, stores, sync, custody, backend, channel)
    finally:
        await engine.close()
        await http.aclose()


async def _until(found: Callable[[], Any], within: float = 30.0) -> Any:
    async with asyncio.timeout(within):
        while True:
            value = found()
            if value:
                return value
            await asyncio.sleep(0.02)


async def _value_of(client: Any, cell: str, frontier: str) -> str:
    record = await (await client.run(CellsTarget(ids=[cell]), frontier=frontier)).wait(60)
    assert record.status == "ok", (record.reason, record.message)
    view = await client.read()
    (found,) = [c for c in view.cells if c.id == cell]
    assert found.output is not None
    return found.output.text.strip()


async def test_a_fence_that_closes_and_opens_again_keeps_the_kernel_and_its_values(
    rig: Rig,
) -> None:
    session = await rig.engine.open(PATH)
    client = session.attach(PERSON)
    await asyncio.wait_for(rig.channel.ready.wait(), 30)
    await (await client.run(CellsTarget(ids=["c1"]), frontier="v1")).wait(60)
    kernel = session.runtime.kernel
    assert kernel is not None
    kernel_id = kernel.kernel_id

    # The backend stalls: beats stop landing and the sync fences itself. A
    # signal arrives meanwhile, so the follower tries to read and cannot.
    rig.sync.fenced = True
    rig.channel.queue.put_nowait(DocSignal(kind="update", actor_id="user:bo"))
    await asyncio.sleep(0.5)  # several of the follower's retries
    assert session.runtime.kernel is kernel, "a paused folder is not a deleted notebook"
    with pytest.raises(FolderPausedError, match="reconnecting"):
        await client.run(CellsTarget(ids=["c2"]), frontier="v1")

    # A beat lands. The follower reads again and reports what moved while it
    # could not; the same kernel still holds x.
    rig.backend.edit("c2", "x + 100")
    rig.sync.fenced = False
    await _until(lambda: session.runtime.stored.token == "v2")
    assert await _value_of(client, "c2", "v2") == "141"
    assert session.runtime.kernel is not None
    assert session.runtime.kernel.kernel_id == kernel_id


async def test_only_custody_letting_go_makes_the_folder_gone(rig: Rig) -> None:
    rig.sync.fenced = True
    with pytest.raises(FolderPausedError) as paused:
        rig.stores.folder(KEY)
    assert paused.value.message == FOLDER_PAUSED_MESSAGE
    assert not isinstance(paused.value, NotFoundError)
    with pytest.raises(FolderPausedError):
        await rig.stores.locate(KEY, PATH)

    rig.custody.held_folder = None
    with pytest.raises(NotFoundError, match="does not hold"):
        rig.stores.folder(KEY)
    with pytest.raises(NotFoundError):
        rig.stores.folder(f"ws:{uuid.uuid4()}")
