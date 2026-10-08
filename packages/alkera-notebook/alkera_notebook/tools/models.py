"""The agent tools' wire shapes: the inputs a model sends and the results it reads.

These models are the tools' public contract. Every class name carries the
``Notebook`` prefix because a harness publishes every tool's schema into one
manifest keyed by class name, where a bare ``CellState`` or ``GraphView`` would
collide with another tool's model. The exception is the result of a batch of
edits: ``notebook.edit`` answers the engine's own ``NotebookOpsResult`` (with
its ``CellAfterOp``, ``CellNotice`` and ``GraphSummary``), the one definition
the engine, the tools and an HTTP notebook route share.

Text that originates in a notebook (outputs, tracebacks, cell source, variable
reprs, widget values) never appears as a bare string in a result: it is an
:class:`Untrusted` envelope, so a model reading the result can tell the
notebook's words from the tool's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alkera_notebook.document.ops import CellStatus, NotebookOpsResult
from alkera_notebook.tools.paging import NotebookMore, NotebookPage, NotebookTextPage

Reactivity = Literal["autorun", "lazy"]
KernelStateName = Literal["absent", "starting", "idle", "busy", "restarting", "stopped"]
PlanReason = Literal["target", "upstream", "descendant"]
OutputPart = Literal["all", "text", "error", "image", "chart", "table", "widget"]

#: A cell id (Crockford base32, lower case, ten characters).
CELL_ID_PATTERN = r"^[0-9a-hjkmnp-tv-z]{10}$"


class _Wire(BaseModel):
    """A tool shape: unknown keys from a model are refused, so a misspelt
    argument is an error the model can fix rather than a silent default."""

    model_config = ConfigDict(extra="forbid")


class NotebookGuide(BaseModel):
    """The essentials of the ``notebooks`` skill, carried by the first notebook
    tool result in a conversation so the agent has them without loading the
    skill. ``version`` changes whenever the text does."""

    version: str
    text: str


class NotebookToolResult(BaseModel):
    """What every notebook tool result carries besides its own fields."""

    notebook_guide: NotebookGuide | None = None
    """Present on the first notebook tool result in a conversation only."""
    blob: dict[str, Any] | None = None
    """When part of this reply was too large to send inline: where it is
    stored whole, the same handle as the page or window it completes. Read it
    with ``fetch_result`` (a list one whole item per row, a text by
    character), as a large SQL result is read."""
    result_name: str = ""
    """What the stored part is, for the chat's reference to it."""
    ref_type: str = ""
    """``rows`` for a stored list, ``text`` for a stored text."""


class Untrusted(BaseModel):
    """Text a notebook produced, carried as data.

    ``author`` names who caused the text to exist (the person or agent whose run
    printed it, or whose edit wrote the source) so the reader can weigh it;
    ``content`` is the text itself, or for structured outputs (a chart spec, a
    table page) the JSON value."""

    model_config = ConfigDict(extra="forbid")

    untrusted: Literal[True] = True
    author: str
    content: Any


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


class NotebookErrorInfo(BaseModel):
    """A cell's error. ``ename`` is the exception class, reduced to a dotted
    identifier; the message and traceback are the notebook's own words."""

    ename: str
    evalue: Untrusted
    traceback: Untrusted | None = None


class NotebookOutputSummary(BaseModel):
    kinds: list[str] = Field(default_factory=list)
    """The MIME types of the cell's outputs, in output order."""
    chars: int = 0
    """How long the output's text is; ``notebook.output`` pages through it."""
    text: Untrusted | None = None
    """The head of the text; left out of an outline."""
    error: NotebookErrorInfo | None = None
    truncated: bool = False
    has_image: bool = False
    has_chart: bool = False
    has_table: bool = False
    has_widget: bool = False


class NotebookRunAttribution(BaseModel):
    run_id: str
    by: str
    trigger: str
    finished_at: datetime | None = None


class NotebookEnvInfo(BaseModel):
    env_id: str
    kind: str
    spec_root: str
    python: str
    state: str
    recorded_in_file: bool


class NotebookQueuedRun(BaseModel):
    run_id: str
    by: str
    trigger: str
    status: str


class NotebookKernelInfo(BaseModel):
    state: KernelStateName
    env: NotebookEnvInfo
    reactivity: Reactivity
    memory_bytes: int | None = None
    started_at: datetime | None = None
    queue: list[NotebookQueuedRun] = Field(default_factory=list)
    """The first queued runs; ``queue_total`` counts them all."""
    queue_total: int = 0


