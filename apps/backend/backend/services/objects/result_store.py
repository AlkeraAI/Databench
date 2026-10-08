"""The cloud result store: Postgres rows behind the daemon's blob handles.

Execution happens on the customer's machine and its results live there
(``.alkera/blobs/``). This store is only the **promotion target**: what
a *saved* result needs so it survives the machine being destroyed, which is
what makes idle machines affordable.

Storage is Postgres rows, behind the *identical*
:class:`~alkera_core.schemas.objects.BlobHandle` /
:class:`~alkera_core.schemas.objects.ResultBlobEnvelope` shapes the daemon
already writes, so moving to object storage later changes nothing a
client sees and nothing this module's callers say.

Two placements, one reader:

* under :data:`INLINE_LIMIT_BYTES` the whole envelope rides inline in the
  object's ``spec`` and no spill rows exist — the common case, and one row read;
* at or above it the rows are paged into ``object_payload_rows`` at
  :data:`ROWS_PER_PAGE` rows a page, so a maximum-size read touches two pages.

Access is always mediated. The caller reaches rows through
``GET /objects/{id}/rows`` with authorization applied, never a storage URL, and
the object is located from the authenticated principal's org rather than from
anything in the request.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

from alkera_core.models import ObjectPayloadRow, WorkspaceObject
from alkera_core.schemas.objects import BlobHandle, ResultBlobEnvelope
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: Below this, the envelope is stored inline in the object's spec. Mirrors the
#: inline/spill boundary the delivery layer already uses for tool results.
INLINE_LIMIT_BYTES = 64 * 1024
#: Rows per spill page. A read is capped at 1000 rows, so one maximum read
#: spans at most two pages.
ROWS_PER_PAGE = 1000
#: Cells beginning with one of these are read as a formula by every major
#: spreadsheet, so an exported cell is prefixed with a quote.
FORMULA_LEAD = ("=", "+", "-", "@")
#: Control characters a spreadsheet also treats as a formula lead-in.
FORMULA_LEAD_CONTROL = ("\t", "\r")
#: A cell whose whole text is a plain number — integer, negative, decimal or
#: scientific. A number can never be a formula, so quoting one would only turn
#: a summable column into text the moment it holds a negative value.
PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
#: The UTF-8 byte-order mark the download is prefixed with. A spreadsheet
#: opening a bare ``.csv`` assumes the local code page and turns an accented
#: customer name into mojibake; the mark is the one signal every major one
#: honours. It is added where the file leaves the product, not in
#: :func:`to_csv`, so the document itself stays plain text.
CSV_BOM = "\ufeff"


class PayloadTooLargeError(ValueError):
    """The uploaded envelope is larger than the store accepts."""


#: A promoted result is a table an operator will read, not a data lake extract.
MAX_TOTAL_ROWS = 200_000


def envelope_bytes(envelope: ResultBlobEnvelope) -> int:
    """The size the placement decision is made on: the envelope as it would be
    stored, measured the same way for both branches."""
    return len(envelope.model_dump_json().encode())


def _pages(rows: list[list[Any]]) -> Iterator[tuple[int, list[list[Any]]]]:
    for index in range(0, max(len(rows), 1), ROWS_PER_PAGE):
        yield index // ROWS_PER_PAGE, rows[index : index + ROWS_PER_PAGE]


async def store_payload(
    db: AsyncSession,
    *,
    obj: WorkspaceObject,
    envelope: ResultBlobEnvelope,
    handle: BlobHandle,
) -> dict[str, Any]:
    """Persist ``envelope`` for ``obj`` and return the spec fields that
    describe where it went (``inline``, ``payload``, ``total_rows``).

    An upload always replaces whatever was there: a promote that is retried
    must not leave half of an older payload behind.
    """
    if envelope.kind == "rows" and len(envelope.rows) > MAX_TOTAL_ROWS:
        raise PayloadTooLargeError(
            f"a promoted result holds at most {MAX_TOTAL_ROWS} rows, got {len(envelope.rows)}"
        )
    await db.execute(delete(ObjectPayloadRow).where(ObjectPayloadRow.object_id == obj.id))
    total_rows = len(envelope.rows) if envelope.kind == "rows" else 0
    if envelope_bytes(envelope) < INLINE_LIMIT_BYTES:
        return {
            "inline": envelope.model_dump(mode="json"),
            "payload": handle.model_dump(mode="json"),
            "total_rows": total_rows,
            "envelope_columns": list(envelope.columns),
        }
    if envelope.kind != "rows":
        raise PayloadTooLargeError(
            "a text payload over the inline limit has no paged form; promote a table"
        )
    for page, rows in _pages(envelope.rows):
        db.add(
            ObjectPayloadRow(
                object_id=obj.id,
                page=page,
                sha256=handle.sha256,
                size=handle.size,
                media_type=handle.media_type,
                page_rows=rows,
            )
        )
    await db.flush()
    return {
        "inline": None,
        "payload": handle.model_dump(mode="json"),
        "total_rows": total_rows,
        # The producer's own column names, kept on the spec because a spilled
        # payload has no inline envelope to read them off — without them a
        # spilled export would ship a headerless file.
        "envelope_columns": list(envelope.columns),
    }


def _inline_envelope(spec: dict[str, Any]) -> ResultBlobEnvelope | None:
    inline = spec.get("inline")
    if not isinstance(inline, dict):
        return None
    return ResultBlobEnvelope.model_validate(inline)


def envelope_keys(spec: dict[str, Any]) -> list[str]:
    """The payload's own column names, whichever placement holds it; empty
    until a payload has landed."""
    envelope = _inline_envelope(spec)
    if envelope is not None:
        return list(envelope.columns)
    stored = spec.get("envelope_columns")
    return [str(name) for name in stored] if isinstance(stored, list) else []


def _declared(spec: dict[str, Any], pick: str) -> list[str]:
    declared = spec.get("columns")
    if not isinstance(declared, list) or not declared:
        return []
    names: list[str] = []
    for column in declared:
        if not isinstance(column, dict):
            continue
        value = column.get(pick) or column.get("name") or ""
        names.append(str(value))
    return names if any(names) else []


def columns_of(spec: dict[str, Any]) -> list[str]:
    """The column names a reader should show: the author's chosen labels when
    the result has them, else the envelope's own names."""
    return _declared(spec, "label") or envelope_keys(spec)


