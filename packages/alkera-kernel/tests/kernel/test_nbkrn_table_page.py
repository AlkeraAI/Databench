"""One table page, whichever way it is asked for.

A cell's output carries a table's first page; ``inspect.frame`` answers every
later one and every sorted or filtered one. Both are made by
``_alkera_kernel.tables``: the same keys, the same column types and the same
encoding of every cell, as plain JSON. A page that differed by route once
left a later page of a dated table unsendable while its first page showed.
"""

from __future__ import annotations

import datetime
import decimal
import json
import uuid
from typing import Any

import pytest
from _alkera_kernel import tables
from nbkrn_harness import KernelFactory, step

TABLE = "application/vnd.alkera.table+json"


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        pytest.param(None, None, id="null"),
        pytest.param(True, True, id="bool"),
        pytest.param("text", "text", id="text"),
        pytest.param(2**53 - 1, 9007199254740991, id="largest-exact-int"),
        pytest.param(2**53, "9007199254740992", id="int-past-a-double"),
        pytest.param(-(2**63), "-9223372036854775808", id="int64-minimum"),
        pytest.param(1.5, 1.5, id="float"),
        pytest.param(float("nan"), "NaN", id="nan"),
        pytest.param(float("inf"), "Infinity", id="infinity"),
        pytest.param(float("-inf"), "-Infinity", id="negative-infinity"),
        pytest.param(decimal.Decimal("12.50"), "12.50", id="decimal-keeps-its-scale"),
        pytest.param(datetime.date(2026, 10, 6), "2026-10-06", id="date"),
        pytest.param(
            datetime.datetime(2026, 10, 6, 11, 5, 7, tzinfo=datetime.UTC),
            "2026-10-06T11:05:07+00:00",
            id="datetime",
        ),
        pytest.param(datetime.time(11, 5), "11:05:00", id="time"),
        pytest.param(datetime.timedelta(days=1, seconds=3), "P1DT0H0M3S", id="duration"),
        pytest.param(b"\x00\xffA", "0x00ff41", id="bytes"),
        pytest.param(uuid.UUID(int=5), "00000000-0000-0000-0000-000000000005", id="uuid"),
        pytest.param(
            [datetime.date(2026, 1, 2), (float("nan"), b"\x01")],
            ["2026-01-02", ["NaN", "0x01"]],
            id="nested-list",
        ),
        pytest.param(
            {"when": datetime.date(2026, 1, 2), 3: decimal.Decimal("0.1")},
            {"when": "2026-01-02", "3": "0.1"},
            id="struct",
        ),
        pytest.param(complex(1, 2), "(1+2j)", id="anything-else-as-its-text"),
    ],
)
def test_a_cell_is_encoded_by_its_type(value: Any, encoded: Any) -> None:
    assert tables.encode_cell(value) == encoded
    assert type(tables.encode_cell(value)) is type(encoded)


def test_numpy_values_are_encoded_as_their_python_values() -> None:
    np = pytest.importorskip("numpy")
    assert tables.encode_cell(np.int64(7)) == 7
    assert tables.encode_cell(np.float64("nan")) == "NaN"
    assert tables.encode_cell(np.array([1, 2**60])) == [1, str(2**60)]


def test_a_page_is_its_schema_rows_total_and_offset_as_strict_json() -> None:
    page = tables.table_page(
        [("day", "Date"), ("n", "Int64")],
        [(datetime.date(2026, 1, 1), 1), (None, float("nan"))],
        total_rows=70,
        offset=50,
    )
    assert page == {
        "schema": [{"name": "day", "type": "Date"}, {"name": "n", "type": "Int64"}],
        "rows": [["2026-01-01", 1], [None, "NaN"]],
        "total_rows": 70,
        "offset": 50,
    }
    assert json.loads(json.dumps(page, allow_nan=False)) == page


