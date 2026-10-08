"""Fork safety: pools under every start method with printing workers, and
the RPC stream stays valid afterwards."""

from __future__ import annotations

import pytest
from nbkrn_harness import KernelFactory, step

WORKER = "import os\n\ndef work(i):\n    print(f'worker {i}', flush=True)\n    return i * i\n"


@pytest.mark.parametrize("method", ["fork", "spawn", "forkserver"])
async def test_nbkrn_pool_workers_print_into_the_cell(
    start_kernel: KernelFactory, tmp_path, method: str
) -> None:
    nb = tmp_path / "nbdir"
    nb.mkdir()
    (nb / "workers.py").write_text(WORKER)
    ks = await start_kernel(notebook_dir=nb)
    code = (
        "import multiprocessing as mp\nimport workers\n"
        f"with mp.get_context({method!r}).Pool(3) as pool:\n"
        "    squares = pool.map(workers.work, range(6))\n"
        "squares"
    )
    result = await ks.run(step("p", code))
    assert result.status == "ok", result.events
    assert result.outputs("p")[0]["text/plain"] == "[0, 1, 4, 9, 16, 25]"
    assert sorted(result.stdout("p").splitlines()) == [f"worker {i}" for i in range(6)]
    # The connection is intact after children inherited (and closed) it.
    after = await ks.run(step("q", "'still here'"))
    assert after.outputs("q")[0]["text/plain"] == "'still here'"


async def test_nbkrn_cell_defined_function_under_fork(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "import multiprocessing as mp\ndef double(x):\n    return 2 * x\n"
        "with mp.get_context('fork').Pool(2) as pool:\n    out = pool.map(double, [1, 2, 3])\nout"
    )
    result = await ks.run(step("p", code))
    assert result.outputs("p")[0]["text/plain"] == "[2, 4, 6]"


async def test_nbkrn_joblib_threads(start_kernel: KernelFactory, rich_python: str) -> None:
    ks = await start_kernel(interpreter=rich_python)
    code = (
        "from joblib import Parallel, delayed\n"
        "def f(i):\n    print('job', i)\n    return i + 1\n"
        "Parallel(n_jobs=3, prefer='threads')(delayed(f)(i) for i in range(5))"
    )
    result = await ks.run(step("j", code))
    assert result.outputs("j")[0]["text/plain"] == "[1, 2, 3, 4, 5]"
    assert sorted(result.stdout("j").splitlines()) == [f"job {i}" for i in range(5)]