def keys_of(spec: dict[str, Any]) -> list[str]:
    """The stable column keys behind :func:`columns_of`, in the same order:
    the declared column names when the author declared columns, else the
    envelope's. A chart's encoding binds to these, never to a label."""
    return _declared(spec, "name") or envelope_keys(spec)


async def read_rows(
    db: AsyncSession, *, obj: WorkspaceObject, offset: int, limit: int
) -> tuple[list[str], list[list[Any]], int]:
    """One page of ``obj``'s payload: ``(columns, rows, total)``.

    Reads the inline envelope when there is one and the spill pages otherwise,
    so a caller never has to know which placement it got.
    """
    spec = dict(obj.spec)
    offset = max(offset, 0)
    columns = columns_of(spec)
    envelope = _inline_envelope(spec)
    if envelope is not None:
        rows = envelope.rows[offset : offset + limit] if limit > 0 else []
        return columns, rows, envelope.total or len(envelope.rows)
    total = int(spec.get("total_rows") or 0)
    if limit <= 0 or offset >= total:
        return columns, [], total
    first_page = offset // ROWS_PER_PAGE
    last_page = (offset + limit - 1) // ROWS_PER_PAGE
    pages = (
        await db.execute(
            select(ObjectPayloadRow)
            .where(
                ObjectPayloadRow.object_id == obj.id,
                ObjectPayloadRow.page >= first_page,
                ObjectPayloadRow.page <= last_page,
            )
            .order_by(ObjectPayloadRow.page)
        )
    ).scalars()
    window: list[list[Any]] = []
    for page in pages:
        window.extend(page.page_rows)
    start = offset - first_page * ROWS_PER_PAGE
    return columns, window[start : start + limit], total


