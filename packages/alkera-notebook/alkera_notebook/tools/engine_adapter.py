"""The tools' port over the open core engine (``alkera_notebook.engine``).

:class:`EngineWorkspace` holds one :class:`~alkera_notebook.engine.NotebookEngine`
for one workspace root and hands out :class:`EngineHost` objects per actor.
:class:`EnginePort` translates between the tools' port records and the
engine's client models; it adds no behaviour of its own, so what the tools
report is what the engine decided.

:func:`engine_host_factory` is what a harness installs: a factory that keeps
one engine per workspace root for the life of the process, built from the
engine's local seams (the file document store, the local subprocess launcher,
a Unix socket transport, process RSS memory, the local environment registry).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import TypeAdapter

from alkera_notebook.tools.models import (
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
    NotebookHost,
    NotebookToolError,
    OpsRecord,
    OutputDetailRecord,
    OutputRecord,
    PlanPreview,
    PlanStepRecord,
    RunRecord,
    RunStatus,
    ValueRecord,
    VariableRecord,
    ViewRecord,
    WidgetRecord,
)

if TYPE_CHECKING:
    from alkera_notebook import engine as eng

HostFactory = Callable[[Path, ActorRef], NotebookHost]
NOTEBOOK_SUFFIX = ".alknb.py"
#: The memory budget a local engine guards when nothing else sets one.
DEFAULT_BUDGET_BYTES = 8 * 1024**3
#: The most output text the tools take from the engine: more than a cell's
#: streams keep (the first 1 MiB and the last 960 KiB), so ``notebook.output``
#: can page through all of it.
OUTPUT_TEXT_CEILING = 4 * 1024 * 1024
_NO_ENV = NotebookEnvInfo(
    env_id="none", kind="none", spec_root="", python="", state="missing", recorded_in_file=False
)

_ENGINE_STATUS: dict[str, RunStatus] = {
    "queued": "queued",
    "running": "running",
    "ok": "finished",
    "error": "finished",
    "interrupted": "interrupted",
    "kernel_restarted": "failed",
    "refused": "failed",
    "needs_confirmation": "needs_confirmation",
    "coalesced": "queued",
    "planned": "queued",
}


def graph_error(cell_id: str, error: Any) -> NotebookGraphError:
    """A graph error from the engine (its structured error, the format's error
    object, or a bare code)."""
    from alkera_notebook.document.ops import GraphErrorInfo

    info = GraphErrorInfo.parse(error)
    return NotebookGraphError(
        cell_id=cell_id, kind=info.code, names=[info.name] if info.name else []
    )


def graph_error_text(error: Any) -> str:
    """A graph error as one line a model reads: the code, and the name it is about."""
    err = graph_error("", error)
    return f"{err.kind}: {', '.join(err.names)}" if err.names else err.kind


def engine_available() -> bool:
    """Whether ``alkera_notebook.engine`` is importable in this build."""
    return importlib.util.find_spec("alkera_notebook.engine") is not None


def _engine_actor(actor: ActorRef) -> eng.Actor:
    from alkera_notebook.engine import Actor

    return Actor(
        kind=actor.kind,
        id=actor.id,
        display_name=actor.display_name,
        can_edit=True,
        can_run=True,
        acting_for=actor.acting_for,
    )


def _actor_ref(actor: Any) -> ActorRef:
    """The tools' record of an engine actor (a requester, an activity's actor)."""
    return ActorRef(
        kind=actor.kind, id=actor.id, display_name=actor.display_name, acting_for=actor.acting_for
    )


def _attribution(run: Any) -> NotebookRunAttribution:
    """A run's attribution as the tools report it: who, by name."""
    return NotebookRunAttribution(
        run_id=run.run_id, by=run.by.label(), trigger=run.trigger, finished_at=run.finished_at
    )


