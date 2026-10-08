"""Turn whatever table the caller holds into the rows a chart spec carries.

The client runs in the person's own environment, so nothing here imports a
data library: pandas, Polars and pyarrow are recognized by duck typing and
read through their own public methods, at whatever version is installed, and
plain lists and dicts work with none of them.

Every cell becomes a JSON scalar: numbers stay numbers (NaN and infinities
become null, a gap rather than a made-up value), dates and timestamps become
ISO 8601 strings, decimals become floats, NumPy scalars their Python value,
and anything else its ``str``.

CSV and TSV are read here, in Python, into the same rows: the renderer never
parses delimited text (Vega's parser compiles code, which the chart frame's
policy blocks), so a chart only ever carries inline rows.
"""

from __future__ import annotations

import csv
import datetime as _dt
import decimal
import io
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The four Vega-Lite measurement types, by their shorthand letter.
TYPE_LETTERS = {"Q": "quantitative", "T": "temporal", "O": "ordinal", "N": "nominal"}

_ISO_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)


class Table:
    """Rows as dicts of JSON scalars, the column order, and each column's
    measurement type as the source's own types (or its values) suggest."""

    __slots__ = ("columns", "kinds", "rows")

    def __init__(
        self, rows: list[dict[str, Any]], columns: list[str], kinds: dict[str, str]
    ) -> None:
        self.rows = rows
        self.columns = columns
        self.kinds = kinds

    def __len__(self) -> int:
        return len(self.rows)


def _missing(value: object) -> bool:
    try:
        return bool(value != value)
    except (TypeError, ValueError):  # an array compares elementwise
        return False


def cell(value: object) -> object:
    """``value`` as a JSON scalar."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return cell(float(value))
    if _missing(value):  # NaN-like (pandas NaT is a datetime): a gap, not a value
        return None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item) and type(value).__module__.split(".")[0] == "numpy":
        try:
            return cell(item())
        except (TypeError, ValueError):
            return str(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    if hasattr(value, "total_seconds"):  # a timedelta: its length in seconds
        return cell(value.total_seconds())
    return str(value)


def _kind_from_dtype(dtype: object) -> str | None:
    """A measurement type from a pandas, Polars or Arrow type's name."""
    name = str(dtype).lower()
    if any(token in name for token in ("datetime", "timestamp", "date32", "date64", "date")):
        return "temporal"
    if name in ("bool", "boolean"):
        return "nominal"
    if name.startswith(("int", "uint", "float", "double", "decimal")) or name in ("halffloat",):
        return "quantitative"
    if "duration" in name or "timedelta" in name:
        return "quantitative"
    return None


def _kind_from_values(values: Iterable[object]) -> str:
    seen = [v for v in values if v is not None]
    if not seen:
        return "nominal"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return "quantitative"
    if all(isinstance(v, str) and _ISO_DATE.match(v) for v in seen):
        return "temporal"
    return "nominal"


def _from_records(
    records: Iterable[Mapping[str, Any]], dtypes: Mapping[str, object] | None = None
) -> Table:
    rows: list[dict[str, Any]] = []
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise TypeError("chart data rows must be mappings of column name to value")
        row: dict[str, Any] = {}
        for key, value in record.items():
            name = str(key)
            if name not in seen:
                seen.add(name)
                columns.append(name)
            row[name] = cell(value)
        rows.append(row)
    kinds: dict[str, str] = {}
    for name in columns:
        from_dtype = _kind_from_dtype(dtypes[name]) if dtypes and name in dtypes else None
        kinds[name] = from_dtype or _kind_from_values(row.get(name) for row in rows)
    return Table(rows, columns, kinds)


_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_TAB_SUFFIXES = (".tsv", ".tab")


def _column_values(values: list[str]) -> list[object]:
    """One CSV column: empty cells are gaps; a column whose every other cell
    is a number becomes numbers (integers stay integers)."""
    present = [v for v in values if v != ""]
    if present and all(_NUMBER.match(v.strip()) for v in present):
        out: list[object] = []
        for v in values:
            text = v.strip()
            if not text:
                out.append(None)
            elif re.fullmatch(r"[+-]?\d+", text):
                out.append(int(text))
            else:
                out.append(cell(float(text)))
        return out
    return [v if v != "" else None for v in values]


