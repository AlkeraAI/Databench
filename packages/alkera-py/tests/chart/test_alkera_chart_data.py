"""Chart data from whatever table the caller holds, with no library required.

pandas and pyarrow are in this repo's environment and are exercised for real;
Polars is exercised when present. The dtype readings for all three are also
pinned by their type names, which is what the client actually reads.
"""

from __future__ import annotations

import ast
import datetime as dt
import decimal
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from alkera.chart._data import Table, cell, from_csv, systematic_sample, to_table

CLIENT = Path(__file__).resolve().parents[2] / "alkera" / "chart"
API_CORE_PROFILE = (
    Path(__file__).resolve().parents[4] / "packages/api-core/alkera_core/charts/profile.py"
)

EXPECTED_ROWS = [
    {"day": "2026-01-01T00:00:00", "region": "north", "units": 12, "price": 4.5},
    {"day": "2026-01-02T00:00:00", "region": "south", "units": 18, "price": None},
]


# Cells --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param(True, True, id="bool_stays_bool"),
        pytest.param(7, 7, id="int"),
        pytest.param(2.5, 2.5, id="float"),
        pytest.param(float("nan"), None, id="nan_is_a_gap"),
        pytest.param(float("-inf"), None, id="infinity_is_a_gap"),
        pytest.param(decimal.Decimal("1.25"), 1.25, id="decimal"),
        pytest.param(decimal.Decimal("NaN"), None, id="decimal_nan"),
        pytest.param(dt.date(2026, 1, 2), "2026-01-02", id="date"),
        pytest.param(dt.datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05", id="datetime"),
        pytest.param(dt.timedelta(minutes=2), 120.0, id="timedelta_in_seconds"),
        pytest.param(b"x", "b'x'", id="anything_else_is_its_str"),
    ],
)
def test_a_cell_becomes_a_json_scalar(value: object, expected: object) -> None:
    assert cell(value) == expected


def test_numpy_scalars_and_pandas_missing_times_become_plain_values() -> None:
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    assert cell(np.int64(3)) == 3
    assert type(cell(np.int64(3))) is int
    assert cell(np.float32(np.nan)) is None
    assert cell(np.bool_(True)) is True
    assert cell(pd.NaT) is None
    assert cell(pd.Timestamp("2026-01-02")) == "2026-01-02T00:00:00"
    assert cell(np.array([1, 2])) == "[1 2]", "an array cell is text, never a crash"


# Tables -------------------------------------------------------------------------


def _check(table: Table) -> None:
    assert table.rows == EXPECTED_ROWS
    assert table.columns == ["day", "region", "units", "price"]
    assert table.kinds == {
        "day": "temporal",
        "region": "nominal",
        "units": "quantitative",
        "price": "quantitative",
    }


def test_a_list_of_rows() -> None:
    _check(
        to_table(
            [
                {"day": dt.datetime(2026, 1, 1), "region": "north", "units": 12, "price": 4.5},
                {
                    "day": dt.datetime(2026, 1, 2),
                    "region": "south",
                    "units": 18,
                    "price": float("nan"),
                },
            ]
        )
    )


def test_a_mapping_of_columns() -> None:
    _check(
        to_table(
            {
                "day": [dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)],
                "region": ["north", "south"],
                "units": [12, 18],
                "price": [4.5, None],
            }
        )
    )


def test_a_pandas_frame() -> None:
    pd = pytest.importorskip("pandas")
    _check(
        to_table(
            pd.DataFrame(
                {
                    "day": pd.to_datetime(["2026-01-01", "2026-01-02"]),
                    "region": ["north", "south"],
                    "units": [12, 18],
                    "price": [4.5, None],
                }
            )
        )
    )


def test_a_pyarrow_table() -> None:
    pa = pytest.importorskip("pyarrow")
    _check(
        to_table(
            pa.table(
                {
                    "day": pa.array(
                        [dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)], pa.timestamp("us")
                    ),
                    "region": ["north", "south"],
                    "units": [12, 18],
                    "price": [4.5, None],
                }
            )
        )
    )


