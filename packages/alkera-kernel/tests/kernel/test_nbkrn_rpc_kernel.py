"""The kernel as an RPC client: hello contents, run-scoped calls from any
thread, cancellation of a call by an interrupt."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from alkera_notebook.rpc import Call
from nbkrn_harness import KernelFactory, step


async def test_nbkrn_hello_describes_the_runtime(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    hello = ks.session.hello
    assert hello["client"]["name"] == "alkera-kernel"
    assert hello["client"]["protocols"] == [1]
    assert set(hello["runtime"]) == {"version", "implementation", "platform", "free_threaded"}
    assert {"json", "rows.json"} <= set(hello["codecs"])
    assert ("arrow.ipc.stream" in hello["codecs"]) == bool(
        hello["libs"]["pyarrow"] or hello["libs"]["polars"]
    )
    assert set(hello["libs"]) == {
        "pyarrow",
        "polars",
        "pandas",
        "numpy",
        "duckdb",
        "matplotlib",
        "ipywidgets",
        "anywidget",
        "plotly",
        "altair",
        "IPython",
    }


async def test_nbkrn_forged_ctx_from_a_thread_is_a_time_window(start_kernel: KernelFactory) -> None:
    """A background thread stamps the active run: accepted and attributed to
    that run while it executes; the same request after run.finished is
    refused (attribution, not containment)."""
    seen: list[dict[str, Any]] = []

    async def probe(call: Call) -> Any:
        seen.append(call.extra)
        return {"ok": True}

    ks = await start_kernel(methods={"ws.probe": probe}, run_scoped=["ws.probe"])
    code = (
        "import sys, threading\n"
        "host = sys.modules['_alkera_runtime'].host\n"
        "ctx = host.run_context()\n"
        "box = {}\n"
        "def worker():\n"
        "    box['r'] = host._kernel.conn.request('ws.probe', {}, ctx=ctx)\n"
        "t = threading.Thread(target=worker)\nt.start()\nt.join()\n"
        "box['r']"
    )
    first = await ks.run(step("a", code))
    assert first.outputs("a")[0]["text/plain"] == "{'ok': True}"
    assert seen == [{"run_id": first.run_id, "cell_id": "a"}]
    replay = await ks.run(
        step(
            "b",
            "try:\n    host._kernel.conn.request('ws.probe', {}, ctx=ctx)\n    out = 'accepted'\n"
            "except Exception as exc:\n    out = exc.data['reason']\nout",
        )
    )
    assert replay.outputs("b")[0]["text/plain"] == "'outside_run'"
    assert len(seen) == 1


async def test_nbkrn_host_call_stamps_the_active_run(start_kernel: KernelFactory) -> None:
    seen: list[dict[str, Any]] = []

    async def late_method(call: Call) -> Any:
        seen.append({"extra": call.extra, "params": call.params})
        return call.params["n"] * 2

    ks = await start_kernel(methods={"brand.new": late_method}, run_scoped=["brand.new"])
    result = await ks.run(
        step("a", "import sys\nsys.modules['_alkera_runtime'].host.call('brand.new', {'n': 21})")
    )
    assert result.outputs("a")[0]["text/plain"] == "42"
    assert seen == [{"extra": {"run_id": result.run_id, "cell_id": "a"}, "params": {"n": 21}}]


async def test_nbkrn_interrupt_cancels_a_pending_service_call(start_kernel: KernelFactory) -> None:
    cancelled = asyncio.Event()

    async def slow(call: Call) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ks = await start_kernel(methods={"slow.op": slow}, run_scoped=["slow.op"])
    run_id = await ks.start_run(
        [step("a", "import sys\nsys.modules['_alkera_runtime'].host.call('slow.op', {})")]
    )
    await ks.wait_for(lambda: any(m == "cell.started" for m, _ in ks.events))
    await asyncio.sleep(0.3)
    await ks.interrupt(run_id)
    result = await ks.finish(run_id, limit_s=5)
    assert result.status == "interrupted"
    await asyncio.wait_for(cancelled.wait(), 5)


async def test_nbkrn_service_error_reaches_the_cell(start_kernel: KernelFactory) -> None:
    from alkera_notebook.rpc import frames

    async def refuse(call: Call) -> Any:
        raise frames.RpcError(-32010, "no such connection", {"name": "sql.unknown_connection"})

    ks = await start_kernel(methods={"sql.execute": refuse}, run_scoped=["sql.execute"])
    result = await ks.run(
        step(
            "a",
            "import sys\ntry:\n    sys.modules['_alkera_runtime'].host.call('sql.execute', {'sql'"
            ": 'select 1'})\nexcept Exception as e:\n    out = (type(e).__name__, e.name)\nout",
        )
    )
    assert result.outputs("a")[0]["text/plain"] == "('RpcError', 'sql.unknown_connection')"


@pytest.mark.parametrize("n", [pytest.param(40, id="above-max-inflight")])
async def test_nbkrn_many_threads_calling_queue_locally(
    start_kernel: KernelFactory, n: int
) -> None:
    open_now = 0
    peak = 0

    async def held(call: Call) -> Any:
        nonlocal open_now, peak
        open_now += 1
        peak = max(peak, open_now)
        await asyncio.sleep(0.2)
        open_now -= 1
        return call.params["i"]

    ks = await start_kernel(methods={"held": held}, run_scoped=["held"])
    code = (
        "import sys\nfrom concurrent.futures import ThreadPoolExecutor\nhost = sys.modules['_alke"
        "ra_runtime'].host\n"
        "ctx = host.run_context()\n"
        f"with ThreadPoolExecutor({n}) as ex:\n"
        "    call = lambda i: host._kernel.conn.request('held', {'i': i}, ctx=ctx)\n"
        f"    out = sorted(ex.map(call, range({n})))\n"
        "out == list(range(%d))" % n
    )
    result = await ks.run(step("a", code))
    assert result.outputs("a")[0]["text/plain"] == "True", result.events
    assert peak == 32