def _target(target: EngineRunTarget) -> Any:
    from alkera_notebook.engine import (
        AboveTarget,
        AllTarget,
        BelowTarget,
        CellsTarget,
        StaleTarget,
    )

    if isinstance(target, RunCells):
        return CellsTarget(ids=list(target.ids))
    if isinstance(target, RunAll):
        return AllTarget()
    if isinstance(target, RunStale):
        return StaleTarget()
    if isinstance(target, RunAbove):
        return AboveTarget(id=target.id)
    if isinstance(target, RunBelow):
        return BelowTarget(id=target.id)
    raise NotebookToolError("unresolved_target", f"The engine cannot plan target {target!r}.")


def _translate_error(exc: Exception) -> NotebookToolError:
    from alkera_notebook.document.ops import NotebookOpError
    from alkera_notebook.engine import (
        EngineClosedError,
        ForbiddenError,
        KernelUnavailableError,
        NotFoundError,
        ReadOnlyError,
    )

    if isinstance(exc, NotebookOpError):
        return NotebookToolError(exc.code, exc.message, op_index=exc.index)
    if isinstance(exc, NotFoundError):
        return NotebookToolError("not_found", str(exc))
    if isinstance(exc, ForbiddenError):
        return NotebookToolError("forbidden", str(exc))
    if isinstance(exc, ReadOnlyError):
        return NotebookToolError("read_only", str(exc))
    if isinstance(exc, KernelUnavailableError | EngineClosedError):
        return NotebookToolError("kernel_unavailable", str(exc))
    if isinstance(exc, ValueError):
        return NotebookToolError("invalid", str(exc))
    raise exc