class NotebookCellState(BaseModel):
    id: str
    name: str
    kind: str
    index: int
    status: CellStatus
    defs: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    names_omitted: int = 0
    """Defs and refs left out of the two lists above (each holds at most 50)."""
    graph_errors: list[str] = Field(default_factory=list)
    upstream: list[str] = Field(default_factory=list)
    """The cells that define a name this cell uses (direct only), by name or
    position, at most 20. ``notebook.graph`` gives the transitive set."""
    downstream: list[str] = Field(default_factory=list)
    """The cells that use a name this cell defines (direct only), at most 20."""
    links_omitted: int = 0
    """Direct upstream and downstream cells left out of the two lists above."""
    lines: int = 0
    """How many lines the source has."""
    head: Untrusted | None = None
    """The source's first non-blank line, cut to 120 characters."""
    source: Untrusted | None = None
    """The requested lines of the source (``source_page`` says which)."""
    source_page: NotebookTextPage | None = None
    matches: list[int] = Field(default_factory=list)
    """For a search: the source lines (0-based) that match, at most 20."""
    meta: dict[str, Any] = Field(default_factory=dict)
    """A SQL cell's ``connection`` (absent: the notebook's DuckDB),
    ``output_var`` and ``show_output``; a Markdown cell's ``quote``."""
    output: NotebookOutputSummary | None = None
    output_outdated: bool = False
    last_run: NotebookRunAttribution | None = None


class NotebookPresence(BaseModel):
    who: str
    cell_id: str | None = None


# ---------------------------------------------------------------------------
# Run targets
# ---------------------------------------------------------------------------


#: How a cell is named in a tool input: its id, its name, its position
#: (``Cell 3`` or ``3``, counted from 1), ``first`` or ``last``.
CELL_REF_HELP = "A cell: its id, its name, its position (Cell 3), first or last."


class RunCells(_Wire):
    """These cells, after any cell they need that has not run yet."""

    kind: Literal["cells"] = "cells"
    ids: list[str] = Field(min_length=1, max_length=500, description=CELL_REF_HELP)


class RunAll(_Wire):
    """Every cell, in dependency order."""

    kind: Literal["all"] = "all"
    restart: bool = Field(
        default=False,
        description="Restart the kernel first, so every cell runs from a clean state.",
    )


class RunStale(_Wire):
    """Every cell whose result no longer matches its code or inputs."""

    kind: Literal["stale"] = "stale"


class RunAbove(_Wire):
    """Every cell above this one in notebook order (not the cell itself)."""

    kind: Literal["above"] = "above"
    id: str = Field(description=CELL_REF_HELP)


class RunBelow(_Wire):
    """This cell and every cell below it in notebook order."""

    kind: Literal["below"] = "below"
    id: str = Field(description=CELL_REF_HELP)


class RunUpstream(_Wire):
    """This cell and every cell it depends on, run again even when they hold values."""

    kind: Literal["upstream"] = "upstream"
    id: str = Field(description=CELL_REF_HELP)


class RunDownstream(_Wire):
    """This cell and every cell that reads it, in either reactivity mode."""

    kind: Literal["downstream"] = "downstream"
    id: str = Field(description=CELL_REF_HELP)


#: The targets an engine plans: what ``notebook.run`` takes once
#: ``upstream`` and ``downstream`` are resolved to ``cells``.
EngineRunTarget = RunCells | RunAll | RunStale | RunAbove | RunBelow

