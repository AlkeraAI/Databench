"""What the agent tools need from an engine: the port they are written against.

The tools never import the engine. They talk to a :class:`NotebookHost` (one
per workspace, bound to the acting agent) and the :class:`NotebookPort` it
opens per notebook. Results cross this boundary as plain records carrying raw
text plus who authored it; the tools decide how that text is wrapped for a
model. Two implementations exist: the adapter over the engine's
``NotebookClient`` (``alkera_notebook.tools.engine_adapter``) and the
simulator's reference engine (``alkera_notebook.sim.reference``).

The port is deliberately narrower than the engine's client: it carries only
what a tool reads, so an engine change that does not reach a tool's output does
not reach this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from alkera_notebook.actors import ActingFor, actor_label
from alkera_notebook.document.ops import NotebookOpsResult
from alkera_notebook.tools.models import (
    CellStatus,
    EngineRunTarget,
    InsertCellOp,
    KernelActionName,
    NotebookEnvInfo,
    NotebookGraphError,
    NotebookKernelInfo,
    NotebookOp,
    NotebookPackage,
    NotebookPresence,
    NotebookRunAttribution,
    OutputPart,
    PlanReason,
)


class ActorRef(BaseModel):
    """Who acts: a person, an agent (possibly for a person), or the system."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["person", "agent", "system"]
    id: str
    display_name: str
    acting_for: ActingFor | None = None

    def label(self) -> str:
        """The name a model or a person reads (never an id)."""
        return actor_label(self.kind, self.display_name, self.acting_for)


class ErrorRecord(BaseModel):
    ename: str
    evalue: str
    traceback: str = ""


class OutputRecord(BaseModel):
    """A cell's latest output, raw."""

    kinds: list[str] = Field(default_factory=list)
    text: str = ""
    error: ErrorRecord | None = None
    truncated: bool = False
    has_image: bool = False
    has_chart: bool = False
    has_table: bool = False
    has_widget: bool = False
    author: str = ""
    """Who ran the code that produced it."""


class CellRecord(BaseModel):
    id: str
    name: str
    kind: str
    index: int
    status: CellStatus
    defs: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    graph_errors: list[str] = Field(default_factory=list)
    source: str | None = None
    source_author: str = ""
    """Who last changed the source; empty when unknown."""
    meta: dict[str, Any] = Field(default_factory=dict)
    output: OutputRecord | None = None
    output_outdated: bool = False
    last_run: NotebookRunAttribution | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    """The cell's settings (``disabled``, ``hide_code``, ...), as the file records them."""


class ViewRecord(BaseModel):
    """A notebook as the tools read it: the engine's view narrowed to what a
    tool reports, with who authored each text (the engine's own view is
    ``alkera_notebook.engine.NotebookView``)."""

    token: str
    settings: dict[str, Any]
    kernel: NotebookKernelInfo
    cells: list[CellRecord]
    presence: list[NotebookPresence] = Field(default_factory=list)


class OpsRecord(NotebookOpsResult):
    """The engine's result of a batch, and the cells left stale after it."""

    stale: list[str] = Field(default_factory=list)


class PlanStepRecord(BaseModel):
    cell_id: str
    name: str
    reason: PlanReason
    index: int | None = None
    """The cell's 0-based position in notebook order: how a person is shown a
    cell nobody named (``cell_display_name``). ``None`` when the engine did not
    say."""
    code: str = ""
    """The code this step will run (the target's current text, or the submitted
    code of an implicit step)."""
    kind: str = "python"
    """The cell's kind; a ``sql`` step also carries its statement."""
    sql: str | None = None
    """For a SQL step: the statement as it will be sent, interpolation applied,
    or ``None`` when it cannot be rendered before the run (a gate then treats
    the step as a write)."""
    connection: str | None = None
    """For a SQL step: the connection it runs on; ``None`` is local SQL over frames."""
    interpolated: bool = False
    """For a SQL step: its text has ``{expr}`` parts only the kernel can evaluate,
    so the statement as sent is not known before the run (a gate treats it as a
    write)."""


