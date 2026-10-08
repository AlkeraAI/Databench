"""The notebook tools, one function each, over a :class:`NotebookPort`.

Every function takes the validated input and the port of the notebook the
input names, and returns the tool's output model. None of them gates: the
gate is decided before the function is reached (``alkera_notebook.tools.catalog``),
so a function here is only ever called once its call is allowed.

Every result is bounded the same way (``alkera_notebook.tools.paging``): lists
are pages with a stated total and the call for the next page, long texts are
windows, and no result weighs more than the size budget whatever the notebook
holds.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from alkera_notebook.cell_names import cell_display_name
from alkera_notebook.document.ops import GraphCellSummary, GraphSummary
from alkera_notebook.format import setup_with_runtime
from alkera_notebook.tools.models import (
    InsertCellOp,
    MoveCellOp,
    NotebookCellBrief,
    NotebookCellsInput,
    NotebookCellsOutput,
    NotebookCellState,
    NotebookCreateInput,
    NotebookCreateOutput,
    NotebookEditInput,
    NotebookEditOutput,
    NotebookEnvInput,
    NotebookEnvOutput,
    NotebookErrorInfo,
    NotebookFramePage,
    NotebookGraphCell,
    NotebookGraphInput,
    NotebookGraphOutput,
    NotebookGraphSummary,
    NotebookImage,
    NotebookInspectInput,
    NotebookInspectOutput,
    NotebookKernelInfo,
    NotebookKernelInput,
    NotebookKernelOutput,
    NotebookOp,
    NotebookOutputInput,
    NotebookOutputOutput,
    NotebookOutputSummary,
    NotebookPlanStep,
    NotebookPresence,
    NotebookReadInput,
    NotebookReadOutput,
    NotebookRunBrief,
    NotebookRunInput,
    NotebookRunOutput,
    NotebookSettingsInput,
    NotebookSettingsOutput,
    NotebookVariable,
    NotebookWidgetInput,
    NotebookWidgetOutput,
    NotebookWidgetState,
    RestoreCellOp,
    RunAll,
    RunCells,
    SetCellConfigOp,
    SetCellKindOp,
)
from alkera_notebook.tools.paging import (
    RESULT_BUDGET_BYTES,
    NotebookMore,
    NotebookPage,
    NotebookTextPage,
    clip_text,
    clip_value,
    fit_prefix,
    fits,
    json_bytes,
    measure,
    more_call,
    page_at,
    page_of,
    spill_items,
    text_window,
)
from alkera_notebook.tools.port import (
    CellRecord,
    ErrorRecord,
    NotebookHost,
    NotebookPort,
    NotebookToolError,
    OutputRecord,
    RunRecord,
    ViewRecord,
    WidgetRecord,
)
from alkera_notebook.tools.refs import engine_target
from alkera_notebook.tools.untrusted import clip, display_name, safe_ename, wrap, wrap_text

#: How much of a cell's output text a full read carries per cell; ``notebook.output``
#: reads the rest.
READ_OUTPUT_CHARS = 2_000
#: How much of an error's message an outline carries.
OUTLINE_ERROR_CHARS = 200
#: The longest source line an outline shows as a cell's head.
HEAD_CHARS = 120
#: Defs and refs a cell lists, each; ``names_omitted`` counts the rest.
NAME_CAP = 50
#: The most one cell's source window weighs in a read.
SOURCE_WINDOW_BYTES = 40_000
#: Line numbers a search reports per cell.
MATCH_CAP = 20
#: The largest image whose bytes a result carries inline, and only when the
#: images are what was asked for (``part="image"``).
INLINE_IMAGE_BYTES = 32 * 1024
#: The largest chart spec carried as JSON; a larger one is paged as text.
CHART_INLINE_BYTES = 16 * 1024
#: How much of an error a ``notebook.output`` result carries (message and
#: traceback each, head and tail).
OUTPUT_ERROR_CHARS = 4_000
#: Columns a table or frame page shows when the reader does not choose.
TABLE_COLUMNS = 50
#: What a sensitive widget's value reads as anywhere but the frontend that set it.
REDACTED = "<redacted>"
#: Lists a result carries in part, with a count of the whole.
QUEUE_SHOWN = 20
PRESENCE_SHOWN = 20
STALE_SHOWN = 50
NOTICES_SHOWN = 50
EDIT_GRAPH_CELLS = 50
EDIT_GRAPH_EDGES = 200
GRAPH_ERRORS_SHOWN = 50
GRAPH_NEIGHBOURS_SHOWN = 500
GRAPH_ROOTS_SHOWN = 20
#: The direct upstream and downstream cells a read names for each cell.
LINKS_SHOWN = 20
#: The names an edge carries as the reason it exists.
VIA_SHOWN = 10
ENVS_SHOWN = 50
#: A run lists the steps of its plan up to this many, and every planned cell's
#: state only up to ``RUN_CELLS_SHOWN`` cells; past that it is summarized.
PLAN_SHOWN = 50
RUN_CELLS_SHOWN = 20
RUN_FAILURES_SHOWN = 10
#: Room kept for what a field-by-field fill does not measure (keys, separators).
_SLACK = 512


# ---------------------------------------------------------------------------
# Conversions shared by several tools
# ---------------------------------------------------------------------------


def error_info(error: ErrorRecord | None, author: str, max_chars: int) -> NotebookErrorInfo | None:
    if error is None:
        return None
    evalue, _ = clip(error.evalue, max_chars)
    traceback, _ = clip(error.traceback, max_chars) if error.traceback else ("", False)
    return NotebookErrorInfo(
        ename=safe_ename(error.ename),
        evalue=wrap(evalue, author),
        traceback=wrap_text(traceback, author),
    )


def output_summary(
    output: OutputRecord | None, max_chars: int, *, outline: bool = False
) -> NotebookOutputSummary | None:
    """A cell's output in brief: kinds and size always; the head of its text and
    its error unless ``outline`` (then only the error's class and first words)."""
    if output is None:
        return None
    if outline:
        error = None
        if output.error is not None:
            evalue, _ = clip(output.error.evalue, OUTLINE_ERROR_CHARS)
            error = NotebookErrorInfo(
                ename=safe_ename(output.error.ename), evalue=wrap(evalue, output.author)
            )
        text, cut = None, False
    else:
        error = error_info(output.error, output.author, max_chars)
        clipped, cut = clip(output.text, max_chars)
        text = wrap_text(clipped, output.author)
    return NotebookOutputSummary(
        kinds=list(output.kinds),
        chars=len(output.text),
        text=text,
        error=error,
        truncated=output.truncated or cut,
        has_image=output.has_image,
        has_chart=output.has_chart,
        has_table=output.has_table,
        has_widget=output.has_widget,
    )


def _head(source: str | None) -> str | None:
    if not source:
        return None
    for line in source.splitlines():
        if line.strip():
            return line if len(line) <= HEAD_CHARS else line[: HEAD_CHARS - 1] + "…"
    return None


def cell_state(
    cell: CellRecord,
    *,
    output_chars: int = READ_OUTPUT_CHARS,
    outline: bool = False,
    source: tuple[str, NotebookTextPage] | None = None,
    matches: Sequence[int] = (),
    upstream: Sequence[str] = (),
    downstream: Sequence[str] = (),
) -> NotebookCellState:
    """A cell as a tool reports it. ``outline`` leaves its source and output
    text out (each cell then costs a few hundred bytes whatever it holds);
    ``source`` is the window of source text to carry, when one was asked for."""
    last_run = cell.last_run
    if last_run is not None:
        last_run = last_run.model_copy(update={"by": display_name(last_run.by)})
    text = cell.source or ""
    head = _head(cell.source)
    omitted = max(len(cell.defs) - NAME_CAP, 0) + max(len(cell.refs) - NAME_CAP, 0)
    return NotebookCellState(
        id=cell.id,
        name=cell.name,
        kind=cell.kind,
        index=cell.index,
        status=cell.status,
        defs=list(cell.defs[:NAME_CAP]),
        refs=list(cell.refs[:NAME_CAP]),
        names_omitted=omitted,
        graph_errors=list(cell.graph_errors[:NAME_CAP]),
        upstream=list(upstream[:LINKS_SHOWN]),
        downstream=list(downstream[:LINKS_SHOWN]),
        links_omitted=max(len(upstream) - LINKS_SHOWN, 0) + max(len(downstream) - LINKS_SHOWN, 0),
        lines=len(text.splitlines()),
        head=wrap(head, cell.source_author) if head is not None else None,
        source=wrap(source[0], cell.source_author) if source is not None else None,
        source_page=source[1] if source is not None else None,
        meta=dict(cell.meta),
        matches=list(matches[:MATCH_CAP]),
        output=output_summary(cell.output, output_chars, outline=outline),
        output_outdated=cell.output_outdated,
        last_run=last_run,
    )


def bounded_kernel(kernel: NotebookKernelInfo) -> NotebookKernelInfo:
    queue = kernel.queue
    return kernel.model_copy(update={"queue": list(queue[:QUEUE_SHOWN]), "queue_total": len(queue)})


def bounded_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: clip_value(value) for key, value in settings.items()}