async def page_count(db: AsyncSession, *, obj: WorkspaceObject) -> int:
    """How many spill pages ``obj`` has (0 when it is stored inline)."""
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ObjectPayloadRow)
                .where(ObjectPayloadRow.object_id == obj.id)
            )
        ).scalar_one()
    )


def escape_csv_cell(value: object) -> str:
    """One cell, rendered so a spreadsheet reads it as text.

    Rows are stored LLM output, so a cell that begins like a formula is
    prefixed with a quote (the standard defence), applied on the way out rather
    than trusted on the way in. ``None``
    becomes the empty string; everything else is stringified.

    A cell that is nothing but a number is exempt: ``-5`` leads with a formula
    character but is arithmetic no spreadsheet can be tricked by, and quoting it
    lands a text cell in a numeric column that no longer sums.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(FORMULA_LEAD_CONTROL):
        return "'" + text
    if text.startswith(FORMULA_LEAD) and not PLAIN_NUMBER.match(text):
        return "'" + text
    return text


def to_csv(columns: list[str], rows: list[list[Any]]) -> str:
    """``columns`` + ``rows`` as CSV, with every cell escaped (stdlib only)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([escape_csv_cell(name) for name in columns])
    for row in rows:
        writer.writerow([escape_csv_cell(cell) for cell in row])
    return buffer.getvalue()


async def stream_csv(db: AsyncSession, *, obj: WorkspaceObject) -> AsyncIterator[str]:
    """``obj``'s whole payload as CSV, one spill page at a time.

    An export is not a read. A reader pages — a thousand rows at a time, with
    a cap, because they are looking at a screen — but whoever exports wants
    the whole table, and a result promoted out of a warehouse query is as long
    as the query made it. Building it in memory made three copies of that
    table in the process serving everyone else: the rows in a list, the
    document in a string, and the response's own body.

    So it is produced as it is sent. Each spill page is read, rendered and
    handed off before the next is asked for, which makes the memory the export
    costs the size of one page rather than the size of the result — and means
    there is no figure at which the export has to start refusing.
    """
    spec = dict(obj.spec)
    yield to_csv(columns_of(spec), [])
    envelope = _inline_envelope(spec)
    if envelope is not None:
        # The whole envelope is already in the spec that was read to get here,
        # so paging it buys nothing but the same constant-sized writes.
        for start in range(0, len(envelope.rows), ROWS_PER_PAGE):
            yield _rows_csv(envelope.rows[start : start + ROWS_PER_PAGE])
        return
    page = -1
    while True:
        row = (
            (
                await db.execute(
                    select(ObjectPayloadRow)
                    .where(ObjectPayloadRow.object_id == obj.id, ObjectPayloadRow.page > page)
                    .order_by(ObjectPayloadRow.page)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return
        page = row.page
        yield _rows_csv(row.page_rows)


def _rows_csv(rows: list[list[Any]]) -> str:
    """``rows`` as CSV lines, with no header — one page of the document."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow([escape_csv_cell(cell) for cell in row])
    return buffer.getvalue()


async def delete_payload(db: AsyncSession, *, object_id: UUID) -> None:
    """Drop every spill page an object holds."""
    await db.execute(delete(ObjectPayloadRow).where(ObjectPayloadRow.object_id == object_id))


__all__ = [
    "CSV_BOM",
    "FORMULA_LEAD",
    "INLINE_LIMIT_BYTES",
    "MAX_TOTAL_ROWS",
    "PLAIN_NUMBER",
    "ROWS_PER_PAGE",
    "PayloadTooLargeError",
    "columns_of",
    "delete_payload",
    "envelope_bytes",
    "envelope_keys",
    "escape_csv_cell",
    "keys_of",
    "page_count",
    "read_rows",
    "store_payload",
    "stream_csv",
    "to_csv",
]
