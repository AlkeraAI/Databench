"""Suspend and resume (a workspace going to sleep and waking)."""

from __future__ import annotations

from pathlib import Path

from alkera_notebook.engine import AllTarget, CellsTarget
from alkera_notebook.outputs import snapshot_path
from nbeng_fakes import group_alive
from nbeng_harness import ANN, engine_for, notebook, run_cells, statuses, until


async def test_sleep_suspend_during_a_run(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["x = 1\nx", "import time\ntime.sleep(30)"])
        await run_cells(ann, a)
        handle = await ann.run(CellsTarget(ids=[b]))
        await until(lambda: _running(ann, b), timeout_s=10)
        pgid = session.runtime.kernel.launched.pgid  # type: ignore[union-attr]
        report = await engine.suspend()
        record = await handle.wait(10)
        assert (record.status, record.reason) == ("interrupted", "suspended")
        assert report.runs_interrupted == [handle.run_id]
        assert len(report.kernels_stopped) == 1
        assert not group_alive(pgid)
        # The snapshot was written at suspend, with a's output.
        assert snapshot_path(tmp_path / "ws" / "nb.alknb.py").is_file()


async def _running(client: object, cid: str) -> bool:
    return (await statuses(client))[cid] == "running"  # type: ignore[arg-type]


async def test_sleep_resume_shows_saved_outputs_and_every_cell_not_run(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(engine, ["x = 2", "y = x * 21\ny"])
        await (await ann.run(AllTarget())).wait(30)
        await engine.suspend()
        await engine.resume()
        view = await ann.read()
        assert [c.status for c in view.cells] == ["not_run", "not_run"]
        assert view.cells[1].output is not None and view.cells[1].output.text.strip() == "42"
        assert view.cells[1].output_origin == "saved"
        assert view.kernel.state == "absent"
        record = await run_cells(ann, b)
        assert [p.cell_id for p in record.plan] == [a, b]


async def test_sleep_corrupt_snapshot_is_ignored_with_a_notice(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, ["x = 1\nx"])
        await (await ann.run(AllTarget())).wait(30)
        await engine.suspend()
    path = snapshot_path(tmp_path / "ws" / "nb.alknb.py")
    path.write_text('{"version": "1", "cells": [ this is not json')
    async with engine_for(tmp_path) as again:
        client = (await again.open("nb.alknb.py")).attach(ANN)
        view = await client.read()
        assert view.cells[0].output is None
        assert any(n.kind == "corrupt_snapshot" for n in view.notices)
        assert (await run_cells(client, a)).status == "ok"
