"""Read after write across clients of one notebook: once a client's delete
returns, no reader of the session lists the deleted cell, even while the
session is still catching up with a change another writer made through the
store just before.

The store here answers loads late (a platform store's load is a round trip):
a load returns the document as it was when the load began. The session's
follower is made to start such a load before the delete, and to finish it
after the delete's own refresh."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.ops import DeleteCell, InsertCell, ReplaceCell
from alkera_notebook.document.store import StoredNotebook
from alkera_notebook.engine import Actor
from nbeng_harness import BOB, engine_for, notebook, until

PATH = "nb.alknb.py"
CAROL = Actor(kind="person", id="u-carol", display_name="Carol", can_edit=True, can_run=True)


class LateLoads:
    """A file store whose loads answer after a yield, and whose next load can
    be held until released."""

    def __init__(self, inner: FileDocumentStore) -> None:
        self.inner = inner
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def load(self, path: str) -> StoredNotebook:
        stored = await self.inner.load(path)
        hold, self.hold = self.hold, None
        if hold is not None:
            self.held.set()
            await hold.wait()
        await asyncio.sleep(0)
        return stored


async def _peer_ops(store: FileDocumentStore, root: Path, keep: str) -> None:
    await store.apply(PATH, [ReplaceCell(cell_id=keep, source="z = 4")], None, CAROL, None)


async def _file_reload(store: FileDocumentStore, root: Path, keep: str) -> None:
    file = root / PATH
    file.write_text(file.read_text().replace("z = 3", "z = 4"))
    await store.reload(PATH)


@pytest.mark.parametrize(
    "other_writer",
    [
        pytest.param(_peer_ops, id="a_peer_writing_through_the_store"),
        pytest.param(_file_reload, id="the_file_changed_outside"),
    ],
)
async def test_a_delete_is_never_unlisted_by_a_late_follow(
    tmp_path: Path, other_writer: Any
) -> None:
    async with engine_for(tmp_path, store_wrapper=LateLoads) as engine:
        store = engine.store
        assert isinstance(store, LateLoads)
        session, ann, (keep_a, gone, keep_z) = await notebook(engine, ["x = 1", "y = 2", "z = 3"])
        bob = session.attach(BOB)

        release = asyncio.Event()
        store.hold = release
        await other_writer(store.inner, tmp_path / "ws", keep_z)
        await asyncio.wait_for(store.held.wait(), 5)

        delete = asyncio.create_task(ann.apply([DeleteCell(cell_id=gone)], None))
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(delete, 5)
        assert gone not in [c.id for c in result.cells if not c.deleted]

        listed: list[list[str]] = []
        for _ in range(40):
            listed.append([c.id for c in (await bob.read()).cells])
            listed.append(list((await bob.graph(None, "both")).cells))
            await asyncio.sleep(0)
        assert all(gone not in ids for ids in listed), listed
        assert listed[-1][:2] == [keep_a, keep_z]
        view = await bob.read()
        assert next(c.source for c in view.cells if c.id == keep_z) == "z = 4"


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(lambda c: c.read(), id="read"),
        pytest.param(lambda c: c.graph(None, "both"), id="graph"),
    ],
)
async def test_another_client_reads_without_the_deleted_cell_right_away(
    tmp_path: Path, reader: Any
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, gone, c) = await notebook(engine, ["x = 1", "y = 2", "z = 3"])
        bob = session.attach(BOB)
        await ann.apply([DeleteCell(cell_id=gone)], None)
        view = await reader(bob)
        ids = [cell.id for cell in view.cells] if hasattr(view, "path") else list(view.cells)
        assert ids == [a, c]


async def test_a_change_landing_right_after_open_reaches_the_session(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        made = await engine.store.create(
            PATH, [InsertCell(source="x = 1"), InsertCell(source="y = 2")], {}, CAROL
        )
        keep, gone = made.document.order
        session = await engine.open(PATH)
        bob = session.attach(BOB)
        # Before the session's follower has run at all.
        await engine.store.apply(PATH, [DeleteCell(cell_id=gone)], None, CAROL, None)

        async def listed() -> list[str]:
            return [c.id for c in (await bob.read()).cells]

        await until(lambda: _equals(listed(), [keep]), timeout_s=5)


async def _equals(value: Any, expected: object) -> bool:
    return bool(await value == expected)
