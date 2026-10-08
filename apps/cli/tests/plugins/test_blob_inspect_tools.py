"""blob.info / blob.profile / blob.query — operate over a spilled result without
paging every row.

These are the READ inspect tools: cheap metadata, a fixed-size statistical
profile, and read-only DuckDB SQL over the rows. The tests pin the behavior a
caller sees (bounded profile output regardless of row count, a query that spills
+ pages like sql.query, and the read-only refusal that keeps blob.query from
writing / exfiltrating / escaping the result set).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_cli.plugins.plugin_base.blob_inspect_tools import register_blob_inspect_tools
from alkera_cli.plugins.plugin_base.blob_tool import register_blob_tools
from alkera_cli.plugins.plugin_base.meta_tools import register_meta_tools
from alkera_cli.plugins.plugin_base.result_blob import write_rows_blob, write_text_blob
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_core.json_safe import NONFINITE_KEY
from alkera_core.project.chats.blobs import BlobStore
from alkera_core.project.directory import ProjectDirectory


def _blobs(tmp_path: Path) -> BlobStore:
    return ProjectDirectory(tmp_path / ".alkera").blobs()


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry(_blobs(tmp_path))
    register_meta_tools(registry)
    register_blob_tools(registry)
    register_blob_inspect_tools(registry)
    return registry


# --- registration / hotness ------------------------------------------------


def test_inspect_tools_are_hot_and_read() -> None:
    from alkera_cli.contracts.tool_types import Effect
    from alkera_cli.plugins.plugin_base.blob_inspect_tools import (
        BlobInfoTool,
        BlobProfileTool,
        BlobQueryTool,
    )

    for tool in (BlobInfoTool, BlobProfileTool, BlobQueryTool):
        assert tool.spec.hot is True, tool.spec.name
        assert tool.spec.effect_hint == Effect.READ, tool.spec.name


# --- blob.info -------------------------------------------------------------


async def test_info_on_rows_blob(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_rows_blob(
        registry._blobs, columns=["id", "name"], rows=[[i, f"r{i}"] for i in range(120)]
    )
    out = await registry.dispatch("blob.info", {"handle": handle.sha256})
    assert out["kind"] == "rows"
    assert out["columns"] == ["id", "name"]
    assert out["total"] == 120
    assert out["size_bytes"] > 0
    assert out["content_type"] == "application/json"


async def test_info_on_text_blob(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text="hello world" * 100)
    out = await registry.dispatch("blob.info", {"handle": handle.sha256})
    assert out["kind"] == "text"
    assert out["total"] == len("hello world" * 100)
    assert out["content_type"] == "text/plain"


async def test_info_on_opaque_blob(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    sha, _ = registry._blobs.write(b"not an envelope at all")
    out = await registry.dispatch("blob.info", {"handle": sha})
    assert out["kind"] == "opaque"
    assert out["size_bytes"] == len(b"not an envelope at all")


async def test_info_unknown_handle_is_clean_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("blob.info", {"handle": "0" * 64})
    assert "no result blob" in out["error"]


async def test_info_malformed_handle_is_clean_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("blob.info", {"handle": "not-a-sha"})
    assert "invalid blob handle" in out["error"]


# --- blob.profile ----------------------------------------------------------


async def test_profile_basic_stats(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    rows = [
        [1, "a", 1.5],
        [2, "a", 2.5],
        [3, "b", None],
        [None, "c", 4.0],
    ]
    handle = write_rows_blob(registry._blobs, columns=["id", "cat", "val"], rows=rows)
    out = await registry.dispatch("blob.profile", {"handle": handle.sha256})
    assert out["row_count"] == 4
    cols = {c["name"]: c for c in out["columns"]}

    assert cols["id"]["dtype"] == "int"
    assert cols["id"]["null_count"] == 1
    assert cols["id"]["distinct_count"] == 3
    assert cols["id"]["min"] == 1 and cols["id"]["max"] == 3

    assert cols["cat"]["dtype"] == "str"
    assert cols["cat"]["null_count"] == 0
    assert cols["cat"]["distinct_count"] == 3
    # top_k: "a" is the most frequent
    assert cols["cat"]["top_k"][0] == {"value": "a", "count": 2}
    assert cols["cat"]["min"] == "a" and cols["cat"]["max"] == "c"

    assert cols["val"]["dtype"] == "float"
    assert cols["val"]["null_count"] == 1
    assert cols["val"]["min"] == 1.5 and cols["val"]["max"] == 4.0


async def test_profile_output_is_bounded_regardless_of_row_count(tmp_path: Path) -> None:
    # The whole point: 50 rows and 50_000 rows produce the SAME-shape summary,
    # never a row dump. Compare the serialized output size for two row counts.
    registry = _registry(tmp_path)
    small = write_rows_blob(
        registry._blobs, columns=["id", "cat"], rows=[[i, f"c{i % 7}"] for i in range(50)]
    )
    big = write_rows_blob(
        registry._blobs, columns=["id", "cat"], rows=[[i, f"c{i % 7}"] for i in range(50_000)]
    )
    out_small = await registry.dispatch("blob.profile", {"handle": small.sha256})
    out_big = await registry.dispatch("blob.profile", {"handle": big.sha256})
    assert out_small["row_count"] == 50 and out_big["row_count"] == 50_000
    # Same number of columns + same top_k width → bounded output.
    assert len(out_big["columns"]) == len(out_small["columns"]) == 2
    assert len(out_big["columns"][1]["top_k"]) == len(out_small["columns"][1]["top_k"]) == 5


async def test_profile_handles_non_finite_floats(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_rows_blob(
        registry._blobs, columns=["x"], rows=[[float("nan")], [float("inf")], [2.0], [3.0]]
    )
    out = await registry.dispatch("blob.profile", {"handle": handle.sha256})
    col = out["columns"][0]
    assert col["dtype"] == "float"
    # NaN is excluded from min/max; +inf is the max.
    assert col["min"] == 2.0
    assert col["max"] == {NONFINITE_KEY: "inf"}


async def test_profile_top_k_is_capped(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_rows_blob(
        registry._blobs, columns=["c"], rows=[[f"v{i % 100}"] for i in range(1000)]
    )
    out = await registry.dispatch("blob.profile", {"handle": handle.sha256, "top_k": 3})
    assert len(out["columns"][0]["top_k"]) == 3
    assert out["columns"][0]["distinct_count"] == 100


async def test_profile_refuses_text_blob(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_text_blob(registry._blobs, text="not tabular")
    out = await registry.dispatch("blob.profile", {"handle": handle.sha256})
    assert "invalid blob handle" in out["error"]
    assert "tabular" in out["error"]


# --- blob.query ------------------------------------------------------------


async def test_query_aggregation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    rows = [[i, "east" if i % 2 == 0 else "west", i * 10] for i in range(10)]
    handle = write_rows_blob(registry._blobs, columns=["id", "region", "amount"], rows=rows)
    out = await registry.dispatch(
        "blob.query",
        {
            "handle": handle.sha256,
            "sql": "SELECT region, count(*) AS n, sum(amount) AS total "
            "FROM result GROUP BY region ORDER BY region",
        },
    )
    assert out["columns"] == ["region", "n", "total"]
    assert out["preview_rows"] == [["east", 5, 200], ["west", 5, 250]]
    assert out["row_count"] == 2
    assert out["blob"] is None


async def test_query_large_result_spills_and_pages(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    handle = write_rows_blob(registry._blobs, columns=["id"], rows=[[i] for i in range(5000)])
    out = await registry.dispatch(
        "blob.query",
        {"handle": handle.sha256, "sql": "SELECT id, id * 2 AS doubled FROM result"},
    )
    assert out["row_count"] == 5000
    assert out["truncated"] is True
    assert out["blob"] is not None
    # The spilled handle pages exactly like any other rows blob.
    page = await registry.dispatch(
        "fetch_result", {"handle": out["blob"]["sha256"], "offset": 0, "limit": 3}
    )
    assert page["kind"] == "rows"
    assert page["rows"] == [[0, 0], [1, 2], [2, 4]]
    assert page["total"] == 5000


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("COPY (SELECT 1) TO '/tmp/x.csv'", id="copy-egress"),
        pytest.param("CREATE TABLE evil AS SELECT * FROM result", id="create"),
        pytest.param("DELETE FROM result", id="delete"),
        pytest.param("INSERT INTO result VALUES (1)", id="insert"),
    ],
)
async def test_query_refuses_non_read(tmp_path: Path, sql: str) -> None:
    registry = _registry(tmp_path)
    handle = write_rows_blob(registry._blobs, columns=["id"], rows=[[1], [2]])
    out = await registry.dispatch("blob.query", {"handle": handle.sha256, "sql": sql})
    assert "read-only" in out["error"]


async def test_query_external_access_is_blocked_even_for_a_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file-reader that is NOT on the DuckDB deny set still classifies as READ, so it reaches
    # execution — where the locked-down in-memory DuckDB (external access disabled) refuses it.
    # This pins the execution-layer backstop beneath the classifier's deny set. Every reader
    # DuckDB ships today is on the set, so the one used here is taken off it for this test:
    # the stand-in for a reader a later DuckDB adds before the set learns its name.
    from alkera_cli.plugins.plugin_base.permissions.commands import DANGEROUS_FUNCS

    monkeypatch.setitem(DANGEROUS_FUNCS, "duckdb", DANGEROUS_FUNCS["duckdb"] - {"read_json_auto"})
    registry = _registry(tmp_path)
    handle = write_rows_blob(registry._blobs, columns=["id"], rows=[[1]])
    out = await registry.dispatch(
        "blob.query",
        {"handle": handle.sha256, "sql": "SELECT * FROM read_json_auto('/etc/hosts')"},
    )
    # Refused at execution: DuckDB reports file-system access disabled (the error
    # may echo the SQL the model wrote, but no file was opened / its contents read).
    assert "error" in out
    assert "query failed" in out["error"]
    assert "disabled" in out["error"]


async def test_query_denied_reader_blocked_at_classification(tmp_path: Path) -> None:
    # A file reader on the DuckDB deny set (read_csv_auto) is refused EARLIER, at the
    # read-only classification gate — it never reaches execution, so no file is opened.
    registry = _registry(tmp_path)
    handle = write_rows_blob(registry._blobs, columns=["id"], rows=[[1]])
    out = await registry.dispatch(
        "blob.query",
        {"handle": handle.sha256, "sql": "SELECT * FROM read_csv_auto('/etc/hosts')"},
    )
    assert "error" in out
    assert "read-only" in out["error"]


async def test_query_heterogeneous_column(tmp_path: Path) -> None:
    # A column with mixed types must not crash the arrow build — it falls back to
    # a string column so the query still runs.
    registry = _registry(tmp_path)
    handle = write_rows_blob(registry._blobs, columns=["mixed"], rows=[[1], ["two"], [3.0], [None]])
    out = await registry.dispatch(
        "blob.query", {"handle": handle.sha256, "sql": "SELECT count(*) AS n FROM result"}
    )
    assert out["preview_rows"] == [[4]]


async def test_query_unknown_handle_is_clean_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    out = await registry.dispatch("blob.query", {"handle": "0" * 64, "sql": "SELECT * FROM result"})
    assert "no result blob" in out["error"]
