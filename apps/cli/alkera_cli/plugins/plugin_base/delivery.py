"""The delivery door — every model-facing emission is bounded and spilled here.

One door owns the sequence: fit the preview against the real wire cost, spill
the remainder to a handle, and surface buried handles in the shape the GC walk
roots. A store failure degrades along the rows door to a noted success; along
the generic door, where no preview shape is deliverable, it returns a shaped
error that keeps the note. The door applies the system's one inline bound, and
its own output is measured under it.

``deliver_rows`` is the tabular door (the ``SqlQueryResult`` shape every rows
producer returns). ``deliver_generic`` is the catch-all for any other oversized
dispatch result. Pages of a spilled envelope are read back by
``result_blob.paginate`` and re-enter the door through ``fit_page``.

Beside the limit and spill notes rides one more: the conflict notice. A held
folder whose file was changed on the web while the agent was changing it keeps
the agent's bytes as a conflicted copy, and the next tool result that names
that file says where they went (``with_conflict_notices``). The folder's live
sync is registered here by the directory the agent works in.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from alkera_core.project.chats.references import collect_blob_references
from pydantic import BaseModel, Field

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.host.limits import env_count
from alkera_cli.plugins.plugin_base.result_blob import (
    BlobHandle,
    page_meta,
    write_rows_blob,
    write_text_blob,
)
from alkera_cli.plugins.plugin_base.wire import (
    longest_fitting_prefix,
    model_facing_text,
    result_field_bytes,
    result_wire_bytes,
    tool_error_result,
)

if TYPE_CHECKING:
    from alkera_core.project.chats.blobs import BlobStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The inline bound and its two companions. Nothing is lost when they bite — the
# full result is in the blob and pages back through ``fetch_result`` — so what
# they choose is how much of an answer arrives in one call and how much costs a
# round trip. A deployment on a model with a much larger window inlines more;
# one paying per token inlines less.
# ---------------------------------------------------------------------------

#: Above ``PREVIEW_ROW_CAP`` rows OR ``RESULT_INLINE_BYTE_CAP`` bytes the full
#: result spills to a blob. The row cap decides what is worth inlining (context
#: ergonomics); the shared byte cap decides what can inline (the transport bound).
#: A non-positive row cap keeps the default: rows still spill on the byte cap, and
#: "inline every row" is the one reading this door cannot honour.
ENV_PREVIEW_ROW_CAP = "ALKERA_RESULT_PREVIEW_ROWS"
_PREVIEW_ROW_CAP_DEFAULT = 50
PREVIEW_ROW_CAP = (
    env_count(os.environ.get(ENV_PREVIEW_ROW_CAP), default=_PREVIEW_ROW_CAP_DEFAULT)
    or _PREVIEW_ROW_CAP_DEFAULT
)

#: A result larger than this (serialized JSON bytes) spills to a blob and pages
#: back through ``fetch_result``. 64 KiB is ~16k tokens; past that, hand a handle.
#: A non-positive value keeps the default — a door with no inline bound would put
#: an unbounded result straight into the model's context.
ENV_RESULT_INLINE_BYTE_CAP = "ALKERA_RESULT_INLINE_BYTES"
_RESULT_INLINE_BYTE_CAP_DEFAULT = 64 * 1024
RESULT_INLINE_BYTE_CAP = (
    env_count(os.environ.get(ENV_RESULT_INLINE_BYTE_CAP), default=_RESULT_INLINE_BYTE_CAP_DEFAULT)
    or _RESULT_INLINE_BYTE_CAP_DEFAULT
)

#: Bytes of an oversized result kept inline as a preview beside the blob handle
#: (the model fetches the rest via ``fetch_result``). Measured inside the result
#: envelope, so it must stay under the inline cap; a non-positive value keeps the
#: default.
ENV_RESULT_PREVIEW_BYTES = "ALKERA_RESULT_PREVIEW_BYTES"
_RESULT_PREVIEW_BYTES_DEFAULT = 2048
RESULT_PREVIEW_BYTES = min(
    env_count(os.environ.get(ENV_RESULT_PREVIEW_BYTES), default=_RESULT_PREVIEW_BYTES_DEFAULT)
    or _RESULT_PREVIEW_BYTES_DEFAULT,
    RESULT_INLINE_BYTE_CAP,
)


def result_preview(text: str) -> str:
    """The head of a string, bounded by what it will cost inline as a result
    field. JSON escaping doubles every quote and backslash, so a pre-escape
    byte slice could ship well over its budget. The prefix is found against
    the measured field cost."""
    return text[
        : longest_fitting_prefix(
            min(len(text), RESULT_PREVIEW_BYTES),
            lambda n: result_field_bytes("preview", text[:n]) <= RESULT_PREVIEW_BYTES,
        )
    ]


#: The widest handle a spill can produce, used only to measure: a sha256 is
#: always 64 hex chars, and 2**53 bytes outweighs any blob the store can hold,
#: so a preview budgeted against this handle can never be undercut by the real one.
_MEASURE_HANDLE = BlobHandle(sha256="f" * 64, size=2**53)

_SPILL_FAILED_NOTE = (
    "Only the preview rows are available because the full result could not be "
    "stored and cannot be paged. Re-run a read with a tighter query or lower "
    "limit, but do not repeat a write."
)

#: The store-failure clause every rows tool's description carries, worded ONCE
#: so the three siblings cannot drift on the one instruction that recovers a
#: withheld-and-unstorable result.
NOTE_NO_BLOB_CLAUSE = (
    "If truncated is true with no blob, the withheld rows could not be stored; read note."
)

#: One bound for every model-supplied result label. It sits inside the measured
#: result envelope, so an unbounded name could defeat the preview's byte floor;
#: refused at input validation, before any query runs.
RESULT_NAME_MAX = 200

#: Asked of the model on every blob-capable tool, declared ONCE so the bound and
#: the wording cannot drift across the tools that reuse it: a short label for
#: the result, used as its display name in the editor when the rows are large
#: enough to open in a separate view.
RESULT_NAME_FIELD = Field(
    default="",
    max_length=RESULT_NAME_MAX,
    description=(
        "A SHORT, human-friendly name for the result you expect (5 words or fewer, "
        "e.g. "
        "'Q3 revenue by region', 'failed login counts'). Always provide it: when "
        "the result is large it's stored and shown under this name in the editor, "
        "so a good label makes it findable. Describe the result, not the query."
    ),
)


class SqlProvenance(BaseModel):
    """Where a SQL result came from, beside the rows it describes.

    Rendered inline on the result card — never behind a click — and copied onto
    the receipt when the result is promoted. Names only: the connection, the
    credential role the statement ran under, the engine. A secret never rides
    here, and the connector that fills it never sees one.
    """

    connection_id: str = ""
    """The team connection's cloud id when the connection is a leased team
    connection; empty for a connection that exists only on this machine."""
    connection_name: str = ""
    """The connection handle the statement was addressed to."""
    role: str = ""
    """The credential role the statement ran under — the lease's role name
    (``primary`` or a named role), ``per_user`` for a caller's own delegated
    token, empty for a connection with no credential at all."""
    engine: str = ""
    """The engine that ran it (``postgres``, ``tinybird``, ...)."""
    executed_at: str = ""
    """When it ran, as a UTC ISO-8601 timestamp."""
    duration_ms: int | None = None
    """How long the round trip took, in milliseconds."""
    row_count: int = 0
    """How many rows the statement returned (after the caller's limit)."""
    sql: str = ""
    """The statement that ran — placeholders intact, never a bound value."""
    sources: list[SqlProvenance] = Field(default_factory=list)
    """For a result combined from several reads (``data.join``): each source
    read's own provenance — its connection, role, engine, statement and row
    count. Empty for a single statement."""
    join_columns: list[str] = Field(default_factory=list)
    """For a join: the columns its ``JOIN … ON`` / ``USING`` clauses match on,
    as the join statement spells them. Empty for a single statement."""


class SqlQueryResult(BaseModel):
    """The shared tabular result shape every rows producer returns, so the
    editor chip and ``fetch_result`` paging are identical for every producer."""

    columns: list[str] = Field(default_factory=list)
    preview_rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    blob: BlobHandle | None = None
    """Set whenever ``preview_rows`` omits fetched rows; the full
    ``{columns, rows}`` envelope pages via ``fetch_result``. None with a
    non-empty ``note`` when the store write failed."""
    ref_type: str = "rows"
    """The result's shape, for the editor's reference renderer — always ``"rows"``
    for a SQL result, so its chip + inline card render as a table."""
    result_name: str = ""
    """The model-provided label for this result — the editor shows it as the
    name of the spilled-result reference chip (the blob handle is in ``blob``)."""
    cost_warnings: list[str] = Field(default_factory=list)
    """Non-blocking budget warnings (80%-of-cap heads-ups) surfaced this query."""
    job_id: str = ""
    """Set only on a BACKGROUNDED query's immediate return — the job id to inspect
    via ``background_status`` / stop via ``background_cancel``. Empty otherwise."""
    note: str = ""
    """A model-facing message when the result needs one: the "started, you'll be
    notified" notice on a backgrounded query's immediate return, the notice that
    the caller's limit stopped retrieval, or why withheld rows cannot be paged
    after a failed store write. Empty otherwise."""
    provenance: SqlProvenance | None = None
    """Set by ``sql.query`` on every executed statement: which connection, role
    and engine answered, when, how many rows and how long. ``None`` for a
    result that ran no statement (a backgrounded query's immediate return, a
    blob computation)."""


def compose_note(*parts: str) -> str:
    """The one ``note`` a result carries, joined from whichever fragments apply
    (the limit notice, the spill failure)."""
    return " ".join(p for p in parts if p)


def limit_note(limit: int, *, effect: Effect) -> str:
    """Fact-only on a READ (the tool description carries the raise-the-limit
    advice, so composing with the spill note leaves one action named). A mutation
    committed in full regardless of ``limit``, which bounds only the RETURNING
    rows fetched, so its note must forbid the replay a read-shaped note invites."""
    if effect is Effect.READ:
        return f"Retrieval stopped at your limit of {limit} rows and the source has more."
    return (
        f"The statement ran in full and its changes are applied. Only the first "
        f"{limit} RETURNING rows were captured and the rest cannot be re-fetched. "
        "Do not re-run the statement."
    )


def fit_preview(rows: list[list[Any]], *, base: SqlQueryResult) -> list[list[Any]]:
    """The longest prefix of ``rows`` within BOTH caps, at most ``PREVIEW_ROW_CAP``
    rows and a whole ``SqlQueryResult`` that fits inline. A partial candidate
    bounds BOTH envelopes a producer can actually ship — the spill succeeded
    (worst-case handle, no failure note) and the spill failed (failure note, no
    handle) — while the full prefix, which withholds nothing, is measured
    exactly as it ships. Every measure runs through the one serialize + encode
    pair, where a non-finite cell weighs its ``$nonfinite`` wrapper rather than
    the raw ``NaN`` token. An admitted non-empty prefix therefore fits whichever
    way the spill goes; an envelope too wide for even zero rows (a pathological
    column list) is over the cap regardless and rides the generic door."""

    def fits(k: int) -> bool:
        if k == len(rows):
            whole = base.model_copy(update={"preview_rows": rows})
            return result_wire_bytes(whole) <= RESULT_INLINE_BYTE_CAP
        spilled = base.model_copy(
            update={"preview_rows": rows[:k], "truncated": True, "blob": _MEASURE_HANDLE}
        )
        failed = base.model_copy(
            update={
                "preview_rows": rows[:k],
                "truncated": True,
                "note": compose_note(base.note, _SPILL_FAILED_NOTE),
            }
        )
        widest = max(result_wire_bytes(spilled), result_wire_bytes(failed))
        return widest <= RESULT_INLINE_BYTE_CAP

    return rows[: longest_fitting_prefix(min(len(rows), PREVIEW_ROW_CAP), fits)]


def deliver_rows(
    blobs: BlobStore, rows: list[list[Any]], *, base: SqlQueryResult
) -> SqlQueryResult:
    """Complete ``base`` with the fitted preview, the spilled remainder, and the
    composed note. The one sequence every producer of the shared shape runs, so
    the measure, the spill, and the note cannot drift apart. Synchronous on
    purpose, so the async producer can run it in a worker thread."""
    preview = fit_preview(rows, base=base)
    blob: BlobHandle | None = None
    note = base.note
    if len(preview) < len(rows):
        blob, spill_note = _spill_rows_best_effort(blobs, columns=base.columns, rows=rows)
        note = compose_note(note, spill_note)
    return base.model_copy(
        update={
            "preview_rows": preview,
            "truncated": base.truncated or len(preview) < len(rows),
            "blob": blob,
            "note": note,
        }
    )


def _spill_rows_best_effort(
    blobs: BlobStore, *, columns: list[str], rows: list[list[Any]]
) -> tuple[BlobHandle | None, str]:
    """Write the full rows for paging, degrading to ``(None, note)`` when the
    store write fails: the spill sits on the success path after cost has
    settled, so a storage failure must never turn a query that succeeded into
    a crash."""
    try:
        return write_rows_blob(blobs, columns=columns, rows=rows), ""
    except Exception:
        # Exception, never BaseException: a CancelledError must keep cancelling the turn.
        logger.warning(
            "rows spill failed (%d rows); returning the preview only",
            len(rows),
            exc_info=True,
        )
        return None, _SPILL_FAILED_NOTE


class StoreSpill:
    """This door's store, as a tool that shapes its own reply sees it.

    A tool that pages its own lists (the notebook tools, through
    ``alkera_notebook.tools.call_tool``) stores what its size budget cuts
    here, so the agent reads it back exactly as a large SQL result: a list as
    a rows blob, one whole item per row, a text as a text blob, both paged by
    ``fetch_result``. A store failure answers ``None`` and the tool falls back
    to its own paging arguments; it never fails the call."""

    def __init__(self, blobs: BlobStore) -> None:
        self._blobs = blobs

    def rows(self, columns: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
        handle, _note = _spill_rows_best_effort(self._blobs, columns=columns, rows=rows)
        return None if handle is None else handle.model_dump(mode="json")

    def text(self, text: str) -> dict[str, Any] | None:
        try:
            return write_text_blob(self._blobs, text=text).model_dump(mode="json")
        except Exception:
            # Exception, never BaseException: a CancelledError must keep cancelling the turn.
            logger.warning("text spill failed (%d characters)", len(text), exc_info=True)
            return None


class FetchResultOutput(BaseModel):
    """Cursor fields FIRST: when an oversized page rides the generic dispatch
    spill, only the head of the serialized result survives as the preview, and
    the cursor (``next_offset``/``has_more``) must be in it or paging dead-ends.
    Serialization follows this declaration order."""

    offset: int = 0
    limit: int = 0
    total: int = 0
    returned: int = 0
    has_more: bool = False
    next_offset: int | None = None
    kind: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    text: str = ""


def fit_page(page: FetchResultOutput) -> FetchResultOutput:
    """A ``paginate`` window shrunk until the whole page envelope fits inline,
    measured on the exact model the tool returns (through the one serialize +
    encode pair), with a floor of one row. Rows are the smallest unit a rows
    page can ship, so a single row wider than the cap goes out whole and rides
    the generic door, rather than as an empty page whose ``next_offset`` never
    advances. Text pages shrink by character and always fit. The model-facing
    ``fetch_result`` path runs every page through here; the editor RPC reads
    fixed-size pages straight from ``paginate``."""
    if result_wire_bytes(page) <= RESULT_INLINE_BYTE_CAP:
        return page

    def refit(body: dict[str, Any], returned: int) -> FetchResultOutput:
        meta = page_meta(offset=page.offset, limit=page.limit, total=page.total, returned=returned)
        return page.model_copy(update={**body, **meta})

    def fits(candidate: FetchResultOutput) -> bool:
        return result_wire_bytes(candidate) <= RESULT_INLINE_BYTE_CAP

    if page.kind == "rows":
        rows = page.rows
        k = longest_fitting_prefix(
            len(rows),
            lambda n: fits(refit({"rows": rows[:n]}, n)),
            floor=min(1, len(rows)),
        )
        return refit({"rows": rows[:k]}, k)
    text = page.text
    k = longest_fitting_prefix(
        len(text),
        lambda n: fits(refit({"text": text[:n]}, n)),
        floor=min(1, len(text)),
    )
    return refit({"text": text[:k]}, k)


def deliver_generic(blobs: BlobStore, name: str, result: dict[str, Any]) -> dict[str, Any]:
    """The catch-all door for any dispatch result: a result too large to inline
    is written to the blob store as a canonical text envelope and replaced by a
    preview + handle, so no tool can flood the model's context with a dump. The
    replacement obeys the same cap as its entry: the kept note rides as a
    bounded head, and buried references stop where the wrap would outgrow the
    budget.

    Best-effort, never a correctness gate: it sits on the success path, so a
    blob-store failure returns a truncated preview + an error marker rather than
    the full result inline (a multi-MB inline dump would fail the whole turn far
    more destructively). Synchronous on purpose; the async dispatcher runs it in
    a worker thread."""
    try:
        encoded = model_facing_text(result)
    except (TypeError, ValueError):
        return result  # not JSON-serializable (shouldn't happen -- model_dump'd), pass through
    size = len(encoded.encode())
    if size <= RESULT_INLINE_BYTE_CAP:
        return result
    # The wrapped result's note rides EVERY wrap exit, success and failure: it
    # carries the one instruction that must not be lost (a committed mutation's
    # "do not re-run"), and the preview starts at the payload head and cannot be
    # trusted to include it. Head-bounded, because the note is itself part of
    # the wrap the door emits under the cap.
    note = result.get("note")
    kept_note = {"note": result_preview(note)} if isinstance(note, str) and note else {}
    try:
        handle = write_text_blob(blobs, text=encoded)
    except Exception:
        logger.warning(
            "blob spill failed for %s result (%d bytes); returning a preview + error",
            name,
            size,
        )
        return tool_error_result(
            "result too large to deliver and the blob store is unavailable",
            tool=name,
            truncated=True,
            preview=result_preview(encoded),
            **kept_note,
        )
    wrapped: dict[str, Any] = {
        "truncated": True,
        "preview": result_preview(encoded),
        "blob": handle.model_dump(mode="json"),
        # A derived label + type so the editor's reference chip is named even
        # for the catch-all spill (the per-tool `result_name` contract covers
        # the tools the model calls deliberately, e.g. sql.query).
        "name": f"{name} result",
        "ref_type": "text",
        # The catch-all spill is a JSON-encoded tool result — surface its media
        # type so the editor renders it as a JSON tree, not raw text.
        "mime": handle.media_type,
        "tool": name,
        **kept_note,
    }
    return _budget_references(wrapped, result, name)


def _budget_references(
    wrapped: dict[str, Any], result: dict[str, Any], name: str
) -> dict[str, Any]:
    """The wrap buries the original result's own handles inside the text blob's
    bytes, where the GC walk cannot see them. Re-emit them in the walk's handle
    shape — as many as keep the wrap itself under the cap, since the wrap is
    the door's own output and obeys the door's bound."""
    refs = [{"sha256": sha} for sha in sorted(collect_blob_references(result))]
    k = longest_fitting_prefix(
        len(refs),
        lambda n: (
            len(model_facing_text({**wrapped, "references": refs[:n]}).encode())
            <= RESULT_INLINE_BYTE_CAP
        ),
    )
    if k < len(refs):
        logger.warning(
            "dropped %d of %d buried references from an oversized %s wrap",
            len(refs) - k,
            len(refs),
            name,
        )
    return {**wrapped, "references": refs[:k]} if k else wrapped


class ConflictNoticeSource(Protocol):
    """What keeps a folder's unsaid conflicts: a held folder's live sync."""

    def take_conflict_notices(self, mentioned: Iterable[str]) -> list[str]:
        """The sentences owed to a tool call whose arguments hold ``mentioned``,
        each said once."""
        ...


#: The conflict notice sources, by the resolved directory an agent works in.
#: A provider rather than the source itself, so a folder whose sync is replaced
#: (a lease re-taken) is read through whatever holds it now.
_CONFLICT_SOURCES: dict[str, Callable[[], ConflictNoticeSource | None]] = {}
_CONFLICT_SOURCES_LOCK = threading.Lock()


def _root_key(root: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(root))


def register_conflict_notices(
    root: str | os.PathLike[str], source: Callable[[], ConflictNoticeSource | None]
) -> None:
    """Say conflicts in the folder at ``root`` on the tool results of an agent
    working there."""
    with _CONFLICT_SOURCES_LOCK:
        _CONFLICT_SOURCES[_root_key(root)] = source


def unregister_conflict_notices(root: str | os.PathLike[str]) -> None:
    with _CONFLICT_SOURCES_LOCK:
        _CONFLICT_SOURCES.pop(_root_key(root), None)


def _strings(value: Any) -> Iterator[str]:
    """Every string a tool call's arguments carry, however deep."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for inner in value.values():
            yield from _strings(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _strings(inner)


def with_conflict_notices(
    result: dict[str, Any], raw_args: Mapping[str, Any], *, sandbox_dir: Any
) -> dict[str, Any]:
    """``result`` with a line for each conflict on a path its call named.

    The line joins the result's ``note``, the one model-facing message a result
    carries. A call with no working directory, or in a folder nobody holds,
    is answered unchanged; so is one whose source cannot answer, because a
    notice is never worth a tool result.
    """
    if not isinstance(sandbox_dir, (str, os.PathLike)) or not isinstance(result, dict):
        # No directory the call is known to work in, so no folder to ask.
        return result
    with _CONFLICT_SOURCES_LOCK:
        provider = _CONFLICT_SOURCES.get(_root_key(sandbox_dir))
    if provider is None:
        return result
    try:
        source = provider()
        notices = [] if source is None else source.take_conflict_notices(_strings(raw_args))
    except Exception:
        logger.warning("conflict notices for %s could not be read", sandbox_dir, exc_info=True)
        return result
    if not notices:
        return result
    note = result.get("note")
    if isinstance(note, str) or note is None:
        return {**result, "note": compose_note(note or "", *notices)}
    return {**result, "conflict_note": compose_note(*notices)}


def conflict_notice_source(root: str | os.PathLike[str]) -> ConflictNoticeSource | None:
    """What says conflicts for the folder at ``root`` right now, if anything."""
    with _CONFLICT_SOURCES_LOCK:
        provider = _CONFLICT_SOURCES.get(_root_key(root))
    return None if provider is None else provider()


__all__ = [
    "PREVIEW_ROW_CAP",
    "RESULT_INLINE_BYTE_CAP",
    "RESULT_NAME_FIELD",
    "RESULT_NAME_MAX",
    "ConflictNoticeSource",
    "FetchResultOutput",
    "SqlProvenance",
    "SqlQueryResult",
    "StoreSpill",
    "conflict_notice_source",
    "deliver_generic",
    "deliver_rows",
    "fit_page",
    "limit_note",
    "register_conflict_notices",
    "unregister_conflict_notices",
    "with_conflict_notices",
]
