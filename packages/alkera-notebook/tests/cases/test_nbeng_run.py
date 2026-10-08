"""Runs through a real engine: concurrency, attribution, interrupt, restart."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from alkera_notebook.document.ops import ReplaceCell
from alkera_notebook.engine import CellsTarget, NotebookClient, RunActor
from nbeng_harness import ANN, BOB, engine_for, notebook, run_cells, statuses, text_of, until


async def running(client: NotebookClient, cid: str) -> None:
    await until(lambda: _status_is(client, cid, "running"), timeout_s=15)


async def _status_is(client: NotebookClient, cid: str, status: str) -> bool:
    return (await statuses(client))[cid] == status


async def test_run_long_run_while_its_cell_is_edited(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, ["import time\ntime.sleep(0.6)\nr = 1\nr"])
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        await ann.apply([ReplaceCell(cell_id=a, source="r = 2\nr")], None)
        record = await handle.wait(20)
        assert record.status == "ok"
        assert await text_of(ann, a) == "1"  # the snapshot it was asked to run
        assert (await statuses(ann))[a] == "edited"


async def test_run_two_clients_run_fifo_and_attributed(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(
            engine, ["import time\ntime.sleep(0.5)\nx = 1", "y = 2"]
        )
        bob = session.attach(BOB)
        first = await ann.run(CellsTarget(ids=[a]))
        second = await bob.run(CellsTarget(ids=[b]))
        assert second.info.queued_behind == [first.run_id]
        r1, r2 = await first.wait(20), await second.wait(20)
        assert (r1.requested_by, r2.requested_by) == (ANN, BOB)
        assert r1.finished_at is not None and r2.started_at is not None
        assert r2.started_at >= r1.finished_at
        out = await bob.output(a)
        assert out.run is not None and out.run.by == RunActor.of(ANN)
        assert out.run.by.label() == "Ann"


async def test_run_identical_requests_coalesce(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(
            engine, ["import time\ntime.sleep(0.5)\nx = 1", "import random\ny = random.random()"]
        )
        bob = session.attach(BOB)
        busy = await ann.run(CellsTarget(ids=[a]))
        once = await ann.run(CellsTarget(ids=[b]))
        again = await bob.run(CellsTarget(ids=[b]))
        assert again.info.status == "coalesced" and again.info.joined == once.run_id
        assert (await again.wait(20)).run_id == once.run_id
        await busy.wait(20)
        runs_of_b = [
            r for r in session.runtime.records.values() if any(p.cell_id == b for p in r.plan)
        ]
        assert len(runs_of_b) == 1


INTERRUPTIBLE = [
    pytest.param("import time\ntime.sleep(30)", id="run.interrupt_sleep"),
    pytest.param(
        "import threading\nlock = threading.Lock()\nlock.acquire()\nlock.acquire()",
        id="run.interrupt_lock",
    ),
    pytest.param(
        "import socket\nleft, right = socket.socketpair()\nleft.recv(10)",
        id="run.interrupt_socket",
    ),
    pytest.param(
        'import subprocess\nsubprocess.run(["sleep", "30"])', id="run.interrupt_child_process"
    ),
]


@pytest.mark.parametrize("code", INTERRUPTIBLE)
async def test_run_interrupt_by_signal(tmp_path: Path, code: str) -> None:
    async with engine_for(tmp_path, escalation=(3.0, 7.0)) as engine:
        _, ann, (a, b) = await notebook(engine, [code, "after = 1"])
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        kernel_id = (await ann.kernel("status")).kernel_id
        started = time.monotonic()
        await ann.kernel("interrupt")
        record = await handle.wait(10)
        assert time.monotonic() - started < 2.5  # the first signal took; no escalation
        assert record.status == "interrupted"
        assert (await statuses(ann))[a] == "interrupted"
        info = await ann.kernel("status")
        assert info.kernel_id == kernel_id and info.state != "absent"
        assert (await run_cells(ann, b)).status == "ok"


async def test_run_interrupt_duckdb_query(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    code = (
        "import duckdb\ncon = duckdb.connect()\n"
        "con.execute('select count(*) from range(100000000000) a').fetchall()"
    )
    async with engine_for(tmp_path, escalation=(1.0, 4.0)) as engine:
        _, ann, (a, b) = await notebook(engine, [code, "z = 1"])
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        started = time.monotonic()
        await ann.kernel("interrupt")
        record = await handle.wait(15)
        assert time.monotonic() - started < 5.0
        # The signal stops the query (DuckDB checks for it), or the escalation
        # restarts the kernel: either way the run ends and the next one works.
        assert record.status in ("interrupted", "kernel_restarted")
        assert (await run_cells(ann, b)).status == "ok"


async def test_run_interrupt_escalates_to_restart(tmp_path: Path) -> None:
    code = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)"
    async with engine_for(tmp_path, escalation=(0.3, 1.0)) as engine:
        session, ann, (a, b) = await notebook(engine, [code, "x = 1\nx"])
        q = ann.queue
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        old = (await ann.kernel("status")).kernel_id
        await ann.kernel("interrupt")
        record = await handle.wait(10)
        assert record.status == "kernel_restarted" and record.reason == "interrupt_restart"
        exits = [e for e in _drain(q) if e.type == "kernel.exited"]
        assert exits and exits[-1].reason == "interrupt_restart"  # type: ignore[union-attr]
        await until(lambda: session.runtime.kernel is not None, timeout_s=10)
        assert (await ann.kernel("status")).kernel_id != old
        assert (await run_cells(ann, b)).status == "ok"
        assert await text_of(ann, b) == "1"


def _drain(q: object) -> list:  # type: ignore[type-arg]
    items = []
    while len(q):  # type: ignore[arg-type]
        items.append(q._items.popleft())  # type: ignore[attr-defined]
    return items


async def test_run_interrupt_when_idle_does_nothing(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, ["x = 41\nx + 1"])
        await run_cells(ann, a)
        before = await ann.kernel("status")
        after = await ann.kernel("interrupt")
        assert after.kernel_id == before.kernel_id and after.state == "idle"
        assert (await statuses(ann))[a] == "fresh"
        assert (await run_cells(ann, a)).status == "ok"


async def test_run_interrupt_and_clear_ends_queued_runs(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, ["import time\ntime.sleep(30)", "y = 1"])
        bob = session.attach(BOB)
        first = await ann.run(CellsTarget(ids=[a]))
        queued = await bob.run(CellsTarget(ids=[b]))
        await running(ann, a)
        await ann.kernel("interrupt_all")
        assert (await queued.wait(5)).status == "interrupted"
        assert (await first.wait(10)).status == "interrupted"
        assert (await statuses(ann))[b] == "not_run"


async def test_run_restart_during_a_run_and_run_after_restart(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b) = await notebook(engine, ["import time\ntime.sleep(30)", "y = 5\ny"])
        handle = await ann.run(CellsTarget(ids=[a]))
        await running(ann, a)
        old = (await ann.kernel("status")).kernel_id
        info = await ann.kernel("restart")
        record = await handle.wait(10)
        assert record.status == "kernel_restarted" and record.reason == "restart"
        assert info.kernel_id != old and info.state == "idle"
        assert set((await statuses(ann)).values()) == {"not_run"}
        assert (await run_cells(ann, b)).status == "ok"
        assert await text_of(ann, b) == "5"