def _presence(view: ViewRecord) -> list[NotebookPresence]:
    return [
        p.model_copy(update={"who": display_name(p.who)}) for p in view.presence[:PRESENCE_SHOWN]
    ]


def _widget_state(widget: WidgetRecord) -> NotebookWidgetState:
    value = REDACTED if widget.sensitive else clip_value(widget.value)
    return NotebookWidgetState(
        model_id=widget.model_id,
        cell_id=widget.cell_id,
        type=widget.type,
        value=wrap(value, None) if value is not None else None,
    )


def run_summary(view: ViewRecord, run: RunRecord, reactivity: str) -> str:
    """One structural sentence about a run's outcome: how many planned cells
    ended in each status, and what the mode did to cells outside the plan."""
    planned = {step.cell_id for step in run.plan}
    by_status = Counter(cell.status for cell in view.cells if cell.id in planned)
    parts = [f"{count} {status}" for status, count in sorted(by_status.items())]
    descendants = sum(1 for step in run.plan if step.reason == "descendant")
    stale = [cell.name for cell in view.cells if cell.status == "stale" and cell.id not in planned]
    sentence = f"Run {run.status}"
    if parts:
        sentence += ": " + ", ".join(parts)
    sentence += "."
    if reactivity == "autorun" and descendants:
        noun = "cell" if descendants == 1 else "cells"
        sentence += (
            f" Autorun re-ran {descendants} dependent {noun} with their last submitted code."
        )
    if stale:
        more = f" and {len(stale) - 20} more" if len(stale) > 20 else ""
        sentence += f" Stale now (not re-run): {', '.join(stale[:20])}{more}."
    return sentence


def resolve_cells(view: ViewRecord, refs: Iterable[str]) -> list[CellRecord]:
    """The cells ``refs`` name, by id or name (every cell of a name), in the
    order named; an unknown ref is refused."""
    by_id = {c.id: c for c in view.cells}
    by_name: dict[str, list[CellRecord]] = {}
    for cell in view.cells:
        by_name.setdefault(cell.name, []).append(cell)
    out: dict[str, CellRecord] = {}
    for ref in refs:
        found = [by_id[ref]] if ref in by_id else by_name.get(ref, []) if ref != "_" else []
        if not found:
            raise NotebookToolError("cell_not_found", f"No cell {ref!r} in this notebook.")
        out.update((c.id, c) for c in found)
    return list(out.values())


# ---------------------------------------------------------------------------
# notebook.read
# ---------------------------------------------------------------------------


