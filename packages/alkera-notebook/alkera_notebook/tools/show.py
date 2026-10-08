"""``notebook.show_output``: one cell's output, shaped for the chat to draw.

``notebook.output`` is what an agent reads; this is what it shows a person.
The result names one kind of output and carries it the way the chat's own
renderers take it: a table as columns and rows (the grid a SQL result uses,
with the whole table stored as a rows blob when it has more rows than the
chat shows), a chart as its spec in the Alkera chart profile, an image by the
hash the notebook stores it under, Markdown as its text. Nothing runs: it
reads the outputs the notebook already holds.
"""

from __future__ import annotations

import hashlib
from typing import Any

from alkera_notebook.cell_names import cell_display_name
from alkera_notebook.outputs import chart_copy
from alkera_notebook.outputs.state import CHART_MIMES
from alkera_notebook.tools.functions import (
    OUTPUT_ERROR_CHARS,
    TABLE_COLUMNS,
    error_info,
    table_columns,
)
from alkera_notebook.tools.models import (
    NotebookShownImage,
    NotebookShownTable,
    NotebookShowOutputInput,
    NotebookShowOutputOutput,
    NotebookStoredChart,
    ShownKind,
)
from alkera_notebook.tools.paging import (
    clip_text,
    clip_value,
    fit_prefix,
    json_bytes,
    measure,
    result_budget,
    spill_table,
)
from alkera_notebook.tools.port import (
    CellRecord,
    NotebookPort,
    NotebookToolError,
    OutputDetailRecord,
)
from alkera_notebook.tools.untrusted import wrap

#: The order ``auto`` picks in: what a person most needs to see first.
SHOW_ORDER: tuple[ShownKind, ...] = ("error", "chart", "table", "image", "markdown", "text")

#: The most characters of Markdown or text shown in the chat.
SHOW_TEXT_CHARS = 8_000

#: The most rows of a large table stored whole for the chat's reference.
STORED_ROWS_CAP = 50_000

#: The rows read from the kernel per request when storing a table.
_FRAME_PAGE = 1_000

#: Room kept free in a result for the stored table's handle and the guide.
_SLACK = 1_024


def _source_name(table: dict[str, Any]) -> str | None:
    source = table.get("source")
    name = source.get("name") if isinstance(source, dict) else None
    return name if isinstance(name, str) else None


def available_kinds(cell: CellRecord, detail: OutputDetailRecord) -> list[ShownKind]:
    """Every kind of output ``cell`` holds, in the order ``auto`` picks."""
    kinds = cell.output.kinds if cell.output is not None else []
    markdown = bool(detail.text) and (cell.kind == "markdown" or "text/markdown" in kinds)
    held: dict[ShownKind, bool] = {
        "error": detail.error is not None,
        "chart": detail.chart_spec is not None,
        "table": isinstance(detail.table, dict),
        "image": bool(detail.images),
        "markdown": markdown,
        "text": bool(detail.text) and not markdown,
    }
    return [kind for kind in SHOW_ORDER if held[kind]]


async def _all_rows(
    port: NotebookPort, table: dict[str, Any], total: int
) -> tuple[list[str], list[list[Any]]] | None:
    """The table's rows from the frame the kernel holds, up to the cap; ``None``
    when the kernel no longer holds it."""
    name = _source_name(table)
    if name is None:
        return None
    columns: list[str] = []
    rows: list[list[Any]] = []
    want = min(total, STORED_ROWS_CAP)
    while len(rows) < want:
        try:
            frame = await port.frame(
                name,
                offset=len(rows),
                limit=min(_FRAME_PAGE, want - len(rows)),
                sort=None,
                filter_sql=None,
            )
        except NotebookToolError:
            return None
        columns = list(frame.columns)
        if not frame.rows:
            break
        rows.extend(list(r) for r in frame.rows)
    return columns, rows


async def _table(
    port: NotebookPort,
    inp: NotebookShowOutputInput,
    base: NotebookShowOutputOutput,
    table: dict[str, Any],
    author: str,
) -> NotebookShowOutputOutput:
    preview = [list(r) for r in table.get("rows") or []]
    total = int(table.get("total_rows") or len(preview))
    columns = table_columns(table)
    whole = (
        await _all_rows(port, table, total)
        if total > len(preview) or total > inp.row_limit
        else None
    )
    if whole is not None:
        columns, everything = whole
    else:
        everything = preview
    width = len(columns)
    shown = [[clip_value(v) for v in row[:TABLE_COLUMNS]] for row in everything[: inp.row_limit]]
    room = result_budget() - measure(base) - _SLACK

    def build(k: int) -> NotebookShownTable:
        return NotebookShownTable(
            columns=columns[:TABLE_COLUMNS],
            rows=wrap(shown[:k], author),
            shown_rows=k,
            total_rows=total,
            total_columns=width,
        )

    k = fit_prefix(
        len(shown), lambda k: json_bytes(build(k).model_dump()) <= room, floor=min(1, len(shown))
    )
    result = base.model_copy(update={"table": build(k)})
    if k >= total:
        return result
    note = f"Showing {k} of {total} rows."
    ref = spill_table(columns, everything) if len(everything) > k else None
    if ref is None:
        return result.model_copy(update={"note": note})
    stored = len(everything)
    note += (
        " The whole table is stored with this result."
        if stored >= total
        else f" The first {stored} rows are stored with this result."
    )
    return result.model_copy(
        update={
            "note": note,
            "blob": ref,
            "ref_type": "rows",
            "result_name": f"{base.cell_name}, {stored} rows",
        }
    )