MIXED = (
    "import datetime, decimal\n"
    "import pandas as pd\n"
    "n = 70\n"
    "mixed = pd.DataFrame({\n"
    "    'id': list(range(n)),\n"
    "    'day': [datetime.date(2026, 1, 1) + datetime.timedelta(days=i) for i in range(n)],\n"
    "    'amount': [decimal.Decimal(i) / 4 for i in range(n)],\n"
    "    'ratio': [float('nan') if i % 7 == 0 else i / 2 for i in range(n)],\n"
    "    'big': [2**60 + i for i in range(n)],\n"
    "    'raw': [bytes([i]) for i in range(n)],\n"
    "    'tags': [[i, str(i)] for i in range(n)],\n"
    "    'at': pd.to_datetime(['2026-01-01T00:00:00'] * n),\n"
    "})\n"
)


def _row(i: int) -> list[Any]:
    """Row ``i`` of ``mixed`` as a page carries it, worked out by hand."""
    day = datetime.date(2026, 1, 1) + datetime.timedelta(days=i)
    return [
        i,
        day.isoformat(),
        str(decimal.Decimal(i) / 4),
        # pandas spells a missing float as NaN: a missing value, so null.
        None if i % 7 == 0 else i / 2,
        str(2**60 + i),
        f"0x{i:02x}",
        [i, str(i)],
        "2026-01-01T00:00:00",
    ]


async def test_the_first_page_and_every_later_page_are_the_same_page(
    start_kernel: KernelFactory,
) -> None:
    """The page a cell's output carries is the page ``inspect.frame`` answers
    for offset 0, key for key and value for value, and a later page is the
    same shape and encoding further down the frame."""
    pytest.importorskip("pandas")
    ks = await start_kernel()
    (bundle,) = (await ks.run(step("a", MIXED + "mixed"))).outputs("a")
    shown = dict(bundle[TABLE])
    assert shown.pop("source") == {"name": "mixed"}

    first = await ks.request("inspect.frame", {"name": "mixed", "offset": 0, "limit": 50})
    assert first["table"] == shown
    assert shown["rows"] == [_row(i) for i in range(50)]
    assert (shown["total_rows"], shown["offset"]) == (70, 0)
    assert [c["name"] for c in shown["schema"]] == [
        "id", "day", "amount", "ratio", "big", "raw", "tags", "at",
    ]  # fmt: skip

    later = await ks.request("inspect.frame", {"name": "mixed", "offset": 50, "limit": 50})
    assert later["table"] == {
        "schema": shown["schema"],
        "rows": [_row(i) for i in range(50, 70)],
        "total_rows": 70,
        "offset": 50,
    }
    for page in (shown, later["table"]):
        assert json.loads(json.dumps(page, allow_nan=False)) == page


async def test_a_sorted_or_filtered_page_encodes_its_cells_the_same_way(
    start_kernel: KernelFactory,
) -> None:
    pytest.importorskip("pandas")
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    await ks.run(step("a", MIXED + "small = mixed[['id', 'day', 'amount', 'raw']]\nNone"))
    ordered = await ks.request(
        "inspect.frame",
        {"name": "small", "offset": 0, "limit": 2, "sort": [{"column": "id", "descending": True}]},
    )
    assert ordered["table"]["rows"] == [
        [69, "2026-03-11", "17.25", "0x45"],
        [68, "2026-03-10", "17", "0x44"],
    ]
    filtered = await ks.request(
        "inspect.frame",
        {
            "name": "small",
            "offset": 1,
            "limit": 1,
            "filter_sql": "SELECT id, day FROM frame WHERE id >= 68",
        },
    )
    assert set(filtered["table"]) == {"schema", "rows", "total_rows", "offset"}
    assert filtered["table"]["rows"] == [[69, "2026-03-11"]]
    assert (filtered["table"]["total_rows"], filtered["table"]["offset"]) == (2, 1)
    assert filtered["total_rows"] == 2