#: What ``notebook.run`` takes. The engine runs the first five; ``upstream``
#: and ``downstream`` are resolved to ``cells`` from the dependency graph
#: before the plan is made (``alkera_notebook.tools.refs``).
RunTarget = Annotated[
    RunCells | RunAll | RunStale | RunAbove | RunBelow | RunUpstream | RunDownstream,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Document operations
# ---------------------------------------------------------------------------


class NotebookTextEdit(_Wire):
    old: str
    """Text the cell holds now; it must occur exactly once unless ``occurrence`` picks one."""
    new: str
    occurrence: int | None = Field(default=None, ge=1)


class InsertCellOp(_Wire):
    op: Literal["insert"] = "insert"
    kind: str = "python"
    source: str = ""
    name: str = "_"
    after: str | None = None
    before: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class EditCellOp(_Wire):
    op: Literal["edit"] = "edit"
    cell_id: str
    edits: list[NotebookTextEdit] = Field(min_length=1, max_length=100)


class ReplaceCellOp(_Wire):
    op: Literal["replace"] = "replace"
    cell_id: str
    source: str


class DeleteCellOp(_Wire):
    op: Literal["delete"] = "delete"
    cell_id: str


class RestoreCellOp(_Wire):
    op: Literal["restore"] = "restore"
    cell_id: str
    after: str | None = None


class MoveCellOp(_Wire):
    op: Literal["move"] = "move"
    cell_id: str
    after: str | None = None
    before: str | None = None


class RenameCellOp(_Wire):
    op: Literal["rename"] = "rename"
    cell_id: str
    name: str


class SetCellKindOp(_Wire):
    op: Literal["set_kind"] = "set_kind"
    cell_id: str
    kind: str


class SetCellConfigOp(_Wire):
    """marimo's cell options, for any cell: ``disabled``, ``hide_code``,
    ``expand_output``, ``column``. A SQL cell's ``show_output``,
    ``output_var`` and ``connection`` are not here: they are ``set_meta``."""

    op: Literal["set_config"] = "set_config"
    cell_id: str
    config: dict[str, Any] = Field(
        description="Only disabled, hide_code, expand_output or column; SQL settings are set_meta."
    )


class SetCellMetaOp(_Wire):
    """A SQL or Markdown cell's settings: for SQL ``connection`` (the name of
    one of the workspace's connections; ``None`` runs it in the notebook's
    DuckDB), ``output_var`` and ``show_output``; for Markdown ``quote``.
    Keys not named keep their value."""

    op: Literal["set_meta"] = "set_meta"
    cell_id: str
    meta: dict[str, Any] = Field(
        description="SQL: connection, output_var, show_output, engine. Markdown: quote."
    )


class SetSettingOp(_Wire):
    op: Literal["set_setting"] = "set_setting"
    key: str
    value: Any


NotebookOp = Annotated[
    InsertCellOp
    | EditCellOp
    | ReplaceCellOp
    | DeleteCellOp
    | RestoreCellOp
    | MoveCellOp
    | RenameCellOp
    | SetCellKindOp
    | SetCellConfigOp
    | SetCellMetaOp
    | SetSettingOp,
    Field(discriminator="op"),
]


class NotebookGraphError(BaseModel):
    cell_id: str
    kind: str
    """``multiple_definitions``, ``cycle``, ``syntax`` or ``delete_nonlocal``."""
    names: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# notebook.read
# ---------------------------------------------------------------------------


#: Cells a read returns when ``limit`` is omitted, and the most it may ask for.
READ_LIMIT = 100
READ_LIMIT_CAP = 500
#: Source lines per cell a read returns when ``source_limit`` is omitted, and the cap.
SOURCE_LIMIT = 400
SOURCE_LIMIT_CAP = 2_000


class NotebookReadInput(_Wire):
    path: str
    cells: list[str] | None = Field(default=None, max_length=READ_LIMIT_CAP)
    """Only these cells: each an id, a name, a position (Cell 3), first or last."""
    search: str | None = Field(default=None, min_length=1, max_length=200)
    """Only cells whose name or source contains this text (any case)."""
    regex: bool = False
    """Read ``search`` as a Python regular expression, matched per line."""
    status: list[CellStatus] | None = Field(default=None, max_length=12)
    """Only cells in one of these statuses (for example ``["error", "stale"]``)."""
    offset: int = Field(default=0, ge=0)
    """The first cell of the page, counted among the cells selected."""
    limit: int = Field(default=READ_LIMIT, ge=1, le=READ_LIMIT_CAP)
    include_source: bool | None = None
    """Return source text. By default only for cells named in ``cells``; every
    other read is an outline (each cell's first line and line count)."""
    source_offset: int = Field(default=0, ge=0)
    """The first source line returned per cell (0-based)."""
    source_limit: int = Field(default=SOURCE_LIMIT, ge=1, le=SOURCE_LIMIT_CAP)
    include_outputs: bool = True


class NotebookReadOutput(NotebookToolResult):
    path: str
    token: str
    settings: dict[str, Any]
    kernel: NotebookKernelInfo
    total_cells: int = 0
    """Every cell in the notebook."""
    matched: int = 0
    """The cells ``cells``, ``search`` and ``status`` selected (all of them when
    none is given); ``page`` is a page of these."""
    page: NotebookPage | None = None
    cells: list[NotebookCellState]
    presence: list[NotebookPresence] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# notebook.create
# ---------------------------------------------------------------------------


class NotebookNewCell(_Wire):
    kind: str = "python"
    source: str = ""
    name: str | None = None
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Cell settings the file records; for a SQL cell: output_var, connection, "
            "engine, show_output."
        ),
    )


