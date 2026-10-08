"""``inspect.frame`` ordered by several keys: every column is checked against
the page's own source before anything is ordered, and an unknown one is a
structured ``inspect.unknown_column`` error that names it, never DuckDB's own
binder message."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.rpc import frames
from nbkrn_harness import KernelFactory, KernelSession, PageTable, step

# Ties on ``g`` everywhere, so only the second key decides the order inside a group.
FRAME = (
    "import pandas as pd\n"
    "df = pd.DataFrame({'id': [1, 2, 3, 4, 5, 6], 'g': [1, 2, 1, 2, 1, 2],"
    " 'v': [5, 9, 3, 1, 4, 7], 'we\"ird': [6, 5, 4, 3, 2, 1]})"
)


async def _frame_kernel(start_kernel: KernelFactory) -> KernelSession:
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    await ks.run(step("a", FRAME))
    return ks


async def _ids(ks: KernelSession, **params: Any) -> list[int]:
    page = await ks.request("inspect.frame", {"name": "df", "offset": 0, "limit": 50, **params})
    table = PageTable(page["table"])
    return [int(row[table.names.index("id")]) for row in table.rows]


@pytest.mark.parametrize(
    ("sort", "ids"),
    [
        pytest.param(
            [{"column": "g"}, {"column": "v", "descending": True}],
            [1, 5, 3, 2, 6, 4],
            id="g_asc_then_v_desc",
        ),
        pytest.param(
            [{"column": "g", "descending": True}, {"column": "v"}],
            [4, 6, 2, 3, 5, 1],
            id="g_desc_then_v_asc",
        ),
        pytest.param(
            [{"column": "g"}, {"column": "v"}],
            [3, 5, 1, 4, 6, 2],
            id="both_asc",
        ),
        pytest.param({"column": "v", "descending": True}, [2, 6, 1, 5, 3, 4], id="single_object"),
        pytest.param([{"column": 'we"ird'}], [6, 5, 4, 3, 2, 1], id="quote_in_column_name"),
        pytest.param([], [1, 2, 3, 4, 5, 6], id="no_keys"),
    ],
)
async def test_nbkrn_frame_orders_by_every_key(
    start_kernel: KernelFactory, sort: Any, ids: list[int]
) -> None:
    ks = await _frame_kernel(start_kernel)
    assert await _ids(ks, sort=sort) == ids


async def test_nbkrn_filtered_source_sorts_by_its_own_columns(
    start_kernel: KernelFactory,
) -> None:
    ks = await _frame_kernel(start_kernel)
    got = await _ids(
        ks,
        filter_sql="select id, v * -1 as neg from frame where g = 1",
        sort=[{"column": "neg"}],
    )
    assert got == [1, 5, 3]


@pytest.mark.parametrize(
    ("params", "column"),
    [
        pytest.param({"sort": [{"column": "nope"}]}, "nope", id="unknown"),
        pytest.param(
            {"sort": [{"column": "g"}, {"column": "missing", "descending": True}]},
            "missing",
            id="unknown_second_key",
        ),
        pytest.param({"sort": {"column": "nope"}}, "nope", id="unknown_single_object"),
        pytest.param({"sort": [{"column": "G"}]}, "G", id="names_match_exactly"),
        pytest.param({"sort": [{"column": 'v" DESC, "id'}]}, 'v" DESC, "id', id="injection_shaped"),
        pytest.param(
            {"sort": [{"column": "v"}], "filter_sql": "select id, g from frame"},
            "v",
            id="frame_column_absent_from_the_filter",
        ),
    ],
)
async def test_nbkrn_unknown_sort_column_is_a_structured_error(
    start_kernel: KernelFactory, params: dict[str, Any], column: str
) -> None:
    ks = await _frame_kernel(start_kernel)
    with pytest.raises(frames.RpcError) as info:
        await ks.request("inspect.frame", {"name": "df", "offset": 0, "limit": 5, **params})
    assert info.value.code == frames.ErrorCode.METHOD_SPECIFIC
    assert info.value.data == {"name": "inspect.unknown_column", "column": column}
    assert "Binder" not in info.value.message


@pytest.mark.parametrize(
    "sort",
    [
        pytest.param([{"column": "id"}] * 17, id="seventeen_keys"),
        pytest.param([{"descending": True}], id="no_column"),
        pytest.param([{"column": ""}], id="empty_column"),
        pytest.param([{"column": 3}], id="column_not_text"),
        pytest.param([{"column": "id", "descending": "yes"}], id="descending_not_bool"),
        pytest.param(["id"], id="key_not_object"),
        pytest.param("id:asc", id="not_a_list"),
    ],
)
async def test_nbkrn_malformed_sort_is_invalid_params(
    start_kernel: KernelFactory, sort: Any
) -> None:
    ks = await _frame_kernel(start_kernel)
    with pytest.raises(frames.RpcError) as info:
        await ks.request("inspect.frame", {"name": "df", "offset": 0, "limit": 5, "sort": sort})
    assert info.value.code == frames.ErrorCode.INVALID_PARAMS


async def test_nbkrn_sixteen_keys_are_taken(start_kernel: KernelFactory) -> None:
    ks = await _frame_kernel(start_kernel)
    sort = [{"column": "g", "descending": True}] + [{"column": "v"}] * 15
    assert await _ids(ks, sort=sort) == [4, 6, 2, 3, 5, 1]