class EnginePort:
    """One notebook, through one attached engine client."""

    def __init__(
        self, session: eng.NotebookSession, client: eng.NotebookClient, actor: ActorRef
    ) -> None:
        self._session = session
        self._client = client
        self._actor = actor
        self._handles: dict[str, eng.RunHandle] = {}

    @property
    def actor(self) -> ActorRef:
        return self._actor

    @property
    def path(self) -> str:
        return self._session.path

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except NotebookToolError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    # -- reading -----------------------------------------------------------------

    def _env(self, env: Any) -> NotebookEnvInfo:
        return NotebookEnvInfo.model_validate(env.model_dump()) if env is not None else _NO_ENV

    def _kernel(self, info: Any) -> NotebookKernelInfo:
        return NotebookKernelInfo(
            state=info.state,
            env=self._env(info.env),
            reactivity=info.reactivity,
            memory_bytes=info.memory_bytes,
            started_at=info.started_at,
            queue=[NotebookQueuedRun.model_validate(q.model_dump()) for q in info.queue],
        )

    async def read(
        self, cells: Sequence[str] | None, *, include_source: bool, include_outputs: bool
    ) -> ViewRecord:
        from alkera_notebook.engine import ReadQuery

        view = await self._call(
            self._client.read(
                ReadQuery(
                    cells=list(cells) if cells is not None else None,
                    include_source=include_source,
                    include_outputs=include_outputs,
                    include_output_items=False,
                )
            )
        )
        if cells is not None:
            known = {c.id for c in view.cells} | {c.name for c in view.cells}
            missing = [ref for ref in cells if ref not in known]
            if missing:
                raise NotebookToolError("cell_not_found", f"No cell {missing[0]!r} in {view.path}.")
        records = []
        for cell in view.cells:
            by = cell.last_run.by.label() if cell.last_run is not None else ""
            output = None
            if cell.output is not None:
                o = cell.output
                output = OutputRecord(
                    kinds=list(o.kinds),
                    text=o.text,
                    error=(
                        ErrorRecord(
                            ename=o.error.ename,
                            evalue=o.error.evalue,
                            traceback="\n".join(o.error.traceback),
                        )
                        if o.error is not None
                        else None
                    ),
                    truncated=o.truncated,
                    has_image=o.has_image,
                    has_chart=o.has_chart,
                    has_table=o.has_table,
                    has_widget=o.has_widget,
                    author=by,
                )
            records.append(
                CellRecord(
                    id=cell.id,
                    name=cell.name,
                    kind=cell.kind,
                    index=cell.index,
                    status=cell.status,
                    defs=list(cell.defs),
                    refs=list(cell.refs),
                    graph_errors=[graph_error_text(e) for e in cell.graph_errors],
                    source=cell.source,
                    meta=dict(cell.meta),
                    output=output,
                    output_outdated=cell.output_outdated,
                    last_run=_attribution(cell.last_run) if cell.last_run is not None else None,
                    config=dict(cell.config),
                )
            )
        settings = view.settings.model_dump(exclude_none=True)
        return ViewRecord(
            token=view.token,
            settings=settings,
            kernel=self._kernel(view.kernel),
            cells=records,
            presence=[NotebookPresence(who=p.who, cell_id=p.cell_id) for p in view.presence],
        )

    async def apply(self, ops: Sequence[NotebookOp], base_token: str | None) -> OpsRecord:
        from alkera_notebook.document.ops import NotebookOp as EngineOp

        engine_ops = TypeAdapter(list[EngineOp]).validate_python([op.model_dump() for op in ops])
        result = await self._call(self._client.apply(engine_ops, base_token))
        view = await self._call(self._client.read())
        return OpsRecord(
            **result.model_dump(),
            stale=[c.id for c in view.cells if c.status == "stale"],
        )

    # -- running -------------------------------------------------------------------

    @staticmethod
    def _steps(entries: Sequence[Any]) -> list[PlanStepRecord]:
        return [
            PlanStepRecord(
                cell_id=e.cell_id,
                name=e.name,
                reason=e.reason,
                index=e.index,
                code=e.code or "",
                kind=e.kind or "python",
                sql=e.sql,
                connection=e.connection,
                interpolated=e.interpolated,
            )
            for e in entries
        ]

    async def preview(self, target: EngineRunTarget) -> PlanPreview:
        handle = await self._call(self._client.run(_target(target), plan_only=True))
        info = handle.info
        blocked = None
        if info.status == "refused":
            blocked = f"The run cannot start: {info.reason or 'refused'}."
        return PlanPreview(
            steps=self._steps(info.plan), estimate_s=info.estimate_s, blocked=blocked
        )

    def _record(self, info: Any) -> RunRecord:
        return RunRecord(
            run_id=info.run_id,
            status=_ENGINE_STATUS.get(info.status, "failed"),
            plan=self._steps(info.plan),
            estimate_s=getattr(info, "estimate_s", None),
            queued_behind=list(getattr(info, "queued_behind", [])),
            requested_by=self._actor,
            trigger=info.trigger,
        )

    async def run(self, target: EngineRunTarget, *, confirm_expensive: bool) -> RunRecord:
        handle = await self._call(
            self._client.run(_target(target), confirm_expensive=confirm_expensive)
        )
        info = handle.info
        if info.status == "refused":
            raise NotebookToolError(
                info.reason or "refused", f"The run was refused: {info.reason or 'refused'}."
            )
        if info.status == "needs_confirmation":
            return RunRecord(
                run_id=None,
                status="needs_confirmation",
                plan=self._steps(info.plan),
                estimate_s=info.estimate_s,
                requested_by=self._actor,
                trigger=info.trigger,
            )
        run_id = info.joined or info.run_id
        self._handles[run_id] = handle
        return self._record(info).model_copy(update={"run_id": run_id})

    async def wait(self, run_id: str, timeout_s: float) -> RunRecord:
        handle = self._handles.get(run_id)
        records = self._session.runtime.records
        if handle is not None:
            try:
                await handle.wait(timeout_s)
            except TimeoutError:
                pass
        record = records.get(run_id)
        if record is None:
            raise NotebookToolError("run_not_found", f"No run {run_id}.")
        return RunRecord(
            run_id=record.run_id,
            status=_ENGINE_STATUS.get(record.status, "failed"),
            plan=self._steps(record.plan),
            requested_by=_actor_ref(record.requested_by),
            trigger=record.trigger,
        )

    async def kernel(self, action: KernelActionName) -> KernelRecord:
        info = await self._call(self._client.kernel(action))
        runs = []
        for record in list(self._session.runtime.records.values())[-20:]:
            runs.append(
                RunRecord(
                    run_id=record.run_id,
                    status=_ENGINE_STATUS.get(record.status, "failed"),
                    plan=[],
                    requested_by=_actor_ref(record.requested_by),
                    trigger=record.trigger,
                )
            )
        return KernelRecord(kernel=self._kernel(info), runs=runs)

    # -- outputs and values ------------------------------------------------------------

    async def clear_outputs(self, cell_ids: Sequence[str] | None) -> list[str]:
        cleared: list[str] = await self._call(
            self._client.clear_outputs(list(cell_ids) if cell_ids is not None else None)
        )
        return cleared

    async def output(self, cell: str, part: OutputPart) -> OutputDetailRecord:
        detail = await self._call(self._client.output(cell, part, OUTPUT_TEXT_CEILING))
        view = await self._call(self._client.read())
        target = next((c for c in view.cells if cell in (c.id, c.name)), None)
        if target is None:
            raise NotebookToolError("cell_not_found", f"No cell {cell!r}.")
        return OutputDetailRecord(
            cell_id=target.id,
            text=detail.text,
            error=(
                ErrorRecord(
                    ename=detail.error.ename,
                    evalue=detail.error.evalue,
                    traceback="\n".join(detail.error.traceback),
                )
                if detail.error is not None
                else None
            ),
            images=[ImageRecord(mime="image/png", data=base64.b64decode(i)) for i in detail.images],
            chart_spec=detail.chart_spec,
            table=detail.table,
            widgets=[
                WidgetRecord(
                    model_id=str(w.get("model_id", "")),
                    cell_id=w.get("cell_id"),
                    type=str(w.get("type", "")),
                    value=w.get("value"),
                )
                for w in detail.widgets
            ],
            run=_attribution(detail.run) if detail.run is not None else None,
            author=detail.run.by.label() if detail.run is not None else "",
            truncated=detail.truncated,
        )

    async def variables(self) -> list[VariableRecord]:
        from alkera_notebook.engine import InspectQuery

        result = await self._call(self._client.inspect(InspectQuery(what="variables")))
        return [
            VariableRecord(
                name=v.name,
                type=v.type,
                cell_id=v.cell_id,
                repr=v.repr,
                size_bytes=v.size_bytes,
                shape=v.shape,
                columns=[str(c) for c in v.columns] if v.columns is not None else None,
            )
            for v in result.variables or []
        ]

    async def frame(
        self, name: str, *, offset: int, limit: int, sort: str | None, filter_sql: str | None
    ) -> FrameRecord:
        from alkera_notebook.engine import InspectQuery
        from alkera_notebook.engine.sort_spec import parse_sort_spec

        try:
            sort_spec = parse_sort_spec(sort) or None
        except ValueError as exc:
            raise NotebookToolError("invalid_sort", str(exc)) from exc
        result = await self._call(
            self._client.inspect(
                InspectQuery(
                    what="frame",
                    name=name,
                    offset=offset,
                    limit=limit,
                    sort=sort_spec,
                    filter_sql=filter_sql,
                )
            )
        )
        # The kernel's table page, the one a table output carries.
        page = result.table or {}
        return FrameRecord(
            name=name,
            columns=[str(c.get("name", "")) for c in page.get("schema", [])],
            rows=list(page.get("rows", [])),
            total_rows=int(page.get("total_rows", result.total_rows or 0)),
            offset=int(page.get("offset", offset)),
        )

    async def value(self, name: str) -> ValueRecord:
        from alkera_notebook.engine import InspectQuery

        result = await self._call(self._client.inspect(InspectQuery(what="value", name=name)))
        summary = result.summary
        return ValueRecord(
            name=name, summary=summary if isinstance(summary, str) else repr(summary)
        )

    async def graph(
        self, cell: str | None, direction: Literal["both", "up", "down"]
    ) -> GraphRecord:
        view = await self._call(self._client.graph(cell, direction))
        return GraphRecord(
            cells={
                cid: (c.name, list(c.defs), list(c.refs), c.status) for cid, c in view.cells.items()
            },
            edges=[(a, b) for a, b in view.edges],
            upstream=list(view.upstream),
            downstream=list(view.downstream),
            errors=[
                graph_error(cid, error) for cid, errors in view.errors.items() for error in errors
            ],
        )

    # -- widgets, environment, settings ------------------------------------------------

    async def widgets(self) -> list[WidgetRecord]:
        from alkera_notebook.engine import WidgetAction

        result = await self._call(self._client.widget(WidgetAction(action="list")))
        return [
            WidgetRecord(model_id=w.model_id, cell_id=w.cell_id, type=w.type, value=w.value)
            for w in result.widgets
        ]

    async def preview_widget(self, model_id: str, state: dict[str, Any]) -> PlanPreview:
        from alkera_notebook.engine import WidgetAction

        del state
        result = await self._call(self._client.widget(WidgetAction(action="list")))
        widget = next((w for w in result.widgets if w.model_id == model_id), None)
        if widget is None:
            raise NotebookToolError("widget_not_found", f"No widget {model_id!r}.")
        graph = await self._call(self._client.graph(None, "both"))
        users = [cid for cid, c in graph.cells.items() if set(c.refs) & set(widget.bound_names)]
        if not users:
            return PlanPreview(steps=[])
        return await self.preview(RunCells(ids=users))

    async def set_widget(self, model_id: str, state: dict[str, Any]) -> RunRecord | None:
        from alkera_notebook.engine import WidgetAction

        result = await self._call(
            self._client.widget(WidgetAction(action="set", model_id=model_id, state=state))
        )
        if result.run is None:
            return None
        return await self.wait(result.run.run_id, 30)

    async def env(
        self, action: EnvActionName, packages: Sequence[str], env: str | None
    ) -> EnvRecord:
        from alkera_notebook.engine import EnvAction

        result = await self._call(
            self._client.env(EnvAction(action=action, packages=list(packages), env=env))
        )
        return EnvRecord(
            env=self._env(result.env),
            envs=[self._env(e) for e in result.envs],
            packages=[NotebookPackage(name=p.name, version=p.version) for p in result.packages],
            spec_changed=list(result.spec_changed),
            log=result.log,
        )

    async def settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        from alkera_notebook.engine import SettingsChange

        settings = await self._call(self._client.settings(SettingsChange(**changes)))
        return dict(settings.model_dump(exclude_none=True))

    async def activity(self, since: datetime) -> Activity:
        activity = await self._call(self._client.activity(since))
        view = await self._call(self._client.read())
        names = {c.id: c.name for c in view.cells}
        statuses = {c.id: c.status for c in view.cells}
        items: list[ActivityItem] = []
        for entry in activity.entries:
            actor = _actor_ref(entry.actor)
            base = {"at": entry.at, "actor": actor}
            if entry.kind == "cell_run":
                items.append(
                    ActivityItem(
                        **base,
                        kind="run",
                        run_id=entry.run_id,
                        status=entry.status,
                        error_class=entry.error_class,
                        cells=[
                            (cid, names.get(cid, "_"), statuses.get(cid, "not_run"))
                            for cid in entry.cell_ids
                        ],
                    )
                )
            elif entry.kind in ("kernel_restart", "kernel_stop"):
                status = "stopped" if entry.kind == "kernel_stop" else "restarted"
                items.append(
                    ActivityItem(**base, kind="kernel", status=status, reason=entry.reason)
                )
            elif entry.kind == "env_change":
                items.append(ActivityItem(**base, kind="env", status=entry.change))
            elif entry.kind == "settings_change":
                items.append(ActivityItem(**base, kind="settings"))
            else:
                kind = _CELL_ACTIVITY.get(entry.change or "", "edit")
                items.extend(
                    ActivityItem(**base, kind=kind, cell_id=cid, cell_name=names.get(cid))
                    for cid in entry.cell_ids
                )
        cursor = max([since, *(e.at for e in activity.entries)])
        stale = [(c.id, c.name) for c in view.cells if c.status == "stale"]
        return Activity(path=view.path, items=items, stale=stale, cursor=cursor)