class NotebookCreateInput(_Wire):
    path: str
    cells: list[NotebookNewCell] = Field(default_factory=list, max_length=500)
    settings: dict[str, Any] = Field(default_factory=dict)


class NotebookCellBrief(BaseModel):
    id: str
    name: str
    kind: str
    index: int


class NotebookCreateOutput(NotebookToolResult):
    path: str
    token: str
    cells: list[NotebookCellBrief]
    """The new cells in order, as many as the size budget holds."""
    total_cells: int = 0
    more: NotebookMore | None = None
    """The read that lists the cells left out, when the budget cut the list."""


# ---------------------------------------------------------------------------
# notebook.edit
# ---------------------------------------------------------------------------


#: The most ops one edit batch holds, and the most text (sources, replacements).
EDIT_OPS_CAP = 200
EDIT_TEXT_CAP_BYTES = 2 * 1024 * 1024


class NotebookEditInput(_Wire):
    path: str
    base_token: str | None = None
    """The token of the read this edit is based on; omit to edit the current text."""
    ops: list[NotebookOp] = Field(min_length=1, max_length=EDIT_OPS_CAP)

    @model_validator(mode="after")
    def _text_cap(self) -> NotebookEditInput:
        size = 0
        for op in self.ops:
            for text in _op_texts(op):
                size += len(text.encode("utf-8", "replace"))
        if size > EDIT_TEXT_CAP_BYTES:
            raise ValueError(
                f"an edit batch carries at most 2 MiB of text ({size} bytes here); split it, "
                "and change a large cell with `edit` replacements rather than its whole source"
            )
        return self


def _op_texts(op: BaseModel) -> list[str]:
    if isinstance(op, EditCellOp):
        return [t for e in op.edits for t in (e.old, e.new)]
    source = getattr(op, "source", None)
    return [source] if isinstance(source, str) else []


class NotebookEditOutput(NotebookOpsResult, NotebookToolResult):
    """The engine's result of the batch (each touched cell with its run
    status, notices), for ``path``. ``graph`` covers the touched cells and
    the edges into and out of them, not the whole notebook."""

    path: str
    stale: list[str] = Field(default_factory=list)
    """Cells whose results no longer match their inputs after this change (the
    first 50; ``stale_total`` counts them all)."""
    stale_total: int = 0


# ---------------------------------------------------------------------------
# notebook.run
# ---------------------------------------------------------------------------


class NotebookRunInput(_Wire):
    path: str
    target: RunTarget
    wait: bool = True
    timeout_s: float = Field(default=300, gt=0, le=3600)
    confirm_expensive: bool = False


class NotebookPlanStep(BaseModel):
    cell_id: str
    name: str
    reason: PlanReason


class NotebookRunOutput(NotebookToolResult):
    path: str
    run_id: str | None
    status: Literal["finished", "running", "needs_confirmation"]
    reactivity: Reactivity
    env: NotebookEnvInfo
    plan: list[NotebookPlanStep]
    """The first 50 steps of the plan; ``plan_total`` counts them all."""
    plan_total: int = 0
    estimate_s: float | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    """The planned cells by status, once the run has been waited on."""
    failures: list[NotebookCellState] = Field(default_factory=list)
    """The first planned cells that ended in error or were interrupted, with
    their error."""
    cells: list[NotebookCellState] = Field(default_factory=list)
    """Every planned cell's state, for a plan of at most 20 cells; a larger run
    is summarized by ``counts`` and ``failures``, and ``see`` names the reads."""
    see: list[NotebookMore] = Field(default_factory=list)
    """The calls that show the rest of a summarized run."""
    queued_behind: list[str] = Field(default_factory=list)
    summary: str = ""
    """One structural sentence about what ran: counts by status, and what the
    reactivity mode did to cells outside the plan."""


# ---------------------------------------------------------------------------
# notebook.kernel
# ---------------------------------------------------------------------------


KernelActionName = Literal["status", "interrupt", "interrupt_all", "restart", "shutdown"]