def _matcher(inp: NotebookReadInput) -> Callable[[str], bool] | None:
    if inp.search is None:
        return None
    if inp.regex:
        try:
            pattern = re.compile(inp.search, re.IGNORECASE)
        except re.error as exc:
            raise NotebookToolError(
                "invalid_search", f"search is not a valid regex: {exc}"
            ) from exc
        return lambda text: pattern.search(text) is not None
    needle = inp.search.casefold()
    return lambda text: needle in text.casefold()


def _search(
    cells: Sequence[CellRecord], matches: Callable[[str], bool]
) -> tuple[list[CellRecord], dict[str, list[int]]]:
    found: list[CellRecord] = []
    lines: dict[str, list[int]] = {}
    for cell in cells:
        hits: list[int] = []
        for number, line in enumerate((cell.source or "").splitlines()):
            if matches(line):
                hits.append(number)
                if len(hits) >= MATCH_CAP:
                    break
        if hits or matches(cell.name):
            found.append(cell)
            lines[cell.id] = hits
    return found, lines


async def notebook_read(
    port: NotebookPort, inp: NotebookReadInput, path: str
) -> NotebookReadOutput:
    view = await port.read(None, include_source=True, include_outputs=inp.include_outputs)
    selected = resolve_cells(view, inp.cells) if inp.cells is not None else list(view.cells)
    if inp.status is not None:
        wanted = set(inp.status)
        selected = [c for c in selected if c.status in wanted]
    lines: dict[str, list[int]] = {}
    matcher = _matcher(inp)
    if matcher is not None:
        selected, lines = _search(selected, matcher)
    full = inp.include_source if inp.include_source is not None else inp.cells is not None
    label = {c.id: cell_display_name(c.name, c.index) for c in view.cells}
    place = {c.id: c.index for c in view.cells}
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for a, b in dict.fromkeys((await port.graph(None, "both")).edges):
        if a != b and a in label and b in label:
            parents.setdefault(b, []).append(a)
            children.setdefault(a, []).append(b)

    def links(found: dict[str, list[str]], cid: str) -> list[str]:
        return [label[c] for c in sorted(found.get(cid, ()), key=place.__getitem__)]

    def render(cell: CellRecord) -> NotebookCellState:
        above, below = links(parents, cell.id), links(children, cell.id)
        if not full:
            return cell_state(
                cell, outline=True, matches=lines.get(cell.id, ()), upstream=above, downstream=below
            )
        window = text_window(
            cell.source or "",
            unit="line",
            offset=inp.source_offset,
            limit=inp.source_limit,
            max_bytes=SOURCE_WINDOW_BYTES,
            tool="notebook.read",
            args=inp,
            offset_arg="source_offset",
            also={"cells": [cell.id]},
        )
        return cell_state(
            cell, source=window, matches=lines.get(cell.id, ()), upstream=above, downstream=below
        )

    def build(cells: list[NotebookCellState], page: NotebookPage) -> NotebookReadOutput:
        return NotebookReadOutput(
            path=path,
            token=view.token,
            settings=bounded_settings(dict(view.settings)),
            kernel=bounded_kernel(view.kernel),
            total_cells=len(view.cells),
            matched=len(selected),
            page=page,
            cells=cells,
            presence=_presence(view),
        )

    return page_of(
        selected,
        offset=inp.offset,
        limit=inp.limit,
        render=render,
        build=build,
        tool="notebook.read",
        args=inp,
        noun="cells",
    )


# ---------------------------------------------------------------------------
# notebook.create and notebook.edit
# ---------------------------------------------------------------------------


def new_notebook_cells(cells: Sequence[InsertCellOp]) -> list[InsertCellOp]:
    """The cells ``notebook.create`` makes a notebook from: ``cells`` with a
    setup cell that imports the runtime module. The setup cell given gets the
    import as its first line when it lacks it; with none given, a setup cell
    holding only the import goes ahead of the rest. The kernel binds the
    module whatever the file says, so this is what lets the file run under
    plain Python and stock marimo too."""
    found = list(cells)
    for index, cell in enumerate(found):
        if cell.kind == "setup":
            found[index] = cell.model_copy(update={"source": setup_with_runtime(cell.source)})
            return found
    return [InsertCellOp(kind="setup", source=setup_with_runtime("")), *found]


async def notebook_create(
    host: NotebookHost, inp: NotebookCreateInput, path: str
) -> NotebookCreateOutput:
    cells = new_notebook_cells(
        [
            InsertCellOp(kind=cell.kind, source=cell.source, name=cell.name or "_", meta=cell.meta)
            for cell in inp.cells
        ]
    )
    port = await host.create(path, cells, dict(inp.settings))
    view = await port.read(None, include_source=False, include_outputs=False)
    briefs = [
        NotebookCellBrief(id=c.id, name=c.name, kind=c.kind, index=c.index) for c in view.cells
    ]

    def build(shown: list[NotebookCellBrief], page: NotebookPage) -> NotebookCreateOutput:
        return NotebookCreateOutput(
            path=path,
            token=view.token,
            cells=shown,
            total_cells=len(briefs),
            more=page.more,
        )

    return page_of(
        briefs,
        offset=0,
        limit=max(1, len(briefs)),
        render=lambda brief: brief,
        build=build,
        tool="notebook.read",
        args=NotebookReadInput(path=path),
        noun="cells",
    )


_REF_FIELDS = ("cell_id", "after", "before")


def _op_refs(op: NotebookOp) -> list[str]:
    return [ref for f in _REF_FIELDS if isinstance(ref := getattr(op, f, None), str)]


