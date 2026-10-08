"""Clearing outputs through a real engine and kernel: every attached view is
told, the saved outputs drop them (a new engine does not bring them back), the
values stay in the kernel, and only someone who may run can do it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from alkera_notebook.engine import AllTarget, ForbiddenError, NotFoundError
from alkera_notebook.events.models import CellOutputsCleared
from alkera_notebook.outputs import snapshot_path
from nbeng_harness import ANN, BOB, VIEWER, engine_for, notebook, text_of

PATH = "nb.alknb.py"


async def _flushed(engine: object) -> None:
    for s in engine.sessions:  # type: ignore[attr-defined]
        await s.snapshots.flush()


async def _cleared_event(queue: object) -> CellOutputsCleared:
    while True:
        event = await asyncio.wait_for(queue.get(), 10)  # type: ignore[attr-defined]
        if isinstance(event, CellOutputsCleared):
            return event


async def test_clearing_one_cell_tells_every_view_and_survives_a_new_engine(
    tmp_path: Path,
) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["print('one')\nx = 1", "print('two')"])
        bob = session.attach(BOB)
        await (await ann.run(AllTarget())).wait(30)
        assert await text_of(ann, a) == "one"
        cleared = await ann.clear_outputs([a])
        assert cleared == [a]
        event = await _cleared_event(bob.queue)
        assert event.cell_ids == [a] and event.actor_id == ANN.id
        view = {c.id: c for c in (await bob.read()).cells}
        assert view[a].output is None and view[a].last_run is None
        assert view[b].output is not None and view[b].output.text.strip() == "two"
        # The value the cleared cell defined is still in the kernel.
        inspect = await ann.read()
        assert {c.id: c.status for c in inspect.cells}[a] == "fresh"
        await _flushed(engine)
    saved = json.loads(snapshot_path(tmp_path / "ws" / PATH).read_text())
    outputs = {c["id"]: c["outputs"] + c["console"] for c in saved["cells"]}
    assert outputs[a] == [] and outputs[b] != []
    async with engine_for(tmp_path) as again:
        client = (await again.open(PATH)).attach(ANN)
        view = {c.id: c for c in (await client.read()).cells}
        assert not (view[a].output and view[a].output.text.strip())
        assert view[b].output is not None and view[b].output.text.strip() == "two"


async def test_clearing_every_cell_names_only_the_cells_that_had_outputs(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c) = await notebook(engine, ["print('one')", "print('two')", "y = 2"])
        await (await ann.run(AllTarget())).wait(30)
        before = {cell.id for cell in (await ann.read()).cells if cell.output is not None}
        cleared = await ann.clear_outputs(None)
        assert set(cleared) == before and {a, b} <= set(cleared)
        assert all(cell.output is None for cell in (await ann.read()).cells)
        assert await ann.clear_outputs(None) == []
        assert c in {cell.id for cell in (await ann.read()).cells}


async def test_a_viewer_cannot_clear_outputs(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(engine, ["print('one')"])
        await (await ann.run(AllTarget())).wait(30)
        viewer = session.attach(VIEWER)
        with pytest.raises(ForbiddenError):
            await viewer.clear_outputs([a])
        assert await text_of(ann, a) == "one"


async def test_clearing_a_cell_that_does_not_exist_clears_nothing(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, ["print('one')"])
        await (await ann.run(AllTarget())).wait(30)
        with pytest.raises(NotFoundError):
            await ann.clear_outputs([a, "zzzzzzzzzz"])
        assert await text_of(ann, a) == "one"
