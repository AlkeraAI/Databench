"""The reference engine: a small in-process engine the simulator drives.

It implements the tools' :class:`~alkera_notebook.tools.port.NotebookPort` and
:class:`~alkera_notebook.tools.port.NotebookHost` over the pure document model
(:mod:`alkera_notebook.sim.oracle`), a planner that follows the submitted-code
rules (targets run their current text, upstream cells that hold no value run
first, autorun re-runs dependents that hold values with their submitted code,
lazy marks them stale), and an in-process kernel that executes cells with
``exec`` in one namespace per notebook.

What it is for: developing and testing the agent tools, the digest and the
simulator itself without a real kernel, and acting as the scripted model's
playground in the evaluation loop's own tests. What it is not: a sandbox. It
executes the code it is given in this process, so it runs only code the
simulator's grammar or a test wrote. The real engine target
(:mod:`alkera_notebook.sim.targets`) runs the same scenarios against real
kernels.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import io
import re
import reprlib
import sys
import traceback
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from alkera_notebook.document.editing import EDIT_WITHOUT_CARET_FOR
from alkera_notebook.document.ops import (
    CellAfterOp,
    CellNotice,
    GraphCellSummary,
    GraphErrorInfo,
    GraphSummary,
)
from alkera_notebook.engine.sort_spec import parse_sort_spec
from alkera_notebook.sim.analysis import GraphAnalysis, analyze, topological
from alkera_notebook.sim.oracle import Cell, DocumentModel, OpError, random_cell_id
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
    NotebookQueuedRun,
    NotebookRunAttribution,
    OutputPart,
    PlanReason,
    RunAbove,
    RunAll,
    RunBelow,
    RunCells,
    RunStale,
)
from alkera_notebook.tools.port import (
    Activity,
    ActivityItem,
    ActivityKind,
    ActorRef,
    CellRecord,
    EnvActionName,
    EnvRecord,
    ErrorRecord,
    FrameRecord,
    GraphRecord,
    ImageRecord,
    KernelRecord,
    NotebookToolError,
    OpsRecord,
    OutputDetailRecord,
    OutputRecord,
    PlanPreview,
    PlanStepRecord,
    RunRecord,
    ValueRecord,
    VariableRecord,
    ViewRecord,
    WidgetRecord,
)

#: The simulation's actors publish no caret: an edit is where its author is.
EDITING_WINDOW = EDIT_WITHOUT_CARET_FOR
#: An ``{expr}`` part of a raw f-string body (``{{`` and ``}}`` are literal braces).
_INTERPOLATION = re.compile(r"(?<!\{)\{(?!\{)[^{}]*\}")
NOTEBOOK_SUFFIX = ".alknb.py"
#: Distributions the reference environment can install, and the module each provides.
INSTALLABLE: dict[str, str] = {"simtable": "simtable", "simplot": "simplot"}
_SETUP_NAME = "setup"


_OP_ACTIVITY: dict[str, ActivityKind] = {
    "edit": "edit",
    "replace": "edit",
    "delete": "delete",
    "restore": "restore",
    "move": "move",
    "rename": "rename",
    "set_kind": "kind",
    "set_config": "edit",
    "set_meta": "edit",
}


class FakeClock:
    """A clock that moves forward by one millisecond every time it is read,
    so every event has its own instant, and jumps when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(milliseconds=1)
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


Clock = Callable[[], datetime]


@dataclass
class CellKernelState:
    """What the kernel knows about one cell."""

    submitted: str | None = None
    has_value: bool = False
    result: Literal["none", "ok", "error", "interrupted", "skipped"] = "none"
    seq: int = 0
    output: OutputRecord | None = None
    last_run: NotebookRunAttribution | None = None
    duration_s: float = 0.0
    bound: list[str] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)


@dataclass
class RunLog:
    run_id: str
    requested_by: ActorRef
    trigger: str
    steps: list[PlanStepRecord]
    status: Literal["queued", "running", "finished", "failed", "interrupted", "cancelled"]
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    executed: list[str] = field(default_factory=list)
    """Cells this run executed, in order (for the one-run-at-a-time invariant)."""


@dataclass
class Widget:
    model_id: str
    cell_id: str
    type: str
    value: Any
    names: list[str] = field(default_factory=list)
    sensitive: bool = False


class _FakeWidget:
    """``alkera.ui.*`` inside the reference kernel."""

    def __init__(self, kind: str, value: Any, sensitive: bool = False) -> None:
        self.kind = kind
        self.value = value
        self.sensitive = sensitive

    def __repr__(self) -> str:
        return f"<{self.kind} value={self.value!r}>"


class _FakeChart:
    """``alkera.chart(data)`` inside the reference kernel: the chainable builder
    of the charting API (marks, encodings, title, interactions), kept as a spec."""

    _MARKS = ("line", "bar", "area", "point", "histogram", "errorband", "rule", "text")
    _OPTIONS = ("title", "tooltip", "zoom", "brush", "legend", "facet", "sort")

    def __init__(self, data: Any) -> None:
        self.data = data
        self.spec: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name not in self._MARKS + self._OPTIONS:
            raise AttributeError(name)

        def step(*args: Any, **kwargs: Any) -> _FakeChart:
            key = "mark" if name in self._MARKS else name
            self.spec[key] = (
                {"type": name, "args": list(args), **kwargs}
                if key == "mark"
                else (args[0] if args else True)
            )
            return self

        return step

    def __repr__(self) -> str:
        return f"<chart {self.spec.get('mark', {}).get('type', 'empty')}>"


