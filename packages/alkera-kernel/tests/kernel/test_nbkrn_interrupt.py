"""Interrupts behave like Jupyter's: a real SIGINT to the kernel's process
group ends whatever the step is blocked in."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest
from nbkrn_harness import KernelFactory, KernelSession, step

#: Printed by each blocking step on the line before the call it blocks in.
REACHED = "reached-the-blocking-call"

#: Every step blocks for an hour, so one the interrupt did not end is still
#: blocked when the run is awaited: the outcome never depends on timing.
BLOCKING = [
    pytest.param("while True:\n    pass\n", id="pure-python-loop"),
    pytest.param("import time\n{reached}time.sleep(3600)\n", id="sleep"),
    pytest.param(
        "import threading\nlock = threading.Lock()\nlock.acquire()\n{reached}"
        "lock.acquire(timeout=3600)\n",
        id="lock-wait",
    ),
    pytest.param(
        "import socket\na, b = socket.socketpair()\n{reached}a.recv(10)\n",
        id="socket-recv",
    ),
    pytest.param(
        "import subprocess\n{reached}subprocess.run(['sleep', '3600'])\n", id="child-process"
    ),
    pytest.param(
        "import multiprocessing as mp\nimport time\nwith mp.get_context('fork').Pool(2) as pool:\n"
        "    {reached}    pool.map(time.sleep, [3600, 3600])\n",
        id="multiprocessing-pool",
    ),
    pytest.param(
        "import asyncio\n{reached}await asyncio.sleep(3600)\n",
        id="top-level-await",
    ),
]


async def _start_blocked(ks: KernelSession, code: str) -> str:
    run_id = await ks.start_run([step("blocked", code + "print('marker-never')\n")])
    await ks.wait_for(lambda: any(m == "cell.started" for m, _ in ks.events))
    await asyncio.sleep(0.4)  # let the step reach its blocking call
    return run_id


@pytest.mark.parametrize("code", BLOCKING)
async def test_nbkrn_interrupt_ends_a_blocked_step(start_kernel: KernelFactory, code: str) -> None:
    """The interrupt is sent once the step says it is about to block (the
    pure loop says nothing: it blocks from its first line), and the step
    then ends ``interrupted``. A step the interrupt did not end blocks for
    an hour, so the run is awaited with no bound of its own."""
    ks = await start_kernel()
    run_id = await ks.start_run(
        [
            step(
                "blocked",
                code.format(reached=f"print({REACHED!r}, flush=True)\n")
                + "print('marker-never')\n",
            )
        ]
    )
    if "{reached}" in code:
        await ks.wait_for(
            lambda: any(REACHED in p.get("text", "") for m, p in ks.events if m == "cell.stream")
        )
    else:
        await ks.wait_for(lambda: any(m == "cell.started" for m, _ in ks.events))
    await ks.interrupt(run_id)
    result = await ks.finish(run_id)
    assert result.status == "interrupted", result.events
    assert result.finished("blocked")["status"] == "interrupted"
    assert "marker-never" not in result.stdout("blocked")
    # The kernel survives and runs the next cell.
    after = await ks.run(step("next", "1 + 1"))
    assert after.status == "ok"
    assert after.outputs("next")[0]["text/plain"] == "2"


async def test_nbkrn_interrupt_reaches_child_processes_in_the_group(
    start_kernel: KernelFactory, tmp_path
) -> None:
    pidfile = tmp_path / "child.pid"
    ks = await start_kernel()
    code = (
        "import subprocess\n"
        "p = subprocess.Popen(['sleep', '30'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\np.wait()\n"
    )
    run_id = await _start_blocked(ks, code)
    await ks.interrupt(run_id)
    await ks.finish(run_id, limit_s=5)
    import os

    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the child survived the interrupt")


async def test_nbkrn_interrupt_for_a_finished_run_does_not_touch_the_next(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    first = await ks.run(step("a", "x = 1"))
    assert first.status == "ok"
    second = await ks.start_run([step("b", "import time\ntime.sleep(1.0)\nprint('done')\n")])
    await ks.wait_for(
        lambda: any(m == "cell.started" and p["run_id"] == second for m, p in ks.events)
    )
    # The engine meant the first run; its hint arrives before the signal.
    await ks.interrupt(first.run_id)
    result = await ks.finish(second, limit_s=5)
    assert result.status == "ok", result.events
    assert result.stdout("b") == "done\n"


async def test_nbkrn_interrupt_without_a_hint_interrupts_what_runs(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    run_id = await _start_blocked(ks, "import time\ntime.sleep(30)\n")
    await ks.interrupt(run_id, hint=False)
    result = await ks.finish(run_id, limit_s=5)
    assert result.status == "interrupted"


async def test_nbkrn_interrupt_when_idle_is_harmless(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    ks.kernel.signal(signal.SIGINT)
    await asyncio.sleep(0.3)
    result = await ks.run(step("a", "21 * 2"))
    assert result.status == "ok"
    assert ks.kernel.returncode is None


async def test_nbkrn_swallowed_interrupt_still_ends_interrupted(
    start_kernel: KernelFactory,
) -> None:
    """A step that catches the KeyboardInterrupt and raises something else
    is still ``interrupted``: an interrupt for its run is pending."""
    ks = await start_kernel()
    code = (
        "import time\ntry:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n    raise ValueError('"
        "cleanup failed')\n"
    )
    run_id = await _start_blocked(ks, code)
    await ks.interrupt(run_id)
    result = await ks.finish(run_id, limit_s=5)
    assert result.finished("blocked")["status"] == "interrupted"


async def test_nbkrn_gil_holding_call_escalates_to_restart(start_kernel: KernelFactory) -> None:
    """A C call that never returns to the interpreter ignores both signals;
    the engine's schedule (signal, signal again, then kill) still ends it."""
    ks = await start_kernel()
    # sum over a range runs in C holding the GIL and never checks signals.
    code = "n = 10**12\nsum(range(n))\n"
    run_id = await _start_blocked(ks, code)
    await ks.interrupt(run_id)
    await asyncio.sleep(0.5)
    assert not any(m == "run.finished" for m, _ in ks.events)
    ks.kernel.signal(signal.SIGINT)
    await asyncio.sleep(0.5)
    assert not any(m == "run.finished" for m, _ in ks.events)
    ks.kernel.kill()
    code_ = await asyncio.wait_for(ks.kernel.wait(), 5)
    assert code_ == -signal.SIGKILL