# The engine's cell changes as the digest's verbs; a config change reads as an edit.
_CELL_ACTIVITY: dict[str, ActivityKind] = {
    "insert": "insert",
    "edit": "edit",
    "delete": "delete",
    "restore": "restore",
    "move": "move",
    "rename": "rename",
    "kind": "kind",
    "config": "edit",
}


class EngineHost:
    """A workspace's notebooks, for one actor."""

    def __init__(self, workspace: EngineWorkspace, actor: ActorRef) -> None:
        self._workspace = workspace
        self._actor = actor

    @property
    def actor(self) -> ActorRef:
        return self._actor

    def resolve(self, path: str) -> str:
        return self._workspace.resolve(path)

    async def open(self, path: str) -> EnginePort:
        return await self._workspace.port(path, self._actor)

    async def create(
        self, path: str, cells: Sequence[InsertCellOp], settings: dict[str, Any]
    ) -> EnginePort:
        return await self._workspace.create(path, cells, settings, self._actor)

    async def notebooks(self) -> list[str]:
        root = self._workspace.root
        found = await asyncio.to_thread(lambda: sorted(root.rglob(f"*{NOTEBOOK_SUFFIX}"))[:500])
        return [str(PurePosixPath(p.relative_to(root))) for p in found]


class EngineWorkspace:
    """One engine for one workspace root, and the clients it attached."""

    def __init__(self, root: Path, engine: eng.NotebookEngine) -> None:
        self.root = root
        self.engine = engine
        self._ports: dict[tuple[str, str], EnginePort] = {}

    def host(self, actor: ActorRef) -> EngineHost:
        return EngineHost(self, actor)

    def resolve(self, path: str) -> str:
        if not path or "\x00" in path:
            raise NotebookToolError("outside_workspace", "A notebook path is required.")
        candidate = Path(path)
        absolute = candidate if candidate.is_absolute() else self.root / candidate
        try:
            rel = Path(os.path.normpath(absolute)).relative_to(self.root)
        except ValueError as exc:
            raise NotebookToolError(
                "outside_workspace", f"{path} is outside this chat's workspace."
            ) from exc
        if not str(rel).endswith(NOTEBOOK_SUFFIX) or str(rel) in (".", NOTEBOOK_SUFFIX):
            raise NotebookToolError(
                "not_a_notebook", f"{path} is not a notebook (the name must end in .alknb.py)."
            )
        return str(PurePosixPath(rel))

    async def port(self, path: str, actor: ActorRef) -> EnginePort:
        key = (path, actor.id)
        cached = self._ports.get(key)
        if cached is not None:
            return cached
        if not (self.root / path).exists():
            raise NotebookToolError(
                "not_found", f"No notebook at {path}; create it with notebook.create."
            )
        try:
            session = await self.engine.open(path)
        except Exception as exc:
            raise _translate_error(exc) from exc
        port = EnginePort(session, session.attach(_engine_actor(actor)), actor)
        self._ports[key] = port
        return port

    async def create(
        self,
        path: str,
        cells: Sequence[InsertCellOp],
        settings: dict[str, Any],
        actor: ActorRef,
    ) -> EnginePort:
        from alkera_notebook.document.ops import InsertCell

        if (self.root / path).exists():
            raise NotebookToolError("exists", f"{path} already exists; edit it instead.")
        inserts = [
            InsertCell(op="insert", kind=c.kind, source=c.source, name=c.name, meta=dict(c.meta))
            for c in cells
        ]
        try:
            await self.engine.create(path, inserts, dict(settings), _engine_actor(actor))
        except Exception as exc:
            raise _translate_error(exc) from exc
        return await self.port(path, actor)

    async def close(self) -> None:
        for port in self._ports.values():
            await port._client.detach()
        self._ports.clear()
        await self.engine.close()