class NotebookKernelInput(_Wire):
    path: str
    action: KernelActionName = Field(
        default="status",
        description=(
            "status; interrupt (the running cell); interrupt_all (the running cell, and drop "
            "every queued run); restart (clears every value); shutdown (stops the kernel)."
        ),
    )


class NotebookRunBrief(BaseModel):
    run_id: str
    by: str
    status: str


class NotebookKernelOutput(NotebookToolResult):
    path: str
    kernel: NotebookKernelInfo
    runs: list[NotebookRunBrief] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# notebook.cells
# ---------------------------------------------------------------------------

CellAction = Literal["clear_outputs", "enable", "disable", "duplicate", "move", "set_kind"]
MoveTo = Literal["up", "down", "top", "bottom"]


class NotebookCellsInput(_Wire):
    path: str
    action: CellAction
    cells: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description=(
            "The cells to act on, each its id, name, position (Cell 3), first or last. "
            "Omit with clear_outputs to clear every cell."
        ),
    )
    to: MoveTo | None = Field(default=None, description="For move: up, down, top or bottom.")
    kind: Literal["python", "sql", "markdown"] | None = Field(
        default=None, description="For set_kind: the new kind."
    )


class NotebookCellsOutput(NotebookToolResult):
    path: str
    action: CellAction
    changed: list[str] = Field(default_factory=list)
    """The cells the action changed, by id: for clear_outputs, those that had
    outputs; for duplicate, the new copies."""
    token: str | None = None
    """The document token after an edit; ``None`` for clear_outputs, which
    changes no text."""
    stale: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# notebook.output
# ---------------------------------------------------------------------------


class NotebookOutputInput(_Wire):
    path: str
    cell: str = Field(description=CELL_REF_HELP)
    part: OutputPart = "all"
    text_offset: int = Field(default=0, ge=0)
    """The first character of the text returned."""
    max_chars: int = Field(default=20_000, ge=100, le=40_000)
    """The most characters of text returned."""
    line_offset: int | None = Field(default=None, ge=0)
    """Return the text by line instead: the first line (0-based)."""
    line_limit: int = Field(default=200, ge=1, le=2_000)
    row_offset: int = Field(default=0, ge=0)
    """A table's first row returned (a frame is paged as ``notebook.inspect frame``)."""
    row_limit: int = Field(default=20, ge=1, le=200)
    item_offset: int = Field(default=0, ge=0)
    """The first image and widget returned."""
    item_limit: int = Field(default=10, ge=1, le=50)


class NotebookImage(BaseModel):
    index: int = 0
    """The image's place among the cell's images."""
    mime: str
    bytes: int
    sha256: str
    data_base64: str | None = None
    """Present when the image is at most 32 KiB and fits the result; a larger
    image is a reference only."""


class NotebookWidgetState(BaseModel):
    model_id: str
    cell_id: str | None = None
    type: str
    value: Untrusted | None = None


class NotebookOutputOutput(NotebookToolResult):
    path: str
    cell_id: str
    text: Untrusted | None = None
    text_page: NotebookTextPage | None = None
    error: NotebookErrorInfo | None = None
    images: list[NotebookImage] = Field(default_factory=list)
    images_page: NotebookPage | None = None
    chart_spec: Untrusted | None = None
    """The chart's spec; a spec too large to carry is its JSON text, paged by
    ``text_offset`` with ``part="chart"`` (``chart_page``)."""
    chart_page: NotebookTextPage | None = None
    table: Untrusted | None = None
    """``{columns, rows, total_rows, offset, source}``: a page of the table's rows."""
    table_page: NotebookPage | None = None
    widgets: list[NotebookWidgetState] = Field(default_factory=list)
    widgets_page: NotebookPage | None = None
    run: NotebookRunAttribution | None = None
    truncated: bool = False
    """True when the engine itself kept only part of the output (a stream past
    its cap); paging never sets it."""


# ---------------------------------------------------------------------------
# notebook.show_output
# ---------------------------------------------------------------------------

#: What ``notebook.show_output`` puts in the chat. ``auto`` picks the cell's
#: most telling output: an error, then a chart, a table, an image, Markdown,
#: and plain text last.
ShowPart = Literal["auto", "table", "chart", "image", "markdown", "text"]
#: What a shown output turned out to be.
ShownKind = Literal["table", "chart", "image", "markdown", "text", "error"]

