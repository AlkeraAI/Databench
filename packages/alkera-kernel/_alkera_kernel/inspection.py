"""Inspection: variable summaries, ``inspect.value``, ``inspect.frame`` and
completion, on their own thread with deadlines.

Anything that calls into a person's object (``repr``, a property) runs in a
disposable daemon thread with a timeout, so a ``__repr__`` that sleeps holds
up neither the inspection queue, the reader nor an interrupt.
"""

from __future__ import annotations

import array
import functools
import inspect as _inspect
import queue
import re
import rlcompleter
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import _frames as f
from . import display, hooks, tables

if TYPE_CHECKING:
    from .runtime import Kernel

DEADLINE_S = 5.0
SUMMARY_REPR_S = 0.5
SUMMARY_REPR_CHARS = 200
FRAME_ROW_LIMIT = 1000
#: The most keys a frame page may be ordered by.
MAX_SORT_KEYS = 16


class Timeout(Exception):  # noqa: N818 - reported to the caller by name
    pass


def call_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """``fn()`` on a daemon thread; ``Timeout`` if it has not returned in
    time (the thread is abandoned, never joined)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    worker = threading.Thread(target=run, name="alkera-inspect-call", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise Timeout(f"gave up after {timeout} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


#: Objects larger than this (by ``__sizeof__``) are described, never repr'd.
LARGE_OBJECT_BYTES = 1024 * 1024
#: A ``repr`` longer than this is discarded rather than truncated for display.
REPR_OUTPUT_GUARD = 1024 * 1024
_CONTAINERS = (list, tuple, dict, set, frozenset)


def _type_name(obj: object) -> str:
    return type(obj).__name__


def _frame_shape(obj: object) -> str | None:
    """``DataFrame 100 rows, 2 columns`` for pandas and Polars frames and
    series, from attributes that never touch the data."""
    module = type(obj).__module__.partition(".")[0]
    if module not in ("pandas", "polars"):
        return None
    shape = getattr(obj, "shape", None)
    if not isinstance(shape, tuple):
        return None
    if len(shape) == 2:
        return f"{_type_name(obj)} {shape[0]:,} rows, {shape[1]:,} columns"
    if len(shape) == 1:
        return f"{_type_name(obj)} {shape[0]:,} rows"
    return None


def structural(obj: object) -> str | None:
    """A description from type, length and shape only, for the values whose
    ``repr`` could be expensive; None for everything else."""
    if isinstance(obj, (bytes, bytearray)):
        return f"{_type_name(obj)} of {len(obj):,} bytes"
    if isinstance(obj, memoryview):
        return f"memoryview of {obj.nbytes:,} bytes"
    if isinstance(obj, array.array):
        return f"array('{obj.typecode}') of {len(obj):,} items"
    if type(obj).__module__.partition(".")[0] == "numpy" and hasattr(obj, "dtype"):
        shape = getattr(obj, "shape", None)
        if isinstance(shape, tuple) and shape:
            return f"{_type_name(obj)} shape {shape} dtype {getattr(obj, 'dtype', '?')}"
    frame = _frame_shape(obj)
    if frame is not None:
        return frame
    if isinstance(obj, _CONTAINERS):
        return None  # reprlib's own limits keep these bounded
    try:
        size = obj.__sizeof__()
    except Exception:
        size = 0
    if isinstance(size, int) and size > LARGE_OBJECT_BYTES:
        try:
            length = f", {len(obj):,} items"  # type: ignore[arg-type]
        except Exception:
            length = ""
        return f"{_type_name(obj)} of {size:,} bytes{length}"
    return None


class _SummaryRepr(display.BoundedRepr):
    def repr_bytes(self, x: bytes, level: int) -> str:
        return f"<{structural(x)}>"

    def repr_bytearray(self, x: bytearray, level: int) -> str:
        return f"<{structural(x)}>"

    def repr_memoryview(self, x: memoryview, level: int) -> str:
        return f"<{structural(x)}>"

    def repr_instance(self, x: object, level: int) -> str:
        described = structural(x)
        if described is not None:
            return f"<{described}>"
        text = repr(x)
        if len(text) > REPR_OUTPUT_GUARD:
            return f"<{_type_name(x)}: repr over 1 MiB>"
        return text if len(text) <= self.maxother else text[: self.maxother - 1] + "…"


def bounded_repr(obj: object, chars: int, timeout: float) -> str:
    """A repr of at most ``chars`` characters, built bounded: expensive
    values are described structurally, containers through reprlib limits,
    anything else through its own ``repr`` under ``timeout``."""
    described = structural(obj)
    if described is not None:
        return described if len(described) <= chars else described[: chars - 1] + "…"
    shaper = _SummaryRepr()
    shaper.maxstring = shaper.maxother = chars
    shaper.maxlist = shaper.maxtuple = shaper.maxdict = shaper.maxset = (
        6 if chars <= SUMMARY_REPR_CHARS else 100
    )

    def make() -> str:
        text = shaper.repr(obj)
        if len(text) > REPR_OUTPUT_GUARD:
            return f"<{_type_name(obj)}: repr over 1 MiB>"
        return text if len(text) <= chars else text[: chars - 1] + "…"

    try:
        return str(call_with_timeout(make, timeout))
    except Timeout:
        return f"<{_type_name(obj)}: repr took longer than {timeout} s>"
    except Exception as exc:
        return f"<{_type_name(obj)}: repr failed with {type(exc).__name__}>"


def _shape_and_columns(obj: object) -> tuple[list[int] | None, list[str] | None]:
    shape = getattr(obj, "shape", None)
    columns = None
    if isinstance(shape, tuple) and all(isinstance(n, int) for n in shape):
        cols = getattr(obj, "columns", None)
        if cols is not None and not callable(cols):
            try:
                columns = [str(c) for c in list(cols)[:200]]
            except Exception:
                columns = None
        return list(shape), columns
    return None, None


def _size_bytes(obj: object) -> int | None:
    for attr in ("nbytes",):
        value = getattr(type(obj), attr, None)
        if value is not None:
            try:
                n = getattr(obj, attr)
                return int(n) if isinstance(n, int) else None
            except Exception:
                return None
    estimated = getattr(obj, "estimated_size", None)
    if callable(estimated) and "polars" in sys.modules:
        try:
            return int(estimated())
        except Exception:
            return None
    try:
        return sys.getsizeof(obj)
    except Exception:
        return None


# The only shape a filter may take: one SELECT over the registered frame.
_SELECT = re.compile(r"\s*select\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(copy|install|load|attach|detach|pragma|set|reset|call|export|import|create|insert|update|delete|"
    r"drop|alter|checkpoint|vacuum|read_[a-z_]+|glob|getenv|current_setting)\b",
    re.IGNORECASE,
)


def check_filter(sql: str) -> None:
    """Refuse anything but a single plain ``SELECT``: no statements after it,
    no file readers, no extension or attach verbs. DuckDB's own lockdown (no
    external access, configuration locked) is the second wall."""
    stripped = sql.strip().rstrip(";").strip()
    if not _SELECT.match(stripped):
        raise f.RpcError(
            f.ErrorCode.METHOD_SPECIFIC,
            "Only a single SELECT is allowed",
            {"name": "inspect.statement_refused"},
        )
    if ";" in stripped or "--" in stripped or "/*" in stripped:
        raise f.RpcError(
            f.ErrorCode.METHOD_SPECIFIC,
            "Only a single SELECT is allowed",
            {"name": "inspect.statement_refused"},
        )
    if _FORBIDDEN.search(re.sub(r"'(?:[^']|'')*'", "''", stripped)):
        raise f.RpcError(
            f.ErrorCode.METHOD_SPECIFIC,
            "This statement is not allowed",
            {"name": "inspect.statement_refused"},
        )


def sort_keys(sort: object) -> list[tuple[str, bool]]:
    """``(column, descending)`` pairs from a request's ``sort``: a list of
    ``{column, descending}`` objects, or one such object on its own."""
    if sort is None:
        return []
    items = [sort] if isinstance(sort, dict) else sort
    if not isinstance(items, list):
        raise f.RpcError.invalid_params("Sort must be a list of {column, descending}")
    if len(items) > MAX_SORT_KEYS:
        raise f.RpcError.invalid_params(f"Sort takes at most {MAX_SORT_KEYS} columns")
    keys: list[tuple[str, bool]] = []
    for item in items:
        column = item.get("column") if isinstance(item, dict) else None
        if not isinstance(column, str) or not column:
            raise f.RpcError.invalid_params("Each sort key needs a column name")
        descending = item.get("descending", False)
        if not isinstance(descending, bool):
            raise f.RpcError.invalid_params("Descending must be true or false")
        keys.append((column, descending))
    return keys


def check_columns(keys: list[tuple[str, bool]], columns: list[str]) -> None:
    """An ``inspect.unknown_column`` error for the first key ``columns`` lacks."""
    known = set(columns)
    for column, _ in keys:
        if column not in known:
            raise f.RpcError(
                f.ErrorCode.METHOD_SPECIFIC,
                f"There is no column named {column!r}",
                {"name": "inspect.unknown_column", "column": column},
            )


def order_by(keys: list[tuple[str, bool]], columns: list[str]) -> str:
    """`` ORDER BY`` over ``keys``, each a quoted identifier; an
    ``inspect.unknown_column`` error for the first key ``columns`` lacks."""
    check_columns(keys, columns)
    terms = ", ".join(
        '"{}" {}'.format(column.replace('"', '""'), "DESC" if descending else "ASC")
        for column, descending in keys
    )
    return f" ORDER BY {terms}"


class Inspector:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel
        self._queue: queue.Queue[tuple[int, str, dict[str, Any]]] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="alkera-inspection", daemon=True)
        self._thread.start()

    def submit(self, rid: int, method: str, params: dict[str, Any]) -> None:
        self._queue.put((rid, method, params))

    def _loop(self) -> None:
        while True:
            rid, method, params = self._queue.get()
            handler = {
                f.INSPECT_VALUE: self.inspect_value,
                f.INSPECT_FRAME: self.inspect_frame,
                f.COMPLETE: self.complete,
            }[method]
            try:
                result = call_with_timeout(functools.partial(handler, params), DEADLINE_S)
                self.kernel.conn.respond(rid, result)
            except Timeout:
                self.kernel.conn.respond_error(
                    rid, f.RpcError.kernel_busy("The inspection took longer than 5 s")
                )
            except f.RpcError as exc:
                self.kernel.conn.respond_error(rid, exc)
            except Exception as exc:
                self.kernel.conn.respond_error(
                    rid, f.RpcError(f.ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
                )

    # ------------------------------------------------------------------ summaries

    def summary(self, name: str, value: object) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": name,
            "type": f"{type(value).__module__}.{type(value).__qualname__}"
            if type(value).__module__ != "builtins"
            else type(value).__qualname__,
            "repr": bounded_repr(value, SUMMARY_REPR_CHARS, SUMMARY_REPR_S),
        }
        size = _size_bytes(value)
        if size is not None:
            out["size_bytes"] = size
        shape, columns = _shape_and_columns(value)
        if shape is not None:
            out["shape"] = shape
        if columns is not None:
            out["columns"] = columns
        return out

    def _lookup(self, name: object) -> object:
        if isinstance(name, str) and name.startswith(display.SHOWN_PREFIX):
            try:
                return self.kernel.shown_frame(name)
            except KeyError:
                raise f.RpcError(
                    f.ErrorCode.METHOD_SPECIFIC,
                    "That table is no longer in the kernel. Run its cell again.",
                    {"name": "inspect.unknown_name"},
                ) from None
        if not isinstance(name, str) or not name.isidentifier():
            raise f.RpcError.invalid_params("name must be an identifier")
        if name not in self.kernel.globals:
            raise f.RpcError(
                f.ErrorCode.METHOD_SPECIFIC,
                f"{name} is not defined",
                {"name": "inspect.unknown_name"},
            )
        return self.kernel.globals[name]

    def inspect_value(self, params: dict[str, Any]) -> dict[str, Any]:
        value = self._lookup(params.get("name"))
        depth = params.get("depth", 1)
        chars = 2000 if not isinstance(depth, int) else min(20000, 2000 * max(1, depth))
        summary = self.summary(str(params["name"]), value)
        summary["repr"] = bounded_repr(value, chars, 1.0)
        return {"summary": summary}

    # ------------------------------------------------------------------ frames

    def inspect_frame(self, params: dict[str, Any]) -> dict[str, Any]:
        """A page of a frame: ``offset``/``limit`` rows, ordered by ``sort``.
        A pandas or Polars frame is paged and sorted by its own library, so
        a person's environment needs nothing else for it; only a
        ``filter_sql`` (and a frame of any other kind) goes through DuckDB."""
        name = params.get("name")
        frame = self._lookup(name)
        offset = max(0, int(params.get("offset") or 0))
        limit = max(0, min(int(params.get("limit") or 100), FRAME_ROW_LIMIT))
        filter_sql = params.get("filter_sql")
        sort = params.get("sort")
        if not filter_sql:
            page = native_page(frame, sort_keys(sort), offset, limit)
            if page is not None:
                return _answer(page)
        try:
            duckdb = hooks.optional_library("duckdb")
        except ImportError as exc:
            if filter_sql:
                raise f.RpcError(
                    f.ErrorCode.METHOD_SPECIFIC,
                    "Filtering a table needs duckdb in this environment",
                    {"name": "inspect.filter_needs_duckdb"},
                ) from exc
            raise f.RpcError.unavailable(
                "Only pandas and Polars frames can be paged without duckdb"
            ) from exc
        con = duckdb.connect(
            ":memory:",
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "memory_limit": "512MB",
                "threads": 2,
            },
        )
        try:
            con.register("frame", frame)
            con.execute("SET lock_configuration = true")
            source = "frame"
            if filter_sql:
                if not isinstance(filter_sql, str):
                    raise f.RpcError.invalid_params("filter_sql must be text")
                check_filter(filter_sql)
                source = f"({filter_sql.strip().rstrip(';')})"
            keys = sort_keys(sort)
            order = ""
            if keys:
                # Bound, never executed: the names the page's source really has.
                order = order_by(keys, con.sql(f"SELECT * FROM {source} AS t").columns)  # noqa: S608 - checked above
            total = con.execute(f"SELECT count(*) FROM {source} AS t").fetchone()[0]  # noqa: S608 - checked above
            rel = con.execute(f"SELECT * FROM {source} AS t{order} LIMIT {limit} OFFSET {offset}")  # noqa: S608
            columns = [(d[0], str(d[1])) for d in rel.description]
            rows = rel.fetchall()
        except f.RpcError:
            raise
        except Exception as exc:
            raise f.RpcError(
                f.ErrorCode.METHOD_SPECIFIC, str(exc), {"name": "inspect.query_failed"}
            ) from exc
        finally:
            con.close()
        return _answer(tables.table_page(columns, rows, int(total), offset))

    # ------------------------------------------------------------------ completion

    def complete(self, params: dict[str, Any]) -> dict[str, Any]:
        code = params.get("code")
        cursor = params.get("cursor")
        if not isinstance(code, str):
            raise f.RpcError.invalid_params("code must be text")
        if not isinstance(cursor, int):
            cursor = len(code)
        before = code[:cursor]
        m = re.search(r"[A-Za-z_][A-Za-z0-9_\.]*$", before)
        text = m.group(0) if m else ""
        completer = rlcompleter.Completer(self.kernel.globals)
        matches: list[str] = []
        state = 0
        while len(matches) < 200:
            match = completer.complete(text, state)
            if match is None:
                break
            matches.append(match.rstrip("("))
            state += 1
        result: dict[str, Any] = {"matches": sorted(set(matches), key=matches.index)}
        if len(result["matches"]) >= 1:
            doc = self._doc(result["matches"][0])
            if doc:
                result["doc"] = doc
        return result

    def _doc(self, dotted: str) -> str | None:
        import builtins

        parts = dotted.split(".")
        obj: Any = self.kernel.globals.get(parts[0], getattr(builtins, parts[0], None))
        try:
            for part in parts[1:]:
                obj = getattr(obj, part)
        except Exception:
            return None
        if obj is None:
            return None
        lines: list[str] = []
        try:
            lines.append(f"{parts[-1]}{_inspect.signature(obj)}")
        except (TypeError, ValueError):
            pass
        doc = _inspect.getdoc(obj)
        if doc:
            lines.append(doc)
        return "\n\n".join(lines) or None


def native_page(
    frame: object, keys: list[tuple[str, bool]], offset: int, limit: int
) -> dict[str, Any] | None:
    """The table page (``tables.frame_page``) of a pandas or Polars frame,
    sorted and sliced by its own library; ``None`` for any other value. A key
    naming no column is ``inspect.unknown_column``, anything the library
    refuses ``inspect.query_failed``."""
    try:
        return tables.frame_page(frame, offset, limit, keys=keys, check=check_columns)
    except f.RpcError:
        raise
    except Exception as exc:
        raise _query_failed(exc) from exc


def _answer(page: dict[str, Any]) -> dict[str, Any]:
    """``inspect.frame``'s result: the page, as a cell's output carries its
    first one (``total_rows`` beside it for a reader of the count alone)."""
    return {"table": page, "total_rows": page["total_rows"]}


def _query_failed(exc: Exception) -> f.RpcError:
    return f.RpcError(f.ErrorCode.METHOD_SPECIFIC, str(exc), {"name": "inspect.query_failed"})
