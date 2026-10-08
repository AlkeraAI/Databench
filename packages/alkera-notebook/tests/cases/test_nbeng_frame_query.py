"""``inspect.frame`` requests: ``filter_sql`` is one SELECT over the table
``frame``, refused by the engine otherwise, and the sort is the kernel's
list of up to sixteen ``{column, descending}`` keys, whose names only the
kernel can check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.engine import InspectQuery, QueryRefusedError
from alkera_notebook.engine.frame_query import MAX_SORT_KEYS, check_frame_filter, frame_sort
from alkera_notebook.rpc import frames
from nbeng_harness import engine_for, notebook, run_cells


@pytest.mark.parametrize(
    ("sql", "sent"),
    [
        pytest.param("select * from frame", "select * from frame", id="plain"),
        pytest.param(
            "  SELECT a FROM frame WHERE b > 1;  ", "SELECT a FROM frame WHERE b > 1", id="trimmed"
        ),
        pytest.param("select * from FRAME", "select * from FRAME", id="any_case"),
        pytest.param('select * from "frame"', 'select * from "frame"', id="quoted"),
        pytest.param(
            "select f.a from frame f join frame g on f.a = g.a",
            "select f.a from frame f join frame g on f.a = g.a",
            id="self_join",
        ),
        pytest.param(
            "select * from frame where a in (select a from frame where b)",
            "select * from frame where a in (select a from frame where b)",
            id="subquery_over_frame",
        ),
    ],
)
def test_filter_accepted(sql: str, sent: str) -> None:
    assert check_frame_filter(sql) == sent


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        pytest.param("", "empty", id="empty"),
        pytest.param(" ; ", "empty", id="only_semicolon"),
        pytest.param("select * from frame; select 2", "statements", id="two_statements"),
        pytest.param("select * from frame; drop table frame", "statements", id="smuggled_drop"),
        pytest.param("insert into frame values (1)", "not_select", id="insert"),
        pytest.param("delete from frame", "not_select", id="delete"),
        pytest.param("select * from frame union select * from frame", "not_select", id="union"),
        pytest.param("select 1", "no_frame", id="no_table"),
        pytest.param("select * from other", "other_table", id="other_table"),
        pytest.param("select * from main.frame", "other_table", id="schema_qualified"),
        pytest.param("select * from db.main.frame", "other_table", id="catalog_qualified"),
        pytest.param("select * from read_csv('x.csv')", "other_table", id="table_function"),
        pytest.param("select * from '/etc/passwd'", "other_table", id="file_path"),
        pytest.param(
            "with t as (select * from frame) select * from t", "other_table", id="cte_name"
        ),
        pytest.param(
            "select * from frame where a in (select a from secrets)",
            "other_table",
            id="other_table_in_subquery",
        ),
        pytest.param("select * from frame join other on true", "other_table", id="join_other"),
        pytest.param("select (* from frame", "parse", id="does_not_parse"),
    ],
)
def test_filter_refused(sql: str, reason: str) -> None:
    with pytest.raises(QueryRefusedError) as caught:
        check_frame_filter(sql)
    assert caught.value.data["reason"] == reason
    assert caught.value.to_dict()["name"] == "query_refused"


SIXTEEN = [{"column": f"c{i}", "descending": i % 2 == 1} for i in range(MAX_SORT_KEYS)]


@pytest.mark.parametrize(
    ("sort", "sent"),
    [
        pytest.param(None, None, id="none"),
        pytest.param([], None, id="empty"),
        pytest.param([{"column": "a"}], [{"column": "a", "descending": False}], id="ascending"),
        pytest.param(
            [{"column": "a", "descending": True}],
            [{"column": "a", "descending": True}],
            id="descending",
        ),
        pytest.param(
            [{"column": "b", "descending": True}, {"column": "a"}],
            [{"column": "b", "descending": True}, {"column": "a", "descending": False}],
            id="two_keys_in_order",
        ),
        pytest.param(SIXTEEN, SIXTEEN, id="sixteen_keys"),
    ],
)
def test_sort_sent_as_ordered_keys(sort: list[dict[str, Any]] | None, sent: Any) -> None:
    assert frame_sort(sort) == sent


@pytest.mark.parametrize(
    "sort",
    [
        pytest.param([*SIXTEEN, {"column": "c16"}], id="seventeen_keys"),
        pytest.param([{"descending": True}], id="no_column"),
        pytest.param([{"column": ""}], id="empty_column"),
        pytest.param([{"column": 3}], id="column_not_text"),
        pytest.param([{"column": "a", "descending": "false"}], id="descending_not_bool"),
        pytest.param([{"column": "a"}, "b"], id="key_not_object"),
    ],
)
def test_sort_refused(sort: list[Any]) -> None:
    with pytest.raises(QueryRefusedError) as caught:
        frame_sort(sort)
    assert caught.value.data["reason"] == "sort"


async def test_refused_before_a_kernel_is_needed(tmp_path: Path) -> None:
    # No kernel runs: a refusable filter is refused, not "no kernel".
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, _ = await notebook(engine, ["rows = [1, 2]"])
        with pytest.raises(QueryRefusedError):
            await ann.inspect(
                InspectQuery(what="frame", name="rows", filter_sql="select * from other")
            )
        assert (await ann.kernel("status")).state == "absent"


FRAME_CELL = (
    "import pandas as pd\ndf = pd.DataFrame({'a': [1, 2, 3, 4], 'b': ['w', 'x', 'y', 'z']})"
)


@pytest.mark.parametrize(
    ("query", "total", "rows"),
    [
        pytest.param({}, 4, [["1", "w"], ["2", "x"], ["3", "y"], ["4", "z"]], id="whole_frame"),
        pytest.param(
            {"filter_sql": "select a, b from frame where a > 2"},
            2,
            [["3", "y"], ["4", "z"]],
            id="filter_over_frame",
        ),
        pytest.param(
            {
                "filter_sql": "select * from frame where a < 4",
                "sort": [{"column": "a", "descending": True}],
            },
            3,
            [["3", "y"], ["2", "x"], ["1", "w"]],
            id="filter_and_sort",
        ),
        pytest.param(
            {
                "filter_sql": "select a % 2 as odd, a, b from frame",
                "sort": [{"column": "odd"}, {"column": "a", "descending": True}],
            },
            4,
            [["0", "4", "z"], ["0", "2", "x"], ["1", "3", "y"], ["1", "1", "w"]],
            id="two_keys_tie_broken_by_the_second",
        ),
        pytest.param(
            {"filter_sql": "SELECT b FROM frame WHERE a IN (SELECT max(a) FROM frame);"},
            1,
            [["z"]],
            id="subquery_trailing_semicolon",
        ),
    ],
)
async def test_real_kernel_runs_the_filter_over_frame(
    tmp_path: Path, query: dict[str, Any], total: int, rows: list[list[str]]
) -> None:
    pytest.importorskip("pandas")
    pytest.importorskip("duckdb")
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, [FRAME_CELL])
        assert (await run_cells(ann, a)).status == "ok"
        page = await ann.inspect(InspectQuery(what="frame", name="df", **query))
        assert page.total_rows == total
        assert page.table is not None
        got = [[str(v) for v in row] for row in page.table["rows"]]
        assert got == rows


async def test_real_kernel_names_an_unknown_sort_column(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    pytest.importorskip("duckdb")
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, [FRAME_CELL])
        assert (await run_cells(ann, a)).status == "ok"
        with pytest.raises(frames.RpcError) as caught:
            await ann.inspect(
                InspectQuery(what="frame", name="df", sort=[{"column": "a"}, {"column": "nope"}])
            )
        assert caught.value.data == {"name": "inspect.unknown_column", "column": "nope"}