SHOW_ROWS = 50
SHOW_ROWS_CAP = 200


class NotebookShowOutputInput(_Wire):
    path: str
    cell: str = Field(default="last", description=CELL_REF_HELP)
    part: ShowPart = "auto"
    image: int = Field(default=0, ge=0)
    """Which of the cell's images, when it shows several (0 is the first)."""
    row_limit: int = Field(default=SHOW_ROWS, ge=1, le=SHOW_ROWS_CAP)
    """The most table rows shown in the chat; the whole table is stored when
    it has more."""


class NotebookShownTable(BaseModel):
    columns: list[str]
    rows: Untrusted
    """The rows shown, a list of lists in column order."""
    shown_rows: int
    total_rows: int
    total_columns: int


class NotebookShownImage(BaseModel):
    index: int
    total: int
    """How many images the cell's output holds."""
    mime: str
    bytes: int
    sha256: str
    """The image's hash: the notebook stores it beside the file under this
    name, which is where a chat reads it from."""


class NotebookStoredChart(BaseModel):
    """A chart too large to carry in the reply, by the file the notebook keeps
    its spec in beside itself (``<sha256>.json`` in the notebook's output
    folder): where a chat or a thread reads the spec to draw it."""

    sha256: str
    bytes: int
    """The stored spec's size."""


class NotebookShowOutputOutput(NotebookToolResult):
    path: str
    cell_id: str
    cell_name: str
    """The cell as a person knows it: its name, else its position."""
    kind: ShownKind
    available: list[ShownKind] = Field(default_factory=list)
    """Every kind of output the cell holds, so another can be asked for."""
    table: NotebookShownTable | None = None
    chart_spec: Untrusted | None = None
    """The chart's spec in the Alkera chart profile, with its rows inline,
    when it is small enough to carry; else ``chart_ref``."""
    chart_ref: NotebookStoredChart | None = None
    """A larger chart: where its spec is stored. The chat draws it from there."""
    chart_summary: Untrusted | None = None
    """What a stored chart shows, for the agent: its mark, title, encoded
    fields and how many rows it draws."""
    image: NotebookShownImage | None = None
    markdown: Untrusted | None = None
    text: Untrusted | None = None
    cell_error: NotebookErrorInfo | None = None
    """The cell's error when that is what is shown. Not named ``error``: a
    harness reads a top-level ``error`` as the call itself having failed."""
    note: str = ""
    """What a reader of the chat should know about what is shown (cut short,
    stored whole, too large to draw here)."""


# ---------------------------------------------------------------------------
# notebook.inspect
# ---------------------------------------------------------------------------


class NotebookInspectInput(_Wire):
    path: str
    what: Literal["variables", "frame", "value"] = "variables"
    name: str | None = None
    """For ``variables``: only names containing this text; for ``frame`` and
    ``value``: the name to inspect."""
    offset: int = Field(default=0, ge=0)
    """The first variable, or the frame's first row."""
    limit: int = Field(default=50, ge=1, le=500)
    column_offset: int = Field(default=0, ge=0)
    """A frame's first column returned."""
    column_limit: int = Field(default=50, ge=1, le=200)
    sort: str | None = None
    """``col:asc,col2:desc``: the columns to order the page by, in order (the
    direction defaults to ``asc``)."""
    filter_sql: str | None = None
    """One ``SELECT`` statement over a table named ``frame`` (the frame being
    inspected), for example ``SELECT * FROM frame WHERE amount > 100``."""


class NotebookVariable(BaseModel):
    name: str
    type: str
    cell_id: str | None = None
    repr: Untrusted
    size_bytes: int | None = None
    shape: list[int] | None = None
    columns: list[str] | None = None
    """The first 50 columns; ``columns_total`` counts them all."""
    columns_total: int | None = None


class NotebookFramePage(BaseModel):
    name: str
    columns: list[str]
    """The columns in this page (``column_offset`` on)."""
    total_columns: int = 0
    column_offset: int = 0
    rows: Untrusted
    total_rows: int
    offset: int


class NotebookInspectOutput(NotebookToolResult):
    path: str
    what: Literal["variables", "frame", "value"]
    variables: list[NotebookVariable] | None = None
    frame: NotebookFramePage | None = None
    page: NotebookPage | None = None
    """For ``variables`` and ``frame``: where the page sits (variables, or rows)."""
    value: Untrusted | None = None