def test_a_polars_frame() -> None:
    pl = pytest.importorskip("polars")
    _check(
        to_table(
            pl.DataFrame(
                {
                    "day": [dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)],
                    "region": ["north", "south"],
                    "units": [12, 18],
                    "price": [4.5, None],
                }
            )
        )
    )


@pytest.mark.parametrize(
    ("dtype", "kind"),
    [
        pytest.param("datetime64[ns]", "temporal", id="pandas_datetime"),
        pytest.param("Datetime(time_unit='us', time_zone=None)", "temporal", id="polars_datetime"),
        pytest.param("timestamp[us]", "temporal", id="arrow_timestamp"),
        pytest.param("date32[day]", "temporal", id="arrow_date"),
        pytest.param("Int64", "quantitative", id="polars_int"),
        pytest.param("float32", "quantitative", id="numpy_float"),
        pytest.param("decimal128(10, 2)", "quantitative", id="arrow_decimal"),
        pytest.param("bool", "nominal", id="bool_is_a_category"),
        pytest.param("Boolean", "nominal", id="polars_bool"),
    ],
)
def test_a_typed_column_takes_its_kind_from_its_type(dtype: str, kind: str) -> None:
    class Typed:
        def __str__(self) -> str:
            return dtype

    from alkera.chart._data import _from_records

    table = _from_records([{"c": "7"}], {"c": Typed()})
    assert table.kinds["c"] == kind


@pytest.mark.parametrize(
    ("values", "kind"),
    [
        pytest.param([1, 2.5, None], "quantitative", id="numbers"),
        pytest.param(["2026-01-01", "2026-01-02T10:00:00Z"], "temporal", id="iso_dates"),
        pytest.param(["1", "2"], "nominal", id="numeric_text_is_text"),
        pytest.param([True, False], "nominal", id="booleans"),
        pytest.param([None, None], "nominal", id="all_missing"),
        pytest.param(["2026-01-01", "soon"], "nominal", id="mixed"),
    ],
)
def test_an_untyped_column_takes_its_kind_from_its_values(values: list[Any], kind: str) -> None:
    assert to_table([{"c": v} for v in values]).kinds["c"] == kind


@pytest.mark.parametrize(
    ("data", "error"),
    [
        pytest.param({"a": [1, 2], "b": [1]}, ValueError, id="ragged_columns"),
        pytest.param([1, 2, 3], TypeError, id="rows_not_mappings"),
        pytest.param("no/such/file.csv", FileNotFoundError, id="a_missing_csv_path"),
        pytest.param("a,b\n1,2,3", ValueError, id="csv_row_wider_than_its_header"),
        pytest.param(42, TypeError, id="a_number"),
    ],
)
def test_data_that_is_not_a_table_is_refused(data: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        to_table(data)


# CSV ----------------------------------------------------------------------------

CSV_TEXT = "day,region,units,price\n2026-01-01,north,12,4.5\n2026-01-02,south,18,\n"
CSV_ROWS = [
    {"day": "2026-01-01", "region": "north", "units": 12, "price": 4.5},
    {"day": "2026-01-02", "region": "south", "units": 18, "price": None},
]
CSV_KINDS = {
    "day": "temporal",
    "region": "nominal",
    "units": "quantitative",
    "price": "quantitative",
}


def _csv_sources(tmp_path: Path) -> dict[str, object]:
    csv_file = tmp_path / "sales.csv"
    csv_file.write_text("\ufeff" + CSV_TEXT, encoding="utf-8")
    tsv_file = tmp_path / "sales.tsv"
    tsv_file.write_text(CSV_TEXT.replace(",", "\t"), encoding="utf-8")
    return {
        "text": CSV_TEXT,
        "tsv_text": CSV_TEXT.replace(",", "\t"),
        "bytes": CSV_TEXT.encode(),
        "path_str": str(csv_file),
        "pathlike_with_bom": csv_file,
        "tsv_path": tsv_file,
        "open_text_file": io.StringIO(CSV_TEXT),
        "open_binary_file": io.BytesIO(CSV_TEXT.encode()),
    }


@pytest.mark.parametrize(
    "source",
    [
        "text",
        "tsv_text",
        "bytes",
        "path_str",
        "pathlike_with_bom",
        "tsv_path",
        "open_text_file",
        "open_binary_file",
    ],
)
def test_csv_in_every_form_reads_into_typed_rows(tmp_path: Path, source: str) -> None:
    table = to_table(_csv_sources(tmp_path)[source])
    assert table.columns == ["day", "region", "units", "price"]
    assert table.rows == CSV_ROWS
    assert table.kinds == CSV_KINDS


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        pytest.param("zip\n02134\n10001\n", [{"zip": 2134}, {"zip": 10001}], id="all_numeric"),
        pytest.param(
            "code\n007\nA7\n", [{"code": "007"}, {"code": "A7"}], id="one_text_keeps_strings"
        ),
        pytest.param("n\n1_000\n", [{"n": "1_000"}], id="python_only_numerals_stay_text"),
        pytest.param("n\nnan\ninf\n", [{"n": "nan"}, {"n": "inf"}], id="nan_names_stay_text"),
        pytest.param("n\n1e3\n-.5\n", [{"n": 1000.0}, {"n": -0.5}], id="exponent_and_fraction"),
        pytest.param("a,b\n1\n", [{"a": 1, "b": None}], id="short_row_pads_with_gaps"),
        pytest.param('a\n"x, y"\n', [{"a": "x, y"}], id="quoted_delimiter"),
        pytest.param("a,a,\n1,2,3\n", [{"a": 1, "a_2": 2, "column_3": 3}], id="header_repairs"),
        pytest.param("", [], id="empty"),
    ],
)
def test_csv_cells(text: str, rows: list[dict[str, Any]]) -> None:
    assert from_csv(text).rows == rows


