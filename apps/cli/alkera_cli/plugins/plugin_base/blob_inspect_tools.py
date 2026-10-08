"""Blob *inspect* tools — operate OVER a large result without paging every row.

A large result spills to a content-addressed blob and the model gets a handle
(``fetch_result`` pages it). Paging the whole thing back into context is exactly
the dump the spill avoids. These tools let the agent compute an *answer* from a
spilled result instead:

- ``blob.info`` — cheap metadata (kind / columns / row count / size) so the model
  can judge relevance before pulling anything.
- ``blob.profile`` — a fixed-size per-column statistical summary (dtype, null /
  distinct counts, min / max, top-k), regardless of row count. "Structure, not the
  answer" — the relevance signal Table-RAG / pandas ``describe()`` surface first.
- ``blob.query`` — read-only DuckDB SQL over the rows (exposed as ``FROM result``),
  returning a NEW (usually tiny) result that pages identically to ``sql.query``.
  This is the "query the result, don't read it" centerpiece — the biggest token win.

All three are READ (no gate) and ``hot`` — a handle can come from *any* tool, so
they must be reachable without a search. ``blob.query`` runs an in-memory DuckDB
with external access disabled (no file / network / extension reach) and refuses any
non-READ statement, so it can't write, exfiltrate, or escape the result set.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import Counter
from typing import Any, ClassVar

from alkera_core.json_safe import json_restore
from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.blob_compute import (
    GONE_MSG,
    build_rows_result,
    load_rows_or_error,
    run_read_only_sql,
    validate_read_only_sql,
)
from alkera_cli.plugins.plugin_base.delivery import (
    NOTE_NO_BLOB_CLAUSE,
    RESULT_NAME_FIELD,
    SqlQueryResult,
)
from alkera_cli.plugins.plugin_base.result_blob import read_envelope
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

#: Distinct values tracked per column in ``blob.profile`` before we stop adding new
#: keys (memory bound for a high-cardinality column). Past it, ``distinct_count`` is
#: reported as unknown (``None``) but ``top_k`` still comes from what we've seen.
_DISTINCT_CAP = 10_000


# ---------------------------------------------------------------------------
# blob.info
# ---------------------------------------------------------------------------


class BlobInfoInput(BaseModel):
    handle: str
    """The ``sha256`` of a result blob (from a tool result's ``blob`` field)."""


class BlobInfoOutput(BaseModel):
    kind: str
    """``"rows"`` (tabular), ``"text"``, or ``"opaque"`` (a non-envelope blob)."""
    columns: list[str] = Field(default_factory=list)
    total: int = 0
    """Total rows (``rows``) or characters (``text``) — so the model knows the size."""
    size_bytes: int = 0
    content_type: str = "application/octet-stream"


class BlobInfoTool(Tool[BlobInfoInput, BlobInfoOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.info",
        title="Inspect a result's shape",
        description=(
            "Cheap metadata for a spilled result (blob handle) WITHOUT fetching its "
            "rows: its kind (rows/text), columns, total row/char count, and size. Use "
            "it to decide whether a result is relevant before paging it — then reach "
            "for blob.profile / blob.query to compute over it, or fetch_result to read it."
        ),
        hot=True,
        app="blob",
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BlobInfoInput
    Output: ClassVar[type[BaseModel]] = BlobInfoOutput

    async def run(self, args: BlobInfoInput, ctx: ToolContext) -> BlobInfoOutput:
        try:
            env = read_envelope(ctx.blobs, args.handle)
            size = ctx.blobs.size(args.handle)
        except FileNotFoundError as exc:
            raise ToolError(GONE_MSG.format(handle=args.handle)) from exc
        except ValueError as exc:
            raise ToolError(f"invalid blob handle: {exc}") from exc
        if env is None:
            return BlobInfoOutput(kind="opaque", total=size, size_bytes=size)
        return BlobInfoOutput(
            kind=env.kind,
            columns=list(env.columns),
            total=env.total,
            size_bytes=size,
            content_type="application/json" if env.kind == "rows" else "text/plain",
        )


# ---------------------------------------------------------------------------
# blob.profile
# ---------------------------------------------------------------------------


class TopValue(BaseModel):
    value: Any = None
    count: int = 0


class ColumnProfile(BaseModel):
    name: str
    dtype: str
    """``int`` / ``float`` / ``bool`` / ``str`` / ``mixed`` / ``empty`` (all-null)."""
    null_count: int = 0
    distinct_count: int | None = None
    """``None`` when the column exceeded the distinct cap (too high cardinality)."""
    distinct_capped: bool = False
    min: Any = None
    max: Any = None
    """Min/max for a homogeneous numeric or string column (NaN excluded); else null."""
    top_k: list[TopValue] = Field(default_factory=list)


class BlobProfileInput(BaseModel):
    handle: str
    """The ``sha256`` of a tabular (rows) result blob."""
    top_k: int = 5
    """How many most-frequent values to report per column (1..50)."""


class BlobProfileOutput(BaseModel):
    row_count: int = 0
    columns: list[ColumnProfile] = Field(default_factory=list)


class BlobProfileTool(Tool[BlobProfileInput, BlobProfileOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.profile",
        title="Profile a tabular result",
        description=(
            "A fixed-size per-column summary of a tabular result (blob handle) — "
            "dtype, null count, distinct count, min/max, and the top-k most frequent "
            "values per column — WITHOUT returning the rows. The output size is the "
            "same whether the result has 50 or 50,000 rows. Use it to understand a "
            "large result's shape and decide which columns matter before querying. "
            'Non-finite numbers appear as {"$nonfinite": "nan"|"inf"|"-inf"}.'
        ),
        hot=True,
        app="blob",
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BlobProfileInput
    Output: ClassVar[type[BaseModel]] = BlobProfileOutput

    async def run(self, args: BlobProfileInput, ctx: ToolContext) -> BlobProfileOutput:
        env = load_rows_or_error(ctx.blobs, args.handle)
        rows = json_restore(env.rows)  # $nonfinite wrappers → real floats for compute
        top_k = max(1, min(args.top_k, 50))
        columns = await asyncio.to_thread(_profile_columns, list(env.columns), rows, top_k)
        return BlobProfileOutput(row_count=len(rows), columns=columns)


# ---------------------------------------------------------------------------
# blob.query
# ---------------------------------------------------------------------------


class BlobQueryInput(BaseModel):
    handle: str
    """The ``sha256`` of a tabular (rows) result blob."""
    sql: str
    """A read-only SQL SELECT over the result, which is exposed as the table
    ``result`` — e.g. ``SELECT region, count(*) FROM result GROUP BY region``."""
    result_name: str = RESULT_NAME_FIELD


class BlobQueryTool(Tool[BlobQueryInput, SqlQueryResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.query",
        title="Query a result with SQL",
        description=" ".join(
            [
                "Run a read-only SQL SELECT over a spilled tabular result instead of",
                "paging every row. The result blob is exposed as the table 'result' —",
                'e.g. "SELECT col, count(*) FROM result WHERE x > 5 GROUP BY col".',
                "Returns a NEW, usually small result (it spills to its own blob handle",
                "when the preview cannot hold every row, paged via fetch_result).",
                NOTE_NO_BLOB_CLAUSE,
                "Read-only: writes / COPY / file or network access are refused",
                "(use blob.materialize to write data out to a file).",
                'Non-finite numbers appear as {"$nonfinite": "nan"|"inf"|"-inf"}.',
            ]
        ),
        hot=True,
        app="blob",
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BlobQueryInput
    Output: ClassVar[type[BaseModel]] = SqlQueryResult

    async def run(self, args: BlobQueryInput, ctx: ToolContext) -> SqlQueryResult:
        env = load_rows_or_error(ctx.blobs, args.handle)
        # Refuse anything that isn't a confident READ. The in-memory DuckDB ALSO
        # disables external access (defense in depth), but keeping blob.query
        # strictly read-only keeps its contract clean + hot.
        validate_read_only_sql(args.sql)
        rows = json_restore(env.rows)
        out_columns, out_rows = await asyncio.to_thread(
            run_read_only_sql, list(env.columns), rows, args.sql
        )
        return await asyncio.to_thread(
            build_rows_result, ctx.blobs, out_columns, out_rows, result_name=args.result_name
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hashable_key(value: Any) -> Any:
    """A hashable stand-in for a cell so unhashable values (nested list/dict from a
    JSON column) can still be counted for distinct/top-k."""
    try:
        hash(value)
    except TypeError:
        return ("\0json", json.dumps(value, sort_keys=True, default=str))
    return value


def _unkey(key: Any) -> Any:
    if isinstance(key, tuple) and len(key) == 2 and key[0] == "\0json":
        try:
            return json.loads(key[1])
        except (json.JSONDecodeError, TypeError):
            return key[1]
    return key


def _infer_dtype(non_null: list[Any]) -> str:
    seen: set[str] = set()
    for v in non_null:
        if isinstance(v, bool):
            seen.add("bool")
        elif isinstance(v, int):
            seen.add("int")
        elif isinstance(v, float):
            seen.add("float")
        elif isinstance(v, str):
            seen.add("str")
        else:
            seen.add("other")
    if not seen:
        return "empty"
    if seen == {"int"}:
        return "int"
    if seen <= {"int", "float"}:
        return "float"
    if seen == {"bool"}:
        return "bool"
    if seen == {"str"}:
        return "str"
    return "mixed"


def _min_max(non_null: list[Any], dtype: str) -> tuple[Any, Any]:
    if dtype in ("int", "float"):
        nums = [
            v
            for v in non_null
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and not (isinstance(v, float) and math.isnan(v))
        ]
        return (min(nums), max(nums)) if nums else (None, None)
    if dtype == "str":
        strs = [v for v in non_null if isinstance(v, str)]
        return (min(strs), max(strs)) if strs else (None, None)
    return (None, None)


def _profile_columns(columns: list[str], rows: list[list[Any]], top_k: int) -> list[ColumnProfile]:
    out: list[ColumnProfile] = []
    for ci, name in enumerate(columns):
        cells = [row[ci] if ci < len(row) else None for row in rows]
        null_count = sum(1 for c in cells if c is None)
        non_null = [c for c in cells if c is not None]
        dtype = _infer_dtype(non_null)
        counter: Counter[Any] = Counter()
        capped = False
        for c in non_null:
            key = _hashable_key(c)
            if key in counter:
                counter[key] += 1
            elif len(counter) < _DISTINCT_CAP:
                counter[key] = 1
            else:
                capped = True
        mn, mx = _min_max(non_null, dtype)
        out.append(
            ColumnProfile(
                name=name,
                dtype=dtype,
                null_count=null_count,
                distinct_count=None if capped else len(counter),
                distinct_capped=capped,
                min=mn,
                max=mx,
                top_k=[TopValue(value=_unkey(k), count=n) for k, n in counter.most_common(top_k)],
            )
        )
    return out


def register_blob_inspect_tools(registry: ToolRegistry) -> None:
    """Register the hot, READ-only blob inspect tools (info / profile / query).
    Always registered (core) so any spilled handle can be inspected + queried."""
    registry.register(BlobInfoTool)
    registry.register(BlobProfileTool)
    registry.register(BlobQueryTool)


__all__ = [
    "BlobInfoTool",
    "BlobProfileTool",
    "BlobQueryTool",
    "ColumnProfile",
    "TopValue",
    "register_blob_inspect_tools",
]