async def resolve_op_cells(port: NotebookPort, ops: Sequence[NotebookOp]) -> list[NotebookOp]:
    """``ops`` with every cell they name by a unique name rewritten to its id.
    An id stays as it is; a name two live cells share is refused; anything
    else (a deleted cell's id for ``restore``) is left for the engine to judge."""
    if not any(_op_refs(op) for op in ops):
        return list(ops)
    view = await port.read(None, include_source=False, include_outputs=False)
    ids = {c.id for c in view.cells}
    by_name: dict[str, list[str]] = {}
    for cell in view.cells:
        if cell.name != "_":
            by_name.setdefault(cell.name, []).append(cell.id)
    resolved: list[NotebookOp] = []
    for index, op in enumerate(ops):
        changes: dict[str, str] = {}
        for field in _REF_FIELDS:
            ref = getattr(op, field, None)
            if not isinstance(ref, str) or ref in ids:
                continue
            if field == "cell_id" and isinstance(op, RestoreCellOp):
                continue
            named = by_name.get(ref, [])
            if len(named) > 1:
                raise NotebookToolError(
                    "ambiguous_cell",
                    f"{len(named)} cells are named {ref!r} ({', '.join(named[:5])}); use an id.",
                    op_index=index,
                )
            if named:
                changes[field] = named[0]
        resolved.append(op.model_copy(update=changes) if changes else op)
    return resolved


def _touched_graph(graph: GraphSummary, touched: Sequence[str]) -> GraphSummary:
    """The document graph narrowed to the cells a batch touched and the edges
    into and out of them."""
    if not graph.computed:
        return graph
    keep = [cid for cid in touched if cid in graph.cells][:EDIT_GRAPH_CELLS]
    wanted = set(keep)
    cells: dict[str, GraphCellSummary] = {}
    for cid in keep:
        info = graph.cells[cid]
        cells[cid] = GraphCellSummary(
            defs=info.defs[:NAME_CAP], refs=info.refs[:NAME_CAP], errors=info.errors[:NAME_CAP]
        )
    edges = [(a, b) for a, b in graph.edges if a in wanted or b in wanted][:EDIT_GRAPH_EDGES]
    return GraphSummary(computed=True, cells=cells, edges=edges)


async def notebook_edit(
    port: NotebookPort, inp: NotebookEditInput, path: str
) -> NotebookEditOutput:
    ops = await resolve_op_cells(port, inp.ops)
    record = await port.apply(ops, inp.base_token)
    touched = list(dict.fromkeys([*(c.id for c in record.cells), *record.created]))
    return NotebookEditOutput(
        path=path,
        token=record.token,
        repeat=record.repeat,
        cells=list(record.cells),
        created=list(record.created),
        notices=[
            n.model_copy(update={"by": display_name(n.by) or None, "message": clip_text(n.message)})
            for n in record.notices[:NOTICES_SHOWN]
        ],
        graph=_touched_graph(record.graph, touched),
        stale=list(record.stale[:STALE_SHOWN]),
        stale_total=len(record.stale),
    )


# ---------------------------------------------------------------------------
# notebook.run
# ---------------------------------------------------------------------------


_FAILED = ("error", "interrupted")


def _run_see(path: str, failures: Sequence[NotebookCellState]) -> list[NotebookMore]:
    if failures:
        return [
            NotebookMore(
                tool="notebook.read",
                args={"path": path, "status": ["error", "interrupted", "skipped"]},
                note="Every planned cell that failed or was skipped, paged.",
            ),
            NotebookMore(
                tool="notebook.output",
                args={"path": path, "cell": failures[0].id, "part": "error"},
                note="The first failure's whole error.",
            ),
        ]
    return [
        NotebookMore(
            tool="notebook.read",
            args={"path": path},
            note="Every cell's status, paged.",
        )
    ]


async def _run_output(
    port: NotebookPort, path: str, run: RunRecord, *, waited: bool
) -> NotebookRunOutput:
    """A run's result as the model reads it: mode, environment, the plan's head,
    and the planned cells (each one for a small plan; counts and the first
    failures for a large one)."""
    planned_ids = [step.cell_id for step in run.plan]
    view = await port.read(None, include_source=False, include_outputs=True)
    reactivity = view.kernel.reactivity
    status: Literal["finished", "running", "needs_confirmation"]
    if run.status == "needs_confirmation":
        status = "needs_confirmation"
    elif run.ended:
        status = "finished"
    else:
        status = "running"
    by_id = {cell.id: cell for cell in view.cells}
    planned = [by_id[cid] for cid in planned_ids if cid in by_id] if waited else []
    small = len(planned_ids) <= RUN_CELLS_SHOWN
    failures = [
        cell_state(c) for c in [c for c in planned if c.status in _FAILED][:RUN_FAILURES_SHOWN]
    ]
    cells = [cell_state(c) for c in planned] if small else []

    def make(n_cells: int, n_failures: int) -> NotebookRunOutput:
        shown = cells[:n_cells]
        listed = failures[:n_failures]
        summarized = waited and (not small or n_cells < len(cells))
        return NotebookRunOutput(
            path=path,
            run_id=run.run_id,
            status=status,
            reactivity=reactivity,
            env=view.kernel.env,
            plan=[
                NotebookPlanStep(cell_id=step.cell_id, name=step.name, reason=step.reason)
                for step in run.plan[:PLAN_SHOWN]
            ],
            plan_total=len(run.plan),
            estimate_s=run.estimate_s,
            counts=dict(Counter(c.status for c in planned)),
            failures=listed,
            cells=shown,
            see=_run_see(path, listed) if summarized else [],
            queued_behind=list(run.queued_behind[:QUEUE_SHOWN]),
            summary=run_summary(view, run, reactivity) if status == "finished" else "",
        )

    n_failures = fit_prefix(len(failures), lambda k: fits(make(0, k)))
    n_cells = fit_prefix(len(cells), lambda k: fits(make(k, n_failures)))
    if waited and planned and (not small or n_cells < len(cells)):
        # Every planned cell, whole, where the host stores large replies; the
        # result names it (the summary above is what fits inline).
        spill_items([cell_state(c) for c in planned])
    return make(n_cells, n_failures)


