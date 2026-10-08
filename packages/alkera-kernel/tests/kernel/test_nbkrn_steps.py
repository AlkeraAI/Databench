"""Running steps: clear-all-first, top-level await, tracebacks with cell
lines, SystemExit and os._exit, input and breakpoint."""

from __future__ import annotations

import asyncio

import pytest
from nbkrn_harness import KernelFactory, step


async def test_nbkrn_clear_runs_first_and_a_failure_stops_the_run(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    first = await ks.run(*(step(f"s{i}", f"v{i} = {i}") for i in range(1, 6)))
    assert first.status == "ok"
    # Re-run all five: every previous def is cleared first, step 2 fails.
    second = await ks.run(
        step("s1", "v1 = 10"),
        step("s2", "raise RuntimeError('boom')"),
        step("s3", "v3 = 30"),
        step("s4", "v4 = 40"),
        step("s5", "v5 = 50"),
        clear=["v1", "v2", "v3", "v4", "v5"],
    )
    assert second.status == "error"
    assert [p["cell_id"] for p in second.of("cell.finished")] == ["s1", "s2"]
    probe = await ks.run(
        step("probe", "sorted(n for n in ('v1','v2','v3','v4','v5') if n in globals())")
    )
    assert probe.outputs("probe")[0]["text/plain"] == "['v1']"


async def test_nbkrn_top_level_await_runs_on_a_persistent_loop(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(
        step(
            "a", "import asyncio\nloop1 = asyncio.get_running_loop()\nawait asyncio.sleep(0)\nx = 5"
        ),
        step(
            "b",
            "import asyncio\nasync def f():\n    return asyncio.get_running_loop()\n(await f()) i"
            "s loop1",
        ),
    )
    assert result.status == "ok", result.events
    assert result.outputs("b")[0]["text/plain"] == "True"


async def test_nbkrn_last_expression_can_await(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "import asyncio\nasync def g():\n    return 7\nawait g()"))
    assert result.outputs("a")[0]["text/plain"] == "7"


async def test_nbkrn_traceback_shows_the_cell_lines(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = "def inner():\n    return 1 / 0  # the faulty line\n\ninner()\n"
    result = await ks.run(step("t", code, line_offset=10))
    error = result.finished("t")["error"]
    assert error["ename"] == "ZeroDivisionError"
    text = "".join(error["traceback"])
    assert "return 1 / 0  # the faulty line" in text
    assert "line 12" in text  # cell line 2 plus the offset
    assert "_alkera_kernel" not in text and "runtime.py" not in text


@pytest.mark.parametrize(
    ("code", "line", "quoted"),
    [
        pytest.param("x = 1\ny = 2\nx / 0\n", 3, "x / 0", id="last-expression"),
        pytest.param(
            "import time\nx = 1\n\ntime.sleep(-1)", 4, "time.sleep(-1)", id="last-expression-call"
        ),
        pytest.param("x = 1\nraise ValueError('v')\nx\n", 2, "raise ValueError('v')", id="body"),
        pytest.param(
            "_hidden = 0\nvalue = 1\n1 / _hidden", 3, "1 / _hidden", id="private-name-as-written"
        ),
        pytest.param(
            "_zero = 0\nratio = 1 / _zero\nratio", 2, "ratio = 1 / _zero", id="body-private-name"
        ),
        pytest.param(
            "total = (\n    1 +\n    1 / 0\n)\ntotal", 3, "1 / 0", id="multi-line-statement"
        ),
        pytest.param("y = 3\n(y,\n 1 / 0)", 3, "1 / 0)", id="multi-line-last-expression"),
    ],
)
async def test_nbkrn_traceback_names_the_cell_line_as_written(
    start_kernel: KernelFactory, code: str, line: int, quoted: str
) -> None:
    """The innermost frame names the cell's own line, for an error in the
    body and in the last expression alike, and quotes the source as the
    person wrote it (no wrapping parentheses, no renamed private names)."""
    ks = await start_kernel()
    result = await ks.run(step("t", code))
    error = result.finished("t")["error"]
    frames = [ln for ln in error["traceback"] if "demo.alknb.py#t" in ln]
    assert frames, error["traceback"]
    innermost = frames[-1]
    assert f", line {line}," in innermost, innermost
    shown = innermost.splitlines()[1].strip()
    assert shown == quoted


async def test_nbkrn_system_exit_does_not_exit_the_kernel(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "import sys\nsys.exit(3)"))
    assert result.finished("a")["status"] == "error"
    assert result.finished("a")["error"]["ename"] == "SystemExit"
    assert (await ks.run(step("b", "'alive'"))).outputs("b")[0]["text/plain"] == "'alive'"


async def test_nbkrn_os_exit_ends_the_kernel(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    await ks.start_run([step("a", "import os\nos._exit(7)")])
    code = await asyncio.wait_for(ks.kernel.wait(), 10)
    assert code == 7


async def test_nbkrn_input_raises_the_notebook_eoferror(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "input('name? ')"), step("b", "x = 1"))
    error = result.finished("a")["error"]
    assert error["ename"] == "EOFError"
    assert error["evalue"] == "Notebooks do not support input(); use alkera.ui.text"
    gp = await ks.run(step("c", "import getpass\ngetpass.getpass()"))
    assert gp.finished("c")["error"]["evalue"].startswith("Notebooks do not support input()")
    stdin = await ks.run(step("d", "import sys\nsys.stdin.read()"))
    assert stdin.outputs("d")[0]["text/plain"] == "''"


async def test_nbkrn_breakpoint_prints_a_notice_and_continues(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "breakpoint()\nprint('after')"))
    assert result.status == "ok"
    assert "breakpoint() is not supported" in result.stdout("a", "stderr")
    assert result.stdout("a") == "after\n"


