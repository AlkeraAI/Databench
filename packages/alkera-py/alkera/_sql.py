"""``alkera.sql``: a query as a dataframe.

- ``connection="name"``: the statement runs on Alkera's side against the
  workspace's connection of that name (credentials never enter the kernel);
  the result comes back as Arrow (or ``rows.json`` when no Arrow reader is
  installed) and becomes a Polars or pandas frame, per the notebook's
  ``dataframe`` setting.
- no connection: DuckDB runs here, over the frames the query names.
- ``engine=obj``: the query runs through that object's DB-API.

The whole result is returned; when the notebook's ``sql_row_limit`` cut it,
the cell shows a notice saying so.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import inspect
import io
import json
import math
import os
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from alkera.ui._runtime import available, host, optional

DATAFRAME_LIBRARIES = ("polars", "pandas")
CODEC_STREAM = "arrow.ipc.stream"
CODEC_FILE = "arrow.ipc.file"
CODEC_ROWS = "rows.json"
NOTICE_MIME = "text/markdown"


class Notice:
    """A short note in the cell's output (the result was cut, ...)."""

    def __init__(self, text: str) -> None:
        self.text = text

    def _repr_mimebundle_(self, include: Any = None, exclude: Any = None) -> dict[str, str]:
        return {NOTICE_MIME: self.text, "text/plain": self.text}

    def _mime_(self) -> tuple[str, str]:
        return NOTICE_MIME, self.text

    def __repr__(self) -> str:
        return self.text


# ---------------------------------------------------------------- the call