async def notebook_run(port: NotebookPort, inp: NotebookRunInput, path: str) -> NotebookRunOutput:
    if isinstance(inp.target, RunAll) and inp.target.restart:
        await port.kernel("restart")
    run = await port.run(engine_target(inp.target), confirm_expensive=inp.confirm_expensive)
    if run.status == "needs_confirmation" or run.run_id is None:
        return await _run_output(port, path, run, waited=False)
    if inp.wait and not run.ended:
        run = await port.wait(run.run_id, inp.timeout_s)
    return await _run_output(port, path, run, waited=inp.wait or run.ended)


async def notebook_kernel(
    port: NotebookPort, inp: NotebookKernelInput, path: str
) -> NotebookKernelOutput:
    record = await port.kernel(inp.action)
    return NotebookKernelOutput(
        path=path,
        kernel=bounded_kernel(record.kernel),
        runs=[
            NotebookRunBrief(
                run_id=run.run_id or "",
                by=display_name(run.requested_by.label()) if run.requested_by else "",
                status=run.status,
            )
            for run in record.runs[-QUEUE_SHOWN:]
            if run.run_id is not None
        ],
    )


# ---------------------------------------------------------------------------
# notebook.output
# ---------------------------------------------------------------------------


def _images(
    data: Sequence[Any], inp: NotebookOutputInput, base: NotebookOutputOutput
) -> NotebookOutputOutput:
    inline = inp.part == "image"

    def render(item: tuple[int, Any]) -> NotebookImage:
        index, image = item
        size = len(image.data)
        return NotebookImage(
            index=index,
            mime=image.mime,
            bytes=size,
            sha256=hashlib.sha256(image.data).hexdigest(),
            data_base64=(
                base64.b64encode(image.data).decode("ascii")
                if inline and size <= INLINE_IMAGE_BYTES
                else None
            ),
        )

    return page_of(
        list(enumerate(data)),
        offset=inp.item_offset,
        limit=inp.item_limit,
        render=render,
        build=lambda images, page: base.model_copy(update={"images": images, "images_page": page}),
        tool="notebook.output",
        args=inp,
        offset_arg="item_offset",
        noun="images",
        budget=RESULT_BUDGET_BYTES // 2,
    )


def table_columns(table: dict[str, Any]) -> list[str]:
    """A table output's column names, from its schema or its column list."""
    spec = table.get("schema") or table.get("columns") or []
    return [str(c.get("name", "")) if isinstance(c, dict) else str(c) for c in spec]


async def _table(
    port: NotebookPort, table: Any, inp: NotebookOutputInput, author: str, room: int
) -> tuple[Any, NotebookPage | None]:
    """A page of a table output's rows: from the frame it shows when the kernel
    still holds it (the same paging as ``notebook.inspect frame``), else from
    the rows the output carries."""
    if not isinstance(table, dict):
        return wrap(clip_value(table), author), None
    source = table.get("source")
    name = source.get("name") if isinstance(source, dict) else None
    columns = table_columns(table)
    rows: list[list[Any]] = []
    total = int(table.get("total_rows") or len(table.get("rows") or []))
    if isinstance(name, str):
        try:
            frame = await port.frame(
                name, offset=inp.row_offset, limit=inp.row_limit, sort=None, filter_sql=None
            )
        except NotebookToolError:
            frame = None
        if frame is not None:
            columns, rows, total = list(frame.columns), list(frame.rows), frame.total_rows
    if not rows:
        preview = list(table.get("rows") or [])
        rows = preview[inp.row_offset : inp.row_offset + inp.row_limit]
        if name is None:
            total = len(preview)
    width = len(columns)
    columns = columns[:TABLE_COLUMNS]
    rows = [[clip_value(v) for v in list(row)[:TABLE_COLUMNS]] for row in rows]

    def content(k: int) -> dict[str, Any]:
        return {
            "columns": columns,
            "total_columns": width,
            "rows": rows[:k],
            "total_rows": total,
            "offset": inp.row_offset,
            "source": name,
        }

    k = fit_prefix(len(rows), lambda k: json_bytes(content(k)) <= room, floor=min(1, len(rows)))
    page = page_at(
        offset=inp.row_offset,
        limit=inp.row_limit,
        total=total,
        returned=k,
        tool="notebook.output",
        args=inp,
        offset_arg="row_offset",
        noun="rows",
    )
    return wrap(content(k), author), page


def _chart(
    spec: Any, inp: NotebookOutputInput, author: str, room: int
) -> tuple[Any, NotebookTextPage | None]:
    if spec is None:
        return None, None
    if json_bytes(spec) <= min(CHART_INLINE_BYTES, room):
        return wrap(spec, author), None
    text = json_dumps(spec)
    if inp.part != "chart":
        page = NotebookTextPage(
            unit="char",
            offset=0,
            returned=0,
            total=len(text),
            next_offset=0,
            more=more_call(
                "notebook.output",
                inp,
                "The chart's spec is too large to carry here; read it as text.",
                part="chart",
                text_offset=0,
            ),
        )
        return None, page
    body, page = text_window(
        text,
        unit="char",
        offset=inp.text_offset,
        limit=inp.max_chars,
        max_bytes=room,
        tool="notebook.output",
        args=inp,
        offset_arg="text_offset",
    )
    return wrap(body, author), page


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _moves(order: list[str], cells: Sequence[str], to: str, *, floor: int = 0) -> list[NotebookOp]:
    """The move ops that take each of ``cells`` one step ``up`` or ``down``, or
    to the ``top`` or ``bottom``, keeping their order among themselves. The
    first ``floor`` places stay put (the setup cell is always first); a cell
    already at that end stays."""
    ops: list[NotebookOp] = []
    picked = [cid for cid in order if cid in set(cells) and order.index(cid) >= floor]
    sequence = picked if to in ("up", "bottom") else list(reversed(picked))
    for cid in sequence:
        at = order.index(cid)
        if to == "up" and at > floor and order[at - 1] not in picked:
            ops.append(MoveCellOp(cell_id=cid, before=order[at - 1]))
            order.insert(at - 1, order.pop(at))
        elif to == "down" and at < len(order) - 1 and order[at + 1] not in picked:
            ops.append(MoveCellOp(cell_id=cid, after=order[at + 1]))
            order.insert(at + 1, order.pop(at))
        elif to == "top" and at > floor:
            ops.append(MoveCellOp(cell_id=cid, before=order[floor]))
            order.insert(floor, order.pop(at))
        elif to == "bottom" and at < len(order) - 1:
            ops.append(MoveCellOp(cell_id=cid, after=order[-1]))
            order.append(order.pop(at))
    return ops