def from_csv(text: str, *, delimiter: str | None = None) -> Table:
    """A :class:`Table` from CSV or TSV text with a header row. Without a
    ``delimiter``, a header with tabs and no commas reads as TSV."""
    if delimiter is None:
        header = text.lstrip("\ufeff").split("\n", 1)[0]
        delimiter = "\t" if "\t" in header and "," not in header else ","
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter)
    records = [row for row in reader if row]
    if not records:
        return Table([], [], {})
    header_row, body = records[0], records[1:]
    columns: list[str] = []
    for i, name in enumerate(header_row):
        name = name.strip() or f"column_{i + 1}"
        while name in columns:
            name = f"{name}_{i + 1}"
        columns.append(name)
    width = len(columns)
    for line, row in enumerate(body, start=2):
        if len(row) > width:
            raise ValueError(f"CSV line {line} has {len(row)} fields; the header names {width}")
    by_column = [
        _column_values([row[i] if i < len(row) else "" for row in body]) for i in range(width)
    ]
    rows = [{name: by_column[c][r] for c, name in enumerate(columns)} for r in range(len(body))]
    kinds = {name: _kind_from_values(row[name] for row in rows) for name in columns}
    return Table(rows, columns, kinds)


def _read_delimited(data: object) -> Table | None:
    """CSV or TSV given as text, bytes, a path or an open file; None for
    anything else."""
    if isinstance(data, os.PathLike) or (isinstance(data, str) and "\n" not in data):
        path = os.fspath(data)
        if not isinstance(path, str):
            return None
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
        tab = path.lower().endswith(_TAB_SUFFIXES)
        return from_csv(text, delimiter="\t" if tab else ",")
    if isinstance(data, str):
        return from_csv(data)
    if isinstance(data, (bytes, bytearray)):
        return from_csv(bytes(data).decode("utf-8-sig"))
    read = getattr(data, "read", None)
    if callable(read):
        content = read()
        if isinstance(content, (bytes, bytearray)):
            content = bytes(content).decode("utf-8-sig")
        if isinstance(content, str):
            name = getattr(data, "name", "")
            tab = isinstance(name, str) and name.lower().endswith(_TAB_SUFFIXES)
            return from_csv(content, delimiter="\t" if tab else None)
    return None


def _module(obj: object) -> str:
    return type(obj).__module__.split(".")[0]


def to_table(data: object) -> Table:
    """A :class:`Table` from a pandas, Polars or Arrow table, a list of row
    mappings, a mapping of column name to values, or CSV: text with a line
    break, bytes, an open file, or a path (a ``str`` without a line break or
    an ``os.PathLike``; ``.tsv`` and ``.tab`` files read as TSV)."""
    module = _module(data)
    if module == "pandas" and hasattr(data, "to_dict") and hasattr(data, "dtypes"):
        frame: Any = data
        dtypes = {str(name): dtype for name, dtype in frame.dtypes.items()}
        return _from_records(frame.to_dict(orient="records"), dtypes)
    if module == "polars" and hasattr(data, "to_dicts"):
        frame = data
        dtypes = {str(name): dtype for name, dtype in frame.schema.items()}
        return _from_records(frame.to_dicts(), dtypes)
    if module == "pyarrow" and hasattr(data, "to_pylist") and hasattr(data, "schema"):
        table: Any = data
        dtypes = {str(f.name): f.type for f in table.schema}
        return _from_records(table.to_pylist(), dtypes)
    if isinstance(data, Mapping):
        columns = {str(k): list(v) for k, v in data.items()}
        lengths = {len(v) for v in columns.values()}
        if len(lengths) > 1:
            raise ValueError("chart data columns must all have the same length")
        count = lengths.pop() if lengths else 0
        return _from_records(
            {name: values[i] for name, values in columns.items()} for i in range(count)
        )
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        return _from_records(data)
    delimited = _read_delimited(data)
    if delimited is not None:
        return delimited
    raise TypeError(
        "chart data must be a pandas, Polars or pyarrow table, a list of row "
        "mappings, a mapping of column name to values, or CSV (text, bytes, a "
        "path or an open file)"
    )


def systematic_sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Every k-th row, keeping the first and the last, in order.

    Deterministic (the same data gives the same chart on every run) and
    shape-preserving for a sorted series, unlike a random draw.
    """
    if len(rows) <= limit:
        return rows
    if limit < 2:
        return rows[:limit]
    step = (len(rows) - 1) / (limit - 1)
    return [rows[min(len(rows) - 1, round(i * step))] for i in range(limit)]
