"""One open notebook: its document view, kernel, run queue, outputs and events.

The engine (``engine/engine.py``) creates one :class:`Session` per path; the
client API is a thin façade over it. All mutation happens on the event loop;
kernel notifications arrive as synchronous callbacks on the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from alkera_notebook.document.editing import blocker, holds_for
from alkera_notebook.document.model import Document
from alkera_notebook.document.ops import (
    CellNotice,
    GraphSummary,
    NotebookOp,
    NotebookOpsResult,
    SetSetting,
)
from alkera_notebook.document.store import DocumentChange, StoredNotebook
from alkera_notebook.engine.activity import batch_changes
from alkera_notebook.engine.errors import (
    ForbiddenError,
    KernelUnavailableError,
    NotFoundError,
    ReadOnlyError,
)
from alkera_notebook.engine.frame_query import check_frame_filter, frame_sort
from alkera_notebook.engine.held_reruns import HeldReruns
from alkera_notebook.engine.models import (
    Activity,
    ActivityActor,
    ActivityEntry,
    Actor,
    CellState,
    EnvInfo,
    ErrorInfo,
    GraphCell,
    GraphView,
    InspectQuery,
    InspectResult,
    KernelInfo,
    KernelState,
    NotebookView,
    OutputDetail,
    OutputPart,
    PlanEntry,
    Presence,
    QueuedRun,
    ReadQuery,
    RunActor,
    RunInfo,
    RunRecord,
    RunStatus,
    RunTrigger,
    Settings,
    VarSummary,
    WidgetAssetRef,
    WidgetInfo,
    system_actor,
)
from alkera_notebook.engine.runtime import CellRuntime, Job
from alkera_notebook.envs import generations
from alkera_notebook.envs.detect import EnvNotFoundError
from alkera_notebook.envs.models import EnvDescriptor, env_actions
from alkera_notebook.envs.registry import EnvBuildError, EnvError
from alkera_notebook.events.models import (
    AnyEvent,
    CellFinishedEvent,
    CellOutputEvent,
    CellOutputsCleared,
    CellStatusEvent,
    CellStreamEvent,
    CellVariablesEvent,
    DocChanged,
    EnvStateEvent,
    FrameAttached,
    FrameMessage,
    GraphEvent,
    KernelExited,
    KernelInterrupt,
    KernelStateEvent,
    NoticeEvent,
    RunFinished,
    RunNeedsConfirmation,
    RunQueued,
    RunStarted,
    SnapshotSaved,
)
from alkera_notebook.events.queue import ClientQueue, EventHub
from alkera_notebook.format.settings import setting_sources
from alkera_notebook.kernels.kernel import ExitInfo, KernelHandle, KernelStartError, start_kernel
from alkera_notebook.kernels.launch_local import kernel_data_dir
from alkera_notebook.outputs import (
    CellOutputs,
    KernelMeta,
    NotebookPlace,
    RunMeta,
    SnapshotCell,
    SnapshotContent,
    SnapshotScheduler,
    SnapshotWriter,
    code_hash,
    lineage_hashes,
    outdated,
    read_snapshot,
    reattach,
    update_gitignore,
)
from alkera_notebook.outputs.state import WIDGET_MIME
from alkera_notebook.plan import CellGraph, PlanCell, PlanInput, display_status, make_plan
from alkera_notebook.plan.planner import Plan, RuntimeStatus, scope_targets
from alkera_notebook.rpc import frames as f
from alkera_notebook.rpc.peer import MethodRegistry, PeerClosedError
from alkera_notebook.widgets.assets import (
    AssetEntry,
    AssetRefusedError,
    KernelAssetFile,
    workspace_scope,
)
from alkera_notebook.widgets.hub import Frame, WidgetHub, WidgetRefusedError

if TYPE_CHECKING:
    from alkera_notebook.engine.engine import NotebookEngine

log = logging.getLogger(__name__)


def variables_of(cell_id: str, raw: object) -> list[VarSummary]:
    """The variable summaries a kernel sent for ``cell_id``. One that does not
    read as a summary is left out and logged with why, never dropped unseen:
    a kernel and an engine that disagree on the shape show as a warning, not
    as a cell with no variables."""
    out: list[VarSummary] = []
    for one in raw if isinstance(raw, list) else []:
        try:
            if not isinstance(one, dict):
                raise TypeError(f"a summary is an object, not {type(one).__name__}")
            out.append(VarSummary.model_validate({**one, "cell_id": cell_id}))
        except (TypeError, ValueError) as exc:
            name = one.get("name") if isinstance(one, dict) else None
            log.warning("cell %s: variable %r left out of its summary: %s", cell_id, name, exc)
    return out


_KERNEL_STATUS: dict[str, Any] = {
    f.STATUS_OK: "fresh",
    f.STATUS_ERROR: "error",
    f.STATUS_INTERRUPTED: "interrupted",
    f.STATUS_STOPPED: "stopped",
}
_RUN_STATUS: dict[str, RunStatus] = {
    f.STATUS_OK: "ok",
    f.STATUS_ERROR: "error",
    f.STATUS_INTERRUPTED: "interrupted",
    f.STATUS_STOPPED: "ok",
}


# Kernel exits that leave no kernel behind; every other exit is a restart.
_STOP_REASONS = frozenset({"shutdown", "suspended", "notebook_deleted", "idle"})
#: The environment kinds the engine builds before a kernel starts in them.
MANAGED_ENV_KINDS = frozenset({"default", "uv_project", "script"})
#: The notice a notebook's readers get when its kernel runs on an environment
#: build that has since been replaced.
ENV_NEWER_NOTICE = "env_newer"
ENV_NEWER_MESSAGE = "A newer environment is ready. Restart the kernel to use it."
#: How much of a failed build's last output line a refusal quotes.
_BUILD_LOG_QUOTE = 300


def env_failure_message(exc: EnvError) -> str:
    """What a failed build says to a reader: the step that failed and the
    last line it printed (the line that usually names the cause)."""
    lines = [line.strip() for line in exc.log.splitlines() if line.strip()]
    last = lines[-1] if lines and not lines[-1].startswith("$ ") else ""
    if len(last) > _BUILD_LOG_QUOTE:
        last = "..." + last[-_BUILD_LOG_QUOTE:]
    head = f"The environment could not be built: {exc}"
    return f"{head}: {last}" if last else head


class Session:
    def __init__(self, engine: NotebookEngine, path: str, stored: StoredNotebook) -> None:
        self.engine = engine
        self.path = path
        self.abs_path = Path(engine.config.workspace_root) / path
        #: Where the outputs are written: the workspace's folder is the
        #: trusted root, and nothing below it is followed through a link.
        self.place = NotebookPlace(engine.workspace_tree, PurePosixPath(path))
        self.stored = stored
        self.hub = EventHub()
        self.cells: dict[str, CellRuntime] = {}
        self.outputs: dict[str, CellOutputs] = {}
        self.kernel: KernelHandle | None = None
        self.kernel_state: KernelState = "absent"
        self.env: EnvDescriptor | None = None
        #: The environment build the running kernel started on, for an
        #: environment kept as generations; ``None`` otherwise.
        self.kernel_build: Path | None = None
        self._told_newer_build: Path | None = None
        #: The environment the notebook last resolved to (``None`` before it
        #: first did): a change is announced, since the running kernel stays
        #: in the one it started in until it restarts.
        self._selected_env_id: str | None = None
        self.queue: deque[Job] = deque()
        self.current: Job | None = None
        self.records: dict[str, RunRecord] = {}
        self.activity_log: list[ActivityEntry] = []
        # Who asked for the kernel's next exit, and the reason the feed shows.
        self._exit_cause: tuple[Actor, str] | None = None
        self._interrupter: Actor | None = None
        self._stale_before: dict[str, set[str]] = {}  # run_id -> stale cells at its start
        self._stale_cache: tuple[tuple[Any, ...], frozenset[str]] | None = None
        self._published_stale: frozenset[str] = frozenset()
        self._kernel_graph_cache: tuple[tuple[tuple[str, str], ...], CellGraph] | None = None
        self.deleted_defs: list[str] = []
        # One widget hub per kernel: model state, frames.
        self.widget_hub: WidgetHub | None = None
        # The cell each widget model was created in, and the names it is bound
        # to (from ``ui.bindings``): what a value change re-runs.
        self.widget_cells: dict[str, str | None] = {}
        self.bindings: dict[str, tuple[str | None, list[str]]] = {}
        # Attached clients and the frames they show.
        self.clients: dict[str, tuple[Actor, ClientQueue]] = {}
        self.frames: dict[str, Frame] = {}
        # Widget assets are stored per (org, workspace) and offered per notebook:
        # only this notebook may fetch what its kernel offered.
        self.asset_store_scope = workspace_scope(engine.config.org_id, engine.config.workspace_id)
        self.asset_scope = f"{self.asset_store_scope}/notebook:{path}"
        self.notices: list[CellNotice] = []
        self._wakeup = asyncio.Event()
        self._doc_lock = asyncio.Lock()
        self._run_waiters: dict[str, asyncio.Future[str]] = {}
        self._comm_waiters: dict[str, asyncio.Future[None]] = {}
        self._steps: dict[str, dict[str, str]] = {}  # run_id -> cell_id -> code
        self._lineage: dict[str, dict[str, str]] = {}  # run_id -> cell_id -> lineage
        self._interrupting: set[str] = set()
        self._escalation: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Future[Any]] = set()
        self._starting: asyncio.Lock = asyncio.Lock()
        self._suspended = False
        self._closed = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._run_status_on_exit: tuple[RunStatus, str] | None = None
        # An engine setting a person may change for this notebook (not saved
        # in the file).
        self.snapshots = SnapshotScheduler(
            SnapshotWriter(self.place, blob_threshold=engine.config.limits.blob_threshold_bytes),
            self._snapshot_content,
            engine.clock,
            debounce_s=engine.config.limits.snapshot_debounce_s,
            on_written=self._on_snapshot_written,
        )
        self.held = HeldReruns(
            editing=lambda: self.engine.store.editing(self.path),
            release=self._rerun_held,
            changed=self._publish_status,
            sleep=engine.clock.sleep,
            interval_s=engine.config.editing_recheck_s,
        )
        self._sync_cells()
        self._reattach_saved()
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._worker()))
        # Subscribe now, not when the follower first runs, so no change made
        # after the document was loaded is missed.
        changes = self.engine.store.changes(self.path)
        self._tasks.append(loop.create_task(self._follow_store(changes)))

    # ------------------------------------------------------------------ document

    @property
    def doc(self) -> Document:
        return self.stored.document

    def _live(self) -> list[Any]:
        return self.doc.live_cells()

    def _sync_cells(self) -> None:
        live = {c.id for c in self._live()}
        for cid in live:
            self.cells.setdefault(cid, CellRuntime())
        for cid in list(self.cells):
            if cid not in live:
                self.held.drop(cid)
                gone = self.cells.pop(cid)
                for name in gone.defs:
                    if name not in self.deleted_defs:
                        self.deleted_defs.append(name)

    def _reattach_saved(self) -> None:
        snap = read_snapshot(self.place)
        for note in snap.notices:
            kind = note.split(":", 1)[0] if ":" in note else "snapshot_notice"
            self._notice(CellNotice(kind=kind, message=note))
        found = reattach(snap, [(c.id, c.code) for c in self._live()])
        for cid, out in found.items():
            self.outputs[cid] = out

    async def refresh_doc(self, change: DocumentChange | None = None) -> None:
        if change is not None and change.origin == "deleted" and self.kernel is not None:
            # A deleted notebook keeps no kernel.
            await self.shutdown_kernel("notebook_deleted")
        # One refresh at a time: a load that started before a write must not
        # replace the document a later load (the writer's own) already took.
        async with self._doc_lock:
            await self._refresh_doc_locked(change)

    async def _refresh_doc_locked(self, change: DocumentChange | None) -> None:
        before = {c.id: c.code for c in self._live()}
        try:
            stored = await self.engine.store.load(self.path)
        except (FileNotFoundError, NotFoundError):
            for n in change.notices if change is not None else ():
                self._notice(n)
            return
        token_changed = stored.token != self.stored.token
        self.stored = stored
        self._sync_cells()
        after = {c.id: c.code for c in self._live()}
        touched = sorted(
            {cid for cid in set(before) | set(after) if before.get(cid) != after.get(cid)}
        )
        notices = list(change.notices) if change is not None else []
        for n in notices:
            self._notice(n)
        if not token_changed and change is None:
            return
        self.publish(
            DocChanged(
                token=stored.token,
                actor_id=change.actor_id if change else None,
                cell_ids=list(change.cell_ids) if change and change.cell_ids else touched,
                origin=change.origin if change else "ops",
            )
        )
        for cid in touched:
            # Its text changed: it needs a run of its own now, not the held one.
            self.held.drop(cid)
            if cid in after:
                self._publish_status(cid)
        self._publish_derived_statuses()
        if touched:
            self.publish(GraphEvent(graph=self._doc_graph_summary()))

    async def _follow_store(self, changes: AsyncIterator[DocumentChange]) -> None:
        try:
            # Catch up with whatever landed between the load and the subscription.
            await self.refresh_doc()
            async for change in changes:
                await self.refresh_doc(change)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("document change stream ended")

    async def apply(
        self,
        actor: Actor,
        ops: list[NotebookOp],
        base_token: str | None,
        submit_id: str | None,
        *,
        record: bool = True,
    ) -> NotebookOpsResult:
        """Apply a batch. With ``record`` the activity feed gets what it
        changed; an engine action that edits the document on someone's behalf
        (installing into a script block) records its own entry instead."""
        if not actor.can_edit:
            raise ForbiddenError("you cannot edit this notebook")
        if self.doc.read_only_reason:
            raise ReadOnlyError(f"read only: {self.doc.read_only_reason}")
        before = self.doc
        stale_before = self._stale_ids()
        result = await self.engine.store.apply(self.path, ops, base_token, actor, submit_id)
        after = (await self.engine.store.load(self.path)).document
        changes = batch_changes(before, after, ops, result.created)
        await self.refresh_doc(
            DocumentChange(
                path=self.path,
                token=result.token,
                actor_id=actor.id,
                cell_ids=changes.cell_ids,
                notices=result.notices,
            )
        )
        # The store holds no run state: each live cell's status is ours.
        result = result.model_copy(
            update={
                "cells": [
                    c if c.deleted else c.model_copy(update={"status": self._cell_status(c.id)})
                    for c in result.cells
                ]
            }
        )
        if not record or result.repeat:
            return result
        for change, cell_ids in changes.cells:
            self.record_activity(actor, "cell_edit", stale_before, change=change, cell_ids=cell_ids)
        if changes.settings:
            self.record_activity(actor, "settings_change", stale_before, settings=changes.settings)
        if changes.env_changed:
            self.record_activity(actor, "env_change", stale_before, change="switch")
        return result

    # ------------------------------------------------------------------ events

    def publish(self, event: AnyEvent) -> AnyEvent:
        if self.kernel is not None and event.kernel_id is None:
            event.kernel_id = self.kernel.kernel_id
        return self.hub.publish(event)

    def subscribe(self, actor: Actor, client_id: str) -> ClientQueue:
        queue = self.hub.subscribe(self.engine.config.client_queue_max, self.view_all)
        self.clients[client_id] = (actor, queue)
        return queue

    def unsubscribe(self, client_id: str) -> None:
        entry = self.clients.pop(client_id, None)
        if entry is not None:
            self.hub.unsubscribe(entry[1])
        for frame_id in [fid for fid, fr in self.frames.items() if fr.client_id == client_id]:
            self.detach_frame(frame_id)

    def _notice(self, notice: CellNotice) -> None:
        self.notices.append(notice)
        del self.notices[:-50]
        self.publish(NoticeEvent(notice=notice))

    def _log(self, entry: ActivityEntry) -> None:
        self.activity_log.append(entry)
        del self.activity_log[:-1000]

    def _stale_ids(self) -> set[str]:
        return {c.id for c in self._live() if self._cell_status(c.id) == "stale"}

    def record_activity(
        self,
        actor: Actor | ActivityActor,
        kind: Any,
        stale_before: set[str] | None = None,
        **fields: Any,
    ) -> None:
        """Add an entry to the feed, naming the cells left stale that were
        not stale before it."""
        made_stale: list[str] = []
        if stale_before is not None:
            fresh_stale = self._stale_ids() - stale_before
            made_stale = [c.id for c in self._live() if c.id in fresh_stale]
        self._log(
            ActivityEntry(
                at=self.engine.clock.now(),
                actor=ActivityActor.of(actor),
                kind=kind,
                stale_cell_ids=made_stale,
                **fields,
            )
        )

    def _publish_status(self, cid: str, run_id: str | None = None) -> None:
        state = self._cell_status(cid)
        if state is not None:
            self.publish(
                CellStatusEvent(
                    cell_id=cid, status=state, run_id=run_id, rerun_waits_for=self.held.who(cid)
                )
            )

    def _set_kernel_state(self, state: KernelState, *, kernel_id: str | None = None) -> None:
        """Say the kernel's state; ``kernel_id`` names a kernel still
        starting (one running is named by ``publish``). A state of a kernel
        names the environment it runs in."""
        self.kernel_state = state
        event = KernelStateEvent(state=state, kernel_id=kernel_id)
        if self.env is not None and (kernel_id is not None or self.kernel is not None):
            event.env_id = self.env.env_id
        self.publish(event)

    # ------------------------------------------------------------------ statuses and views

    def _cell_status(self, cid: str) -> Any:
        cell = self.doc.cells.get(cid)
        rt = self.cells.get(cid)
        if cell is None or rt is None or cell.deleted:
            return None
        return display_status(
            runtime=self._runtime_status(cid, rt),
            activity=rt.activity,
            disabled=cell.disabled,
            current_code=cell.code,
            submitted_code=rt.submitted,
        )

    def _runtime_status(self, cid: str, rt: CellRuntime) -> RuntimeStatus:
        """The kernel's status for the cell, with a fresh cell read as stale
        while an ancestor it ran against has changed since."""
        if rt.status == "fresh" and cid in self._stale_from_above():
            return "stale"
        return rt.status

    def _stale_from_above(self) -> frozenset[str]:
        """Fresh cells below an ancestor in the kernel graph that is not fresh
        itself: one whose text was edited after it ran, one that is stale, in
        error, not run or disabled. Editing the text back to what ran, or
        running the ancestor, clears it."""
        live = self._live()
        key = tuple(
            (c.id, c.code, c.disabled, rt.status, rt.submitted)
            for c in live
            for rt in (self.cells.get(c.id) or CellRuntime(),)
        )
        cached = self._stale_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        changed = [
            cid
            for cid, code, disabled, status, submitted in key
            if disabled or status != "fresh" or (submitted is not None and submitted != code)
        ]
        found: frozenset[str] = frozenset()
        if changed:
            below = self._kernel_graph().descendants_of(changed)
            found = frozenset(
                cid for cid, _c, _d, status, _s in key if cid in below and status == "fresh"
            )
        self._stale_cache = (key, found)
        return found

    def _publish_derived_statuses(self, run_id: str | None = None) -> None:
        """Publish the status of every cell that turned stale, or stopped being
        stale, because of a change above it rather than to itself."""
        now = self._stale_from_above()
        for cid in sorted(now ^ self._published_stale):
            self._publish_status(cid, run_id)
        self._published_stale = now

    def _doc_graph(self) -> CellGraph:
        live = self._live()
        return CellGraph.from_analysis(
            [c.id for c in live], self.engine.fmt.analyze_code([(c.id, c.code) for c in live])
        )

    def _doc_graph_summary(self) -> GraphSummary:
        live = self._live()
        return GraphSummary.of_analysis(
            self.engine.fmt.analyze_code([(c.id, c.code) for c in live])
        )

    def _env_fingerprint(self) -> str | None:
        return self.engine.envs.fingerprint(self.env) if self.env is not None else None

    def _current_lineage(self) -> dict[str, str]:
        live = self._live()
        g = self._doc_graph()
        return lineage_hashes([(c.id, c.code) for c in live], g.edges)

    def _is_outdated(self, cid: str, lineage: dict[str, str]) -> bool:
        out = self.outputs.get(cid)
        if out is None or cid not in lineage:
            return False
        return outdated(out, lineage[cid], self._env_fingerprint())

    def env_info(self) -> EnvInfo | None:
        if self.env is None:
            return None
        return self.env_info_of(self.env)

    def env_info_of(self, desc: EnvDescriptor) -> EnvInfo:
        return EnvInfo(
            env_id=desc.env_id,
            kind=desc.kind,
            spec_root=desc.spec_root,
            python=desc.python_version,
            state=desc.state,
            recorded_in_file=self.doc.setting("env") is not None,
            recorded=desc.recorded,
            last_failure=desc.last_failure,
            allowed_actions=env_actions(desc),
        )

    def note_selected_env(self, selected: EnvDescriptor) -> None:
        """Announce (``env.state``) that the notebook's environment is now
        ``selected``, when it differs from the one it last resolved to. A
        notebook that records no ``env`` follows detection, so a spec
        appearing beside it (an uploaded ``pyproject.toml``) changes its
        environment; the running kernel keeps the one it started in (its
        ``kernel.state`` names it) until it restarts, and clients read the
        two apart. The first resolution is announced only when it differs
        from the running kernel's."""
        if selected.env_id == self._selected_env_id:
            return
        first = self._selected_env_id is None
        self._selected_env_id = selected.env_id
        running = self.env if self.kernel is not None else None
        if first and (running is None or running.env_id == selected.env_id):
            return
        self.publish(EnvStateEvent(env=self.env_info_of(selected)))

    async def _check_selected_env(self) -> None:
        """Re-resolve a notebook that follows detection, announcing a change."""
        if self.doc.setting("env") is not None:
            return
        try:
            selected = await self.engine.envs.resolve(str(self.abs_path), None)
        except (EnvError, EnvNotFoundError):
            return
        self.note_selected_env(selected)

    def settings_model(self) -> Settings:
        """Each setting's effective value: the file's, else the workspace's
        default (the engine's configuration), else its own default."""
        workspace = self.engine.config.setting_defaults()
        values: dict[str, Any] = {}
        # ``sources`` describes the values; it is not a setting of its own.
        for key in Settings.model_fields.keys() - {"sources"}:
            found = self.doc.settings.get(key)
            if found is None:
                found = workspace.get(key)
            if found is None:
                found = self.doc.setting(key)
            if found is not None:
                values[key] = found
        sources = setting_sources(self.doc.settings, workspace)
        try:
            return Settings(**values, sources=sources)
        except ValueError:
            return Settings(sources=sources)

    def env_outdated(self) -> bool:
        """Whether the kernel runs on an environment build an install or a
        rebuild has since replaced: imports of what was added fail in it
        until the kernel restarts."""
        if self.kernel is None or self.env is None or self.kernel_build is None:
            return False
        return generations.in_use(Path(self.env.prefix)) != self.kernel_build

    def say_if_env_outdated(self) -> None:
        """Tell the notebook's readers a newer environment is ready, once per
        build, when the kernel still runs on an older one."""
        if not self.env_outdated() or self.env is None:
            return
        current = generations.in_use(Path(self.env.prefix))
        if current is not None and current == self._told_newer_build:
            return
        self._told_newer_build = current
        self._notice(CellNotice(kind=ENV_NEWER_NOTICE, message=ENV_NEWER_MESSAGE))

    def kernel_info(self) -> KernelInfo:
        queue = [
            QueuedRun(
                run_id=j.run_id, by=j.actor.label(), trigger=j.trigger, status=j.record.status
            )
            for j in ([self.current] if self.current else []) + list(self.queue)
        ]
        reactivity = self.settings_model().reactivity
        return KernelInfo(
            state=self.kernel_state,
            env=self.env_info(),
            reactivity=reactivity,
            memory_bytes=self.kernel.rss_bytes() if self.kernel else None,
            started_at=self.kernel.started_at if self.kernel else None,
            queue=queue,
            kernel_id=self.kernel.kernel_id if self.kernel else None,
            env_outdated=self.env_outdated(),
        )

    async def view_all(self) -> NotebookView:
        return await self.view(ReadQuery())

    async def view(self, query: ReadQuery) -> NotebookView:
        g = self._doc_graph()
        lineage = self._current_lineage()
        editing = await self.engine.store.editing(self.path)
        now = self.engine.clock.now()
        wanted = set(query.cells) if query.cells is not None else None
        cells: list[CellState] = []
        for index, cell in enumerate(self._live()):
            if wanted is not None and cell.id not in wanted:
                continue
            out = self.outputs.get(cell.id)
            cells.append(
                CellState(
                    id=cell.id,
                    name=cell.name,
                    kind=cell.kind,
                    index=index,
                    status=self._cell_status(cell.id),
                    rerun_waits_for=self.held.who(cell.id),
                    defs=list(g.defs.get(cell.id, ())),
                    refs=list(g.refs.get(cell.id, ())),
                    graph_errors=[e.label() for e in g.errors.get(cell.id, ())],
                    source=cell.source if query.include_source else None,
                    output=out.summary() if (out is not None and query.include_outputs) else None,
                    outputs=(
                        out.items(cell.id)
                        if out is not None and query.include_outputs and query.include_output_items
                        else []
                    ),
                    output_outdated=self._is_outdated(cell.id, lineage),
                    output_origin=(
                        ("kernel" if out.origin == "kernel" else out.origin) if out else None
                    ),
                    last_run=out.attribution() if out else None,
                    config=dict(cell.config),
                    meta=dict(cell.meta),
                    extra=dict(cell.extra),
                )
            )
        return NotebookView(
            path=self.path,
            token=self.stored.token,
            settings=self.settings_model(),
            kernel=self.kernel_info(),
            cells=cells,
            presence=[
                Presence(
                    who=i.display_name,
                    cell_id=c,
                    kind=i.kind,
                    at=i.at,
                    actor_id=i.actor_id,
                    caret=i.caret,
                    expires_in=holds_for(i, now=now).total_seconds(),
                )
                for c, infos in editing.items()
                for i in infos
            ],
            read_only_reason=self.doc.read_only_reason,
            notices=list(self.notices[-20:]),
            output_frame_url=self.engine.config.output_frame_url,
        )

    def graph_view(self, cell: str | None, direction: Literal["both", "up", "down"]) -> GraphView:
        g = self._doc_graph()
        names = {c.id: c.name for c in self._live()}
        if cell is not None and cell not in names:
            raise NotFoundError(f"no cell {cell}")
        up = sorted(g.ancestors(cell), key=g.order.index) if cell and direction != "down" else []
        down = sorted(g.descendants(cell), key=g.order.index) if cell and direction != "up" else []
        return GraphView(
            cells={
                c: GraphCell(
                    name=names[c],
                    defs=list(g.defs[c]),
                    refs=list(g.refs[c]),
                    status=self._cell_status(c),
                )
                for c in g.order
            },
            edges=list(g.edges),
            upstream=up,
            downstream=down,
            errors={c: list(e) for c, e in g.errors.items() if e},
        )

    def output_detail(self, cid: str, part: OutputPart, max_chars: int = 20000) -> OutputDetail:
        if cid not in self.doc.cells:
            raise NotFoundError(f"no cell {cid}")
        out = self.outputs.get(cid)
        if out is None:
            return OutputDetail()
        return out.detail(part, max_chars)

    def clear_outputs(self, actor: Actor, cell_ids: Sequence[str] | None) -> list[str]:
        """Drop the outputs of ``cell_ids`` (every live cell when ``None``), as
        the run that made them never showed anything: every view drops them,
        and the next saved outputs no longer hold them. Values stay in the
        kernel and the cells' run status is unchanged. Needs the right to run,
        as the run that made them did. Returns the cells that had outputs, in
        notebook order."""
        if not actor.can_run:
            raise ForbiddenError("you cannot clear this notebook's outputs")
        live = [c.id for c in self._live()]
        if cell_ids is not None:
            unknown = [cid for cid in cell_ids if cid not in live]
            if unknown:
                raise NotFoundError(f"no cell {unknown[0]}")
        wanted = set(live if cell_ids is None else cell_ids)
        cleared = [cid for cid in live if cid in wanted and self.outputs.pop(cid, None) is not None]
        if not cleared:
            return []
        self.publish(CellOutputsCleared(cell_ids=cleared, actor_id=actor.id))
        self.snapshots.schedule()
        return cleared

    def activity(self, since: datetime, exclude_actor: str | None = None) -> Activity:
        """Entries strictly after ``since`` (a reader passes the time of the
        newest entry it has seen), without ``exclude_actor``'s own."""
        return Activity(
            since=since,
            exclude_actor=exclude_actor,
            entries=[
                e
                for e in self.activity_log
                if e.at > since and (exclude_actor is None or e.actor.id != exclude_actor)
            ],
        )

    # ------------------------------------------------------------------ run requests

    def _plan_cells(self, target_codes: dict[str, str]) -> list[PlanCell]:
        out: list[PlanCell] = []
        for cell in self._live():
            rt = self.cells.setdefault(cell.id, CellRuntime())
            out.append(
                PlanCell(
                    id=cell.id,
                    name=cell.name,
                    current_code=target_codes.get(cell.id, cell.code),
                    submitted_code=rt.submitted,
                    status=self._runtime_status(cell.id, rt),
                    holds_value=rt.holds_value,
                    disabled=cell.disabled,
                    kind=cell.kind,
                    duration_s=rt.duration_s,
                    connection=(cell.meta or {}).get("connection"),
                )
            )
        return out

    async def _editing_for(self, actor: Actor) -> tuple[dict[str, str], frozenset[str]]:
        """Whose editing makes a re-run of which cell wait for a run ``actor``
        asked for, and which of those cells only ``actor``'s own caret is in."""
        waits: dict[str, str] = {}
        own: set[str] = set()
        for cid, infos in (await self.engine.store.editing(self.path)).items():
            found = blocker(infos, actor.id)
            if found is None:
                continue
            waits[cid] = found.display_name
            if found.actor_id == actor.id:
                own.add(cid)
        return waits, frozenset(own)

    async def _rerun_held(self, cid: str, requester: Actor) -> None:
        """The editing a held cell waited for ended: autorun re-runs it with
        the code the kernel knows (never its live text); lazy leaves it stale."""
        rt = self.cells.get(cid)
        if self._closed or rt is None or not rt.holds_value:
            return
        if self.settings_model().reactivity != "autorun":
            return
        await self.request_run(
            requester,
            scope="cells",
            targets=[cid],
            trigger="autorun",
            frontier=None,
            confirm=True,
            target_code="submitted",
        )

    def _make_plan(self, job: Job, editing: tuple[dict[str, str], frozenset[str]]) -> Plan:
        cells = self._plan_cells(job.target_codes)
        prev = {cid: list(rt.defs) for cid, rt in self.cells.items()}
        return make_plan(
            PlanInput(
                cells=cells,
                scope=job.scope,
                targets=job.targets,
                reactivity=self.settings_model().reactivity,
                editing=editing[0],
                own_caret=editing[1],
                prev_defs=prev,
                deleted_defs=list(self.deleted_defs),
                target_code=job.target_code,
                cost_guard_seconds=self.engine.config.cost_guard_seconds,
                confirm_expensive=job.confirm,
                metered_connections=frozenset(self.engine.metered_connections()),
            ),
            self.engine.fmt.analyze_code,
        )

    def _positions(self) -> dict[str, int]:
        """Each live cell's 0-based position in notebook order."""
        return {cell.id: index for index, cell in enumerate(self._live())}

    def _plan_entries(self, plan: Plan) -> list[PlanEntry]:
        at = self._positions()
        return [
            PlanEntry(cell_id=s.cell_id, name=s.name, reason=s.reason, index=at.get(s.cell_id))
            for s in plan.steps
        ]

    def _plan_detail(self, plan: Plan) -> list[PlanEntry]:
        """Plan entries with the code each step runs and, for SQL cells, the
        statement and connection (for a permission gate to classify)."""
        at = self._positions()
        out: list[PlanEntry] = []
        for step in plan.steps:
            cell = self.doc.cells.get(step.cell_id)
            entry = PlanEntry(
                cell_id=step.cell_id,
                name=step.name,
                reason=step.reason,
                code=step.code,
                index=at.get(step.cell_id),
            )
            kind, source, meta = self.engine.fmt.classify(step.code)
            entry.kind = kind if cell is None or cell.kind != "setup" else "setup"
            if kind == "sql":
                entry.sql = source
                entry.connection = meta.get("connection")
                entry.interpolated = _has_interpolation(source)
            out.append(entry)
        return out

    async def request_run(
        self,
        actor: Actor,
        *,
        scope: Literal["cells", "all", "stale", "above", "below"],
        targets: list[str],
        trigger: RunTrigger,
        frontier: str | None,
        confirm: bool,
        target_code: Literal["current", "submitted"] = "current",
        plan_only: bool = False,
        run_id: str | None = None,
    ) -> tuple[RunInfo, Job]:
        if not actor.can_run:
            raise ForbiddenError("you cannot run this notebook")
        if self._closed:
            raise KernelUnavailableError("the notebook is closed")
        if frontier is not None and frontier != self.stored.token:
            await self._wait_frontier(frontier)
        await self.refresh_doc()
        # The targets' text is taken now, at the request's frontier.
        codes = {c.id: c.code for c in self._live()}
        # A run the platform recorded first keeps the id it was recorded under,
        # so its events name the run the requester is watching.
        run_id = run_id or self.engine.ids.run_id()
        now = self.engine.clock.now()
        record = RunRecord(
            run_id=run_id,
            requested_by=actor,
            trigger=trigger,
            frontier=self.stored.token,
            status="queued",
        )
        job = Job(
            kind="run",
            run_id=run_id,
            actor=actor,
            trigger=trigger,
            record=record,
            done=asyncio.get_running_loop().create_future(),
            scope=scope,
            targets=list(targets),
            target_code=target_code,
            confirm=confirm,
        )
        preview_targets = scope_targets(
            PlanInput(cells=self._plan_cells({}), scope=scope, targets=targets)
        )
        if target_code == "current":
            job.target_codes = {t: codes[t] for t in preview_targets if t in codes}
        job.key = (trigger, scope, tuple(targets), tuple(sorted(job.target_codes.items())))
        # Identical queued requests coalesce into the earlier one.
        for queued in self.queue:
            if queued.kind == "run" and queued.key == job.key and not queued.started:
                info = RunInfo(
                    run_id=queued.run_id,
                    status="coalesced",
                    trigger=trigger,
                    plan=list(queued.record.plan),
                    joined=queued.run_id,
                    queued_behind=self._queued_ids(before=queued),
                )
                return info, queued
        editing = await self._editing_for(actor)
        plan = self._make_plan(job, editing)
        record.plan = self._plan_entries(plan)
        if plan_only:
            job.finish("planned", plan.refusal.code if plan.refusal else None, now)
            return RunInfo(
                run_id=run_id,
                status="refused" if plan.refusal else "planned",
                reason=plan.refusal.code if plan.refusal else None,
                trigger=trigger,
                plan=self._plan_detail(plan),
                estimate_s=plan.estimate_s,
            ), job
        self.records[run_id] = record
        if plan.refusal is not None:
            job.finish("refused", plan.refusal.code, now, plan.refusal.message)
            self._notice(
                CellNotice(
                    kind=plan.refusal.code,
                    cell_id=plan.refusal.cell_id,
                    message=plan.refusal.message,
                    by=plan.refusal.by,
                )
            )
            self.publish(
                RunFinished(
                    run_id=run_id,
                    status="refused",
                    reason=plan.refusal.code,
                    message=plan.refusal.message,
                )
            )
            return RunInfo(
                run_id=run_id,
                status="refused",
                reason=plan.refusal.code,
                trigger=trigger,
                plan=record.plan,
            ), job
        if plan.needs_confirmation:
            job.finish("needs_confirmation", ",".join(plan.confirmation_reasons), now)
            self.publish(
                RunNeedsConfirmation(
                    run_id=run_id,
                    plan=record.plan,
                    estimate_s=plan.estimate_s,
                    reasons=list(plan.confirmation_reasons),
                )
            )
            return RunInfo(
                run_id=run_id,
                status="needs_confirmation",
                reason=",".join(plan.confirmation_reasons),
                trigger=trigger,
                plan=record.plan,
                estimate_s=plan.estimate_s,
            ), job
        behind = self._queued_ids()
        self.queue.append(job)
        for step in plan.steps:
            rt = self.cells.get(step.cell_id)
            if rt is not None and rt.activity is None:
                rt.activity = "queued"
                self._publish_status(step.cell_id, run_id)
        self.publish(
            RunQueued(run_id=run_id, requested_by=actor, trigger=trigger, position=len(self.queue))
        )
        self._wakeup.set()
        return RunInfo(
            run_id=run_id,
            status="queued",
            trigger=trigger,
            plan=record.plan,
            estimate_s=plan.estimate_s,
            queued_behind=behind,
        ), job

    def _queued_ids(self, before: Job | None = None) -> list[str]:
        out = [self.current.run_id] if self.current is not None else []
        for j in self.queue:
            if j is before:
                break
            out.append(j.run_id)
        return out

    async def _wait_frontier(self, frontier: str) -> None:
        deadline = self.engine.config.frontier_wait_s
        waited = 0.0
        while waited < deadline:
            await self.refresh_doc()
            if self.stored.token == frontier:
                return
            await asyncio.sleep(0.05)
            waited += 0.05

    # ------------------------------------------------------------------ the queue

    async def _worker(self) -> None:
        while True:
            while not self.queue:
                self._wakeup.clear()
                await self._wakeup.wait()
            job = self.queue.popleft()
            self.current = job
            job.started = True
            try:
                if job.kind == "comm":
                    await self._exec_comm(job)
                else:
                    await self._exec_run(job)
            except asyncio.CancelledError:
                self._end(job, "interrupted", "closed")
                raise
            except Exception as exc:
                log.exception("run %s failed in the engine", job.run_id)
                detail = f"{type(exc).__name__}: {exc}"
                job.finish("error", f"engine: {detail}", self.engine.clock.now(), detail)
                self.publish(
                    RunFinished(run_id=job.run_id, status="error", reason="engine", message=detail)
                )
            finally:
                self.current = None
                self._clear_activity(job.run_id)
                if self.kernel is not None and self.kernel_state == "busy" and not self.queue:
                    self._set_kernel_state("idle")
                self._arm_idle_stop()

    def _end(self, job: Job, status: RunStatus, reason: str) -> None:
        """Finish a run that will not execute (further) and announce it: a
        run never ends without its last word."""
        job.finish(status, reason, self.engine.clock.now())
        self.publish(RunFinished(run_id=job.run_id, status=status, reason=reason))

    def _arm_idle_stop(self) -> None:
        """With ``kernel_idle_seconds`` set, stop a kernel nothing used for
        that long (the default is no idle stop)."""
        idle = self.engine.config.kernel_idle_seconds
        if idle is None or self.kernel is None:
            return
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        kernel = self.kernel

        async def stop_when_idle() -> None:
            await asyncio.sleep(idle)
            if self.kernel is kernel and self.current is None and not self.queue:
                await self.shutdown_kernel("idle")

        self._idle_task = asyncio.get_running_loop().create_task(stop_when_idle())

    def _clear_activity(self, run_id: str) -> None:
        for cid in self._steps.pop(run_id, {}):
            rt = self.cells.get(cid)
            if rt is not None and rt.activity is not None:
                rt.activity = None
                self._publish_status(cid, run_id)
        for cid, rt in self.cells.items():
            if rt.activity == "queued" and not self._queued_for(cid):
                rt.activity = None
                self._publish_status(cid)

    def _queued_for(self, cid: str) -> bool:
        return any(any(p.cell_id == cid for p in j.record.plan) for j in self.queue)

    async def _exec_run(self, job: Job) -> None:
        now = self.engine.clock.now
        if self._suspended:
            self._end(job, "interrupted", "suspended")
            return
        await self.refresh_doc()
        editing = await self._editing_for(job.actor)
        job.confirm = True  # the cost guard was answered at request time
        plan = self._make_plan(job, editing)
        job.record.plan = self._plan_entries(plan)
        if plan.refusal is not None:
            job.finish("refused", plan.refusal.code, now(), plan.refusal.message)
            self._notice(
                CellNotice(
                    kind=plan.refusal.code,
                    cell_id=plan.refusal.cell_id,
                    message=plan.refusal.message,
                    by=plan.refusal.by,
                )
            )
            self.publish(
                RunFinished(
                    run_id=job.run_id,
                    status="refused",
                    reason=plan.refusal.code,
                    message=plan.refusal.message,
                )
            )
            return
        for step in plan.steps:
            self.held.drop(step.cell_id)
        for cid in plan.skipped_editing:
            who = editing[0][cid]
            self._notice(
                CellNotice(
                    kind="cell_skipped_editing",
                    cell_id=cid,
                    message=f"Not re-run while {who} is editing it",
                    by=who,
                )
            )
            self.held.hold(cid, who, job.actor)
        if not plan.steps:
            self._mark_stale(plan.stale, job.run_id)
            job.record.started_at = now()
            job.finish("ok", None, now())
            self.publish(RunFinished(run_id=job.run_id, status="ok"))
            return
        try:
            kernel = await self.ensure_kernel()
        except (KernelUnavailableError, KernelStartError) as exc:
            reason = exc.name if isinstance(exc, KernelUnavailableError) else exc.reason
            message = exc.message
            job.finish("refused", reason, now(), message)
            # Said on the run's first cell too, so a reader looking at the
            # cell they ran sees why nothing happened.
            first = plan.steps[0].cell_id
            self._notice(
                CellNotice(
                    kind="kernel_unavailable",
                    cell_id=first,
                    message=message,
                    data={"reason": reason, "run_id": job.run_id},
                )
            )
            self.publish(
                RunFinished(run_id=job.run_id, status="refused", reason=reason, message=message)
            )
            return
        job.record.status = "running"
        job.record.started_at = now()
        job.record.kernel_id = kernel.kernel_id
        self._mark_busy()
        self._stale_before[job.run_id] = self._stale_ids()
        self._mark_stale(plan.stale, job.run_id)
        self.publish(RunStarted(run_id=job.run_id, plan=job.record.plan))
        codes = {s.cell_id: s.code for s in plan.steps}
        self._steps[job.run_id] = codes
        self._lineage[job.run_id] = lineage_hashes(
            [(c, self._graph_code(c, codes)) for c in plan.graph.order], plan.graph.edges
        )
        steps: list[dict[str, Any]] = []
        for s in plan.steps:
            compiled = self.engine.fmt.compile_step(s.code, cell_id=s.cell_id)
            steps.append(
                {
                    "cell_id": s.cell_id,
                    "body": compiled.get("body", s.code),
                    "last_expr": compiled.get("last_expr", "None"),
                    "source": s.code,
                    "filename": f"cell-{s.cell_id}",
                    "line_offset": 0,
                    "defs": list(compiled.get("defs", [])),
                    "refs": list(compiled.get("refs", [])),
                }
            )
        for s in plan.steps:
            rt = self.cells[s.cell_id]
            rt.activity = "queued"
            # The kernel deletes every planned step's previous names first.
            rt.holds_value = False
            self._publish_status(s.cell_id, job.run_id)
        waiter: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._run_waiters[job.run_id] = waiter
        with contextlib.suppress(ValueError):
            kernel.service.scope.begin(job.run_id)
        try:
            await kernel.request(
                f.RUN_EXECUTE,
                {
                    "run_id": job.run_id,
                    "steps": steps,
                    "clear": list(plan.clear),
                    "trigger": job.trigger,
                },
            )
            self.deleted_defs = []
            exited = asyncio.ensure_future(asyncio.shield(kernel.exited))
            pending: set[asyncio.Future[Any]] = {waiter, exited}
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            exited.cancel()
            if waiter in done:
                kernel_status = waiter.result()
            else:
                kernel_status = "kernel_exited"
        except (PeerClosedError, f.RpcError, OSError) as exc:
            log.info("run %s: kernel request failed: %s", job.run_id, exc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(kernel.exited), 5)
            kernel_status = "kernel_exited"
        finally:
            self._run_waiters.pop(job.run_id, None)
            with contextlib.suppress(Exception):
                kernel.service.scope.end(job.run_id)
        self._finish_run(job, plan, kernel_status)

    def _graph_code(self, cid: str, codes: dict[str, str]) -> str:
        if cid in codes:
            return codes[cid]
        rt = self.cells.get(cid)
        if rt is not None and rt.submitted is not None:
            return rt.submitted
        cell = self.doc.cells.get(cid)
        return cell.code if cell is not None else ""

    def _mark_stale(self, cells: Sequence[str], run_id: str) -> None:
        for cid in cells:
            rt = self.cells.get(cid)
            if rt is not None and rt.status == "fresh":
                rt.status = "stale"
                self._publish_status(cid, run_id)

    def _mark_busy(self) -> None:
        if self.kernel is not None and self.kernel_state != "busy":
            self._set_kernel_state("busy")

    def _end_interrupt(self, run_id: str, kernel_id: str | None) -> bool:
        """Close an interrupt of ``run_id`` that is under way: clients are
        told the interrupt is over (the run's own end follows; an escalation
        still running sees the run gone and stops by itself). Whether one
        was under way."""
        if run_id not in self._interrupting:
            return False
        self._interrupting.discard(run_id)
        self.publish(
            KernelInterrupt(
                kernel_id=kernel_id, run_id=run_id, step="done", at=self.engine.clock.now()
            )
        )
        return True

    def _finish_run(self, job: Job, plan: Plan, kernel_status: str) -> None:
        now = self.engine.clock.now()
        interrupted = self._end_interrupt(job.run_id, job.record.kernel_id)
        status: RunStatus
        reason: str | None = None
        if kernel_status == "kernel_exited":
            status, reason = self._run_status_on_exit or ("kernel_restarted", "kernel_exited")
        else:
            status = _RUN_STATUS.get(kernel_status, "error")
            if interrupted and status != "ok":
                status, reason = "interrupted", "interrupt"
        for s in plan.steps:
            rt = self.cells.get(s.cell_id)
            if rt is None:
                continue
            if rt.activity is not None and kernel_status != "kernel_exited":
                # Planned but never finished: the run ended before it.
                rt.activity = None
                rt.status = "skipped"
                rt.holds_value = False
                self._publish_status(s.cell_id, job.run_id)
        self._publish_derived_statuses(job.run_id)
        job.finish(status, reason, now)
        broker = self.engine.sql_broker()
        if broker is not None and job.record.kernel_id is not None:
            broker.run_finished(job.record.kernel_id, job.run_id)
        self.publish(RunFinished(run_id=job.run_id, status=status, reason=reason))
        self.record_activity(
            job.actor,
            "cell_run",
            self._stale_before.pop(job.run_id, set()),
            run_id=job.run_id,
            status=status,
            cell_ids=[s.cell_id for s in plan.steps],
            error_class=next(
                (
                    o.error.ename
                    for c in plan.steps
                    if (o := self.outputs.get(c.cell_id)) is not None
                    and o.error is not None
                    and o.run is not None
                    and o.run.run_id == job.run_id
                ),
                None,
            ),
        )
        self.snapshots.schedule()

    # ------------------------------------------------------------------ kernel notifications

    def on_kernel_notification(
        self, kernel: KernelHandle, method: str, params: dict[str, Any]
    ) -> None:
        if kernel is not self.kernel:
            return
        run_id = params.get("run_id")
        cid = params.get("cell_id")
        handler = getattr(self, "_k_" + method.replace(".", "_"), None)
        if handler is None:
            return
        try:
            handler(run_id, cid, params)
        except Exception:
            log.exception("kernel notification %s failed", method)

    def _job_for(self, run_id: Any) -> Job | None:
        if self.current is not None and self.current.run_id == run_id:
            return self.current
        return None

    def _k_cell_started(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        job = self._job_for(run_id)
        code = self._steps.get(str(run_id), {}).get(str(cid))
        rt = self.cells.get(str(cid))
        if job is None or code is None or rt is None:
            return
        rt.activity = "running"
        rt.submitted = code
        out = self.outputs.setdefault(str(cid), CellOutputs())
        out.begin_run(
            RunMeta(
                run_id=job.run_id,
                trigger=job.trigger,
                by=RunActor.of(job.actor),
                status="running",
                started_at=self.engine.clock.now(),
            ),
            code_hash=code_hash(code),
            lineage_hash=self._lineage.get(job.run_id, {}).get(str(cid)),
            env_fingerprint=self._env_fingerprint(),
        )
        self._publish_status(str(cid), job.run_id)

    def _k_cell_output(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        out = self.outputs.get(str(cid))
        bundle = params.get("output")
        if out is None or not isinstance(bundle, dict):
            return
        mode: Literal["replace", "append"] = (
            "append" if params.get("mode") == "append" else "replace"
        )
        stored = out.apply_output(bundle, mode, self.engine.config.limits)
        self.publish(CellOutputEvent(cell_id=str(cid), run_id=run_id, output=stored, mode=mode))

    def _k_cell_stream(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        name: Literal["stdout", "stderr"] = "stderr" if params.get("name") == "stderr" else "stdout"
        text = str(params.get("text", ""))
        out = self.outputs.setdefault(str(cid), CellOutputs())
        out.apply_stream(name, text, self.engine.config.limits)
        self.publish(CellStreamEvent(cell_id=str(cid), run_id=run_id, name=name, text=text))

    def _k_thread_output(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        self._k_cell_stream(None, cid, params)

    def _k_cell_finished(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        job = self._job_for(run_id)
        rt = self.cells.get(str(cid))
        if job is None or rt is None:
            return
        kstatus = str(params.get("status", f.STATUS_ERROR))
        status = _KERNEL_STATUS.get(kstatus, "error")
        if job.run_id in self._interrupting and status == "error":
            status = "interrupted"
        rt.status = status
        rt.activity = None
        rt.holds_value = status == "fresh"
        rt.defs = [str(d) for d in params.get("defs", []) or []]
        if isinstance(params.get("duration_ms"), int | float):
            rt.duration_s = float(params["duration_ms"]) / 1000.0
        err = params.get("error")
        error = (
            ErrorInfo(
                ename=str(err.get("ename", "Error")),
                evalue=str(err.get("evalue", "")),
                traceback=[str(t) for t in err.get("traceback", []) or []],
            )
            if isinstance(err, dict)
            else None
        )
        out = self.outputs.setdefault(str(cid), CellOutputs())
        out.apply_finished(error, kstatus)
        if out.run is not None:
            out.run.finished_at = self.engine.clock.now()
        self.publish(
            CellFinishedEvent(
                cell_id=str(cid),
                run_id=job.run_id,
                status=status,
                error=error,
                duration_ms=params.get("duration_ms")
                if isinstance(params.get("duration_ms"), int)
                else None,
            )
        )
        self._publish_status(str(cid), job.run_id)
        self.snapshots.schedule()

    def _k_cell_variables(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        rt = self.cells.get(str(cid))
        if rt is None:
            return
        variables = variables_of(str(cid), params.get("variables"))
        rt.variables = variables
        self.publish(CellVariablesEvent(cell_id=str(cid), run_id=run_id, variables=variables))

    def _k_run_finished(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        waiter = self._run_waiters.get(str(run_id))
        if waiter is not None and not waiter.done():
            waiter.set_result(str(params.get("status", f.STATUS_ERROR)))

    @staticmethod
    def _buffers(params: dict[str, Any]) -> list[bytes]:
        out: list[bytes] = []
        for b in params.get("buffers") or []:
            if isinstance(b, f.Segment):
                out.append(b.data)
            elif isinstance(b, bytes | bytearray):
                out.append(bytes(b))
        return out

    @staticmethod
    def _flag_sensitive(params: dict[str, Any], content: dict[str, Any]) -> dict[str, Any]:
        """The kernel flags secrets (passwords, ``_sensitive`` models); the hub
        redacts models whose state says so."""
        if not params.get("sensitive"):
            return content
        data = dict(content.get("data") or {})
        state = dict(data.get("state") or {})
        state.setdefault("_sensitive", True)
        data["state"] = state
        return {**content, "data": data}

    def _k_comm_open(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        hub = self.widget_hub
        if hub is None:
            return
        comm_id = str(params.get("comm_id"))
        content = self._flag_sensitive(params, dict(params.get("content") or {}))
        metadata = params.get("metadata")
        hub.kernel_open(
            comm_id,
            content,
            self._buffers(params),
            metadata if isinstance(metadata, dict) else None,
        )
        self.widget_cells[comm_id] = self._running_cell()
        self._route_frames()

    def _k_comm_msg(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        hub = self.widget_hub
        if hub is None:
            return
        content = self._flag_sensitive(params, dict(params.get("content") or {}))
        parent = params.get("parent_msg_id")
        hub.kernel_msg(
            str(params.get("comm_id")),
            content,
            self._buffers(params),
            parent if isinstance(parent, str) else None,
        )
        self._route_frames()

    def _k_comm_close(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        comm_id = str(params.get("comm_id"))
        self.widget_cells.pop(comm_id, None)
        self.bindings.pop(comm_id, None)
        if self.widget_hub is not None:
            self.widget_hub.kernel_close(comm_id)
            self._route_frames()

    def _k_comm_idle(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        msg_id = str(params.get("msg_id"))
        waiter = self._comm_waiters.pop(msg_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def _k_widget_asset(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        """The kernel offers a widget library's files from the notebook's
        environment, as bytes: each file's ``data``, or the notification's
        ``buffers`` in file order. A refused offer is a notice; nothing of it
        is stored."""
        module = params.get("module")
        version = params.get("version")
        raw = params.get("files")
        files: list[KernelAssetFile] = []
        try:
            if not isinstance(module, str) or not isinstance(version, str):
                raise AssetRefusedError("an offer names its module and version")
            if not isinstance(raw, list):
                raise AssetRefusedError("the offer has no files")
            buffers = self._buffers(params)
            if "buffers" in params and len(buffers) != len(raw):
                raise AssetRefusedError("the offer's files and buffers do not match")
            for i, item in enumerate(raw):
                if not isinstance(item, dict):
                    raise AssetRefusedError("an offered file is not an object")
                data = self._buffers({"buffers": [item.get("data")]}) or buffers[i : i + 1]
                path, sha = item.get("path"), item.get("sha256")
                if not data or not isinstance(path, str) or not isinstance(sha, str):
                    raise AssetRefusedError("an offered file needs a path, a hash and bytes")
                files.append(KernelAssetFile(path, sha, data[0]))
            self.engine.widget_assets.offer_kernel_asset(
                self.asset_scope, module, version, files, store_scope=self.asset_store_scope
            )
        except AssetRefusedError as exc:
            log.warning("widget asset offer refused: %s", exc)
            self._notice(
                CellNotice(
                    kind="widget_asset_refused",
                    cell_id=cid if isinstance(cid, str) else None,
                    message=f"Widget code for {module} was refused: {exc}",
                    data={"module": module if isinstance(module, str) else None},
                )
            )

    def widget_asset_ref(self, module: str, version: str | None) -> WidgetAssetRef:
        """A module's entry code: a platform bundle, or the ``index.js`` this
        notebook's kernel offered (the exact version when it was offered,
        else the latest). A module's other files are fetched by hash."""
        assets = self.engine.widget_assets
        sha = assets.resolve(self.asset_scope, module, version)
        entry: AssetEntry | None = None
        if sha is not None:
            entry = assets.platform(module)
            if entry is None or entry.sha256 != sha:
                entry = next((e for e in assets.offered(self.asset_scope) if e.sha256 == sha), None)
        if entry is None:
            raise NotFoundError(f"no widget module {module} {version or ''}".rstrip())
        return _asset_ref(entry, module)

    def widget_asset_bytes(self, sha256: str) -> bytes:
        data = self.engine.widget_assets.fetch(self.asset_scope, sha256)
        if data is None:
            raise NotFoundError("no such widget asset for this notebook")
        return data

    def _route_frames(self) -> None:
        """Hand each pending hub message to the client owning its frame."""
        hub = self.widget_hub
        if hub is None:
            return
        for out in hub.drain():
            frame = self.frames.get(out.frame_id)
            entry = self.clients.get(frame.client_id) if frame is not None else None
            if entry is not None:
                self.hub.publish_to(
                    entry[1],
                    FrameMessage(
                        frame_id=out.frame_id, message=out.message, buffers=list(out.buffers)
                    ),
                )

    def _k_ui_bindings(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        # The kernel reports every live binding after each step; the step's
        # cell is not the model's. A model's cell is where it was opened.
        for model_id, names in (params.get("bindings") or {}).items():
            mid = str(model_id)
            self.bindings[mid] = (self.widget_cells.get(mid), [str(n) for n in names or []])

    def _k_module_missing(self, run_id: Any, cid: Any, params: dict[str, Any]) -> None:
        self._notice(
            CellNotice(
                kind="module_missing",
                cell_id=str(cid) if cid else None,
                message=f"No module named {params.get('module')}",
                data={"module": params.get("module")},
            )
        )

    def _client_can_run(self, client_id: str) -> bool:
        entry = self.clients.get(client_id)
        return entry is not None and entry[0].can_run

    def _running_cell(self) -> str | None:
        for cid, rt in self.cells.items():
            if rt.activity == "running":
                return cid
        return None

    # ------------------------------------------------------------------ kernel requests

    def method_registry(self, kernel_id: str, data_dir: str) -> MethodRegistry:
        """The methods this kernel may call on the engine: ``sql.execute``,
        served by the SQL broker over a context that knows which run
        is executing on this kernel and who asked for it."""
        registry = MethodRegistry()
        broker = self.engine.sql_broker()
        if broker is not None:
            from alkera_notebook.sql.broker import register_sql_methods

            register_sql_methods(registry, broker, _SessionSqlContext(self, kernel_id, data_dir))
        return registry

    def sql_run(self, run_id: str) -> Any:
        """The run executing now under ``run_id``, for SQL attribution."""
        from alkera_notebook.sql.broker import RunInfo as SqlRunInfo

        job = self.current
        if job is None or job.run_id != run_id or job.record.status != "running":
            return None
        return SqlRunInfo(run_id, job.actor, self.path)

    # ------------------------------------------------------------------ kernel lifecycle

    async def ensure_kernel(self) -> KernelHandle:
        async with self._starting:
            if self.kernel is not None and self.kernel.alive:
                await self._check_selected_env()
                return self.kernel
            return await self._start_kernel()

    async def _start_kernel(self) -> KernelHandle:
        engine = self.engine
        # Refusals carry their reason as a KernelStartError so a run reports
        # exactly why it could not start (``kernel_cap``, ``memory``).
        if engine.live_kernel_count() >= engine.config.max_kernels:
            raise KernelStartError(
                "kernel_cap", f"at most {engine.config.max_kernels} kernels may run at once"
            )
        if not engine.guard.admit():
            raise KernelStartError("memory", "not enough memory to start a kernel")
        env = await self._run_env()
        self.env = env
        self._selected_env_id = env.env_id
        self.kernel_build = generations.in_use(Path(env.prefix))
        kernel_id = engine.ids.kernel_id()
        self._set_kernel_state("starting", kernel_id=kernel_id)
        settings = self.settings_model()
        data_dir = str(kernel_data_dir(engine.config.data_root, kernel_id))
        try:
            handle = await start_kernel(
                kernel_id=kernel_id,
                kind="python",
                interpreter=env.interpreter,
                notebook_dir=str(self.abs_path.parent),
                mount=engine.kernel_mount(),
                launcher=engine.launcher,
                transport=engine.transport,
                base_env=engine.kernel_base_env(env),
                data_dir=data_dir,
                hello_result={
                    "kernel_id": kernel_id,
                    "settings": {
                        "dataframe": settings.dataframe,
                        "reactivity": settings.reactivity,
                        "autoreload": settings.autoreload,
                        "sql_row_limit": settings.sql_row_limit,
                        "args": dict(engine.config.kernel_args),
                    },
                },
                registry=self.method_registry(kernel_id, data_dir),
                connect_timeout_s=engine.config.connect_timeout_s,
                on_notification=self.on_kernel_notification,
                started_at=engine.clock.now(),
                env_id=env.env_id,
            )
        except KernelStartError as exc:
            self._set_kernel_state("absent")
            # Named, so the kernel said to be starting is said to be gone.
            self.publish(KernelExited(kernel_id=kernel_id, reason=exc.reason, message=exc.message))
            raise
        self.kernel = handle
        self.widget_hub = WidgetHub(
            scope=self.asset_scope,
            assets=engine.value_assets,
            asset_scope=self.asset_store_scope,
            can_run=self._client_can_run,
        )
        self._run_status_on_exit = None
        self._set_kernel_state("idle")
        self._tasks.append(asyncio.get_running_loop().create_task(self._watch_exit(handle)))
        return handle

    async def _run_env(self) -> EnvDescriptor:
        """The environment a new kernel runs in. A managed one is brought up
        to date first: built when it is missing, failed, or its spec changed
        since its last build (a build that is current returns at once). Why
        it cannot be had is a :class:`KernelStartError` the run ends
        ``refused`` with: ``env_build_failed`` (a build command failed) or
        ``env_unavailable`` (it cannot be found or built at all)."""
        envs = self.engine.envs
        try:
            env = await envs.resolve(str(self.abs_path), self.doc.setting("env"))
            if env.kind in MANAGED_ENV_KINDS:
                env = await envs.materialize(env.env_id)
        except EnvBuildError as exc:
            raise KernelStartError("env_build_failed", env_failure_message(exc)) from exc
        except (EnvError, EnvNotFoundError) as exc:
            raise KernelStartError("env_unavailable", str(exc)) from exc
        return env

    async def _watch_exit(self, handle: KernelHandle) -> None:
        info = await handle.exited
        if handle is not self.kernel:
            return
        self._on_kernel_exit(handle, info)

    def _on_kernel_exit(self, handle: KernelHandle, info: ExitInfo) -> None:
        self.kernel = None
        reason = info.reason
        if reason == "crashed" and self.engine.oom_recent():
            reason = "out_of_memory"
        self._set_kernel_state(
            "restarting" if reason in ("restart", "interrupt_restart") else "absent"
        )
        self.publish(
            KernelExited(
                kernel_id=handle.kernel_id,
                reason=reason,
                exit_code=info.exit_code,
                peak_rss_bytes=info.data.get("peak_rss_bytes"),
                limit_bytes=info.data.get("limit_bytes"),
                largest_process=info.data.get("largest_process"),
                message=(
                    "The kernel ran out of memory and was restarted"
                    if reason == "out_of_memory"
                    else info.message
                ),
            )
        )
        on_exit: tuple[RunStatus, str] = (
            ("interrupted", "suspended") if reason == "suspended" else ("kernel_restarted", reason)
        )
        self._run_status_on_exit = on_exit
        self.held.drop_all()
        for rt in self.cells.values():
            rt.reset()
        # The models died with the kernel: frames showing them are dropped.
        self.widget_hub = None
        self.widget_cells.clear()
        self.bindings.clear()
        self.frames.clear()
        self.deleted_defs = []
        for waiter in self._comm_waiters.values():
            if not waiter.done():
                waiter.set_result(None)
        self._comm_waiters.clear()
        while self.queue:
            self._end(self.queue.popleft(), on_exit[0], on_exit[1])
        for cell in self._live():
            self._publish_status(cell.id)
        self._record_exit(reason)
        self.snapshots.schedule()

    def _record_exit(self, reason: str) -> None:
        """The feed's entry for a kernel that is gone: who asked for it (the
        system when nobody did) and why."""
        cause, self._exit_cause = self._exit_cause, None
        actor: Actor | ActivityActor = system_actor()
        if cause is not None and reason not in ("out_of_memory", "crashed"):
            actor, reason = cause
        kind = "kernel_stop" if reason in _STOP_REASONS else "kernel_restart"
        self.record_activity(actor, kind, reason=reason)

    async def interrupt(self, *, clear_queue: bool, actor: Actor | None = None) -> None:
        if clear_queue:
            while self.queue:
                self._end(self.queue.popleft(), "interrupted", "interrupt")
            self._clear_activity("")
        active = self.current
        kernel = self.kernel
        if active is None or kernel is None or not active.started:
            return
        if active.record.status != "running":
            return  # nothing is executing: an interrupt does nothing
        run_id = active.run_id
        self._interrupting.add(run_id)
        self._interrupter = actor
        # The hint goes first but is not awaited: a kernel stuck in C code
        # holding the GIL cannot answer it, and the signal must still go.
        hint = asyncio.ensure_future(kernel.request(f.RUN_INTERRUPT, {"run_id": run_id}))
        hint.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._background.add(hint)
        hint.add_done_callback(self._background.discard)
        await asyncio.sleep(0.01)
        kernel.interrupt_signal()
        self._interrupt_step(kernel, run_id, "signalled")
        broker = self.engine.sql_broker()
        if broker is not None:
            # Statements the run has in flight stop at the data system too.
            await broker.cancel_run(kernel.kernel_id, run_id)
        if self._escalation is None or self._escalation.done():
            self._escalation = asyncio.get_running_loop().create_task(
                self._escalate(kernel, run_id)
            )

    async def _escalate(self, kernel: KernelHandle, run_id: str) -> None:
        first, restart_after = self.engine.config.interrupt_escalation_s

        def still_running() -> bool:
            return (
                kernel.alive
                and self.current is not None
                and self.current.run_id == run_id
                and run_id in self._run_waiters
            )

        await asyncio.sleep(first)
        if not still_running():
            return
        kernel.interrupt_signal()
        self._interrupt_step(kernel, run_id, "second_signal")
        await asyncio.sleep(max(0.0, restart_after - first))
        if not still_running():
            return
        self._interrupt_step(kernel, run_id, "restarting")
        if self._interrupter is not None:
            self._exit_cause = (self._interrupter, "interrupt_restart")
        kernel.kill("interrupt_restart")
        with contextlib.suppress(Exception):
            await kernel.exited
        with contextlib.suppress(KernelUnavailableError, KernelStartError):
            await self.ensure_kernel()

    def _interrupt_step(
        self,
        kernel: KernelHandle,
        run_id: str,
        step: Literal["signalled", "second_signal", "restarting"],
    ) -> None:
        self.publish(
            KernelInterrupt(
                kernel_id=kernel.kernel_id, run_id=run_id, step=step, at=self.engine.clock.now()
            )
        )

    async def restart(self, actor: Actor | None = None, reason: str = "restart") -> None:
        """Restart the kernel; ``reason`` is what the feed shows (``restart``
        when someone asked, ``env_changed`` after an environment switch)."""
        kernel = self.kernel
        if kernel is not None:
            if actor is not None:
                self._exit_cause = (actor, reason)
            await kernel.shutdown("restart", grace_s=1.0)
            # Let the exit handler run before the new kernel starts.
            await asyncio.sleep(0)
        await self.ensure_kernel()

    async def shutdown_kernel(self, reason: str = "shutdown", actor: Actor | None = None) -> None:
        kernel = self.kernel
        if kernel is not None:
            if actor is not None:
                self._exit_cause = (actor, reason)
            await kernel.shutdown(reason, grace_s=1.0)
            await asyncio.sleep(0)
        if self.kernel is None:
            self._set_kernel_state("stopped" if reason != "suspended" else "absent")

    # ------------------------------------------------------------------ comms and widgets

    def attach_frame(
        self,
        client_id: str,
        frame_id: str | None,
        model_ids: Sequence[str],
        *,
        output_id: str | None = None,
        readonly: bool = False,
    ) -> FrameAttached:
        """A client's output frame joins: the comm-open replays of the models
        it shows, from the hub's cache. The frame shows ``model_ids``, or
        the widgets of output ``output_id`` (``<cell_id>`` for every widget
        the cell displays, ``<cell_id>/<n>`` for its n-th display). Without
        ``frame_id`` the engine names the frame."""
        hub = self.widget_hub
        if hub is None:
            raise KernelUnavailableError("no kernel is running", reason="no_kernel")
        if client_id not in self.clients:
            raise NotFoundError("unknown client")
        roots = list(dict.fromkeys(model_ids))
        if not roots:
            if output_id is None:
                raise ValueError("a frame needs model_ids or an output_id")
            roots = self._output_models(output_id)
        unknown = [m for m in roots if hub.model(m) is None]
        if unknown:
            raise NotFoundError(f"no widget {unknown[0]}")
        if frame_id is None:
            frame_id = self.engine.ids.message_id().replace("msg_", "frm_", 1)
        held = self.frames.get(frame_id)
        if held is not None and held.client_id != client_id:
            raise ForbiddenError("that frame is not yours")
        frame = Frame(frame_id, client_id, tuple(roots), readonly)
        try:
            replays = hub.attach(frame)
        except WidgetRefusedError as exc:
            raise ForbiddenError(str(exc)) from exc
        self.frames[frame_id] = frame
        return FrameAttached(
            frame_id=frame_id,
            output_id=output_id,
            model_ids=roots,
            opens=[
                FrameMessage(frame_id=r.frame_id, message=r.message, buffers=list(r.buffers))
                for r in replays
            ],
        )

    def _output_models(self, output_id: str) -> list[str]:
        """The widget models an output displays, by ``<cell_id>[/<n>]``."""
        cell_id, _, index = output_id.partition("/")
        out = self.outputs.get(cell_id)
        bundles = list(out.bundles) if out is not None else []
        if index:
            if not index.isdigit() or int(index) >= len(bundles):
                raise NotFoundError(f"no output {output_id}")
            bundles = [bundles[int(index)]]
        models = [
            str(b[WIDGET_MIME]["model_id"])
            for b in bundles
            if isinstance(b.get(WIDGET_MIME), dict) and b[WIDGET_MIME].get("model_id")
        ]
        if not models:
            raise NotFoundError(f"output {output_id} shows no widget")
        return list(dict.fromkeys(models))

    def detach_frame(self, frame_id: str) -> None:
        self.frames.pop(frame_id, None)
        if self.widget_hub is not None:
            self.widget_hub.detach(frame_id)

    def _frame_for(self, client_id: str, comm_id: str) -> str:
        """The client's frame showing ``comm_id``; one is attached for it when
        it has none (an agent setting a widget has no frame of its own)."""
        hub = self.widget_hub
        assert hub is not None
        for frame_id, frame in self.frames.items():
            if frame.client_id == client_id and comm_id in hub.frame_closure(frame):
                return frame_id
        return self.attach_frame(client_id, f"{client_id}:{comm_id}", [comm_id]).frame_id

    async def comm_send(
        self,
        actor: Actor,
        client_id: str,
        comm_id: str,
        msg_id: str,
        content: dict[str, Any],
        buffers: list[bytes],
        frame_id: str | None = None,
    ) -> Job:
        """A frontend's widget message, authorized per message, delivered to
        the kernel as a run in the notebook's queue."""
        if not actor.can_run:
            raise ForbiddenError("you cannot interact with this notebook's widgets")
        hub = self.widget_hub
        if self.kernel is None or hub is None:
            raise KernelUnavailableError("no kernel is running", reason="no_kernel")
        frame = frame_id or self._frame_for(client_id, comm_id)
        try:
            # The hub decides whether this client holds the frame and the comm.
            delivery = hub.frontend_send(
                frame, comm_id, msg_id, content, buffers, client_id=client_id
            )
        except WidgetRefusedError as exc:
            raise ForbiddenError(str(exc)) from exc
        data = delivery.msg["content"].get("data") or {}
        state = data.get("state") if data.get("method") == "update" else None
        # Consecutive updates to one model merge while still queued; the merged
        # message's frame still gets its idle when the survivor is handled.
        if state is not None and self.queue:
            tail = self.queue[-1]
            tail_data = (tail.content.get("content") or {}).get("data") or {}
            if (
                tail.kind == "comm"
                and tail.comm_id == comm_id
                and not tail.started
                and tail_data.get("method") == "update"
                and not buffers
                and not tail.buffers
            ):
                tail_data.setdefault("state", {}).update(state)
                tail.merged_msg_ids.append(msg_id)
                return tail
        run_id = self.engine.ids.run_id()
        record = RunRecord(run_id=run_id, requested_by=actor, trigger="widget", status="queued")
        job = Job(
            kind="comm",
            run_id=run_id,
            actor=actor,
            trigger="widget",
            record=record,
            done=asyncio.get_running_loop().create_future(),
            comm_id=comm_id,
            msg_id=msg_id,
            content=delivery.msg,
            buffers=list(delivery.buffers),
        )
        self.records[run_id] = record
        self.queue.append(job)
        self._wakeup.set()
        return job

    async def _exec_comm(self, job: Job) -> None:
        kernel = self.kernel
        hub = self.widget_hub
        now = self.engine.clock.now
        if kernel is None or hub is None:
            job.finish("kernel_restarted", "no_kernel", now())
            return
        job.record.status = "running"
        job.record.started_at = now()
        job.record.kernel_id = kernel.kernel_id
        self._mark_busy()
        assert job.msg_id is not None and job.comm_id is not None
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._comm_waiters[job.msg_id] = waiter
        with contextlib.suppress(ValueError):
            kernel.service.scope.begin(job.run_id)
        try:
            await kernel.request(
                f.COMM_DELIVER,
                {
                    "run_id": job.run_id,
                    "msg_id": job.msg_id,
                    "msg": job.content,
                    "buffers": [f.Segment(b) for b in job.buffers],
                },
            )
            exited = asyncio.ensure_future(asyncio.shield(kernel.exited))
            both: set[asyncio.Future[Any]] = {waiter, exited}
            await asyncio.wait(both, return_when=asyncio.FIRST_COMPLETED)
            exited.cancel()
        except (PeerClosedError, f.RpcError, OSError):
            job.finish("kernel_restarted", "kernel_exited", now())
            return
        finally:
            self._comm_waiters.pop(job.msg_id, None)
            with contextlib.suppress(Exception):
                kernel.service.scope.end(job.run_id)
            self._end_interrupt(job.run_id, kernel.kernel_id)
        # The frames that sent this message (and any merged into it) may now
        # release their buffered changes.
        for msg_id in [job.msg_id, *job.merged_msg_ids]:
            hub.kernel_idle(msg_id)
        self._route_frames()
        job.finish("ok", None, now())
        broker = self.engine.sql_broker()
        if broker is not None:
            broker.run_finished(kernel.kernel_id, job.run_id)
        data = (job.content.get("content") or {}).get("data") or {}
        state = data.get("state") or {}
        bound = self.bindings.get(job.comm_id)
        if data.get("method") == "update" and bound is not None and bound[1]:
            names = bound[1]
            graph = self._kernel_graph()
            wanted = set(names)
            targets = [c for c in graph.order if wanted & set(graph.refs.get(c, ()))]
            if targets and state:
                info, _ = await self.request_run(
                    job.actor,
                    scope="cells",
                    targets=targets,
                    trigger="widget",
                    frontier=None,
                    confirm=True,
                    target_code="submitted",
                )
                job.record.reason = f"run:{info.run_id}"

    def _kernel_graph(self) -> CellGraph:
        live = self._live()
        codes = tuple((c.id, self._graph_code(c.id, {})) for c in live)
        cached = self._kernel_graph_cache
        if cached is not None and cached[0] == codes:
            return cached[1]
        graph = CellGraph.from_analysis(
            [c.id for c in live], self.engine.fmt.analyze_code(list(codes))
        )
        self._kernel_graph_cache = (codes, graph)
        return graph

    def widget_list(self) -> list[WidgetInfo]:
        hub = self.widget_hub
        if hub is None:
            return []
        out = []
        for model in hub.models():
            state = model.state
            bound = self.bindings.get(model.comm_id)
            value = state.get("value")
            if model.sensitive and "value" in state:
                value = "<redacted>"
            out.append(
                WidgetInfo(
                    model_id=model.comm_id,
                    cell_id=self.widget_cells.get(model.comm_id) or (bound[0] if bound else None),
                    type=str(state.get("_model_name", "widget")),
                    value=value,
                    bound_names=list(bound[1]) if bound else [],
                )
            )
        return out

    # ------------------------------------------------------------------ inspection

    async def inspect(self, query: InspectQuery) -> InspectResult:
        if query.what == "variables":
            variables = [v for rt in self.cells.values() for v in rt.variables]
            if query.name is not None:
                variables = [v for v in variables if v.name == query.name]
            return InspectResult(variables=variables)
        params: dict[str, Any] = {"name": query.name}
        if query.what == "frame":
            # Refused before anything reaches the kernel (or starts one).
            params.update(offset=query.offset, limit=query.limit)
            sort = frame_sort(query.sort)
            if sort is not None:
                params["sort"] = sort
            if query.filter_sql is not None:
                params["filter_sql"] = check_frame_filter(query.filter_sql)
        kernel = self.kernel
        if kernel is None:
            raise KernelUnavailableError("no kernel is running", reason="no_kernel")
        if not query.name:
            raise NotFoundError("name is required")
        if query.what == "frame":
            result = await kernel.request(f.INSPECT_FRAME, params)
            if isinstance(result, dict):
                return InspectResult(
                    table=_table_page(result.get("table")),
                    total_rows=result.get("total_rows"),
                )
            return InspectResult()
        result = await kernel.request(f.INSPECT_VALUE, {"name": query.name, "depth": query.depth})
        return InspectResult(
            summary=(result or {}).get("summary") if isinstance(result, dict) else None
        )

    # ------------------------------------------------------------------ snapshots

    def _snapshot_content(self) -> SnapshotContent:
        cells = [
            SnapshotCell(cell_id=c.id, code=c.code, outputs=self.outputs.get(c.id))
            for c in self._live()
        ]
        kernel = None
        if self.env is not None:
            kernel = KernelMeta(
                kernel_id=self.kernel.kernel_id if self.kernel else "",
                env_id=self.env.env_id,
                env_fingerprint=self._env_fingerprint() or "",
            )
        return SnapshotContent(
            cells=cells, kernel=kernel, outputs_in_git=bool(self.doc.setting("outputs_in_git"))
        )

    def _on_snapshot_written(self, path: Path, cells: int) -> None:
        with contextlib.suppress(OSError):
            update_gitignore(self.place, bool(self.doc.setting("outputs_in_git")))
        self.publish(SnapshotSaved(path=str(path), cells=cells))

    # ------------------------------------------------------------------ settings and envs

    async def set_settings(self, actor: Actor, changes: dict[str, Any]) -> Settings:
        if not changes:
            return self.settings_model()
        if not actor.can_edit:
            raise ForbiddenError("you cannot edit this notebook")
        changes = dict(changes)
        env_changed = "env" in changes and changes["env"] != self.doc.setting("env")
        ops: list[NotebookOp] = [SetSetting(key=k, value=v) for k, v in changes.items()]
        await self.apply(actor, ops, None, None)
        if env_changed:
            self.env = None
            if self.kernel is not None:
                await self.restart(actor, "env_changed")
        if "outputs_in_git" in changes:
            with contextlib.suppress(OSError):
                update_gitignore(self.place, bool(changes["outputs_in_git"]))
        return self.settings_model()

    # ------------------------------------------------------------------ suspend, close

    async def suspend(self) -> tuple[list[str], list[str]]:
        self._suspended = True
        stopped: list[str] = []
        interrupted: list[str] = []
        if self.current is not None and self.current.kind == "run":
            interrupted.append(self.current.run_id)
        interrupted += [j.run_id for j in self.queue]
        if self.kernel is not None:
            stopped.append(self.kernel.kernel_id)
            await self.shutdown_kernel("suspended")
        while self.queue:
            self._end(self.queue.popleft(), "interrupted", "suspended")
        # Let the worker observe the exit before the snapshot is taken.
        for _ in range(20):
            if self.current is None:
                break
            await asyncio.sleep(0.01)
        await self.snapshots.flush()
        return stopped, interrupted

    async def resume(self) -> None:
        self._suspended = False
        await self.refresh_doc()
        for rt in self.cells.values():
            rt.reset()
        self.outputs.clear()
        self._reattach_saved()
        self._set_kernel_state("absent")
        for cell in self._live():
            self._publish_status(cell.id)

    async def close(self) -> None:
        self._closed = True
        if self.kernel is not None:
            await self.shutdown_kernel("shutdown")
        if self._escalation is not None:
            self._escalation.cancel()
        if self._idle_task is not None:
            self._idle_task.cancel()
        with contextlib.suppress(Exception):
            await self.snapshots.close()
        await self.held.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        while self.queue:
            self._end(self.queue.popleft(), "interrupted", "closed")
        self.hub.close()


class _SessionSqlContext:
    """The SQL broker's view of one kernel: the workspace it acts for, the
    codecs it said it decodes, its data directory, and which run is executing
    on it now (and who asked)."""

    def __init__(self, session: Session, kernel_id: str, data_dir: str) -> None:
        self._session = session
        self._kernel_id = kernel_id
        self._data_dir = Path(data_dir)

    @property
    def kernel_id(self) -> str:
        return self._kernel_id

    @property
    def workspace(self) -> Any:
        from alkera_notebook.sql.provider import SqlWorkspace

        cfg = self._session.engine.config
        return SqlWorkspace(
            id=cfg.workspace_id,
            root=cfg.workspace_root,
            org_id=cfg.org_id,
        )

    @property
    def codecs(self) -> frozenset[str]:
        kernel = self._session.kernel
        if kernel is None or kernel.kernel_id != self._kernel_id:
            return frozenset()
        return kernel.session.codecs

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    @property
    def sql_row_limit(self) -> int | None:
        return self._session.settings_model().sql_row_limit

    def active_run(self, run_id: str) -> Any:
        kernel = self._session.kernel
        if kernel is None or kernel.kernel_id != self._kernel_id:
            return None
        return self._session.sql_run(run_id)


def _has_interpolation(sql: str) -> bool:
    """Whether an ``rf`` SQL body has ``{expr}`` parts (``{{`` is a literal brace)."""
    stripped = sql.replace("{{", "").replace("}}", "")
    return "{" in stripped


def run_info_from_record(record: RunRecord) -> RunInfo:
    return RunInfo(
        run_id=record.run_id,
        status=record.status,
        reason=record.reason,
        trigger=record.trigger,
        plan=list(record.plan),
    )


__all__ = ["Session", "run_info_from_record"]


def _table_page(table: Any) -> dict[str, Any] | None:
    """``inspect.frame``'s table page (``{schema, rows, total_rows,
    offset}``, plain JSON), as the kernel made it: the same page a cell's
    table output carries, passed on untouched."""
    if isinstance(table, dict) and isinstance(table.get("rows"), list):
        return table
    return None


def _asset_ref(entry: AssetEntry, module: str) -> WidgetAssetRef:
    return WidgetAssetRef(
        module=module,
        version=entry.version,
        sha256=entry.sha256,
        bytes=entry.bytes,
        kind=entry.kind,
    )