async def _cell_ops(port: NotebookPort, inp: NotebookCellsInput) -> list[NotebookOp]:
    """The document ops an edit action of ``notebook.cells`` is."""
    cells = list(inp.cells or [])
    if inp.action in ("enable", "disable"):
        disabled = inp.action == "disable"
        return [SetCellConfigOp(cell_id=cid, config={"disabled": disabled}) for cid in cells]
    if inp.action == "set_kind":
        if inp.kind is None:
            raise NotebookToolError("kind_required", "notebook.cells set_kind needs `kind`.")
        return [SetCellKindOp(cell_id=cid, kind=inp.kind) for cid in cells]
    view = await port.read(None, include_source=True, include_outputs=False)
    if inp.action == "move":
        if inp.to is None:
            raise NotebookToolError("to_required", "notebook.cells move needs `to`.")
        floor = 1 if view.cells and view.cells[0].kind == "setup" else 0
        return _moves([c.id for c in view.cells], cells, inp.to, floor=floor)
    by_id = {c.id: c for c in view.cells}
    return [
        InsertCellOp(
            kind=by_id[cid].kind,
            source=by_id[cid].source or "",
            config=dict(by_id[cid].config),
            meta=dict(by_id[cid].meta),
            after=cid,
        )
        for cid in cells
    ]


async def notebook_cells(
    port: NotebookPort, inp: NotebookCellsInput, path: str
) -> NotebookCellsOutput:
    if inp.action == "clear_outputs":
        cleared = await port.clear_outputs(inp.cells)
        return NotebookCellsOutput(path=path, action=inp.action, changed=cleared)
    if not inp.cells:
        raise NotebookToolError("cells_required", f"notebook.cells {inp.action} needs `cells`.")
    ops = await _cell_ops(port, inp)
    if not ops:
        view = await port.read(None, include_source=False, include_outputs=False)
        return NotebookCellsOutput(path=path, action=inp.action, token=view.token)
    record = await port.apply(ops, None)
    changed = list(record.created) if inp.action == "duplicate" else list(inp.cells)
    return NotebookCellsOutput(
        path=path, action=inp.action, changed=changed, token=record.token, stale=list(record.stale)
    )


async def notebook_output(
    port: NotebookPort, inp: NotebookOutputInput, path: str
) -> NotebookOutputOutput:
    detail = await port.output(inp.cell, inp.part)
    author = detail.author or (detail.run.by if detail.run else "")
    run = detail.run
    if run is not None:
        run = run.model_copy(update={"by": display_name(run.by)})
    result = NotebookOutputOutput(
        path=path,
        cell_id=detail.cell_id,
        error=error_info(detail.error, author, OUTPUT_ERROR_CHARS),
        run=run,
    )
    widgets = [_widget_state(w) for w in detail.widgets]
    if widgets:
        result = page_of(
            widgets,
            offset=inp.item_offset,
            limit=inp.item_limit,
            render=lambda w: w,
            build=lambda shown, page: result.model_copy(
                update={"widgets": shown, "widgets_page": page}
            ),
            tool="notebook.output",
            args=inp,
            offset_arg="item_offset",
            noun="widgets",
            budget=RESULT_BUDGET_BYTES // 4,
        )
    if detail.images:
        result = _images(detail.images, inp, result)
    if detail.table is not None:
        room = (RESULT_BUDGET_BYTES - measure(result)) // 2
        table, table_page = await _table(port, detail.table, inp, author, room)
        result = result.model_copy(update={"table": table, "table_page": table_page})
    room = RESULT_BUDGET_BYTES - measure(result) - _SLACK
    chart, chart_page = _chart(detail.chart_spec, inp, author, room)
    result = result.model_copy(update={"chart_spec": chart, "chart_page": chart_page})
    if detail.text:
        room = RESULT_BUDGET_BYTES - measure(result) - _SLACK
        line_mode = inp.line_offset is not None
        body, text_page = text_window(
            detail.text,
            unit="line" if line_mode else "char",
            offset=inp.line_offset if inp.line_offset is not None else inp.text_offset,
            limit=inp.line_limit if line_mode else inp.max_chars,
            max_bytes=max(room, 1),
            tool="notebook.output",
            args=inp,
            offset_arg="line_offset" if line_mode else "text_offset",
        )
        result = result.model_copy(
            update={"text": wrap_text(body, author) if body else None, "text_page": text_page}
        )
    return result.model_copy(update={"truncated": detail.truncated})


# ---------------------------------------------------------------------------
# notebook.inspect
# ---------------------------------------------------------------------------


