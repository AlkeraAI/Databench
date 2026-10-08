"""Inspection, completion and autoreload."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from alkera_notebook.rpc import frames
from nbkrn_harness import KernelFactory, PageTable, step


async def test_nbkrn_cell_variables_after_a_step(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(
        step(
            "a",
            "import pandas as pd\nn = 3\nname = 'x' * 500\ndf = pd.DataFrame({'a': [1, 2], 'b': ["
            "3, 4]})",
        )
    )
    (event,) = result.of("cell.variables", "a")
    by_name = {v["name"]: v for v in event["variables"]}
    assert by_name["n"]["repr"] == "3" and by_name["n"]["type"] == "int"
    assert len(by_name["name"]["repr"]) <= 200
    assert by_name["df"]["shape"] == [2, 2]
    assert by_name["df"]["columns"] == ["a", "b"]
    assert by_name["df"]["type"] == "pandas.DataFrame" or by_name["df"]["type"].endswith(
        "DataFrame"
    )


async def test_nbkrn_slow_repr_blocks_neither_reader_nor_interrupt(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    started = time.monotonic()
    result = await ks.run(
        step(
            "a",
            "import time\nclass Slow:\n    def __repr__(self):\n        time.sleep(10)\n        r"
            "eturn 'slow'\nslow = Slow()",
        )
    )
    assert time.monotonic() - started < 3
    (event,) = result.of("cell.variables", "a")
    (var,) = [v for v in event["variables"] if v["name"] == "slow"]
    assert "longer than" in var["repr"]
    # inspect.value of it answers within its own bound, and the reader stays responsive.
    t0 = time.monotonic()
    value = await ks.request("inspect.value", {"name": "slow", "depth": 1})
    assert "longer than" in value["summary"]["repr"]
    assert time.monotonic() - t0 < 3
    run_id = await ks.start_run([step("b", "import time\ntime.sleep(30)")])
    await ks.wait_for(
        lambda: any(m == "cell.started" and p["run_id"] == run_id for m, p in ks.events)
    )
    await asyncio.sleep(0.3)
    await ks.interrupt(run_id)
    assert (await ks.finish(run_id, limit_s=5)).status == "interrupted"


async def test_nbkrn_inspect_value_unknown_name(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    with pytest.raises(frames.RpcError) as info:
        await ks.request("inspect.value", {"name": "nope", "depth": 1})
    assert info.value.name == "inspect.unknown_name"
    with pytest.raises(frames.RpcError) as bad:
        await ks.request("inspect.value", {"name": "__import__('os')", "depth": 1})
    assert bad.value.code == frames.ErrorCode.INVALID_PARAMS


async def test_nbkrn_inspect_frame_pages_and_filters(start_kernel: KernelFactory) -> None:
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    await ks.run(
        step(
            "a",
            "import pandas as pd\ndf = pd.DataFrame({'id': range(100), 'v': [i * 2 for i in range"
            "(100)]})",
        )
    )
    page = await ks.request("inspect.frame", {"name": "df", "offset": 10, "limit": 3})
    table = PageTable(page["table"])
    assert page["total_rows"] == 100
    assert table.names == ["id", "v"]
    assert [list(r) for r in table.rows] == [[10, 20], [11, 22], [12, 24]]
    filtered = await ks.request(
        "inspect.frame",
        {
            "name": "df",
            "offset": 0,
            "limit": 5,
            "filter_sql": "select id from frame where v > 190",
            "sort": {"column": "id", "descending": True},
        },
    )
    assert filtered["total_rows"] == 4
    assert [r[0] for r in PageTable(filtered["table"]).rows] == [99, 98, 97, 96]


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("COPY frame TO '/tmp/out.csv'", id="copy-to"),
        pytest.param("select * from read_csv('/etc/hosts')", id="read-csv"),
        pytest.param("INSTALL httpfs", id="install"),
        pytest.param("ATTACH '/tmp/x.db'", id="attach"),
        pytest.param("select 1; select 2", id="two-statements"),
        pytest.param("select * from frame -- comment", id="comment"),
        pytest.param("update frame set v = 0", id="update"),
        pytest.param("select * from 'file.parquet'", id="file-literal"),
        pytest.param("select getenv('HOME')", id="getenv"),
    ],
)
async def test_nbkrn_inspect_frame_refuses_anything_but_one_select(
    start_kernel: KernelFactory, sql: str
) -> None:
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    await ks.run(step("a", "import pandas as pd\ndf = pd.DataFrame({'v': [1]})"))
    with pytest.raises(frames.RpcError) as info:
        await ks.request(
            "inspect.frame", {"name": "df", "offset": 0, "limit": 5, "filter_sql": sql}
        )
    assert info.value.name in {"inspect.statement_refused", "inspect.query_failed"}


async def test_nbkrn_completion_over_the_live_namespace(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    await ks.run(
        step("a", "def compute_total(a, b=2):\n    'Add things.'\n    return a + b\ncompost = 1")
    )
    result = await ks.request("complete", {"code": "x = comp", "cursor": 8})
    assert result["matches"][:2] == ["compost", "compute_total"] or set(result["matches"]) >= {
        "compost",
        "compute_total",
    }
    doc = await ks.request("complete", {"code": "compute_t", "cursor": 9})
    assert doc["matches"] == ["compute_total"]
    assert doc["doc"].startswith("compute_total(a, b=2)")
    assert "Add things." in doc["doc"]
    await ks.run(step("b", "import os"))
    attr = await ks.request("complete", {"code": "os.pa", "cursor": 5})
    assert "os.path" in attr["matches"]


async def test_nbkrn_autoreload_reloads_changed_workspace_modules(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    nb = tmp_path / "ws"
    nb.mkdir()
    (nb / "base.py").write_text("VALUE = 1\n")
    (nb / "uses.py").write_text(
        "import base\ndef get():\n    return base.VALUE\nfrom base import VALUE as COPIED\n"
    )
    ks = await start_kernel(notebook_dir=nb, settings={"autoreload": "on"})
    first = await ks.run(step("a", "import uses\n(uses.get(), uses.COPIED)"))
    assert first.outputs("a")[0]["text/plain"] == "(1, 1)"
    (nb / "base.py").write_text("VALUE = 2\n")
    stat = (nb / "base.py").stat()
    os.utime(nb / "base.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = await ks.run(step("b", "(uses.get(), uses.COPIED)"))
    assert second.outputs("b")[0]["text/plain"] == "(2, 2)"
    (started,) = second.of("run.started")
    assert started["reloaded"] == ["base", "uses"]


async def test_nbkrn_autoreload_is_off_by_default(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    nb = tmp_path / "ws2"
    nb.mkdir()
    (nb / "base.py").write_text("VALUE = 1\n")
    ks = await start_kernel(notebook_dir=nb)
    await ks.run(step("a", "import base"))
    (nb / "base.py").write_text("VALUE = 2\n")
    stat = (nb / "base.py").stat()
    os.utime(nb / "base.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = await ks.run(step("b", "base.VALUE"))
    assert second.outputs("b")[0]["text/plain"] == "1"


async def test_nbkrn_summaries_of_huge_bytes_stay_cheap(start_kernel: KernelFactory) -> None:
    """A 200 MiB bytearray is summarized from a slice: no full repr (which
    would allocate about four times its size)."""
    ks = await start_kernel()
    await ks.run(step("a", "import gc\nx = 1"))
    base = ks.kernel.rss_bytes()
    result = await ks.run(step("b", "blob = bytearray(b'x') * (200 * 1024 * 1024)\nblob"))
    (variables,) = result.of("cell.variables", "b")
    (summary,) = variables["variables"]
    assert summary["repr"] == "bytearray of 209,715,200 bytes"
    assert summary["size_bytes"] >= 200 * 1024 * 1024
    plain = result.outputs("b")[0]["text/plain"]
    assert plain.endswith("(209,715,200 bytes)") and len(plain) < 110_000
    value = await ks.request("inspect.value", {"name": "blob", "depth": 3})
    assert value["summary"]["repr"] == "bytearray of 209,715,200 bytes"
    grown = ks.kernel.rss_bytes() - base
    assert grown < 300 * 1024 * 1024, grown / 2**20  # a full repr would add ~800 MiB


def test_nbkrn_structural_summaries() -> None:
    import array

    from _alkera_kernel.inspection import bounded_repr

    class Big:
        def __sizeof__(self) -> int:
            return 2 * 1024 * 1024

        def __repr__(self) -> str:
            raise AssertionError("a large object is never repr'd")

    class Loud:
        def __repr__(self) -> str:
            return "L" * (2 * 1024 * 1024)

    cases = {
        "bytes": (b"x" * 10, "bytes of 10 bytes"),
        "memoryview": (memoryview(b"abcd"), "memoryview of 4 bytes"),
        "array": (array.array("d", [1.0, 2.0]), "array('d') of 2 items"),
        "big-object": (Big(), "Big of 2,097,152 bytes"),
        "list-of-buffers": (
            [b"x" * 5, bytearray(3)],
            "[<bytes of 5 bytes>, <bytearray of 3 bytes>]",
        ),
        "long-list": (list(range(100)), "[0, 1, 2, 3, 4, 5, ...]"),
        "loud-repr": (Loud(), "<Loud: repr over 1 MiB>"),
        "plain": ({"a": 1}, "{'a': 1}"),
    }
    for name, (value, expected) in cases.items():
        assert bounded_repr(value, 200, 1.0) == expected, name


async def test_nbkrn_numpy_and_frame_summaries_never_touch_the_data(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = (
        "import numpy as np, pandas as pd\n"
        "arr = np.zeros((3, 4))\n"
        "df = pd.DataFrame({'a': range(5), 'b': range(5)})\n"
        "s = df['a']"
    )
    result = await ks.run(step("n", code))
    (event,) = result.of("cell.variables", "n")
    reprs = {v["name"]: v["repr"] for v in event["variables"]}
    assert reprs["arr"] == "ndarray shape (3, 4) dtype float64"
    assert reprs["df"] == "DataFrame 5 rows, 2 columns"
    assert reprs["s"] == "Series 5 rows"