# ---------------------------------------------------------------------------
# notebook.graph
# ---------------------------------------------------------------------------


class NotebookGraphInput(_Wire):
    path: str
    cell: str | None = None
    """Scope the graph to this cell's neighbourhood (id, name, Cell 3, first or last)."""
    direction: Literal["both", "up", "down"] = "both"
    depth: Annotated[int, Field(ge=1, le=100)] | Literal["all"] = 1
    """How many links from ``cell`` to follow: 1 is the direct cells only,
    ``"all"`` is every cell reachable (the transitive set)."""
    summary: bool | None = None
    """Counts, roots and errors instead of edges. The default without ``cell``;
    pass false for the whole graph's edges, paged."""
    offset: int = Field(default=0, ge=0)
    """The first edge of the page."""
    limit: int = Field(default=200, ge=1, le=1_000)


class NotebookGraphCell(BaseModel):
    name: str
    defs: list[str]
    refs: list[str]
    status: CellStatus
    distance: int | None = None
    """Links from the scoped ``cell`` (0 for the cell itself)."""


class NotebookGraphSummary(BaseModel):
    cells: int
    edges: int
    roots: int
    """Cells that use no other cell's names."""
    leaves: int
    """Cells no other cell uses."""
    errors: int
    by_status: dict[str, int] = Field(default_factory=dict)
    first_roots: list[str] = Field(default_factory=list)
    """The first 20 roots, in notebook order."""


class NotebookGraphOutput(NotebookToolResult):
    path: str
    summary: NotebookGraphSummary | None = None
    cells: dict[str, NotebookGraphCell]
    """The cells the returned edges and lists name."""
    edges: list[tuple[str, str, list[str]]]
    """``(from, to, via)``: ``to`` uses the names ``via``, which ``from``
    defines (at most 10 names an edge)."""
    page: NotebookPage | None = None
    upstream: list[str] = Field(default_factory=list)
    """Every cell ``cell`` depends on within ``depth``, nearest first, at most
    500: ``upstream_direct`` then ``upstream_transitive``."""
    upstream_direct: list[str] = Field(default_factory=list)
    """The cells that define a name ``cell`` uses."""
    upstream_transitive: list[str] = Field(default_factory=list)
    """The cells ``cell`` depends on only through another cell (two links or
    more away). Empty at ``depth`` 1: ask with ``depth: "all"``."""
    upstream_total: int = 0
    downstream: list[str] = Field(default_factory=list)
    """Every cell that depends on ``cell`` within ``depth``, nearest first, at
    most 500: ``downstream_direct`` then ``downstream_transitive``."""
    downstream_direct: list[str] = Field(default_factory=list)
    """The cells that use a name ``cell`` defines."""
    downstream_transitive: list[str] = Field(default_factory=list)
    """The cells that depend on ``cell`` only through another cell."""
    downstream_total: int = 0
    complete: bool = True
    """False when ``depth`` stopped the walk before the graph did: more cells
    are reachable further out."""
    errors: list[NotebookGraphError] = Field(default_factory=list)
    """The first 50 graph errors; ``errors_total`` counts them all."""
    errors_total: int = 0


# ---------------------------------------------------------------------------
# notebook.widget
# ---------------------------------------------------------------------------


class NotebookWidgetInput(_Wire):
    path: str
    action: Literal["list", "get", "set"] = "list"
    model_id: str | None = None
    state: dict[str, Any] | None = None
    offset: int = Field(default=0, ge=0)
    """For ``list``: the first widget of the page."""
    limit: int = Field(default=50, ge=1, le=200)


class NotebookWidgetOutput(NotebookToolResult):
    path: str
    widgets: list[NotebookWidgetState] = Field(default_factory=list)
    """Each value is cut to 1,000 characters of JSON."""
    page: NotebookPage | None = None
    run: NotebookRunOutput | None = None


# ---------------------------------------------------------------------------
# notebook.env
# ---------------------------------------------------------------------------


class NotebookEnvInput(_Wire):
    path: str
    action: Literal[
        "info", "list", "packages", "install", "remove", "materialize", "cancel", "switch"
    ] = "info"
    packages: list[str] = Field(default_factory=list, max_length=50)
    env: str | None = None
    """For ``switch``: the environment to record (``default``, ``script`` or a
    ``./`` path); switching restarts the kernel."""
    search: str | None = Field(default=None, min_length=1, max_length=200)
    """For ``packages``: only packages whose name contains this text."""
    offset: int = Field(default=0, ge=0)
    """For ``packages``: the first package of the page."""
    limit: int = Field(default=200, ge=1, le=1_000)