def local_engine(
    root: Path,
    *,
    data_root: Path | None = None,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    envs: Any = None,
    sql: Sequence[Any] = (),
    cost_guard_seconds: float = 60.0,
    store: Any = None,
    workspace_id: str = "local",
    org_id: str = "",
    shared_envs: bool = True,
    launcher: Any = None,
    index_args: Sequence[str] = (),
    default_template: Path | None = None,
    python: str | None = None,
    kernel_mount: Path | None = None,
) -> eng.NotebookEngine:
    """An engine for a workspace root on this machine, from the core's local
    seams; ``store`` replaces the file document store (a live document, when
    the notebooks are co-edited), ``launcher`` the kernel launcher (one its
    caller can fence).

    Without ``envs``, a notebook runs in the workspace's environments: its
    project's, or the workspace's shared default environment, which is made
    from the template on first use and takes installs like any other. There
    is no fixed environment. ``index_args`` are passed to every build (a
    local wheel index in tests), and ``default_template`` replaces the
    built-in template directory (a template a local index can lock).
    ``python`` is the interpreter or version environments are built with
    (the registry's default when not given), and ``kernel_mount`` the
    directory holding the kernel's platform files (linked from the installed
    sources when not given)."""
    from alkera_notebook.document.file_store import FileDocumentStore
    from alkera_notebook.document.fmt import default_format
    from alkera_notebook.engine import (
        EngineConfig,
        MemoryPolicy,
        NotebookEngine,
        RandomIds,
        SystemClock,
    )
    from alkera_notebook.envs.runner import LocalCommandRunner
    from alkera_notebook.envs.template import TemplatedEnvRegistry, default_env_template
    from alkera_notebook.kernels.launcher import LocalSubprocessLauncher, UnixSocketTransport
    from alkera_notebook.memory.source import ProcessRssSource
    from alkera_notebook.sql.provider import SqlProviderRegistry

    data = data_root or root / ".alkera" / "notebooks"
    fmt = default_format()
    clock = SystemClock()
    config = EngineConfig(
        workspace_root=str(root),
        env_root=str(data / "envs"),
        data_root=str(data / "data"),
        memory=MemoryPolicy(),
        cost_guard_seconds=cost_guard_seconds,
        workspace_id=workspace_id,
        org_id=org_id,
        shared_envs=shared_envs,
        kernel_mount=None if kernel_mount is None else str(kernel_mount),
    )
    return NotebookEngine(
        config,
        store=store if store is not None else FileDocumentStore(str(root), fmt=fmt, clock=clock),
        launcher=launcher if launcher is not None else LocalSubprocessLauncher(),
        transport=UnixSocketTransport(),
        memory=ProcessRssSource(budget_bytes),
        sql=SqlProviderRegistry(list(sql)),
        envs=envs
        or TemplatedEnvRegistry(
            root,
            data / "envs",
            runner=LocalCommandRunner(),
            python=python,
            # Installs fetch from the package index.
            offline=False,
            index_args=list(index_args),
            default_template=default_template or default_env_template(data / "env-template"),
        ),
        clock=clock,
        ids=RandomIds(),
        fmt=fmt,
    )


class LocalEngines:
    """One engine per workspace root for the life of a process."""

    def __init__(self, build: Callable[[Path], eng.NotebookEngine] = local_engine) -> None:
        self._build = build
        self._workspaces: dict[Path, EngineWorkspace] = {}

    def workspace(self, root: Path) -> EngineWorkspace:
        key = root.resolve()
        found = self._workspaces.get(key)
        if found is None:
            found = EngineWorkspace(key, self._build(key))
            self._workspaces[key] = found
        return found

    def __call__(self, root: Path, actor: ActorRef) -> NotebookHost:
        return self.workspace(root).host(actor)

    async def close(self) -> None:
        for workspace in self._workspaces.values():
            await workspace.close()
        self._workspaces.clear()


def engine_host_factory() -> HostFactory | None:
    """The engine-backed host factory, or ``None`` without an engine."""
    if not engine_available():
        return None
    return LocalEngines()


__all__ = [
    "EngineHost",
    "EnginePort",
    "EngineWorkspace",
    "HostFactory",
    "LocalEngines",
    "engine_available",
    "engine_host_factory",
    "local_engine",
]