class PlanPreview(BaseModel):
    """What a run request would execute if it were made now."""

    steps: list[PlanStepRecord]
    estimate_s: float | None = None
    needs_confirmation: bool = False
    blocked: str | None = None
    """Why the run cannot start (a needed cell is being edited by someone), or ``None``."""


EnvActionName = Literal[
    "info", "list", "packages", "install", "remove", "materialize", "cancel", "switch"
]

RunStatus = Literal[
    "queued", "running", "finished", "failed", "interrupted", "cancelled", "needs_confirmation"
]


class RunRecord(BaseModel):
    run_id: str | None
    status: RunStatus
    plan: list[PlanStepRecord]
    estimate_s: float | None = None
    queued_behind: list[str] = Field(default_factory=list)
    requested_by: ActorRef | None = None
    trigger: str = "run"

    @property
    def ended(self) -> bool:
        return self.status in ("finished", "failed", "interrupted", "cancelled")


class ImageRecord(BaseModel):
    mime: str
    data: bytes


class WidgetRecord(BaseModel):
    model_id: str
    cell_id: str | None = None
    type: str
    value: Any = None
    sensitive: bool = False


class OutputDetailRecord(BaseModel):
    cell_id: str
    text: str = ""
    error: ErrorRecord | None = None
    images: list[ImageRecord] = Field(default_factory=list)
    chart_spec: Any = None
    table: Any = None
    widgets: list[WidgetRecord] = Field(default_factory=list)
    run: NotebookRunAttribution | None = None
    author: str = ""
    truncated: bool = False
    """The engine kept only part of the text (a stream past its cap)."""


class VariableRecord(BaseModel):
    name: str
    type: str
    cell_id: str | None = None
    repr: str
    size_bytes: int | None = None
    shape: list[int] | None = None
    columns: list[str] | None = None
    author: str = ""


class FrameRecord(BaseModel):
    name: str
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int
    offset: int
    author: str = ""


class ValueRecord(BaseModel):
    name: str
    summary: str
    author: str = ""


class GraphRecord(BaseModel):
    cells: dict[str, tuple[str, list[str], list[str], CellStatus]]
    """id -> (name, defs, refs, status)."""
    edges: list[tuple[str, str]]
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    errors: list[NotebookGraphError] = Field(default_factory=list)


class EnvRecord(BaseModel):
    env: NotebookEnvInfo
    envs: list[NotebookEnvInfo] = Field(default_factory=list)
    packages: list[NotebookPackage] = Field(default_factory=list)
    spec_changed: list[str] = Field(default_factory=list)
    log: str = ""


class KernelRecord(BaseModel):
    kernel: NotebookKernelInfo
    runs: list[RunRecord] = Field(default_factory=list)


ActivityKind = Literal[
    "edit",
    "insert",
    "delete",
    "restore",
    "move",
    "rename",
    "kind",
    "run",
    "kernel",
    "settings",
    "env",
]


class ActivityItem(BaseModel):
    """One thing that happened in a notebook, as structure only.

    Nothing here carries output text: the digest built from it is read by a
    model on every turn, so it names who, which cell and what status, never
    what a cell printed."""

    at: datetime
    actor: ActorRef
    kind: ActivityKind
    cell_id: str | None = None
    cell_name: str | None = None
    run_id: str | None = None
    status: str | None = None
    """For a run: its end status; for a cell in a run: the cell's status."""
    error_class: str | None = None
    reason: str | None = None
    """For a kernel event: why (``restart``, ``out_of_memory``, ``interrupt_restart``, ...)."""
    cells: list[tuple[str, str, str]] = Field(default_factory=list)
    """For a run: (cell id, cell name, status) of every planned cell."""