async def notebook_inspect(
    port: NotebookPort, inp: NotebookInspectInput, path: str
) -> NotebookInspectOutput:
    if inp.what == "variables":
        variables = await port.variables()
        if inp.name is not None:
            variables = [v for v in variables if v.name == inp.name]
        return page_of(
            variables,
            offset=inp.offset,
            limit=inp.limit,
            render=lambda v: NotebookVariable(
                name=v.name,
                type=clip_text(v.type, 200),
                cell_id=v.cell_id,
                repr=wrap(clip_text(v.repr, 500), v.author),
                size_bytes=v.size_bytes,
                shape=v.shape[:8] if v.shape is not None else None,
                columns=[clip_text(c, 200) for c in v.columns[:TABLE_COLUMNS]]
                if v.columns is not None
                else None,
                columns_total=len(v.columns) if v.columns is not None else None,
            ),
            build=lambda shown, page: NotebookInspectOutput(
                path=path, what="variables", variables=shown, page=page
            ),
            tool="notebook.inspect",
            args=inp,
            noun="variables",
        )
    if inp.name is None:
        raise NotebookToolError("name_required", f"notebook.inspect {inp.what} needs `name`.")
    if inp.what == "frame":
        frame = await port.frame(
            inp.name, offset=inp.offset, limit=inp.limit, sort=inp.sort, filter_sql=inp.filter_sql
        )
        first = inp.column_offset
        last = first + inp.column_limit
        rows = [[clip_value(v) for v in list(row)[first:last]] for row in frame.rows]

        def make(k: int) -> NotebookInspectOutput:
            return NotebookInspectOutput(
                path=path,
                what="frame",
                frame=NotebookFramePage(
                    name=frame.name,
                    columns=list(frame.columns[first:last]),
                    total_columns=len(frame.columns),
                    column_offset=first,
                    rows=wrap(rows[:k], frame.author),
                    total_rows=frame.total_rows,
                    offset=frame.offset,
                ),
                page=page_at(
                    offset=frame.offset,
                    limit=inp.limit,
                    total=frame.total_rows,
                    returned=k,
                    tool="notebook.inspect",
                    args=inp,
                    noun="rows",
                ),
            )

        return make(fit_prefix(len(rows), lambda k: fits(make(k)), floor=min(1, len(rows))))
    value = await port.value(inp.name)
    summary, _ = clip(value.summary, 8_000)
    return NotebookInspectOutput(path=path, what="value", value=wrap(summary, value.author))


# ---------------------------------------------------------------------------
# notebook.graph
# ---------------------------------------------------------------------------


def _neighbours(
    edges: Sequence[tuple[str, str]], start: str, *, up: bool, depth: int
) -> dict[str, int]:
    """Cells within ``depth`` links of ``start`` (upstream when ``up``), with
    their distance."""
    links: dict[str, list[str]] = {}
    for a, b in edges:
        src, dst = (b, a) if up else (a, b)
        links.setdefault(src, []).append(dst)
    distance: dict[str, int] = {}
    frontier = [start]
    for step in range(1, depth + 1):
        found: list[str] = []
        for node in frontier:
            for nxt in links.get(node, ()):
                if nxt != start and nxt not in distance:
                    distance[nxt] = step
                    found.append(nxt)
        if not found:
            break
        frontier = found
    return distance


def _graph_summary(
    order: Sequence[str], edges: Sequence[tuple[str, str]], statuses: Mapping[str, str], errors: int
) -> NotebookGraphSummary:
    has_parent = {b for a, b in edges if a != b}
    has_child = {a for a, b in edges if a != b}
    roots = [cid for cid in order if cid not in has_parent]
    return NotebookGraphSummary(
        cells=len(order),
        edges=len(edges),
        roots=len(roots),
        leaves=sum(1 for cid in order if cid not in has_child),
        errors=errors,
        by_status=dict(Counter(statuses.values())),
        first_roots=roots[:GRAPH_ROOTS_SHOWN],
    )


async def notebook_graph(
    port: NotebookPort, inp: NotebookGraphInput, path: str
) -> NotebookGraphOutput:
    graph = await port.graph(None, "both")
    order = list(graph.cells)
    position = {cid: i for i, cid in enumerate(order)}
    edges = list(dict.fromkeys(graph.edges))
    errors = list(graph.errors)

    def via(edge: tuple[str, str]) -> tuple[str, str, list[str]]:
        """The edge with the names that make it: what ``to`` uses of ``from``'s."""
        src, dst = edge
        used = set(graph.cells[dst][2]) if dst in graph.cells else set()
        defined = graph.cells[src][1] if src in graph.cells else []
        return src, dst, sorted(name for name in defined if name in used)[:VIA_SHOWN]

    def graph_cell(cid: str, distance: int | None = None) -> NotebookGraphCell:
        name, defs, refs, status = graph.cells[cid]
        return NotebookGraphCell(
            name=name,
            defs=list(defs[:NAME_CAP]),
            refs=list(refs[:NAME_CAP]),
            status=status,
            distance=distance,
        )

    base = NotebookGraphOutput(
        path=path,
        cells={},
        edges=[],
        errors=errors[:GRAPH_ERRORS_SHOWN],
        errors_total=len(errors),
    )
    summary = inp.summary if inp.summary is not None else inp.cell is None
    if summary:
        statuses = {cid: info[3] for cid, info in graph.cells.items()}
        overview = _graph_summary(order, edges, statuses, len(errors))
        return base.model_copy(
            update={
                "summary": overview,
                "cells": {cid: graph_cell(cid) for cid in overview.first_roots},
            }
        )
    distance: dict[str, int] = {}
    if inp.cell is not None:
        focus = _graph_focus(graph.cells, inp.cell)
        # No path is longer than the notebook, so that many links is all of them.
        depth = len(order) if inp.depth == "all" else inp.depth
        up = _neighbours(edges, focus, up=True, depth=depth)
        down = _neighbours(edges, focus, up=False, depth=depth)
        further_up = len(_neighbours(edges, focus, up=True, depth=depth + 1)) > len(up)
        further_down = len(_neighbours(edges, focus, up=False, depth=depth + 1)) > len(down)
        if inp.direction == "down":
            up = {}
        if inp.direction == "up":
            down = {}
        distance = {focus: 0, **up, **down}
        nodes = set(distance)
        edges = [(a, b) for a, b in edges if a in nodes and b in nodes]

        def nearest(found: dict[str, int]) -> list[str]:
            return sorted(found, key=lambda c: (found[c], position.get(c, 0)))

        upstream = nearest(up)[:GRAPH_NEIGHBOURS_SHOWN]
        downstream = nearest(down)[:GRAPH_NEIGHBOURS_SHOWN]
        base = base.model_copy(
            update={
                "upstream": upstream,
                "upstream_direct": [c for c in upstream if up[c] == 1],
                "upstream_transitive": [c for c in upstream if up[c] > 1],
                "upstream_total": len(up),
                "downstream": downstream,
                "downstream_direct": [c for c in downstream if down[c] == 1],
                "downstream_transitive": [c for c in downstream if down[c] > 1],
                "downstream_total": len(down),
                "complete": not (
                    (further_up and inp.direction != "down")
                    or (further_down and inp.direction != "up")
                ),
            }
        )

    def build(shown: list[tuple[str, str, list[str]]], page: NotebookPage) -> NotebookGraphOutput:
        named = list(
            dict.fromkeys(
                [*(c for c in distance if distance[c] == 0)] + [c for e in shown for c in e[:2]]
            )
        )
        return base.model_copy(
            update={
                "edges": shown,
                "page": page,
                "cells": {cid: graph_cell(cid, distance.get(cid)) for cid in named},
            }
        )

    return page_of(
        edges,
        offset=inp.offset,
        limit=inp.limit,
        render=via,
        build=build,
        tool="notebook.graph",
        args=inp,
        noun="edges",
    )