class _FakeMarkdown:
    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _alkera_stand_in(notebook: ReferenceNotebook) -> types.ModuleType:
    module = types.ModuleType("alkera")
    ui = types.ModuleType("alkera.ui")

    def slider(start: float = 0, stop: float = 10, value: float | None = None) -> _FakeWidget:
        return _FakeWidget("slider", start if value is None else value)

    def text(value: str = "", kind: str = "text") -> _FakeWidget:
        return _FakeWidget("text", value, sensitive=kind == "password")

    def dropdown(options: Sequence[str], value: str | None = None) -> _FakeWidget:
        return _FakeWidget("dropdown", value if value is not None else next(iter(options)))

    ui.slider = slider  # type: ignore[attr-defined]
    ui.text = text  # type: ignore[attr-defined]
    ui.dropdown = dropdown  # type: ignore[attr-defined]
    module.ui = ui  # type: ignore[attr-defined]
    module.md = _FakeMarkdown  # type: ignore[attr-defined]
    module.chart = _FakeChart  # type: ignore[attr-defined]

    def sql(query: str, *, connection: str | None = None, output: bool = True) -> Any:
        return notebook.run_sql(query, connection)

    module.sql = sql  # type: ignore[attr-defined]
    return module


class ReferenceWorkspace:
    """The notebooks of one workspace root in the reference engine."""

    def __init__(
        self,
        root: str = "/workspace",
        *,
        clock: Clock | None = None,
        seed: int = 0,
        cost_guard_seconds: float = 60.0,
        warehouse: dict[str, Any] | None = None,
    ) -> None:
        import random

        self.root = root.rstrip("/")
        self.clock: Clock = clock or FakeClock()
        self.rng = random.Random(seed)  # noqa: S311 -- simulated cell ids, reproducible by seed
        self.cost_guard_seconds = cost_guard_seconds
        self.notebooks: dict[str, ReferenceNotebook] = {}
        self.warehouse = warehouse or {}
        """Tables the fake warehouse connection serves, by name (pandas frames)."""
        self.closed = False

    def host(self, actor: ActorRef) -> ReferenceHost:
        return ReferenceHost(self, actor)

    def resolve(self, path: str) -> str:
        if not path or "\x00" in path:
            raise NotebookToolError("outside_workspace", "A notebook path is required.")
        parts: list[str] = []
        raw = path if path.startswith("/") else f"{self.root}/{path}"
        if not raw.startswith(self.root + "/"):
            raise NotebookToolError(
                "outside_workspace", f"{path} is outside this chat's workspace."
            )
        for part in raw[len(self.root) + 1 :].split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise NotebookToolError(
                        "outside_workspace", f"{path} is outside this chat's workspace."
                    )
                parts.pop()
                continue
            parts.append(part)
        rel = "/".join(parts)
        if not rel.endswith(NOTEBOOK_SUFFIX):
            raise NotebookToolError(
                "not_a_notebook", f"{path} is not a notebook (the name must end in .alknb.py)."
            )
        return rel

    async def close(self) -> None:
        for notebook in self.notebooks.values():
            await notebook.shutdown_kernel()
        self.closed = True


class ReferenceHost:
    def __init__(self, workspace: ReferenceWorkspace, actor: ActorRef) -> None:
        self._workspace = workspace
        self._actor = actor

    @property
    def actor(self) -> ActorRef:
        return self._actor

    def resolve(self, path: str) -> str:
        return self._workspace.resolve(path)

    async def open(self, path: str) -> ReferencePort:
        notebook = self._workspace.notebooks.get(path)
        if notebook is None:
            raise NotebookToolError(
                "not_found", f"No notebook at {path}; create it with notebook.create."
            )
        return ReferencePort(notebook, self._actor)

    async def create(
        self, path: str, cells: Sequence[InsertCellOp], settings: dict[str, Any]
    ) -> ReferencePort:
        if path in self._workspace.notebooks:
            raise NotebookToolError("exists", f"{path} already exists; edit it instead.")
        notebook = ReferenceNotebook(self._workspace, path)
        ops: list[NotebookOp] = [cell.model_copy() for cell in cells]
        for key, value in settings.items():
            from alkera_notebook.tools.models import SetSettingOp

            ops.append(SetSettingOp(key=key, value=value))
        if ops:
            notebook.apply(ops, None, self._actor)
        self._workspace.notebooks[path] = notebook
        return ReferencePort(notebook, self._actor)

    async def notebooks(self) -> list[str]:
        return sorted(self._workspace.notebooks)