def sql(
    query: str,
    *,
    connection: str | None = None,
    engine: Any = None,
    output: bool = True,
    params: Mapping[str, Any] | Sequence[Any] | None = None,
) -> Any:
    """Runs ``query`` and returns the result as a dataframe."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("alkera.sql needs a query")
    if connection is not None and engine is not None:
        raise ValueError("pass connection= or engine=, not both")
    current = host()
    library = _preferred_library(current)
    notice: str | None = None
    if connection is not None:
        frame, notice = _via_connection(current, query, connection, params, library)
    elif engine is not None:
        frame = _via_engine(engine, query, params, library)
    else:
        frame = _via_duckdb(current, query, params, library, sys._getframe(1))
    if current is not None:
        if notice:
            current.display(Notice(notice))
        if output:
            current.display(frame)
    return frame


def _preferred_library(current: Any) -> str:
    settings = getattr(current, "settings", None)
    chosen: Any = None
    if callable(settings):
        chosen = (settings() or {}).get("dataframe")
    if chosen in DATAFRAME_LIBRARIES:
        return str(chosen)
    # No setting (a script): whichever is installed, Polars first.
    return "polars" if available("polars") or not available("pandas") else "pandas"


# ---------------------------------------------------------------- connection


def _via_connection(
    current: Any, query: str, connection: str, params: Any, library: str
) -> tuple[Any, str | None]:
    if current is None or not hasattr(current, "call"):
        raise RuntimeError(
            "alkera.sql(connection=...) runs inside an Alkera notebook; "
            "outside one, pass engine= a DB-API connection"
        )
    # Ask for the library first: a missing one is an install offer, and no
    # query should run for nothing.
    optional(library)
    request: dict[str, Any] = {"sql": query, "connection": connection}
    if params is not None:
        request["params"] = dict(params) if isinstance(params, Mapping) else list(params)
    result = current.call("sql.execute", request)
    data_dir = getattr(current, "data_dir", None)
    frame = to_frame(result["table"], library, data_dir)
    return frame, result.get("notice") if result.get("truncated") else None


def to_frame(table: Any, library: str, data_dir: str | None) -> Any:
    """A result table (a segment, or a file in ``data_dir``) as a frame. A
    file is deleted once the frame is built."""
    codec = getattr(table, "codec", None)
    if codec == CODEC_ROWS:
        return _rows_frame(_decode_rows(table.data), library)
    if codec == CODEC_STREAM:
        return _arrow_frame(io.BytesIO(table.data), library, stream=True)
    if codec == CODEC_FILE:
        if data_dir is None:
            raise RuntimeError("the result arrived as a file but this kernel has no data directory")
        path = table.path_in(data_dir)
        try:
            return _arrow_frame(path, library, stream=False)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    raise ValueError(f"unexpected result codec {codec!r}")


def _arrow_frame(source: Any, library: str, *, stream: bool) -> Any:
    if library == "polars":
        pl = optional("polars")
        if stream:
            return pl.read_ipc_stream(source)
        return _polars_read_ipc_file(pl, source)
    pa = optional("pyarrow")
    optional("pandas")
    if stream:
        return pa.ipc.open_stream(source).read_pandas()
    with pa.memory_map(source) as mapped:
        return pa.ipc.open_file(mapped).read_pandas()


def _polars_read_ipc_file(pl: Any, source: Any) -> Any:
    """An Arrow IPC file as a Polars frame (Polars reads Arrow itself, no
    pyarrow). Where ``read_ipc`` still takes ``memory_map`` it is asked to map
    the file, whose mapping outlives its name so the file can go once the
    frame exists; releases that dropped the keyword are not given it."""
    try:
        takes_memory_map = "memory_map" in inspect.signature(pl.read_ipc).parameters
    except (TypeError, ValueError):
        takes_memory_map = False
    if takes_memory_map:
        return pl.read_ipc(source, memory_map=True)
    return pl.read_ipc(source)


def _rows_frame(table: tuple[list[str], list[list[Any]]], library: str) -> Any:
    names, rows = table
    if library == "polars":
        pl = optional("polars")
        columns = {f"__c{i}": [row[i] for row in rows] for i in range(len(names))}
        frame = pl.DataFrame(columns, strict=False) if names else pl.DataFrame()
        if len(columns) != len(names):
            raise ValueError("zip() arguments have different lengths")
        return frame.rename(dict(zip(columns, names))) if names else frame
    pd = optional("pandas")
    return pd.DataFrame.from_records(rows, columns=names)


# ---------------------------------------------------------------- rows.json

_DURATION = re.compile(
    r"(?P<sign>-)?P(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?(?:(?P<seconds>[0-9]+(?:\.[0-9]{1,6})?)S)?)?"
)


def _duration(text: str) -> dt.timedelta:
    m = _DURATION.fullmatch(text)
    if m is None:
        raise ValueError(f"{text!r} is not a duration")
    seconds = decimal.Decimal(m["seconds"] or "0")
    delta = dt.timedelta(
        days=int(m["days"] or 0),
        hours=int(m["hours"] or 0),
        minutes=int(m["minutes"] or 0),
        microseconds=int(seconds * 1_000_000),
    )
    return -delta if m["sign"] else delta


_SPECIAL_FLOATS = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}

_SCALARS: dict[str, Any] = {
    "decimal": decimal.Decimal,
    "datetime": dt.datetime.fromisoformat,
    "date": dt.date.fromisoformat,
    "time": dt.time.fromisoformat,
    "timedelta": _duration,
    "bytes": lambda v: base64.b64decode(v, validate=True),
    "uuid": uuid.UUID,
    "int": int,
    "float": lambda v: _SPECIAL_FLOATS[v] if v in _SPECIAL_FLOATS else float(v),
}


def _untag(value: Any) -> Any:
    if isinstance(value, list):
        return [_untag(v) for v in value]
    if isinstance(value, dict):
        tag = value.get("$t")
        if tag == "object":
            return {k: _untag(v) for k, v in value["v"].items()}
        if isinstance(tag, str):
            convert = _SCALARS.get(tag)
            if convert is None:
                raise ValueError(f"unknown value type {tag!r}")
            return convert(value["v"])
        return {k: _untag(v) for k, v in value.items()}
    return value


def _decode_rows(data: bytes) -> tuple[list[str], list[list[Any]]]:
    obj = json.loads(data.decode("utf-8"))
    names = [str(c["name"]) for c in obj["columns"]]
    return names, [[_untag(v) for v in row] for row in obj["rows"]]


# ---------------------------------------------------------------- engine=


def _via_engine(engine: Any, query: str, params: Any, library: str) -> Any:
    raw = engine.raw_connection() if hasattr(engine, "raw_connection") else engine
    cursor = raw.cursor()
    try:
        if params is None:
            cursor.execute(query)
        else:
            cursor.execute(query, params)
        fetch_arrow = getattr(cursor, "fetch_arrow_table", None)
        if callable(fetch_arrow):
            arrow = fetch_arrow()
            if library == "polars":
                return optional("polars").from_arrow(arrow)
            return arrow.to_pandas()
        names = [d[0] for d in cursor.description or []]
        rows = [list(r) for r in cursor.fetchall()]
        return _rows_frame((names, rows), library)
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------- local DuckDB

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FRAME_MODULES = ("polars", "pandas", "pyarrow")


def _frames_in_scope(query: str, caller: Any) -> dict[str, Any]:
    """The frames the query names, from the caller's locals and globals."""
    names = set(_WORD.findall(query))
    scope = {**caller.f_globals, **caller.f_locals}
    found: dict[str, Any] = {}
    for name in names:
        value = scope.get(name)
        module = type(value).__module__.partition(".")[0] if value is not None else ""
        if module in _FRAME_MODULES:
            found[name] = value
    return found


def _via_duckdb(current: Any, query: str, params: Any, library: str, caller: Any) -> Any:
    duckdb = optional("duckdb")
    optional(library)
    conn = duckdb.connect(":memory:")
    register = getattr(current, "register_interruptible", None)
    unregister = getattr(current, "unregister_interruptible", None)
    if callable(register):
        register(conn)
    try:
        for name, value in _frames_in_scope(query, caller).items():
            conn.register(name, value)
        relation = conn.execute(query, params) if params is not None else conn.execute(query)
        return relation.pl() if library == "polars" else relation.df()
    finally:
        if callable(unregister):
            unregister(conn)
        conn.close()