def chart_summary(spec: dict[str, Any]) -> str:
    """What a chart shows, in one line an agent can read without its rows:
    the mark, the title, each encoded channel's field and the rows drawn."""
    mark = spec.get("mark")
    mark_type = mark.get("type") if isinstance(mark, dict) else mark
    title = spec.get("title")
    title_text = title.get("text") if isinstance(title, dict) else title
    encoding = spec.get("encoding")
    channels = [
        f"{channel}={value['field']}"
        for channel, value in (encoding.items() if isinstance(encoding, dict) else ())
        if isinstance(value, dict) and isinstance(value.get("field"), str)
    ]
    datasets = spec.get("datasets")
    data = spec.get("data")
    tables = list(datasets.values()) if isinstance(datasets, dict) else []
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        tables.append(data["values"])
    rows = sum(len(t) for t in tables if isinstance(t, list))
    parts = [
        f"{mark_type} chart" if isinstance(mark_type, str) else "chart",
        f"titled {title_text!r}" if isinstance(title_text, str) and title_text else "",
        f"({', '.join(channels)})" if channels else "",
        f"drawing {rows} rows",
    ]
    return " ".join(p for p in parts if p)


def _chart(base: NotebookShowOutputOutput, spec: Any, author: str) -> NotebookShowOutputOutput:
    """A chart small enough to carry goes inline. A larger one is named by the
    file the notebook keeps its spec in, which the chat draws from, with a
    one-line summary for the agent: no chart is too large to show."""
    stored = chart_copy(CHART_MIMES[0], spec)
    if stored is None:
        return base.model_copy(update={"chart_spec": wrap(spec, author)})
    return base.model_copy(
        update={
            "chart_ref": NotebookStoredChart(sha256=stored.sha256, bytes=len(stored.data)),
            "chart_summary": wrap(chart_summary(spec), author),
        }
    )


def _image(
    base: NotebookShowOutputOutput, detail: OutputDetailRecord, index: int
) -> NotebookShowOutputOutput:
    total = len(detail.images)
    if index >= total:
        raise NotebookToolError(
            "image_not_found",
            f"{base.cell_name} has {total} {'image' if total == 1 else 'images'}; "
            f"image {index} is not one of them (the first is 0).",
        )
    image = detail.images[index]
    shown = NotebookShownImage(
        index=index,
        total=total,
        mime=image.mime,
        bytes=len(image.data),
        sha256=hashlib.sha256(image.data).hexdigest(),
    )
    return base.model_copy(update={"image": shown})


def _text(
    base: NotebookShowOutputOutput, field: str, text: str, author: str
) -> NotebookShowOutputOutput:
    shown = clip_text(text, SHOW_TEXT_CHARS)
    update: dict[str, Any] = {field: wrap(shown, author)}
    if len(shown) < len(text):
        update["note"] = "The text is cut short here. Open the notebook to read all of it."
    return base.model_copy(update=update)


async def notebook_show_output(
    port: NotebookPort, inp: NotebookShowOutputInput, path: str
) -> NotebookShowOutputOutput:
    view = await port.read([inp.cell], include_source=False, include_outputs=True)
    if not view.cells:
        raise NotebookToolError("cell_not_found", f"No cell {inp.cell!r} in {path}.")
    cell = view.cells[0]
    name = cell_display_name(cell.name, cell.index)
    detail = await port.output(cell.id, "all")
    available = available_kinds(cell, detail)
    if not available:
        raise NotebookToolError(
            "no_output", f"{name} has no output to show. Run it with notebook.run first."
        )
    kind: ShownKind = available[0] if inp.part == "auto" else inp.part
    if kind not in available:
        raise NotebookToolError(
            "output_not_found",
            f"{name} has no {kind} output. It has: {', '.join(available)}.",
        )
    author = detail.author or (detail.run.by if detail.run else "")
    base = NotebookShowOutputOutput(
        path=path, cell_id=cell.id, cell_name=name, kind=kind, available=available
    )
    if kind == "error":
        return base.model_copy(
            update={"cell_error": error_info(detail.error, author, OUTPUT_ERROR_CHARS)}
        )
    if kind == "chart":
        return _chart(base, detail.chart_spec, author)
    if kind == "table":
        assert isinstance(detail.table, dict)
        return await _table(port, inp, base, detail.table, author)
    if kind == "image":
        return _image(base, detail, inp.image)
    return _text(base, kind, detail.text, author)


__all__ = ["SHOW_ORDER", "available_kinds", "notebook_show_output"]