def _graph_focus(cells: dict[str, Any], ref: str) -> str:
    if ref in cells:
        return ref
    named = [cid for cid, info in cells.items() if info[0] == ref and ref != "_"]
    if len(named) > 1:
        raise NotebookToolError(
            "ambiguous_cell", f"{len(named)} cells are named {ref!r}; use an id."
        )
    if not named:
        raise NotebookToolError("cell_not_found", f"No cell {ref!r} in this notebook.")
    return named[0]


# ---------------------------------------------------------------------------
# notebook.widget, notebook.env, notebook.settings
# ---------------------------------------------------------------------------


async def notebook_widget(
    port: NotebookPort, inp: NotebookWidgetInput, path: str
) -> NotebookWidgetOutput:
    if inp.action in ("get", "set") and inp.model_id is None:
        raise NotebookToolError(
            "model_id_required", f"notebook.widget {inp.action} needs `model_id`."
        )
    if inp.action == "set":
        if inp.state is None:
            raise NotebookToolError("state_required", "notebook.widget set needs `state`.")
        assert inp.model_id is not None
        run = await port.set_widget(inp.model_id, dict(inp.state))
        widgets = [w for w in await port.widgets() if w.model_id == inp.model_id]
        run_out = await _run_output(port, path, run, waited=run.ended) if run is not None else None
        return NotebookWidgetOutput(
            path=path, widgets=[_widget_state(w) for w in widgets], run=run_out
        )
    widgets = await port.widgets()
    if inp.action == "get":
        widgets = [w for w in widgets if w.model_id == inp.model_id]
        if not widgets:
            raise NotebookToolError("widget_not_found", f"No widget {inp.model_id!r} in {path}.")
        return NotebookWidgetOutput(path=path, widgets=[_widget_state(widgets[0])])
    return page_of(
        widgets,
        offset=inp.offset,
        limit=inp.limit,
        render=_widget_state,
        build=lambda shown, page: NotebookWidgetOutput(path=path, widgets=shown, page=page),
        tool="notebook.widget",
        args=inp,
        noun="widgets",
    )


async def notebook_env(port: NotebookPort, inp: NotebookEnvInput, path: str) -> NotebookEnvOutput:
    if inp.action in ("install", "remove") and not inp.packages:
        raise NotebookToolError("packages_required", f"notebook.env {inp.action} needs `packages`.")
    if inp.action == "switch" and not inp.env:
        raise NotebookToolError("env_required", "notebook.env switch needs `env`.")
    record = await port.env(inp.action, list(inp.packages), inp.env)
    log, _ = clip(record.log, 8_000)
    base = NotebookEnvOutput(
        path=path,
        env=record.env,
        envs=list(record.envs[:ENVS_SHOWN]),
        spec_changed=list(record.spec_changed[:ENVS_SHOWN]),
        log=wrap_text(log, None),
    )
    packages = list(record.packages)
    if inp.search is not None:
        needle = inp.search.casefold()
        packages = [p for p in packages if needle in p.name.casefold()]
    if not packages and inp.action != "packages":
        return base
    return page_of(
        packages,
        offset=inp.offset,
        limit=inp.limit,
        render=lambda p: p.model_copy(
            update={"name": clip_text(p.name, 200), "version": clip_text(p.version, 100)}
        ),
        build=lambda shown, page: base.model_copy(update={"packages": shown, "page": page}),
        tool="notebook.env",
        args=inp,
        noun="packages",
    )


async def notebook_settings(
    port: NotebookPort, inp: NotebookSettingsInput, path: str
) -> NotebookSettingsOutput:
    settings = await port.settings(inp.changes())
    return NotebookSettingsOutput(path=path, settings=bounded_settings(dict(settings)))


def plan_targets(inp: NotebookRunInput) -> Sequence[str]:
    """The cells a run names directly (empty for a scope target)."""
    return list(inp.target.ids) if isinstance(inp.target, RunCells) else []


__all__ = [
    "INLINE_IMAGE_BYTES",
    "PLAN_SHOWN",
    "READ_OUTPUT_CHARS",
    "REDACTED",
    "bounded_kernel",
    "cell_state",
    "error_info",
    "new_notebook_cells",
    "notebook_cells",
    "notebook_create",
    "notebook_edit",
    "notebook_env",
    "notebook_graph",
    "notebook_inspect",
    "notebook_kernel",
    "notebook_output",
    "notebook_read",
    "notebook_run",
    "notebook_settings",
    "notebook_widget",
    "output_summary",
    "plan_targets",
    "resolve_cells",
    "resolve_op_cells",
    "run_summary",
]