class NotebookPackage(BaseModel):
    name: str
    version: str


class NotebookEnvOutput(NotebookToolResult):
    path: str
    env: NotebookEnvInfo
    envs: list[NotebookEnvInfo] = Field(default_factory=list)
    packages: list[NotebookPackage] = Field(default_factory=list)
    page: NotebookPage | None = None
    """For ``packages``: where the page sits."""
    spec_changed: list[str] = Field(default_factory=list)
    log: Untrusted | None = None


# ---------------------------------------------------------------------------
# notebook.settings
# ---------------------------------------------------------------------------


class NotebookSettingsInput(_Wire):
    path: str
    reactivity: Reactivity | None = None
    dataframe: Literal["polars", "pandas", "auto"] | None = None
    env: str | None = None
    outputs_in_git: bool | None = None
    autoreload: Literal["off", "on"] | None = None
    sql_row_limit: int | None = Field(
        default=None,
        ge=1,
        le=10_000_000,
        description="The most rows kept from a SQL query that has no LIMIT of its own.",
    )

    def changes(self) -> dict[str, Any]:
        """The settings this call changes, by header key."""
        return {
            key: value
            for key, value in self.model_dump(exclude={"path"}).items()
            if value is not None
        }


class NotebookSettingsOutput(NotebookToolResult):
    path: str
    settings: dict[str, Any]


__all__ = [
    "CELL_ID_PATTERN",
    "CELL_REF_HELP",
    "EDIT_OPS_CAP",
    "EDIT_TEXT_CAP_BYTES",
    "READ_LIMIT",
    "READ_LIMIT_CAP",
    "SOURCE_LIMIT",
    "SOURCE_LIMIT_CAP",
    "CellAction",
    "CellStatus",
    "DeleteCellOp",
    "EditCellOp",
    "EngineRunTarget",
    "InsertCellOp",
    "KernelActionName",
    "MoveCellOp",
    "MoveTo",
    "NotebookCellBrief",
    "NotebookCellState",
    "NotebookCellsInput",
    "NotebookCellsOutput",
    "NotebookCreateInput",
    "NotebookCreateOutput",
    "NotebookEditInput",
    "NotebookEditOutput",
    "NotebookEnvInfo",
    "NotebookEnvInput",
    "NotebookEnvOutput",
    "NotebookErrorInfo",
    "NotebookFramePage",
    "NotebookGraphCell",
    "NotebookGraphError",
    "NotebookGraphInput",
    "NotebookGraphOutput",
    "NotebookGraphSummary",
    "NotebookGuide",
    "NotebookImage",
    "NotebookInspectInput",
    "NotebookInspectOutput",
    "NotebookKernelInfo",
    "NotebookKernelInput",
    "NotebookKernelOutput",
    "NotebookNewCell",
    "NotebookOp",
    "NotebookOutputInput",
    "NotebookOutputOutput",
    "NotebookOutputSummary",
    "NotebookPackage",
    "NotebookPlanStep",
    "NotebookPresence",
    "NotebookQueuedRun",
    "NotebookReadInput",
    "NotebookReadOutput",
    "NotebookRunAttribution",
    "NotebookRunBrief",
    "NotebookRunInput",
    "NotebookRunOutput",
    "NotebookSettingsInput",
    "NotebookSettingsOutput",
    "NotebookShowOutputInput",
    "NotebookShowOutputOutput",
    "NotebookShownImage",
    "NotebookShownTable",
    "NotebookStoredChart",
    "NotebookTextEdit",
    "NotebookToolResult",
    "NotebookVariable",
    "NotebookWidgetInput",
    "NotebookWidgetOutput",
    "NotebookWidgetState",
    "OutputPart",
    "PlanReason",
    "Reactivity",
    "RenameCellOp",
    "ReplaceCellOp",
    "RestoreCellOp",
    "RunAbove",
    "RunAll",
    "RunBelow",
    "RunCells",
    "RunDownstream",
    "RunStale",
    "RunTarget",
    "RunUpstream",
    "SetCellConfigOp",
    "SetCellKindOp",
    "SetCellMetaOp",
    "SetSettingOp",
    "ShowPart",
    "ShownKind",
    "Untrusted",
]
