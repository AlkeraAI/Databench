from __future__ import annotations

from nbkrn_harness import KernelFactory, step


async def test_nbkrn_runs_a_cell_and_prints(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "x = 40\nprint('hi', x)\nx + 2"))
    assert result.status == "ok", result.events
    assert result.stdout("a") == "hi 40\n"
    assert result.outputs("a")[0]["text/plain"] == "42"
    assert result.finished("a")["defs"] == ["x"]
