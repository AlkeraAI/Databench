"""A structured table read spells only identifiers into its statement."""

from __future__ import annotations

import pytest
from alkera_cli.plugins.plugin_base.sql_tools import QueryByTable, ToolError, _compile_table_query


@pytest.mark.parametrize(
    "table",
    [
        pytest.param("orders", id="bare"),
        pytest.param("analytics.orders", id="dotted"),
        pytest.param('"Weird Name".orders', id="double-quoted-schema"),
        pytest.param("`db`.`orders`", id="backticked"),
        pytest.param("[dbo].[Orders]", id="bracketed"),
        pytest.param("_t$1.v2", id="dollar-and-digits"),
    ],
)
def test_an_identifier_path_compiles(table: str) -> None:
    spec = QueryByTable(connection="c", table=table, columns=["id", "amount_usd"], limit=3)
    sql = _compile_table_query(spec, "no-such-engine")
    assert sql == f"SELECT id, amount_usd FROM {table} LIMIT 3"


@pytest.mark.parametrize(
    "table",
    [
        pytest.param("t; DROP TABLE x", id="second-statement"),
        pytest.param("t LIMIT 1 --", id="trailing-clause-and-comment"),
        pytest.param("t WHERE 1=1", id="where-clause"),
        pytest.param("t/**/x", id="comment-in-name"),
        pytest.param("(SELECT 1)", id="subquery"),
        pytest.param("t.", id="dangling-dot"),
        pytest.param("", id="empty"),
        pytest.param('"a" OR "b"', id="operator-between-quoted"),
    ],
)
def test_anything_but_an_identifier_path_is_refused_before_it_is_spelled(table: str) -> None:
    spec = QueryByTable(connection="c", table=table, limit=3)
    with pytest.raises(ToolError, match="table name"):
        _compile_table_query(spec, "no-such-engine")


@pytest.mark.parametrize(
    ("columns", "ok"),
    [
        pytest.param(["*"], True, id="star"),
        pytest.param(["id", "t.name"], True, id="plain-and-qualified"),
        pytest.param(["id, (SELECT password FROM users)"], False, id="subquery-column"),
        pytest.param(["id FROM other --"], False, id="from-smuggled"),
        pytest.param(["count(*)"], False, id="call"),
    ],
)
def test_columns_follow_the_same_rule(columns: list[str], ok: bool) -> None:
    spec = QueryByTable(connection="c", table="t", columns=columns, limit=3)
    if ok:
        assert _compile_table_query(spec, "no-such-engine").startswith("SELECT ")
    else:
        with pytest.raises(ToolError, match="column names"):
            _compile_table_query(spec, "no-such-engine")