#: A cross join of 10^13 rows: a query the interrupt did not stop runs for
#: hours, so the run is awaited with no bound of its own.
DUCKDB_BLOCKING = "select count(*) from range(10000000000) a, range(1000) b"


@pytest.mark.parametrize(
    ("setup", "run"),
    [
        pytest.param(
            "con = duckdb.connect()\n"
            "sys.modules['_alkera_runtime'].host.register_interruptible(con)\n",
            "con.execute({q}).fetchall()",
            id="registered-by-the-cell",
        ),
        pytest.param(
            "con = duckdb.connect()\n", "con.execute({q}).fetchall()", id="own-connection"
        ),
        pytest.param("", "duckdb.connect().execute({q}).fetchall()", id="temporary-connection"),
        pytest.param("", "duckdb.sql({q}).fetchall()", id="default-connection-sql"),
        pytest.param("", "duckdb.execute({q}).fetchall()", id="default-connection-execute"),
    ],
)
async def test_nbkrn_interrupt_stops_a_duckdb_query(
    start_kernel: KernelFactory, setup: str, run: str
) -> None:
    """DuckDB runs a query in C with the GIL released and never looks at
    Python signals; the watcher calls ``interrupt()`` on every connection the
    person's code can reach, the default one included. The step ends
    ``interrupted`` and the next query on the same route answers: DuckDB's
    own signal check alone leaves the stopped query's workers running, and
    the connection's next query would wait for them forever."""
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    code = (
        "import duckdb, sys\n"
        + setup
        + f"print({REACHED!r}, flush=True)\n"
        + run.format(q=repr(DUCKDB_BLOCKING))
        + "\nprint('marker-never')\n"
    )
    run_id = await ks.start_run([step("blocked", code)])
    await ks.wait_for(
        lambda: any(REACHED in p.get("text", "") for m, p in ks.events if m == "cell.stream")
    )
    await ks.interrupt(run_id)
    result = await ks.finish(run_id)
    assert result.status == "interrupted", result.events
    assert result.finished("blocked")["status"] == "interrupted"
    assert "marker-never" not in result.stdout("blocked")
    after = await ks.run(step("next", run.format(q=repr("select 42")) + "[0][0]"))
    assert after.status == "ok", after.events
    assert after.outputs("next")[0]["text/plain"] == "42"


@pytest.mark.parametrize(
    ("code", "frames"),
    [
        pytest.param("import time\ntime.sleep(30)\n", True, id="interrupted-in-the-cell"),
        pytest.param(
            "import time\ntry:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n"
            "    raise ValueError('cleanup failed')\n",
            False,
            id="interrupt-turned-into-another-error",
        ),
    ],
)
async def test_nbkrn_an_interrupt_is_reported_once(
    start_kernel: KernelFactory, code: str, frames: bool
) -> None:
    """The error names the interrupt, and its traceback reads in Python's
    order: the ``Traceback`` header, only the person's frames where it
    landed, then the ``KeyboardInterrupt`` line, once (no traceback at all
    when what ended the step was another exception the interrupt caused)."""
    ks = await start_kernel()
    run_id = await _start_blocked(ks, code)
    await ks.interrupt(run_id)
    result = await ks.finish(run_id, limit_s=5)
    error = result.finished("blocked")["error"]
    assert (error["ename"], error["evalue"]) == ("KeyboardInterrupt", "")
    lines = error["traceback"]
    text = "".join(lines)
    assert "cleanup failed" not in text
    assert "_alkera_kernel" not in text
    if frames:
        assert lines[0] == "Traceback (most recent call last):\n"
        assert lines[-1] == "KeyboardInterrupt\n"
        assert text.count("KeyboardInterrupt") == 1
        assert (
            text.index("Traceback") < text.index("time.sleep(30)") < text.index("KeyboardInterrupt")
        )
        assert "demo.alknb.py#blocked" in text
    else:
        assert lines == []


async def test_nbkrn_running_cells_writes_nothing_beside_the_notebook(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    """Saved outputs are the engine's to write: the kernel, running in the
    notebook's folder, leaves no ``__marimo__`` session there."""
    folder = tmp_path / "folder"
    ks = await start_kernel(notebook_dir=folder)
    result = await ks.run(
        step("a", "print('out')\ndisplay({'k': 1})\n1 + 1"), step("b", "raise ValueError('x')")
    )
    assert result.status == "error"
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == []
