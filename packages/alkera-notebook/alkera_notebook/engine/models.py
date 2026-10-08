"""Shapes of the engine's client API.

Plain pydantic models: what a person (through the integration), an agent
tool or a test passes in and gets back. Behaviour lives in
:mod:`alkera_notebook.engine.engine`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from alkera_notebook.actors import (
    UNKNOWN_PERSON_LABEL,
    ActingFor,
    ActorKind,
    actor_label,
    actor_names,
)
from alkera_notebook.document.ops import (
    CellAfterOp,
    CellNotice,
    CellStatus,
    GraphCellSummary,
    GraphErrorInfo,
    GraphSummary,
    NotebookOpsResult,
)
from alkera_notebook.envs.models import AdmittedEnvAction
from alkera_notebook.format.settings import Source as SettingSource

KernelState = Literal["absent", "starting", "idle", "busy", "restarting", "stopped"]

RunTrigger = Literal["run", "run_all", "run_stale", "widget", "autorun"]

# Final statuses of a run record. ``ok``: every planned step finished;
# ``error``: a step raised; ``interrupted``: interrupted (``reason`` says by
# whom: ``interrupt``, ``suspended``, ``restart``); ``kernel_restarted``: the
# kernel went away under it (crash, memory guard, restart); ``refused``: the
# plan could not be made (``reason``: ``upstream_being_edited``, ...);
# ``coalesced``: merged into an identical queued request.
RunStatus = Literal[
    "queued",
    "running",
    "ok",
    "error",
    "interrupted",
    "kernel_restarted",
    "refused",
    "needs_confirmation",
    "coalesced",
    "planned",
]

PlanReason = Literal["target", "upstream", "descendant"]


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    id: str
    display_name: str
    can_edit: bool
    can_run: bool
    # The person an agent acts for, when it acts for one.
    acting_for: ActingFor | None = None

    def label(self) -> str:
        return actor_label(self.kind, self.display_name, self.acting_for)


# Run targets -----------------------------------------------------------------


class CellsTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["cells"] = "cells"
    ids: list[str] = Field(min_length=1)


class AllTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["all"] = "all"


class StaleTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["stale"] = "stale"


class AboveTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["above"] = "above"
    id: str


class BelowTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["below"] = "below"
    id: str


RunTarget = Annotated[
    CellsTarget | AllTarget | StaleTarget | AboveTarget | BelowTarget,
    Field(discriminator="kind"),
]


# Reading ---------------------------------------------------------------------


class ReadQuery(BaseModel):
    cells: list[str] | None = None
    include_source: bool = True
    include_outputs: bool = True
    # Each cell's outputs in full (``CellState.outputs``), beside the summary:
    # what a client needs to show them. Readers that want only the summary
    # (the agent tools) leave them out.
    include_output_items: bool = True


class ErrorInfo(BaseModel):
    ename: str
    evalue: str
    traceback: list[str] = Field(default_factory=list)


class OutputSummary(BaseModel):
    kinds: list[str]
    text: str
    error: ErrorInfo | None = None
    truncated: bool = False
    has_image: bool = False
    has_chart: bool = False
    has_table: bool = False
    has_widget: bool = False


class RunActor(BaseModel):
    """Who asked for a run: the person or agent whose action caused it (for
    a widget-triggered or autorun run too), ``system`` only for what nobody
    asked for."""

    model_config = ConfigDict(frozen=True)

    kind: ActorKind
    id: str
    display_name: str
    acting_for: ActingFor | None = None

    @classmethod
    def of(cls, actor: Actor) -> RunActor:
        return cls(
            kind=actor.kind,
            id=actor.id,
            display_name=actor.display_name,
            acting_for=actor.acting_for,
        )

    def label(self) -> str:
        return actor_label(self.kind, self.display_name, self.acting_for)


class RunAttribution(BaseModel):
    run_id: str
    by: RunActor
    trigger: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DisplayOutput(BaseModel):
    """A rich output: one MIME bundle."""

    output_id: str
    type: Literal["display"] = "display"
    # (Titled apart from other ``data`` objects: the generated Python client
    # names an inline object by its title.)
    data: dict[str, Any] = Field(title="Output bundle")


class StreamOutput(BaseModel):
    output_id: str
    type: Literal["stream"] = "stream"
    name: Literal["stdout", "stderr"]
    text: str


class ErrorOutput(BaseModel):
    output_id: str
    type: Literal["error"] = "error"
    error: ErrorInfo


#: One of a cell's outputs as a client shows it (what a client joining after
#: the run needs to show what the others see).
OutputItem = Annotated[DisplayOutput | StreamOutput | ErrorOutput, Field(discriminator="type")]


class CellState(BaseModel):
    id: str
    name: str
    kind: str
    index: int
    status: CellStatus
    # Whose editing this cell's re-run waits for (an autorun skipped it while
    # they were in it); None once it ran, changed or the editing ended.
    rerun_waits_for: str | None = None
    defs: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    graph_errors: list[str] = Field(default_factory=list)
    source: str | None = None
    output: OutputSummary | None = None
    output_outdated: bool = False
    # Where the shown output came from: this kernel, a snapshot written by
    # Alkera, or a snapshot with no Alkera provenance (stock marimo).
    output_origin: Literal["kernel", "saved", "unknown"] | None = None
    # The outputs themselves, in the order a client shows them, when the
    # read asked for them.
    outputs: list[OutputItem] = Field(default_factory=list)
    last_run: RunAttribution | None = None
    # The cell's settings as the document holds them: marimo's cell config
    # (non-default keys), the kind's own keys (a SQL cell's connection and
    # output variable) and other alkera_* keywords.
    # (Titled apart from the operations' own ``config`` and ``meta``: the
    # generated Python client names an inline object by its title.)
    config: dict[str, Any] = Field(default_factory=dict, title="Cell config")
    meta: dict[str, Any] = Field(default_factory=dict, title="Cell meta")
    extra: dict[str, Any] = Field(default_factory=dict, title="Cell extra")


class EnvInfo(BaseModel):
    env_id: str
    kind: str
    spec_root: str
    python: str
    state: str
    recorded_in_file: bool
    #: What a notebook's ``env`` setting says to choose this environment
    #: (``default``, ``script`` or a relative path).
    recorded: str = ""
    #: Why the last build attempt failed or stopped, while an older build is
    #: still the one in use; empty otherwise.
    last_failure: str = ""
    #: What this environment admits now, whoever asks (the person's own
    #: rights are decided apart): ``build`` an unbuilt, changed or failed
    #: one, ``install`` and ``remove`` packages where Alkera owns the spec,
    #: ``cancel`` a build under way.
    #: ``None`` from an engine that predates it (which offered only installs).
    allowed_actions: list[AdmittedEnvAction] | None = None


class QueuedRun(BaseModel):
    run_id: str
    # Who asked for it, as a name (``actor_label``).
    by: str
    trigger: RunTrigger
    status: RunStatus


class KernelInfo(BaseModel):
    state: KernelState
    env: EnvInfo | None
    reactivity: Literal["autorun", "lazy"]
    memory_bytes: int | None = None
    started_at: datetime | None = None
    queue: list[QueuedRun] = Field(default_factory=list)
    kernel_id: str | None = None
    # The sequence number of the kernel's latest event this state reflects,
    # when the reader tracks the event stream (the platform does).
    seq: int | None = None
    # The kernel runs on an environment build that has since been replaced
    # (an install or a rebuild): a restart takes the newer one.
    env_outdated: bool = False


class Presence(BaseModel):
    """Someone who is in a cell now (the rule is ``document/editing.py``)."""

    who: str
    cell_id: str
    kind: Literal["person", "agent", "system"] | None = None
    at: datetime | None = None
    # Who it is as an actor (what a run's requester is compared with), and
    # whether their caret stands in the cell (False: an edit by an actor that
    # publishes no caret). Absent from a writer that predates the rule.
    actor_id: str | None = None
    caret: bool | None = None
    # Seconds the claim still holds from the moment it was answered, unless
    # renewed (``document/editing.holds_for``): a reader drops it once they
    # pass. Absent from a writer that predates it.
    expires_in: float | None = None


class Settings(BaseModel):
    format: str = "1.0"
    reactivity: Literal["autorun", "lazy"] = "autorun"
    dataframe: Literal["polars", "pandas", "auto"] = "auto"
    env: str | None = None
    outputs_in_git: bool = False
    autoreload: Literal["off", "on"] = "off"
    #: Rows kept from a SQL query without its own LIMIT; None keeps the
    #: whole result.
    sql_row_limit: int | None = None
    #: Where each value came from: ``notebook`` (the file), ``workspace``
    #: (the workspace's defaults), ``detected`` (``env``) or ``default``.
    sources: dict[str, SettingSource] = Field(default_factory=dict)


class SettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reactivity: Literal["autorun", "lazy"] | None = None
    dataframe: Literal["polars", "pandas", "auto"] | None = None
    env: str | None = None
    outputs_in_git: bool | None = None
    autoreload: Literal["off", "on"] | None = None
    sql_row_limit: int | None = Field(default=None, ge=1, le=10_000_000)


class NotebookView(BaseModel):
    """A notebook as a reader sees it: its document, its kernel and who is
    where. The one definition: the engine's client, the agent tools, the
    platform's notebook route and the web client (through the generated API
    types) all use this shape."""

    path: str
    token: str
    settings: Settings
    kernel: KernelInfo
    cells: list[CellState]
    presence: list[Presence] = Field(default_factory=list)
    read_only_reason: str | None = None
    notices: list[CellNotice] = Field(default_factory=list)
    output_frame_url: str | None = None


class StoredNotebook(BaseModel):
    """A notebook file as it is stored: its cells as the file holds them, each
    with the outputs saved beside it. Read with no kernel and no live
    document, so a reader who may only see the file gets it as a notebook."""

    cells: list[CellState]
    read_only_reason: str | None = None
    notices: list[CellNotice] = Field(default_factory=list)


# Running ---------------------------------------------------------------------


class PlanEntry(BaseModel):
    cell_id: str
    name: str = "_"
    reason: PlanReason
    # The cell's 0-based position in notebook order, so a person can be shown
    # "Cell 4" for a cell nobody named. ``None`` from an engine that predates it.
    index: int | None = None
    # Filled for ``plan_only`` requests, so a permission gate can classify
    # exactly what would execute: the code each step runs, and for SQL cells
    # the statement and connection. ``interpolated`` marks SQL whose text has
    # ``{expr}`` parts only the kernel can evaluate.
    kind: str | None = None
    code: str | None = None
    sql: str | None = None
    connection: str | None = None
    interpolated: bool = False


class RunInfo(BaseModel):
    """What a run request returned, and later how it ended."""

    run_id: str
    status: RunStatus
    reason: str | None = None
    trigger: RunTrigger
    plan: list[PlanEntry] = Field(default_factory=list)
    estimate_s: float | None = None
    queued_behind: list[str] = Field(default_factory=list)
    # A ``coalesced`` request names the run it joined.
    joined: str | None = None


class RunRecord(BaseModel):
    run_id: str
    requested_by: Actor
    trigger: RunTrigger
    frontier: str | None = None
    plan: list[PlanEntry] = Field(default_factory=list)
    status: RunStatus
    reason: str | None = None
    # What a reader is told about why the run ended as it did, beside the
    # machine-readable ``reason`` (a refusal's cause, an engine failure).
    message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    kernel_id: str | None = None


# Outputs, inspection, graph ---------------------------------------------------

OutputPart = Literal["all", "text", "error", "image", "chart", "table", "widget"]


class OutputDetail(BaseModel):
    text: str = ""
    error: ErrorInfo | None = None
    images: list[str] = Field(default_factory=list)  # base64 PNG data
    chart_spec: dict[str, Any] | None = None
    table: dict[str, Any] | None = None
    widgets: list[dict[str, Any]] = Field(default_factory=list)
    run: RunAttribution | None = None
    truncated: bool = False


class InspectQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: Literal["variables", "frame", "value"]
    name: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=1000)
    sort: list[dict[str, Any]] | None = None
    filter_sql: str | None = None
    depth: int = Field(default=1, ge=0, le=5)


class VarSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    type: str
    repr: str
    size_bytes: int | None = None
    shape: list[int] | None = None
    columns: list[Any] | None = None
    cell_id: str | None = None


class InspectResult(BaseModel):
    variables: list[VarSummary] | None = None
    table: dict[str, Any] | None = None
    total_rows: int | None = None
    summary: Any = None


class GraphCell(BaseModel):
    name: str
    defs: list[str]
    refs: list[str]
    status: CellStatus


class GraphView(BaseModel):
    cells: dict[str, GraphCell]
    edges: list[tuple[str, str]]
    upstream: list[str] = Field(default_factory=list)
    downstream: list[str] = Field(default_factory=list)
    errors: dict[str, list[GraphErrorInfo]] = Field(default_factory=dict)
    """Per cell, the graph errors it is part of."""


# Widgets, environments, activity ---------------------------------------------


class WidgetAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "get", "set"]
    model_id: str | None = None
    state: dict[str, Any] | None = None


class WidgetInfo(BaseModel):
    model_id: str
    cell_id: str | None
    type: str
    value: Any = None
    bound_names: list[str] = Field(default_factory=list)


class WidgetAssetRef(BaseModel):
    """A widget module's entry code the notebook may load: a platform bundle,
    or the ``index.js`` its kernel offered from the environment."""

    module: str
    version: str
    sha256: str
    bytes: int
    kind: Literal["platform", "environment", "value"]


class WidgetResult(BaseModel):
    widgets: list[WidgetInfo] = Field(default_factory=list)
    run: RunInfo | None = None


class EnvAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "info", "list", "packages", "install", "remove", "cancel", "materialize", "switch"
    ]
    packages: list[str] = Field(default_factory=list)
    env: str | None = None


class PackageInfo(BaseModel):
    name: str
    version: str


class EnvListing(BaseModel):
    """The notebook's environment and every environment found for it, and
    whether the workspace's members share its environments on this machine."""

    current: EnvInfo
    envs: list[EnvInfo] = Field(default_factory=list)
    shared: bool = True


class EnvPackages(BaseModel):
    env_id: str
    packages: list[PackageInfo] = Field(default_factory=list)
    #: The spec's own requirements as written, which is what a person adds
    #: to and removes from; ``packages`` is what the build installed.
    requirements: list[str] = Field(default_factory=list)


class EnvResult(BaseModel):
    env: EnvInfo | None = None
    envs: list[EnvInfo] = Field(default_factory=list)
    packages: list[PackageInfo] = Field(default_factory=list)
    spec_changed: list[str] = Field(default_factory=list)
    log: str = ""


ActivityKind = Literal[
    "cell_edit", "cell_run", "kernel_restart", "kernel_stop", "env_change", "settings_change"
]
CellChange = Literal["insert", "edit", "delete", "restore", "move", "rename", "kind", "config"]
EnvChange = Literal["switch", "install", "remove", "cancel", "materialize"]


class ActivityActor(BaseModel):
    """Who did it: ``system`` for what nobody asked for (a crash, the memory
    guard, a suspend)."""

    kind: ActorKind
    id: str
    display_name: str
    acting_for: ActingFor | None = None

    @classmethod
    def of(cls, actor: Actor | ActivityActor) -> ActivityActor:
        return cls(
            kind=actor.kind,
            id=actor.id,
            display_name=actor.display_name,
            acting_for=actor.acting_for,
        )

    def label(self) -> str:
        return actor_label(self.kind, self.display_name, self.acting_for)


def system_actor() -> ActivityActor:
    """The system as an actor, named by :func:`actor_names` when it acts."""
    return ActivityActor(kind="system", id="system", display_name=actor_names().system)


class ActivityEntry(BaseModel):
    """One thing that happened in a notebook, as structure only (never cell
    text or output).

    ``cell_ids`` names only the cells it changed or ran, and
    ``stale_cell_ids`` the cells it left ``stale`` that were not before.
    ``change`` says what kind of cell edit (``CellChange``) or environment
    change (``EnvChange``) it was; ``reason`` why a kernel restarted or
    stopped; ``settings`` which setting keys changed."""

    at: datetime
    actor: ActivityActor
    kind: ActivityKind
    change: str | None = None
    cell_ids: list[str] = Field(default_factory=list)
    stale_cell_ids: list[str] = Field(default_factory=list)
    run_id: str | None = None
    status: str | None = None
    error_class: str | None = None
    reason: str | None = None
    settings: list[str] = Field(default_factory=list)


class Activity(BaseModel):
    """Entries after ``since`` (strictly), oldest first; ``exclude_actor``
    leaves out one actor's own entries."""

    since: datetime
    exclude_actor: str | None = None
    entries: list[ActivityEntry] = Field(default_factory=list)


class SuspendReport(BaseModel):
    kernels_stopped: list[str] = Field(default_factory=list)
    runs_interrupted: list[str] = Field(default_factory=list)
    snapshots_written: list[str] = Field(default_factory=list)


__all__ = [
    "UNKNOWN_PERSON_LABEL",
    "AboveTarget",
    "ActingFor",
    "Activity",
    "ActivityActor",
    "ActivityEntry",
    "ActivityKind",
    "Actor",
    "ActorKind",
    "AllTarget",
    "BelowTarget",
    "CellAfterOp",
    "CellChange",
    "CellNotice",
    "CellState",
    "CellStatus",
    "CellsTarget",
    "DisplayOutput",
    "EnvAction",
    "EnvChange",
    "EnvInfo",
    "EnvResult",
    "ErrorInfo",
    "ErrorOutput",
    "GraphCell",
    "GraphCellSummary",
    "GraphErrorInfo",
    "GraphSummary",
    "GraphView",
    "InspectQuery",
    "InspectResult",
    "KernelInfo",
    "KernelState",
    "NotebookOpsResult",
    "NotebookView",
    "OutputDetail",
    "OutputItem",
    "OutputPart",
    "OutputSummary",
    "PackageInfo",
    "PlanEntry",
    "PlanReason",
    "Presence",
    "QueuedRun",
    "ReadQuery",
    "RunActor",
    "RunAttribution",
    "RunInfo",
    "RunRecord",
    "RunStatus",
    "RunTarget",
    "RunTrigger",
    "Settings",
    "SettingsChange",
    "StaleTarget",
    "StoredNotebook",
    "StreamOutput",
    "SuspendReport",
    "VarSummary",
    "WidgetAction",
    "WidgetInfo",
    "WidgetResult",
    "actor_label",
    "system_actor",
]