class ReferenceNotebook:
    """One notebook: its document, its kernel and its history."""

    def __init__(self, workspace: ReferenceWorkspace, path: str) -> None:
        self.workspace = workspace
        self.path = path
        self.doc = DocumentModel()
        self.kernel_state: Literal["absent", "idle", "busy"] = "absent"
        self.kernel_started_at: datetime | None = None
        self.namespace: dict[str, Any] = {}
        self.kstate: dict[str, CellKernelState] = {}
        self.runs: list[RunLog] = []
        self.queue: list[RunLog] = []
        self.activity: list[ActivityItem] = []
        self.source_author: dict[str, str] = {}
        self.editing: dict[str, tuple[ActorRef, datetime]] = {}
        self.widgets: dict[str, Widget] = {}
        self.installed: set[str] = set()
        self.cleared_names: set[str] = set()
        self.duration_override: dict[str, float] = {}
        self.metered_sql: bool = False
        self.step_seq = 0
        self.run_seq = 0
        self.interrupt_requested: str | None = None
        self.worker: asyncio.Task[None] | None = None
        self.executions: list[tuple[str, str]] = []
        """(run id, cell id) of every step executed, in order."""
        self._submit_ids: dict[str, OpsRecord] = {}

    # -- time and identity -------------------------------------------------------

    def now(self) -> datetime:
        return self.workspace.clock()

    def new_cell_id(self) -> str:
        return random_cell_id(self.workspace.rng)

    def log(self, actor: ActorRef, kind: ActivityKind, **fields: Any) -> None:
        self.activity.append(ActivityItem(at=self.now(), actor=actor, kind=kind, **fields))

    # -- graphs and statuses ------------------------------------------------------

    def kernel_code(self, cell: Cell) -> str:
        state = self.kstate.get(cell.id)
        if state is not None and state.submitted is not None:
            return state.submitted
        return cell.source

    def kernel_graph(self, overrides: dict[str, str] | None = None) -> GraphAnalysis:
        overrides = overrides or {}
        return analyze(
            [
                (c.id, c.kind, overrides.get(c.id, self.kernel_code(c)), c.meta)
                for c in self.doc.live()
            ]
        )

    def document_graph(self) -> GraphAnalysis:
        return analyze([(c.id, c.kind, c.source, c.meta) for c in self.doc.live()])

    def statuses(self) -> dict[str, CellStatus]:
        """Every live cell's status, computed from scratch."""
        graph = self.kernel_graph()
        live = self.doc.live()
        running = self.queue[0] if self.queue and self.queue[0].status == "running" else None
        queued_cells: set[str] = set()
        for run in self.queue:
            queued_cells.update(s.cell_id for s in run.steps if s.cell_id not in run.executed)
        memo: dict[str, CellStatus] = {}

        def status(cid: str, trail: frozenset[str] = frozenset()) -> CellStatus:
            if cid in memo:
                return memo[cid]
            cell = self.doc.cells[cid]
            state = self.kstate.get(cid)
            result: CellStatus
            if cell.config.get("disabled"):
                result = "disabled"
            elif (
                running is not None
                and running.executed
                and running.executed[-1] == cid
                and self.kernel_state == "busy"
            ):
                result = "running"
            elif cid in queued_cells:
                result = "queued"
            elif state is None or state.result == "none":
                result = "not_run"
            elif state.result == "error":
                result = "error"
            elif state.result == "interrupted":
                result = "interrupted"
            elif state.result == "skipped":
                result = "skipped"
            elif cell.source != state.submitted:
                result = "edited"
            else:
                result = "fresh"
                for parent in graph.ancestors(cid):
                    if parent in trail or parent not in self.doc.cells:
                        continue
                    pstate = self.kstate.get(parent)
                    if pstate is None or not pstate.has_value or pstate.seq > state.seq:
                        result = "stale"
                        break
                    if status(parent, trail | {cid}) != "fresh":
                        result = "stale"
                        break
            memo[cid] = result
            return result

        return {c.id: status(c.id) for c in live}

    # -- the document ---------------------------------------------------------------

    def apply(
        self,
        ops: Sequence[NotebookOp],
        base_token: str | None,
        actor: ActorRef,
        submit_id: str | None = None,
    ) -> OpsRecord:
        if submit_id is not None and submit_id in self._submit_ids:
            return self._submit_ids[submit_id].model_copy(update={"repeat": True})
        before = {c.id: c for c in self.doc.live()}
        try:
            doc, created, changed = self.doc.apply(ops, self.new_cell_id)
        except OpError as exc:
            raise NotebookToolError(exc.code, str(exc), op_index=exc.index) from exc
        now = self.now()
        notices: list[CellNotice] = []
        running_cells = self._running_cells()
        for cid in changed:
            other = self.editing.get(cid)
            if other is not None and other[0].id != actor.id and now - other[1] <= EDITING_WINDOW:
                notices.append(
                    CellNotice(
                        kind="concurrent_edit",
                        cell_id=cid,
                        by=other[0].display_name,
                        message=f"{other[0].display_name} edited this cell in the last 15 seconds.",
                    )
                )
            if cid in running_cells:
                notices.append(
                    CellNotice(
                        kind="cell_running",
                        cell_id=cid,
                        message="This cell is running; the run uses the code it started with.",
                    )
                )
        self.doc = doc
        for op in ops:
            kind = op.op
            op_cell: str | None = getattr(op, "cell_id", None)
            if kind == "insert":
                continue
            if kind == "set_setting":
                self.log(actor, "settings")
                continue
            target = self.doc.cells.get(op_cell) if op_cell else None
            self.log(
                actor,
                _OP_ACTIVITY.get(kind, "edit"),
                cell_id=op_cell,
                cell_name=target.name if target is not None else None,
            )
            if op_cell and kind in ("edit", "replace", "set_kind"):
                self.source_author[op_cell] = actor.display_name
                self.editing[op_cell] = (actor, now)
            if op_cell and kind == "delete" and op_cell in before:
                self.cleared_names.update(self.kstate.get(op_cell, CellKernelState()).bound)
        for cid in created:
            cell = self.doc.cells[cid]
            self.source_author[cid] = actor.display_name
            self.editing[cid] = (actor, now)
            self.log(actor, "insert", cell_id=cid, cell_name=cell.name)
        statuses = self.statuses()
        graph = self.document_graph()
        index = {c.id: i for i, c in enumerate(self.doc.live())}
        record = OpsRecord(
            token=str(self.doc.version),
            repeat=False,
            cells=[
                CellAfterOp(
                    id=cid,
                    name=self.doc.cells[cid].name,
                    kind=self.doc.cells[cid].kind,
                    index=index[cid],
                    status=statuses[cid],
                )
                for cid in changed
                if cid in index
            ],
            created=created,
            notices=notices,
            graph=self._graph_summary(graph),
            stale=[cid for cid, status in statuses.items() if status == "stale"],
        )
        if submit_id is not None:
            self._submit_ids[submit_id] = record
        del base_token
        return record

    def _graph_summary(self, graph: GraphAnalysis) -> GraphSummary:
        """The engine's document graph summary of the reference analysis: one
        error per name it is about (a bare code when it names none)."""
        return GraphSummary(
            cells={
                cid: GraphCellSummary(
                    defs=list(info.defs),
                    refs=list(info.refs),
                    errors=[
                        GraphErrorInfo(code=kind, name=name)
                        for kind in graph.errors.get(cid, [])
                        for name in self._error_names_or_none(graph, cid, kind)
                    ],
                )
                for cid, info in graph.cells.items()
            },
            edges=list(graph.edges),
        )

    def _error_names_or_none(self, graph: GraphAnalysis, cid: str, kind: str) -> list[str | None]:
        return [*self._error_names(graph, cid, kind)] or [None]

    def _error_names(self, graph: GraphAnalysis, cid: str, kind: str) -> list[str]:
        return sorted(set(graph.names.get(cid, {}).get(kind, [])))

    def _running_cells(self) -> set[str]:
        if not self.queue or self.queue[0].status != "running":
            return set()
        return {s.cell_id for s in self.queue[0].steps}

    # -- planning ------------------------------------------------------------------

    def target_ids(self, target: EngineRunTarget) -> list[str]:
        live = self.doc.live()
        ids = [c.id for c in live]
        if isinstance(target, RunCells):
            out: list[str] = []
            for ref in target.ids:
                cell = self.doc.find(ref)
                if cell is None:
                    raise NotebookToolError("cell_not_found", f"No cell {ref!r} in {self.path}.")
                out.append(cell.id)
            return list(dict.fromkeys(out))
        if isinstance(target, RunAll):
            return ids
        if isinstance(target, RunStale):
            statuses = self.statuses()
            return [cid for cid in ids if statuses[cid] in ("stale", "edited", "not_run")]
        ref = target.id
        cell = self.doc.find(ref)
        if cell is None:
            raise NotebookToolError("cell_not_found", f"No cell {ref!r} in {self.path}.")
        pos = ids.index(cell.id)
        if isinstance(target, RunAbove):
            return ids[:pos]
        assert isinstance(target, RunBelow)
        return ids[pos + 1 :]

    def plan(self, target: EngineRunTarget, actor: ActorRef) -> PlanPreview:
        targets = [
            t for t in self.target_ids(target) if not self.doc.cells[t].config.get("disabled")
        ]
        new_code = {t: self.doc.cells[t].source for t in targets}
        graph = self.kernel_graph(new_code)
        statuses = self.statuses()
        reason: dict[str, PlanReason] = {t: "target" for t in targets}
        code: dict[str, str] = dict(new_code)
        now = self.now()
        for t in targets:
            for parent in graph.ancestors(t):
                if parent in reason or self.doc.cells[parent].config.get("disabled"):
                    continue
                state = self.kstate.get(parent)
                if statuses.get(parent) == "fresh" or (state is not None and state.has_value):
                    continue
                if state is None or state.submitted is None:
                    editor = self.editing.get(parent)
                    if (
                        editor is not None
                        and editor[0].id != actor.id
                        and now - editor[1] <= (EDITING_WINDOW)
                    ):
                        name = self.doc.cells[parent].name
                        return PlanPreview(
                            steps=[],
                            blocked=(
                                f"cell `{name}` is being edited by {editor[0].display_name}; "
                                "run it when they are done, or ask them."
                            ),
                        )
                    code[parent] = self.doc.cells[parent].source
                else:
                    code[parent] = state.submitted
                reason[parent] = "upstream"
        if self.doc.settings.get("reactivity", "autorun") == "autorun":
            for t in list(reason):
                for child in graph.descendants(t):
                    if child in reason or self.doc.cells[child].config.get("disabled"):
                        continue
                    state = self.kstate.get(child)
                    if state is None or not state.has_value or state.submitted is None:
                        continue
                    if statuses.get(child) in ("not_run", "error", "skipped"):
                        continue
                    reason[child] = "descendant"
                    code[child] = state.submitted
        order = topological(graph, list(reason), [c.id for c in self.doc.live()])
        steps = [self._step(cid, reason[cid], code[cid]) for cid in order]
        implicit = sum(self._duration(s.cell_id) for s in steps if s.reason != "target")
        metered = self.metered_sql and any(
            self.doc.cells[s.cell_id].kind == "sql" and s.reason != "target" for s in steps
        )
        estimate = sum(self._duration(s.cell_id) for s in steps)
        return PlanPreview(
            steps=steps,
            estimate_s=estimate,
            needs_confirmation=implicit > self.workspace.cost_guard_seconds or metered,
        )

    def _step(self, cid: str, reason: PlanReason, code: str) -> PlanStepRecord:
        cell = self.doc.cells[cid]
        is_sql = cell.kind == "sql"
        live = [c.id for c in self.doc.live()]
        return PlanStepRecord(
            cell_id=cid,
            name=cell.name,
            reason=reason,
            index=live.index(cid) if cid in live else None,
            code=code,
            kind=cell.kind,
            sql=code if is_sql else None,
            connection=cell.meta.get("connection") if is_sql else None,
            interpolated=is_sql and _INTERPOLATION.search(code) is not None,
        )

    def widget_users(self, model_id: str) -> list[str]:
        widget = self.widgets.get(model_id)
        if widget is None:
            raise NotebookToolError("widget_not_found", f"No widget {model_id!r}.")
        graph = self.kernel_graph()
        return [c.id for c in self.doc.live() if set(graph.cells[c.id].refs) & set(widget.names)]

    def _duration(self, cid: str) -> float:
        if cid in self.duration_override:
            return self.duration_override[cid]
        state = self.kstate.get(cid)
        return state.duration_s if state is not None else 0.0

    # -- running ---------------------------------------------------------------------

    def request_run(
        self,
        target: EngineRunTarget,
        actor: ActorRef,
        *,
        confirm_expensive: bool,
        trigger: str = "run",
    ) -> RunRecord:
        preview = self.plan(target, actor)
        if preview.blocked:
            raise NotebookToolError("upstream_being_edited", preview.blocked)
        if preview.needs_confirmation and not confirm_expensive:
            return RunRecord(
                run_id=None,
                status="needs_confirmation",
                plan=preview.steps,
                estimate_s=preview.estimate_s,
                requested_by=actor,
                trigger=trigger,
            )
        self.run_seq += 1
        run = RunLog(
            run_id=f"r{self.run_seq}",
            requested_by=actor,
            trigger=trigger,
            steps=preview.steps,
            status="queued",
            queued_at=self.now(),
        )
        queued_behind = [r.run_id for r in self.queue]
        self.runs.append(run)
        self.queue.append(run)
        self._ensure_worker()
        return self._record(run, queued_behind=queued_behind, estimate=preview.estimate_s)

    def _record(
        self, run: RunLog, *, queued_behind: list[str] | None = None, estimate: float | None = None
    ) -> RunRecord:
        return RunRecord(
            run_id=run.run_id,
            status=run.status,
            plan=run.steps,
            estimate_s=estimate,
            queued_behind=queued_behind or [],
            requested_by=run.requested_by,
            trigger=run.trigger,
        )

    def _ensure_worker(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        while self.queue:
            run = self.queue[0]
            await self._execute(run)
            self.queue.pop(0)
            run.done.set()

    async def _execute(self, run: RunLog) -> None:
        if self.kernel_state == "absent":
            self.kernel_started_at = self.now()
            self._reset_kernel_namespace()
        self.kernel_state = "busy"
        run.status = "running"
        run.started_at = self.now()
        for name in list(self.cleared_names):
            self.namespace.pop(name, None)
        self.cleared_names.clear()
        for step in run.steps:
            for name in self.kstate.get(step.cell_id, CellKernelState()).bound:
                self.namespace.pop(name, None)
        graph = self.kernel_graph({s.cell_id: s.code for s in run.steps})
        failed: set[str] = set()
        interrupted = False
        first_error: str | None = None
        for step in run.steps:
            await asyncio.sleep(0)
            if self._kernel_gone():
                interrupted = True
            if self.interrupt_requested == run.run_id or interrupted:
                interrupted = True
                self._finish_step(run, step, "interrupted", None)
                continue
            if graph.ancestors(step.cell_id) & failed:
                failed.add(step.cell_id)
                self._finish_step(run, step, "skipped", None)
                continue
            run.executed.append(step.cell_id)
            self.executions.append((run.run_id, step.cell_id))
            errors = graph.errors.get(step.cell_id)
            if errors:
                failed.add(step.cell_id)
                ename = {
                    "multiple_definitions": "MultipleDefinitionError",
                    "cycle": "CycleError",
                    "syntax_error": "SyntaxError",
                    "delete_nonlocal": "DeleteNonlocalError",
                }.get(errors[0], "GraphError")
                record = ErrorRecord(
                    ename=ename, evalue=f"{errors[0].replace('_', ' ')} in this cell"
                )
                self._finish_step(run, step, "error", record)
                first_error = first_error or ename
                continue
            error = self._exec_step(run, step)
            if error is not None:
                failed.add(step.cell_id)
                first_error = first_error or error.ename
                self._finish_step(run, step, "error", error)
            else:
                self._finish_step(run, step, "ok", None)
        run.status = "interrupted" if interrupted else "finished"
        run.finished_at = self.now()
        if self.interrupt_requested == run.run_id:
            self.interrupt_requested = None
        if self.kernel_state == "busy":
            self.kernel_state = "idle" if len(self.queue) <= 1 else "busy"
        statuses = self.statuses()
        self.log(
            run.requested_by,
            "run",
            run_id=run.run_id,
            status=run.status,
            error_class=first_error,
            cells=[
                (s.cell_id, self.doc.cells[s.cell_id].name, statuses.get(s.cell_id, "not_run"))
                for s in run.steps
                if s.cell_id in self.doc.cells
            ],
        )

    def _kernel_gone(self) -> bool:
        """Whether the kernel was shut down while a run was executing."""
        return self.kernel_state == "absent"

    def _finish_step(
        self,
        run: RunLog,
        step: PlanStepRecord,
        result: Literal["ok", "error", "interrupted", "skipped"],
        error: ErrorRecord | None,
    ) -> None:
        state = self.kstate.setdefault(step.cell_id, CellKernelState())
        if result in ("ok", "error"):
            self.step_seq += 1
            state.seq = self.step_seq
            state.submitted = step.code
        state.result = result
        state.has_value = result == "ok"
        if error is not None:
            state.output = OutputRecord(
                kinds=["application/vnd.alkera.error+json"],
                error=error,
                author=run.requested_by.display_name,
            )
        state.last_run = NotebookRunAttribution(
            run_id=run.run_id,
            by=run.requested_by.display_name,
            trigger=run.trigger,
            finished_at=self.now(),
        )

    def _import(self, name: str, *args: Any, **kwargs: Any) -> Any:
        top = name.split(".")[0]
        if top == "alkera":
            return _alkera_stand_in(self)
        if top in INSTALLABLE.values():
            if top not in self.installed:
                raise ModuleNotFoundError(f"No module named '{top}'", name=top)
            return types.ModuleType(top)
        if top.startswith("simmissing"):
            raise ModuleNotFoundError(f"No module named '{top}'", name=top)
        return __import__(name, *args, **kwargs)

    def _reset_kernel_namespace(self) -> None:
        builtins_ns = dict(vars(builtins))
        builtins_ns["__import__"] = self._import
        builtins_ns["display"] = lambda *values: None
        self.namespace = {"__builtins__": builtins_ns, "__name__": "__main__"}

    def _exec_step(self, run: RunLog, step: PlanStepRecord) -> ErrorRecord | None:
        cell = self.doc.cells[step.cell_id]
        stdout = io.StringIO()
        started = asyncio.get_running_loop().time()
        state = self.kstate.setdefault(step.cell_id, CellKernelState())
        before = set(self.namespace)
        value: Any = None
        has_value = False
        try:
            with contextlib.redirect_stdout(stdout):
                if cell.kind == "markdown":
                    text = step.code
                    if cell.meta.get("quote") == "rf":
                        text = self._interpolate(text)
                    value = _FakeMarkdown(text)
                    has_value = True
                elif cell.kind == "sql":
                    frame = self.run_sql(self._interpolate(step.code), cell.meta.get("connection"))
                    self.namespace[str(cell.meta.get("output_var", "_df"))] = frame
                    value, has_value = frame, True
                else:
                    tree = ast.parse(step.code)
                    last: ast.expr | None = None
                    if tree.body and isinstance(tree.body[-1], ast.Expr):
                        last = tree.body.pop().value  # type: ignore[attr-defined]
                    exec(compile(tree, f"<cell {cell.name}>", "exec"), self.namespace)  # noqa: S102
                    if last is not None:
                        expr = ast.Expression(last)
                        value = eval(compile(expr, f"<cell {cell.name}>", "eval"), self.namespace)  # noqa: S307
                        has_value = value is not None
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            tb = "".join(traceback.format_exception(exc)[-6:])
            state.output = OutputRecord(
                kinds=["application/vnd.alkera.error+json"],
                text=stdout.getvalue(),
                error=ErrorRecord(ename=type(exc).__name__, evalue=str(exc), traceback=tb),
                author=run.requested_by.display_name,
            )
            state.duration_s = asyncio.get_running_loop().time() - started
            return state.output.error
        text = stdout.getvalue()
        kinds = ["text/plain"] if text or has_value else []
        if has_value:
            rendered = value.text if isinstance(value, _FakeMarkdown) else reprlib.repr(value)
            text = f"{text}{rendered}" if text else rendered
            if isinstance(value, _FakeMarkdown):
                kinds = ["text/markdown"]
            elif isinstance(value, _FakeChart):
                kinds = ["application/vnd.alkera.chart+json", "text/plain"]
            elif _is_frame(value):
                kinds = ["application/vnd.alkera.table+json", "text/plain"]
        state.output = (
            None
            if not kinds
            else OutputRecord(
                kinds=kinds,
                text=text,
                author=run.requested_by.display_name,
                has_table=_is_frame(value),
                has_widget=isinstance(value, _FakeWidget),
                has_chart=isinstance(value, _FakeChart),
            )
        )
        state.duration_s = asyncio.get_running_loop().time() - started
        public = _public_defs(cell.kind, step.code, cell.meta)
        bound = [n for n in self.namespace if n not in before or n in public]
        state.bound = [n for n in bound if not n.startswith("__")]
        self._register_widgets(step.cell_id, state.bound)
        return None

    def _interpolate(self, text: str) -> str:
        if "{" not in text:
            return text
        try:
            return str(eval("f" + repr(text), self.namespace))  # noqa: S307
        except Exception as exc:
            raise ValueError(f"interpolation failed: {type(exc).__name__}") from exc

    def run_sql(self, query: str, connection: str | None) -> Any:
        import duckdb

        con = duckdb.connect()
        try:
            source = self.workspace.warehouse if connection else self.namespace
            for name, value in source.items():
                if _is_frame(value):
                    con.register(name, value)
            return con.execute(query).df()
        finally:
            con.close()

    def select_from_frame(self, value: Any, query: str) -> Any:
        """One ``SELECT`` statement over the table ``frame``, refused otherwise."""
        import duckdb
        import sqlglot

        try:
            statements = sqlglot.parse(query, read="duckdb")
        except Exception as exc:
            raise NotebookToolError("invalid_filter", "filter_sql does not parse.") from exc
        if len(statements) != 1 or not isinstance(statements[0], sqlglot.exp.Select):
            raise NotebookToolError("invalid_filter", "filter_sql must be one SELECT over frame.")
        con = duckdb.connect()
        try:
            con.register("frame", value)
            return con.execute(query).df()
        except duckdb.Error as exc:
            raise NotebookToolError(
                "invalid_filter", f"filter_sql failed: {type(exc).__name__}"
            ) from exc
        finally:
            con.close()

    def _register_widgets(self, cell_id: str, names: Sequence[str]) -> None:
        for model_id in [m for m, w in self.widgets.items() if w.cell_id == cell_id]:
            del self.widgets[model_id]
        for name in names:
            value = self.namespace.get(name)
            if isinstance(value, _FakeWidget):
                model_id = f"w-{cell_id}-{name}"
                self.widgets[model_id] = Widget(
                    model_id, cell_id, value.kind, value.value, [name], value.sensitive
                )

    # -- kernel actions -------------------------------------------------------------

    async def shutdown_kernel(
        self, reason: str = "shutdown", actor: ActorRef | None = None
    ) -> None:
        for run in list(self.queue):
            if run.status == "queued":
                run.status = "cancelled"
                run.finished_at = self.now()
                run.done.set()
        if self.worker is not None and not self.worker.done():
            self.kernel_state = "absent"
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self.worker), timeout=5)
        self.queue = [r for r in self.queue if not r.done.is_set()]
        self.kernel_state = "absent"
        self.namespace = {}
        for state in self.kstate.values():
            state.has_value = False
            state.result = "none"
            state.submitted = None
        self.widgets.clear()
        if actor is not None:
            self.log(
                actor,
                "kernel",
                status="stopped" if reason == "shutdown" else "restarted",
                reason=reason,
            )

    def kernel_info(self) -> NotebookKernelInfo:
        state: Literal["absent", "idle", "busy"] = self.kernel_state
        return NotebookKernelInfo(
            state=state,
            env=self.env_info(),
            reactivity=self.doc.settings.get("reactivity", "autorun"),
            memory_bytes=None,
            started_at=self.kernel_started_at if state != "absent" else None,
            queue=[
                NotebookQueuedRun(
                    run_id=r.run_id,
                    by=r.requested_by.display_name,
                    trigger=r.trigger,
                    status=r.status,
                )
                for r in self.queue
            ],
        )

    def env_info(self) -> NotebookEnvInfo:
        env = str(self.doc.settings.get("env", "default"))
        return NotebookEnvInfo(
            env_id=env,
            kind="default" if env == "default" else "venv",
            spec_root=".alkera/envs/default" if env == "default" else env,
            python=f"{sys.version_info.major}.{sys.version_info.minor}",
            state="ready",
            recorded_in_file="env" in self.doc.settings,
        )