async def test_nbkrn_defs_report_only_names_that_exist(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "a = 1\ndel a\nb = 2", defs=["a", "b"]))
    assert result.finished("a")["defs"] == ["b"]
    (variables,) = result.of("cell.variables", "a")
    assert [v["name"] for v in variables["variables"]] == ["b"]
    assert variables["variables"][0]["type"] == "int"


async def test_nbkrn_names_delete_between_runs(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    await ks.run(step("a", "x = 1\ny = 2"))
    assert await ks.request("names.delete", {"names": ["x"]}) == {}
    probe = await ks.run(step("p", "('x' in globals(), 'y' in globals())"))
    assert probe.outputs("p")[0]["text/plain"] == "(False, True)"


async def test_nbkrn_missing_module_is_reported(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "import surely_not_installed_pkg.sub"))
    assert result.of("module.missing", "a") == [
        {"run_id": result.run_id, "cell_id": "a", "module": "surely_not_installed_pkg"}
    ]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        pytest.param({"run_id": 1, "steps": []}, "run_id", id="run-id-type"),
        pytest.param({"run_id": "r", "steps": [{"body": "x"}]}, "cell_id", id="no-cell-id"),
        pytest.param({"run_id": "r", "steps": [], "clear": [1]}, "clear", id="clear-type"),
        pytest.param(
            {"run_id": "r", "steps": [{"cell_id": "a", "body": 3}]}, "text", id="body-type"
        ),
    ],
)
async def test_nbkrn_run_execute_validates_params(
    start_kernel: KernelFactory, params: dict, message: str
) -> None:
    from alkera_notebook.rpc import frames

    ks = await start_kernel()
    with pytest.raises(frames.RpcError) as info:
        await ks.request("run.execute", params)
    assert info.value.code == frames.ErrorCode.INVALID_PARAMS
    assert message in info.value.message


async def test_nbkrn_runs_queue_fifo(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    r1 = await ks.start_run([step("a", "import time\ntime.sleep(0.3)\norder = ['a']")])
    r2 = await ks.start_run([step("b", "order.append('b')\norder")])
    await ks.finish(r1)
    second = await ks.finish(r2)
    assert second.outputs("b")[0]["text/plain"] == "['a', 'b']"


async def test_nbkrn_unknown_method_is_not_found(start_kernel: KernelFactory) -> None:
    from alkera_notebook.rpc import frames

    ks = await start_kernel()
    with pytest.raises(frames.RpcError) as info:
        await ks.request("kernel.selfdestruct")
    assert info.value.code == frames.ErrorCode.METHOD_NOT_FOUND


async def test_nbkrn_shutdown_exits(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    assert await ks.request("kernel.shutdown") == {}
    assert await asyncio.wait_for(ks.kernel.wait(), 5) == 0


async def test_nbkrn_dropped_connection_ends_the_kernel(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    await ks.session.peer.close("engine_gone")
    assert await asyncio.wait_for(ks.kernel.wait(), 5) == 0