def test_a_chart_from_csv_carries_inline_rows_the_profile_admits(tmp_path: Path) -> None:
    import alkera

    path = tmp_path / "sales.csv"
    path.write_text(CSV_TEXT, encoding="utf-8")
    spec = alkera.chart(str(path)).bar(x="region", y="units").to_dict()
    (rows,) = spec["datasets"].values()
    assert rows == CSV_ROWS
    assert "format" not in json.dumps(spec)
    assert alkera.chart.validate(spec)["datasets"] == spec["datasets"]


def test_rows_with_different_keys_union_their_columns_in_first_seen_order() -> None:
    table = to_table([{"a": 1}, {"b": 2, "a": 3}])
    assert table.columns == ["a", "b"]


# Sampling -----------------------------------------------------------------------


@pytest.mark.parametrize(("n", "limit"), [(10, 3), (101, 10), (5000, 4999), (7, 2)])
def test_systematic_sample_keeps_ends_order_and_count(n: int, limit: int) -> None:
    rows = [{"i": i} for i in range(n)]
    kept = systematic_sample(rows, limit)
    indices = [r["i"] for r in kept]
    assert len(kept) == limit
    assert indices[0] == 0
    assert indices[-1] == n - 1
    assert indices == sorted(set(indices))


def test_systematic_sample_under_the_limit_is_the_rows_themselves() -> None:
    rows = [{"i": i} for i in range(5)]
    assert systematic_sample(rows, 5) is rows


# Portability --------------------------------------------------------------------


def test_the_clients_profile_is_byte_identical_to_the_servers() -> None:
    """Regenerate with ``make gen-chart-profile`` when this fails."""
    assert (CLIENT / "_profile.py").read_bytes() == API_CORE_PROFILE.read_bytes()


# Which Python the client parses and runs on is the floor gate's
# (packages/alkera-notebook/tests/test_alkera_python_floor.py).
@pytest.mark.parametrize("module", sorted(p.name for p in CLIENT.glob("*.py")))
def test_the_client_imports_only_the_standard_library(module: str) -> None:
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        pytest.skip("listing the standard library needs Python 3.10")
    tree = ast.parse((CLIENT / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            assert top in stdlib or name.startswith("alkera.chart"), f"{module} imports {name}"