def _is_frame(value: Any) -> bool:
    return type(value).__name__ == "DataFrame"


def _public_defs(kind: str, code: str, meta: Any) -> set[str]:
    from alkera_notebook.sim.analysis import analyze

    return set(analyze([("x", kind, code, meta)]).cells["x"].defs)


class ReferencePort:
    """One notebook as one actor sees it."""

    def __init__(self, notebook: ReferenceNotebook, actor: ActorRef) -> None:
        self.notebook = notebook
        self._actor = actor

    @property
    def actor(self) -> ActorRef:
        return self._actor

    # -- reads -----------------------------------------------------------------------

    def _cells(self, refs: Sequence[str] | None) -> list[Cell]:
        nb = self.notebook
        if refs is None:
            return nb.doc.live()
        out: list[Cell] = []
        for ref in refs:
            cell = nb.doc.find(ref)
            if cell is None:
                raise NotebookToolError("cell_not_found", f"No cell {ref!r} in {nb.path}.")
            out.append(cell)
        return out

    async def read(
        self, cells: Sequence[str] | None, *, include_source: bool, include_outputs: bool
    ) -> ViewRecord:
        nb = self.notebook
        statuses = nb.statuses()
        graph = nb.document_graph()
        index = {c.id: i for i, c in enumerate(nb.doc.live())}
        now = nb.now()
        records = []
        for cell in self._cells(cells):
            state = nb.kstate.get(cell.id)
            records.append(
                CellRecord(
                    id=cell.id,
                    name=cell.name,
                    kind=cell.kind,
                    index=index[cell.id],
                    status=statuses[cell.id],
                    defs=list(graph.cells[cell.id].defs),
                    refs=list(graph.cells[cell.id].refs),
                    graph_errors=list(graph.errors.get(cell.id, [])),
                    source=cell.source if include_source else None,
                    source_author=nb.source_author.get(cell.id, ""),
                    meta=dict(cell.meta),
                    output=state.output if include_outputs and state is not None else None,
                    output_outdated=False,
                    last_run=state.last_run if state is not None else None,
                    config=dict(cell.config),
                )
            )
        presence = [
            NotebookPresence(who=actor.display_name, cell_id=cid)
            for cid, (actor, at) in nb.editing.items()
            if actor.id != self._actor.id and now - at <= EDITING_WINDOW and cid in index
        ]
        return ViewRecord(
            token=str(nb.doc.version),
            settings=dict(nb.doc.settings),
            kernel=nb.kernel_info(),
            cells=records,
            presence=presence,
        )

    async def apply(self, ops: Sequence[NotebookOp], base_token: str | None) -> OpsRecord:
        return self.notebook.apply(ops, base_token, self._actor)

    async def preview(self, target: EngineRunTarget) -> PlanPreview:
        return self.notebook.plan(target, self._actor)

    async def run(self, target: EngineRunTarget, *, confirm_expensive: bool) -> RunRecord:
        return self.notebook.request_run(target, self._actor, confirm_expensive=confirm_expensive)

    async def wait(self, run_id: str, timeout_s: float) -> RunRecord:
        nb = self.notebook
        run = next((r for r in nb.runs if r.run_id == run_id), None)
        if run is None:
            raise NotebookToolError("run_not_found", f"No run {run_id}.")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(run.done.wait(), timeout=timeout_s)
        return nb._record(run)

    async def kernel(self, action: KernelActionName) -> KernelRecord:
        nb = self.notebook
        if action in ("interrupt", "interrupt_all"):
            if nb.queue:
                nb.interrupt_requested = nb.queue[0].run_id
                nb.log(self._actor, "kernel", status="interrupted", reason=action)
            if action == "interrupt_all":
                for run in nb.queue[1:]:
                    if run.status == "queued":
                        run.status = "cancelled"
                        run.finished_at = nb.now()
                        run.done.set()
        elif action in ("restart", "shutdown"):
            await nb.shutdown_kernel(action, self._actor)
            if action == "restart":
                nb.kernel_state = "idle"
                nb.kernel_started_at = nb.now()
                nb._reset_kernel_namespace()
        return KernelRecord(
            kernel=nb.kernel_info(),
            runs=[nb._record(r) for r in nb.runs[-20:]],
        )

    async def clear_outputs(self, cell_ids: Sequence[str] | None) -> list[str]:
        nb = self.notebook
        cells = [c.id for c in self._cells(cell_ids)]
        cleared: list[str] = []
        for cid in cells:
            state = nb.kstate.get(cid)
            if state is None or (state.output is None and not state.images):
                continue
            state.output = None
            state.last_run = None
            state.images = []
            cleared.append(cid)
        return cleared

    async def output(self, cell: str, part: OutputPart) -> OutputDetailRecord:
        nb = self.notebook
        target = nb.doc.find(cell)
        if target is None:
            raise NotebookToolError("cell_not_found", f"No cell {cell!r} in {nb.path}.")
        state = nb.kstate.get(target.id)
        output = state.output if state is not None else None
        widgets = [
            WidgetRecord(
                model_id=w.model_id,
                cell_id=w.cell_id,
                type=w.type,
                value=w.value,
                sensitive=w.sensitive,
            )
            for w in nb.widgets.values()
            if w.cell_id == target.id
        ]
        if output is None:
            return OutputDetailRecord(cell_id=target.id)
        detail = OutputDetailRecord(
            cell_id=target.id,
            text=output.text if part in ("all", "text") else "",
            error=output.error if part in ("all", "error") else None,
            images=[ImageRecord(mime="image/png", data=data) for data in state.images]
            if state is not None and part in ("all", "image")
            else [],
            widgets=widgets if part in ("all", "widget") else [],
            run=state.last_run if state is not None else None,
            author=output.author,
        )
        if part in ("all", "table") and output.has_table:
            name = target.name if target.kind == "sql" else None
            value = nb.namespace.get(name) if name else None
            if value is not None and _is_frame(value):
                detail = detail.model_copy(
                    update={
                        "table": {
                            "columns": [str(c) for c in value.columns],
                            "rows": value.head(10).astype(str).values.tolist(),
                            "total_rows": len(value),
                            "source": {"name": name},
                        }
                    }
                )
        return detail

    async def variables(self) -> list[VariableRecord]:
        nb = self.notebook
        owner: dict[str, str] = {}
        for cid, state in nb.kstate.items():
            for name in state.bound:
                owner[name] = cid
        out: list[VariableRecord] = []
        for name, value in nb.namespace.items():
            if name.startswith("_") or isinstance(value, types.ModuleType) or name not in owner:
                continue
            shape = list(value.shape) if _is_frame(value) else None
            columns = [str(c) for c in value.columns] if _is_frame(value) else None
            out.append(
                VariableRecord(
                    name=name,
                    type=type(value).__name__,
                    cell_id=owner.get(name),
                    repr=reprlib.repr(value)[:200],
                    shape=shape,
                    columns=columns,
                    author=nb.source_author.get(owner.get(name, ""), ""),
                )
            )
        return sorted(out, key=lambda v: v.name)

    async def frame(
        self, name: str, *, offset: int, limit: int, sort: str | None, filter_sql: str | None
    ) -> FrameRecord:
        nb = self.notebook
        value = nb.namespace.get(name)
        if value is None or not _is_frame(value):
            raise NotebookToolError("not_a_frame", f"{name!r} is not a data frame in the kernel.")
        frame = value
        if filter_sql:
            frame = nb.select_from_frame(value, filter_sql)
        try:
            keys = parse_sort_spec(sort)
        except ValueError as exc:
            raise NotebookToolError("invalid_sort", str(exc)) from exc
        if keys:
            for key in keys:
                if key["column"] not in frame.columns:
                    raise NotebookToolError("invalid_sort", f"No column {key['column']!r}.")
            frame = frame.sort_values(
                [key["column"] for key in keys],
                ascending=[not key["descending"] for key in keys],
                kind="stable",
            )
        page = frame.iloc[offset : offset + limit]
        return FrameRecord(
            name=name,
            columns=[str(c) for c in frame.columns],
            rows=page.astype(object).where(page.notna(), None).values.tolist(),
            total_rows=len(frame),
            offset=offset,
        )

    async def value(self, name: str) -> ValueRecord:
        nb = self.notebook
        if name not in nb.namespace:
            raise NotebookToolError("name_not_found", f"No value {name!r} in the kernel.")
        return ValueRecord(name=name, summary=reprlib.repr(nb.namespace[name])[:2000])

    async def graph(
        self, cell: str | None, direction: Literal["both", "up", "down"]
    ) -> GraphRecord:
        nb = self.notebook
        graph = nb.kernel_graph()
        statuses = nb.statuses()
        cells = {
            c.id: (
                c.name,
                list(graph.cells[c.id].defs),
                list(graph.cells[c.id].refs),
                statuses[c.id],
            )
            for c in nb.doc.live()
        }
        upstream: list[str] = []
        downstream: list[str] = []
        if cell is not None:
            found = nb.doc.find(cell)
            if found is None:
                raise NotebookToolError("cell_not_found", f"No cell {cell!r} in {nb.path}.")
            order = [c.id for c in nb.doc.live()]
            if direction in ("both", "up"):
                upstream = [c for c in order if c in graph.ancestors(found.id)]
            if direction in ("both", "down"):
                downstream = [c for c in order if c in graph.descendants(found.id)]
        return GraphRecord(
            cells=cells,
            edges=list(graph.edges),
            upstream=upstream,
            downstream=downstream,
            errors=[
                NotebookGraphError(cell_id=cid, kind=kind, names=nb._error_names(graph, cid, kind))
                for cid, kinds in graph.errors.items()
                for kind in kinds
            ],
        )

    async def widgets(self) -> list[WidgetRecord]:
        return [
            WidgetRecord(
                model_id=w.model_id,
                cell_id=w.cell_id,
                type=w.type,
                value=w.value,
                sensitive=w.sensitive,
            )
            for w in self.notebook.widgets.values()
        ]

    async def preview_widget(self, model_id: str, state: dict[str, Any]) -> PlanPreview:
        del state
        users = self.notebook.widget_users(model_id)
        if not users:
            return PlanPreview(steps=[])
        return self.notebook.plan(RunCells(ids=users), self._actor)

    async def set_widget(self, model_id: str, state: dict[str, Any]) -> RunRecord | None:
        nb = self.notebook
        widget = nb.widgets.get(model_id)
        if widget is None:
            raise NotebookToolError("widget_not_found", f"No widget {model_id!r}.")
        if "value" not in state:
            raise NotebookToolError("invalid_state", "A widget state needs `value`.")
        widget.value = state["value"]
        live = nb.namespace.get(widget.names[0]) if widget.names else None
        if isinstance(live, _FakeWidget):
            live.value = state["value"]
        users = nb.widget_users(model_id)
        if not users:
            return None
        record = nb.request_run(
            RunCells(ids=users), self._actor, confirm_expensive=True, trigger="widget"
        )
        if record.run_id is not None:
            return await self.wait(record.run_id, 30)
        return record

    async def env(
        self, action: EnvActionName, packages: Sequence[str], env: str | None
    ) -> EnvRecord:
        nb = self.notebook
        if action == "switch":
            await self.settings({"env": env})
            return EnvRecord(env=nb.env_info())
        if action == "materialize":
            return EnvRecord(env=nb.env_info(), log="Already built.")
        info = nb.env_info()
        if action == "install":
            unknown = [p for p in packages if p not in INSTALLABLE]
            if unknown:
                raise NotebookToolError(
                    "unknown_distribution",
                    f"No distribution named {', '.join(unknown)} in this environment's index.",
                )
            nb.installed.update(INSTALLABLE[p] for p in packages)
            nb.log(self._actor, "env", status="installed")
            return EnvRecord(
                env=info,
                spec_changed=[f"{info.spec_root}/pyproject.toml"],
                log="\n".join(f"Installed {p}" for p in packages),
                packages=self._packages(),
            )
        return EnvRecord(
            env=info,
            envs=[info] if action == "list" else [],
            packages=self._packages() if action == "packages" else [],
        )

    def _packages(self) -> list[NotebookPackage]:
        base = [
            NotebookPackage(name="pandas", version="2"),
            NotebookPackage(name="duckdb", version="1"),
        ]
        return base + [
            NotebookPackage(name=p, version="1.0") for p in sorted(self.notebook.installed)
        ]

    async def settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        from alkera_notebook.tools.models import SetSettingOp

        nb = self.notebook
        if not changes:
            return dict(nb.doc.settings)
        nb.apply([SetSettingOp(key=k, value=v) for k, v in changes.items()], None, self._actor)
        if "env" in changes:
            await nb.shutdown_kernel("env_changed", self._actor)
        return dict(nb.doc.settings)

    async def activity(self, since: datetime) -> Activity:
        nb = self.notebook
        items = [i for i in nb.activity if i.at > since]
        statuses = nb.statuses()
        stale = [(c.id, c.name) for c in nb.doc.live() if statuses[c.id] == "stale"]
        cursor = max([since, *(i.at for i in items)])
        return Activity(path=nb.path, items=items, stale=stale, cursor=cursor)


__all__ = [
    "EDITING_WINDOW",
    "INSTALLABLE",
    "CellKernelState",
    "FakeClock",
    "ReferenceHost",
    "ReferenceNotebook",
    "ReferencePort",
    "ReferenceWorkspace",
    "RunLog",
]
