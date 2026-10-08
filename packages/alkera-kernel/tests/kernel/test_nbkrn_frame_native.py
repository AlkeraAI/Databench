"""``inspect.frame`` in an environment with no DuckDB: a pandas or Polars
frame pages and sorts through its own library, so a table's grid shows rows
in an environment that has only the frame library. Only ``filter_sql`` needs
DuckDB, and is refused with a code that says so.

Real kernels in real environments holding pandas alone and Polars alone."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.rpc import frames
from nbkrn_harness import KernelFactory, KernelSession, PageTable, cached_env, step

FRAMES = {
    "pandas": (
        "import pandas as pd\n"
        "big = pd.DataFrame({'id': range(100), 'v': [i * 2 for i in range(100)]})\n"
        "df = pd.DataFrame({'id': [1, 2, 3, 4, 5, 6], 'g': [1, 2, 1, 2, 1, 2],"
        " 'v': [5, None, 3, 1, None, 7], 's': ['f', 'e', 'd', 'c', 'b', 'a']})\n"
        "ser = pd.Series([3, 1, 2], name='n')\n"
        "frame = pd.DataFrame({1: [2, 1], 'x': [0, 0]})\n"
    ),
    "polars": (
        "import polars as pl\n"
        "big = pl.DataFrame({'id': range(100), 'v': [i * 2 for i in range(100)]})\n"
        "df = pl.DataFrame({'id': [1, 2, 3, 4, 5, 6], 'g': [1, 2, 1, 2, 1, 2],"
        " 'v': [5, None, 3, 1, None, 7], 's': ['f', 'e', 'd', 'c', 'b', 'a']})\n"
        "ser = pl.Series('n', [3, 1, 2])\n"
        "frame = pl.DataFrame({'1': [2, 1], 'x': [0, 0]})\n"
    ),
}
INT_TYPE = {"pandas": "int64", "polars": "Int64"}


def _python(request: pytest.FixtureRequest, library: str) -> str:
    py = cached_env(request.config, f"{library}-only-3.13")
    if py is None:
        pytest.skip(f"could not build an environment holding only {library}")
    return py


@pytest.fixture(scope="session", params=["pandas", "polars"])
def library_python(request: pytest.FixtureRequest) -> tuple[str, str]:
    library = str(request.param)
    return library, _python(request, library)


async def _kernel(start_kernel: KernelFactory, library_python: tuple[str, str]) -> KernelSession:
    library, python = library_python
    ks = await start_kernel(interpreter=python)
    assert (await ks.run(step("a", FRAMES[library]))).status == "ok"
    # The environment really has no DuckDB.
    probe = await ks.run(
        step("b", "import importlib.util\nassert importlib.util.find_spec('duckdb') is None")
    )
    assert probe.status == "ok"
    return ks


async def _page(ks: KernelSession, name: str, **params: Any) -> tuple[Any, int]:
    page = await ks.request("inspect.frame", {"name": name, **params})
    return PageTable(page["table"]), int(page["total_rows"])


async def test_a_page_without_duckdb(
    start_kernel: KernelFactory, library_python: tuple[str, str]
) -> None:
    library, _ = library_python
    ks = await _kernel(start_kernel, library_python)
    table, total = await _page(ks, "big", offset=10, limit=3)
    assert total == 100
    assert table.names == ["id", "v"]
    assert [list(r) for r in table.rows] == [[10, 20], [11, 22], [12, 24]]
    assert [t for _, t in table.columns] == [INT_TYPE[library]] * 2
    # Past the end: no rows, the total still told.
    table, total = await _page(ks, "big", offset=99, limit=50)
    assert ([list(r) for r in table.rows], total) == ([[99, 198]], 100)
    table, total = await _page(ks, "big", offset=500, limit=50)
    assert (list(table.rows), total) == ([], 100)


@pytest.mark.parametrize(
    ("sort", "ids"),
    [
        pytest.param([{"column": "id", "descending": True}], [6, 5, 4, 3, 2, 1], id="desc"),
        pytest.param({"column": "s"}, [6, 5, 4, 3, 2, 1], id="text_single_object"),
        # Missing values go last in either direction.
        pytest.param([{"column": "v"}], [4, 3, 1, 6, 2, 5], id="nulls_last_asc"),
        pytest.param(
            [{"column": "v", "descending": True}], [6, 1, 3, 4, 2, 5], id="nulls_last_desc"
        ),
        # Ties on g keep the second key's order inside each group.
        pytest.param(
            [{"column": "g"}, {"column": "s", "descending": True}],
            [1, 3, 5, 2, 4, 6],
            id="g_asc_then_s_desc",
        ),
        pytest.param(
            [{"column": "g", "descending": True}, {"column": "s"}],
            [6, 4, 2, 5, 3, 1],
            id="g_desc_then_s_asc",
        ),
        pytest.param([], [1, 2, 3, 4, 5, 6], id="no_keys"),
    ],
)
async def test_sorted_pages_without_duckdb(
    start_kernel: KernelFactory, library_python: tuple[str, str], sort: Any, ids: list[int]
) -> None:
    ks = await _kernel(start_kernel, library_python)
    table, total = await _page(ks, "df", offset=0, limit=50, sort=sort)
    assert total == 6
    assert [int(r[table.names.index("id")]) for r in table.rows] == ids


async def test_a_sorted_page_is_sliced_after_sorting(
    start_kernel: KernelFactory, library_python: tuple[str, str]
) -> None:
    ks = await _kernel(start_kernel, library_python)
    table, _ = await _page(ks, "df", offset=2, limit=2, sort=[{"column": "id", "descending": True}])
    assert [int(r[0]) for r in table.rows] == [4, 3]
    # A missing value reads as nothing, not as NaN or a placeholder.
    table, _ = await _page(ks, "df", offset=4, limit=2, sort=[{"column": "v"}])
    assert [r[table.names.index("v")] for r in table.rows] == [None, None]


async def test_a_series_and_non_text_column_names_page(
    start_kernel: KernelFactory, library_python: tuple[str, str]
) -> None:
    ks = await _kernel(start_kernel, library_python)
    table, total = await _page(ks, "ser", offset=0, limit=10, sort={"column": "n"})
    assert (table.names, [list(r) for r in table.rows], total) == (["n"], [[1], [2], [3]], 3)
    table, _ = await _page(ks, "frame", offset=0, limit=10, sort={"column": "1"})
    assert table.names == ["1", "x"]
    assert [list(r) for r in table.rows] == [[1, 0], [2, 0]]


async def test_an_unknown_sort_column_is_named_without_duckdb(
    start_kernel: KernelFactory, library_python: tuple[str, str]
) -> None:
    ks = await _kernel(start_kernel, library_python)
    with pytest.raises(frames.RpcError) as info:
        await ks.request(
            "inspect.frame",
            {"name": "df", "offset": 0, "limit": 5, "sort": [{"column": "g"}, {"column": "G"}]},
        )
    assert info.value.data == {"name": "inspect.unknown_column", "column": "G"}
    assert info.value.message == "There is no column named 'G'"


async def test_a_filter_needs_duckdb_and_says_so(
    start_kernel: KernelFactory, library_python: tuple[str, str]
) -> None:
    ks = await _kernel(start_kernel, library_python)
    with pytest.raises(frames.RpcError) as info:
        await ks.request(
            "inspect.frame",
            {"name": "df", "offset": 0, "limit": 5, "filter_sql": "select id from frame"},
        )
    assert info.value.code == frames.ErrorCode.METHOD_SPECIFIC
    assert info.value.data == {"name": "inspect.filter_needs_duckdb"}
    # Shown beside the table as it is: a sentence.
    assert info.value.message == "Filtering a table needs duckdb in this environment"


async def test_a_value_that_is_not_a_frame_is_unavailable_without_duckdb(
    start_kernel: KernelFactory, library_python: tuple[str, str]
) -> None:
    ks = await _kernel(start_kernel, library_python)
    await ks.run(step("c", "rows = [1, 2, 3]"))
    with pytest.raises(frames.RpcError) as info:
        await ks.request("inspect.frame", {"name": "rows", "offset": 0, "limit": 5})
    assert info.value.code == frames.ErrorCode.UNAVAILABLE
