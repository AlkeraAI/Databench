"""Blob *write* tools — turn a spilled result into something the agent can act on.

- ``blob.materialize`` — write a tabular result to a real FILE in the per-chat
  sandbox (Parquet by default; also CSV / JSON / Arrow). The agent then runs its
  OWN code over the file (``python``, ``duckdb``, …) via bash — the "materialize
  once, then compute with code" pattern. The bytes live in the sandbox, NOT the
  content-addressed blob store.
- ``blob.derive`` — reshape a result (select columns / filter / sort / distinct /
  limit) into a NEW, smaller result blob, via typed params (the SQL-free on-ramp;
  it compiles to a single read-only SELECT over ``result``). Pages like sql.query.
- ``blob.create`` — author a result blob from supplied rows or text, so the agent
  can stash a constructed intermediate and hand its handle to another tool.

``blob.materialize`` / ``blob.create`` are WRITE; their writes are confined to the
per-chat sandbox / the internal blob store, so they're auto-allowed (no prompt) —
the agent's private scratch, not the user's project files. ``blob.derive`` is READ
(it only reads + reshapes; the spilled output is the same internal scratch every
query produces).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal

from alkera_core.json_safe import json_restore
from alkera_core.project.chats.blobs import ChatBlobs
from alkera_core.project.directory import ProjectDirectory, _path_component
from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.files.chat_fs import ChatTree
from alkera_cli.plugins.plugin_base.agent_paths import agent_path
from alkera_cli.plugins.plugin_base.agent_tree import tool_tree
from alkera_cli.plugins.plugin_base.blob_compute import (
    build_rows_result,
    load_result_or_error,
    load_rows_or_error,
    run_read_only_sql,
    to_arrow_table,
    validate_read_only_sql,
)
from alkera_cli.plugins.plugin_base.delivery import (
    NOTE_NO_BLOB_CLAUSE,
    RESULT_NAME_FIELD,
    SqlQueryResult,
)
from alkera_cli.plugins.plugin_base.result_blob import (
    BlobHandle,
    write_rows_blob,
    write_text_blob,
)
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec

#: format → file extension for blob.materialize.
_FORMAT_EXT = {"parquet": ".parquet", "csv": ".csv", "json": ".json", "arrow": ".arrow"}


def _require_sandbox(ctx: ToolContext) -> ChatTree:
    """The per-chat sandbox as the tree a tool writes into. Errors cleanly when
    no chat session is bound (the editor ``tool.call`` / no-session paths).

    A local session's sandbox is a directory of the chat's own records, made
    here on first use; a bounded session's is its working directory, which
    exists before any tool runs and is never made as the daemon (a directory
    the daemon made would not be the chat's to write)."""
    if ctx.sandbox_dir is None:
        raise ToolError("no sandbox is available — this tool must be run from a chat session.")
    sandbox = Path(ctx.sandbox_dir)
    if ctx.fence is None:
        sandbox.mkdir(parents=True, exist_ok=True)
    return tool_tree(ctx, sandbox)


def _sandbox_filename(name: str, *, default_stem: str, ext: str) -> str:
    """Validate a model-supplied filename to a single safe component under the
    sandbox (no path separators, no dot-escapes) and force the format's extension."""
    stem = name.strip() or default_stem
    try:
        stem = _path_component(stem, "filename")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if stem.endswith(ext):
        return stem
    # Drop any other extension the model tacked on, then append the right one.
    return f"{Path(stem).stem}{ext}"


# ---------------------------------------------------------------------------
# blob.materialize
# ---------------------------------------------------------------------------


class BlobMaterializeInput(BaseModel):
    handle: str
    """The ``sha256`` of a result blob — tabular (rows) OR text (a spilled tool
    result, e.g. a large ``web.fetch`` page)."""
    format: Literal["parquet", "csv", "json", "arrow"] = "parquet"
    """Output format for a TABULAR blob. Ignored for a text blob, which is written
    verbatim — name it via ``filename`` with the extension you want (``.md`` /
    ``.json`` / ``.html`` / …); it defaults to ``.txt``."""
    filename: str = Field(
        default="",
        description=(
            "Optional file name (a single name, no path). The right extension is "
            "added automatically; defaults to a name derived from the handle."
        ),
    )


class BlobMaterializeOutput(BaseModel):
    path: str = Field(
        description=(
            "Absolute path to the written file (under the chat sandbox). Run your own code "
            'over it — e.g. `python -c "import pandas as pd; df = pd.read_parquet(...)"`. '
            "Reference it in a reply by its name relative to the working directory."
        )
    )
    format: str
    row_count: int = 0
    """Rows written (tabular blob); 0 for a text blob."""
    char_count: int = 0
    """Characters written (text blob); 0 for a tabular blob."""
    columns: list[str] = Field(default_factory=list)
    bytes: int = 0


class BlobMaterializeTool(Tool[BlobMaterializeInput, BlobMaterializeOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.materialize",
        title="Write a result to a file",
        description=(
            "Write a result (blob handle) to a real FILE in your sandbox so you can "
            "run your own code over it. Works on ANY result blob: a TABULAR blob "
            "(e.g. sql.query rows) writes as Parquet by default (typed, compact — "
            "read with pandas/pyarrow/duckdb; also csv, json, arrow), and a TEXT "
            "blob (a spilled tool result — e.g. a large web.fetch page) writes "
            "verbatim to a .txt file (the format option applies only to tabular "
            "blobs; pass a `filename` with your own extension — page.md, data.json "
            "— to pick another). Returns the absolute file path; then use bash to "
            "run python/duckdb over it. Use this instead of paging a large result "
            "when you need to compute something the built-in tools don't cover."
        ),
        hot=False,
        app="blob",
        effect_hint=Effect.WRITE,
    )
    Input: ClassVar[type[BaseModel]] = BlobMaterializeInput
    Output: ClassVar[type[BaseModel]] = BlobMaterializeOutput

    async def run(self, args: BlobMaterializeInput, ctx: ToolContext) -> BlobMaterializeOutput:
        sandbox = _require_sandbox(ctx)
        env = load_result_or_error(ctx.blobs, args.handle)
        if env.kind == "text":
            return await self._materialize_text(args, env.text, sandbox, ctx)
        rows = json_restore(env.rows)
        columns = list(env.columns)
        ext = _FORMAT_EXT[args.format]
        name = _sandbox_filename(args.filename, default_stem=f"result-{args.handle[:8]}", ext=ext)
        size = await asyncio.to_thread(_write_table_file, args.format, columns, rows, sandbox, name)
        return BlobMaterializeOutput(
            path=agent_path(ctx, sandbox.root / name),
            format=args.format,
            row_count=len(rows),
            columns=columns,
            bytes=size,
        )

    async def _materialize_text(
        self, args: BlobMaterializeInput, text: str, sandbox: ChatTree, ctx: ToolContext
    ) -> BlobMaterializeOutput:
        """Write a text blob's content VERBATIM — a spilled tool result (e.g. a
        large ``web.fetch`` page), which may be any text: JSON, HTML, markdown,
        logs, CSV. The tabular ``format`` doesn't apply, and we deliberately do
        NOT sniff the content to guess a type — the extension is a caller hint:
        whatever the model named the file (via ``filename``), else ``.txt``."""
        name = _sandbox_text_filename(args.filename, default_stem=f"result-{args.handle[:8]}")
        size = await asyncio.to_thread(_write_text_file, text, sandbox, name)
        return BlobMaterializeOutput(
            path=agent_path(ctx, sandbox.root / name),
            format="text",
            char_count=len(text),
            bytes=size,
        )


def _sandbox_text_filename(name: str, *, default_stem: str) -> str:
    """A safe single-component filename for a TEXT blob. Unlike the tabular
    helper, this PRESERVES the caller's own extension (text has no single right
    one — .txt / .json / .md / .html are all valid), defaulting to ``.txt`` when
    the model supplied no name or a bare stem. No content inspection."""
    raw = name.strip()
    stem = raw or f"{default_stem}.txt"
    try:
        stem = _path_component(stem, "filename")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return stem if Path(stem).suffix else f"{stem}.txt"


@contextlib.contextmanager
def _open_sandbox_file(sandbox: ChatTree, name: str) -> Iterator[BinaryIO]:
    """``name`` (directly in the sandbox) open for writing, through nothing the
    agent planted there, handed to the chat.

    The daemon writes as itself (root on a box) into a directory the agent
    writes too, by a name the model chose. A symbolic link the agent left at
    that name would turn the write into one on whatever the link names, on the
    host, and a FIFO there would hold the daemon until a reader came; the chat
    tree opens the name with no link followed and without blocking, at the
    open itself, and refuses anything that is not a plain file, so there is no
    window between a check and the write. The file is the chat's to read and
    write afterwards.
    """
    stack = contextlib.ExitStack()
    try:
        handle = stack.enter_context(sandbox.open_write(name))
    except OSError as exc:
        raise ToolError(
            f"cannot write {name!r} in the sandbox: {exc.strerror or exc}; "
            "the name is taken by something that is not a plain file"
        ) from exc
    with stack:
        yield handle


def _written_size(sandbox: ChatTree, name: str) -> int:
    info = sandbox.stat(name)
    return info.st_size if info is not None else 0


def _write_text_file(text: str, sandbox: ChatTree, name: str) -> int:
    with _open_sandbox_file(sandbox, name) as f:
        f.write(text.encode("utf-8"))
    return _written_size(sandbox, name)


def _write_table_file(
    fmt: str, columns: list[str], rows: list[list[Any]], sandbox: ChatTree, name: str
) -> int:
    if fmt == "json":
        # Lossless {columns, rows} — rebuild with pd.DataFrame(d["rows"], columns=d["columns"]).
        with _open_sandbox_file(sandbox, name) as f:
            f.write(json.dumps({"columns": columns, "rows": rows}, default=str).encode("utf-8"))
        return _written_size(sandbox, name)
    table = to_arrow_table(columns, rows)
    with _open_sandbox_file(sandbox, name) as f:
        if fmt == "parquet":
            import pyarrow.parquet as pq

            pq.write_table(table, f)
        elif fmt == "csv":
            import pyarrow.csv as pacsv

            pacsv.write_csv(table, f)
        elif fmt == "arrow":
            import pyarrow.feather as feather

            feather.write_feather(table, f)
        else:  # pragma: no cover, guarded by the Literal
            raise ToolError(f"unsupported format {fmt!r}")
    return _written_size(sandbox, name)


# ---------------------------------------------------------------------------
# blob.derive
# ---------------------------------------------------------------------------


class BlobDeriveInput(BaseModel):
    handle: str
    """The ``sha256`` of a tabular (rows) result blob."""
    select_columns: list[str] | None = Field(
        default=None, description="Columns to keep (omit for all)."
    )
    where: str | None = Field(
        default=None,
        description='A SQL boolean predicate over the columns (e.g. "amount > 100").',
    )
    order_by: list[str] | None = Field(default=None, description="Columns to sort by.")
    descending: bool = False
    distinct: bool = False
    """Drop duplicate rows (SELECT DISTINCT)."""
    limit: int | None = None
    result_name: str = RESULT_NAME_FIELD


class BlobDeriveTool(Tool[BlobDeriveInput, SqlQueryResult]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.derive",
        title="Reshape a result",
        description=(
            "Derive a NEW, smaller result from a tabular result (blob handle) using "
            "typed params — pick columns (select_columns), filter rows (where), sort "
            "(order_by/descending), drop duplicates (distinct), and cap rows (limit). "
            "Read-only; returns a preview, plus a new blob handle (paged via "
            "fetch_result) when the derived rows exceed the preview. "
            + NOTE_NO_BLOB_CLAUSE
            + " The SQL-free alternative to blob.query."
        ),
        hot=False,
        app="blob",
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = BlobDeriveInput
    Output: ClassVar[type[BaseModel]] = SqlQueryResult

    async def run(self, args: BlobDeriveInput, ctx: ToolContext) -> SqlQueryResult:
        env = load_rows_or_error(ctx.blobs, args.handle)
        sql = _compile_derive_sql(args)
        # Validate the COMPILED sql — a `where`/`order_by` injection (e.g. a trailing
        # "; DROP …") classifies as non-READ and is refused, same as blob.query.
        validate_read_only_sql(sql)
        rows = json_restore(env.rows)
        out_columns, out_rows = await asyncio.to_thread(
            run_read_only_sql, list(env.columns), rows, sql
        )
        return await asyncio.to_thread(
            build_rows_result, ctx.blobs, out_columns, out_rows, result_name=args.result_name
        )


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _compile_derive_sql(args: BlobDeriveInput) -> str:
    # The compiled SQL is validated read-only (validate_read_only_sql) AND runs in a
    # locked-down in-memory DuckDB (no external access), so an injected `where`/
    # `order_by` either classifies as non-READ and is refused or can't escape the
    # result set — same posture as sql.query's _compile_table_query.
    cols = ", ".join(_quote_ident(c) for c in args.select_columns) if args.select_columns else "*"
    sql = f"SELECT {'DISTINCT ' if args.distinct else ''}{cols} FROM result"  # noqa: S608
    if args.where:
        sql += f" WHERE {args.where}"
    if args.order_by:
        order = ", ".join(_quote_ident(c) for c in args.order_by)
        sql += f" ORDER BY {order}{' DESC' if args.descending else ''}"
    if args.limit is not None:
        sql += f" LIMIT {int(args.limit)}"
    return sql


# ---------------------------------------------------------------------------
# blob.create
# ---------------------------------------------------------------------------


class BlobCreateInput(BaseModel):
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    """Provide ``rows`` (+ optional ``columns``) for a tabular blob..."""
    text: str | None = None
    """...or ``text`` for a text blob. Exactly one of rows / text."""
    result_name: str = RESULT_NAME_FIELD


class BlobCreateOutput(BaseModel):
    blob: BlobHandle
    kind: str
    total: int = 0
    """Row count (rows) or character count (text)."""
    result_name: str = ""


class BlobCreateTool(Tool[BlobCreateInput, BlobCreateOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.create",
        title="Create a result blob",
        description=(
            "Author a NEW result blob from data you assembled yourself (not from a "
            "tool result) — pass 'rows' (a list of row arrays, with optional 'columns') "
            "for a tabular result, or 'text' for a text result. Returns a blob handle. "
            "Two uses: stash a constructed intermediate to hand to another tool, OR "
            "PRESENT data to the user — reference the returned handle in your reply as a "
            "markdown link `[label](blob:<handle>)` on its OWN LINE and it renders as an "
            "expandable, paginatable table the user can explore. For data that already "
            "came from a tool, reuse that result's existing handle instead of recreating it."
        ),
        hot=True,
        app="blob",
        effect_hint=Effect.WRITE,
    )
    Input: ClassVar[type[BaseModel]] = BlobCreateInput
    Output: ClassVar[type[BaseModel]] = BlobCreateOutput

    async def run(self, args: BlobCreateInput, ctx: ToolContext) -> BlobCreateOutput:
        if args.text is not None and args.rows is not None:
            raise ToolError("provide either rows or text, not both")
        if args.text is not None:
            handle = await asyncio.to_thread(write_text_blob, ctx.blobs, text=args.text)
            return BlobCreateOutput(
                blob=handle, kind="text", total=len(args.text), result_name=args.result_name
            )
        if args.rows is not None:
            handle = await asyncio.to_thread(
                write_rows_blob, ctx.blobs, columns=list(args.columns or []), rows=args.rows
            )
            return BlobCreateOutput(
                blob=handle, kind="rows", total=len(args.rows), result_name=args.result_name
            )
        raise ToolError("provide rows (+ optional columns) for a tabular blob, or text")


# ---------------------------------------------------------------------------
# blob.delete
# ---------------------------------------------------------------------------


class BlobDeleteInput(BaseModel):
    handle: str
    """The ``sha256`` of the result blob to delete."""


class BlobDeleteOutput(BaseModel):
    deleted: bool
    freed_bytes: int = 0
    """The size of the result this chat let go of."""
    message: str = ""


class BlobDeleteTool(Tool[BlobDeleteInput, BlobDeleteOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="blob.delete",
        title="Delete a result blob",
        description=(
            "Delete a spilled result blob to reclaim space. DELETE LARGE RESULTS YOU "
            "NO LONGER NEED — once deleted, the result is no longer viewable if the "
            "user later scrolls back through this chat's history, so prefer deleting "
            "big intermediate results when you're done with them. The result is "
            "regenerable (re-run the query/tool) if you need it again. Only a result "
            "this chat produced can be deleted."
        ),
        hot=False,
        app="blob",
        effect_hint=Effect.WRITE,
    )
    Input: ClassVar[type[BaseModel]] = BlobDeleteInput
    Output: ClassVar[type[BaseModel]] = BlobDeleteOutput

    async def run(self, args: BlobDeleteInput, ctx: ToolContext) -> BlobDeleteOutput:
        if not ctx.session_id or ctx.alkera_dir is None or not isinstance(ctx.blobs, ChatBlobs):
            raise ToolError("blob.delete must be run from a chat session.")
        try:
            held = ctx.blobs.holds(args.handle)
        except ValueError as exc:
            raise ToolError(f"invalid blob handle: {exc}") from exc
        freed = (
            await asyncio.to_thread(
                _delete_for_chat, ctx.alkera_dir, ctx.blobs, args.handle, ctx.session_id
            )
            if held
            else None
        )
        if freed is None:
            return BlobDeleteOutput(deleted=False, message="Already gone (nothing to delete).")
        return BlobDeleteOutput(
            deleted=True, freed_bytes=freed, message=f"Deleted ({freed} bytes)."
        )


def _delete_for_chat(alkera_dir: Any, blobs: ChatBlobs, sha: str, session_id: str) -> int | None:
    """Let this chat go of a blob it holds, and return its size, or ``None``
    when its bytes were already gone. The bytes are reclaimed only when no other
    chat still names them, by its index or its transcript; the answer is the
    same either way, so it says nothing about any other chat."""
    try:
        size = blobs.size(sha)
    except FileNotFoundError:
        blobs.release(sha)
        return None
    others = ProjectDirectory(alkera_dir, create_if_missing=False).chats()
    blobs.delete(sha, kept_for=others.referenced_blobs(exclude_session_id=session_id))
    return size


def register_blob_write_tools(registry: ToolRegistry) -> None:
    """Register the blob write + lifecycle tools (materialize / derive / create /
    delete). All lazy (found via tool search when the agent needs to act on a result)
    EXCEPT ``blob.create``, which is hot — authoring a blob to present to the user is a
    first-class, frictionless step, so it's always on the surface."""
    registry.register(BlobMaterializeTool)
    registry.register(BlobDeriveTool)
    registry.register(BlobCreateTool)
    registry.register(BlobDeleteTool)


__all__ = [
    "BlobCreateTool",
    "BlobDeleteTool",
    "BlobDeriveTool",
    "BlobMaterializeTool",
    "register_blob_write_tools",
]
