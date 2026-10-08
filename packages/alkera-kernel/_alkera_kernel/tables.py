"""A page of a table, made in one place.

Every table a notebook shows is a *table page*::

    {"schema": [{"name": "day", "type": "Date"}, ...],
     "rows": [["2026-01-01", ...], ...],
     "total_rows": 70,
     "offset": 0}

The first page a cell's output carries (``display``), every later page, and
every sorted or filtered page (``inspect.frame``) are made here by
:func:`table_page`, so they are the same shape with the same values. A page
is plain JSON: it crosses every hop (the engine, the box's post, the backend,
the saved output, an agent's read) unchanged, and nothing after the kernel
encodes a cell again.

``type`` is the frame library's own name for the column's type (Polars
``Date``, ``Decimal(precision=4, scale=2)``, ``Int64``, ``String``; pandas
``int64``, ``object``). A cell is encoded by :func:`encode_cell`, by its
Python type:

==========================  ================================================
value                       in the page
==========================  ================================================
``None``, a missing value   ``null``
``bool``, ``str``           itself
``int``                     itself, or its decimal text past ±(2**53 - 1),
                            which a JSON reader would round
``float``                   itself; ``"NaN"``, ``"Infinity"``,
                            ``"-Infinity"`` for the three JSON cannot spell
``Decimal``                 its text (``"12.50"``), exact
date, datetime, time        ISO 8601 text
``timedelta``               ISO 8601 duration (``"P1DT2H3M4.5S"``)
bytes                       ``"0x"`` and its hex digits
``UUID``                    its text
list, tuple, set            a list of encoded values
dict (a struct)             an object of encoded values, keys as text
a NumPy scalar or array     its Python value, encoded
anything else               ``str(value)``
==========================  ================================================
"""

from __future__ import annotations

import datetime as _dt
import decimal
import math
import sys
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from . import _frames as f

#: The largest integer a JSON reader keeps exactly (an IEEE double's).
MAX_SAFE_INT = 2**53 - 1

Columns = Sequence[tuple[str, str]]


def encode_cell(value: Any) -> Any:
    """One cell as the page carries it (see the module's table)."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INT <= value <= MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return f.format_duration(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): encode_cell(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode_cell(v) for v in value]
    if type(value).__module__.partition(".")[0] == "numpy":
        plain = _numpy_value(value)
        if type(plain) is not type(value):
            return encode_cell(plain)
    return str(value)


def _numpy_value(value: Any) -> Any:
    tolist = getattr(value, "tolist", None)
    if not callable(tolist):
        return value
    try:
        return tolist()
    except Exception:
        return value


def table_page(
    columns: Columns, rows: Iterable[Sequence[Any]], total_rows: int, offset: int = 0
) -> dict[str, Any]:
    """The page holding ``rows`` (raw values, one list per row in column
    order) of a table of ``total_rows`` rows, starting at row ``offset``."""
    return {
        "schema": [{"name": str(name), "type": str(kind)} for name, kind in columns],
        "rows": [[encode_cell(v) for v in row] for row in rows],
        "total_rows": int(total_rows),
        "offset": int(offset),
    }


Keys = list[tuple[str, bool]]
CheckColumns = Callable[[Keys, list[str]], None]


def frame_page(
    frame: object,
    offset: int,
    limit: int,
    *,
    keys: Keys | None = None,
    check: CheckColumns | None = None,
) -> dict[str, Any] | None:
    """A page of a pandas or Polars frame (or series), sorted by ``keys``
    (``(column, descending)`` pairs) and sliced by its own library: nulls
    last, ties kept in the frame's order. ``check`` is given the keys and the
    frame's column names before anything is sorted. ``None`` for any other
    value."""
    keys = keys or []
    pl = sys.modules.get("polars")
    if pl is not None and isinstance(frame, (pl.DataFrame, pl.Series)):
        df = frame.to_frame() if isinstance(frame, pl.Series) else frame
        if check is not None:
            check(keys, [str(c) for c in df.columns])
        if keys:
            df = df.sort(
                [c for c, _ in keys],
                descending=[d for _, d in keys],
                nulls_last=True,
                maintain_order=True,
            )
        part = df.slice(offset, limit)
        columns = [(str(n), str(t)) for n, t in part.schema.items()]
        return table_page(columns, part.rows(), df.height, offset)
    pd = sys.modules.get("pandas")
    if pd is not None and isinstance(frame, (pd.DataFrame, pd.Series)):
        pdf = frame.to_frame() if isinstance(frame, pd.Series) else frame
        labels: dict[str, Any] = {}
        for label in pdf.columns:
            labels.setdefault(str(label), label)
        if check is not None:
            check(keys, list(labels))
        if keys:
            pdf = pdf.sort_values(
                by=[labels[c] for c, _ in keys],
                ascending=[not d for _, d in keys],
                na_position="last",
                kind="stable",
            )
        part = pdf.iloc[offset : offset + limit]
        columns = [(str(c), str(t)) for c, t in zip(part.columns, part.dtypes, strict=True)]
        rows = (
            [_pandas_cell(v, pd.isna) for v in row]
            for row in part.itertuples(index=False, name=None)
        )
        return table_page(columns, rows, len(pdf), offset)
    return None


def _pandas_cell(value: Any, isna: Callable[[Any], Any]) -> Any:
    """A pandas cell with its missing value (``NaN``, ``NaT``, ``NA``) as
    ``None``: pandas spells "no value" as a float ``NaN``."""
    try:
        if isna(value) is True:
            return None
    except (TypeError, ValueError):
        pass
    return value