class Activity(BaseModel):
    path: str
    items: list[ActivityItem] = Field(default_factory=list)
    stale: list[tuple[str, str]] = Field(default_factory=list)
    """(cell id, cell name) of cells now stale."""
    cursor: datetime
    """Where the next read starts: the time of the newest item considered."""


class NotebookToolError(Exception):
    """A refusal the model can act on (a missing cell, an edit that does not
    match, a path outside the workspace). ``code`` is a stable name."""

    def __init__(self, code: str, message: str, *, op_index: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.op_index = op_index


@runtime_checkable
class NotebookPort(Protocol):
    """One notebook, as the acting agent sees it."""

    @property
    def actor(self) -> ActorRef: ...

    async def read(
        self, cells: Sequence[str] | None, *, include_source: bool, include_outputs: bool
    ) -> ViewRecord: ...

    async def apply(self, ops: Sequence[NotebookOp], base_token: str | None) -> OpsRecord: ...

    async def preview(self, target: EngineRunTarget) -> PlanPreview: ...

    async def run(self, target: EngineRunTarget, *, confirm_expensive: bool) -> RunRecord: ...

    async def wait(self, run_id: str, timeout_s: float) -> RunRecord: ...

    async def kernel(self, action: KernelActionName) -> KernelRecord: ...

    async def clear_outputs(self, cell_ids: Sequence[str] | None) -> list[str]:
        """Clear the outputs of ``cell_ids`` (every cell when ``None``) for
        everyone looking at the notebook, and from its saved outputs; returns
        the cells that had outputs."""
        ...

    async def output(self, cell: str, part: OutputPart) -> OutputDetailRecord: ...

    async def variables(self) -> list[VariableRecord]: ...

    async def frame(
        self, name: str, *, offset: int, limit: int, sort: str | None, filter_sql: str | None
    ) -> FrameRecord: ...

    async def value(self, name: str) -> ValueRecord: ...

    async def graph(
        self, cell: str | None, direction: Literal["both", "up", "down"]
    ) -> GraphRecord: ...

    async def widgets(self) -> list[WidgetRecord]: ...

    async def preview_widget(self, model_id: str, state: dict[str, Any]) -> PlanPreview:
        """What setting the widget would run, without setting it."""
        ...

    async def set_widget(self, model_id: str, state: dict[str, Any]) -> RunRecord | None: ...

    async def env(
        self, action: EnvActionName, packages: Sequence[str], env: str | None
    ) -> EnvRecord: ...

    async def settings(self, changes: dict[str, Any]) -> dict[str, Any]: ...

    async def activity(self, since: datetime) -> Activity: ...


@runtime_checkable
class NotebookHost(Protocol):
    """The notebooks of one workspace, for one actor."""

    @property
    def actor(self) -> ActorRef: ...

    def resolve(self, path: str) -> str:
        """The notebook's canonical path inside the workspace; raises
        :class:`NotebookToolError` (``outside_workspace``, ``not_a_notebook``)."""
        ...

    async def open(self, path: str) -> NotebookPort: ...

    async def create(
        self, path: str, cells: Sequence[InsertCellOp], settings: dict[str, Any]
    ) -> NotebookPort:
        """Create the notebook from its cells, in order; raises
        :class:`NotebookToolError` ``exists`` when the file is already there."""
        ...

    async def notebooks(self) -> list[str]:
        """Paths of the notebooks this workspace holds."""
        ...


__all__ = [
    "Activity",
    "ActivityItem",
    "ActivityKind",
    "ActorRef",
    "CellRecord",
    "EnvRecord",
    "ErrorRecord",
    "FrameRecord",
    "GraphRecord",
    "ImageRecord",
    "KernelRecord",
    "NotebookHost",
    "NotebookPort",
    "NotebookToolError",
    "OpsRecord",
    "OutputDetailRecord",
    "OutputRecord",
    "PlanPreview",
    "PlanStepRecord",
    "RunRecord",
    "RunStatus",
    "ValueRecord",
    "VariableRecord",
    "ViewRecord",
    "WidgetRecord",
]
