"""The notebook engine: opens notebooks, plans and runs cells, routes events.

One :class:`NotebookEngine` per workspace root (or standalone root). Nothing
here is module-global: two engines in one process share nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from alkera_notebook.document.fmt import FormatApi, default_format
from alkera_notebook.document.ops import InsertCell, NotebookOp, NotebookOpsResult
from alkera_notebook.document.store import DocumentStore
from alkera_notebook.engine.config import Clock, EngineConfig, IdSource
from alkera_notebook.engine.errors import (
    EngineClosedError,
    ForbiddenError,
    KernelUnavailableError,
    NotFoundError,
)
from alkera_notebook.engine.models import (
    AboveTarget,
    Activity,
    Actor,
    AllTarget,
    BelowTarget,
    CellsTarget,
    EnvAction,
    EnvInfo,
    EnvListing,
    EnvPackages,
    EnvResult,
    GraphView,
    InspectQuery,
    InspectResult,
    KernelInfo,
    NotebookView,
    OutputDetail,
    OutputPart,
    PackageInfo,
    ReadQuery,
    RunInfo,
    RunRecord,
    RunTarget,
    Settings,
    SettingsChange,
    StaleTarget,
    SuspendReport,
    WidgetAction,
    WidgetAssetRef,
    WidgetResult,
)
from alkera_notebook.engine.runtime import Job
from alkera_notebook.engine.session import Session
from alkera_notebook.envs.models import EnvDescriptor, EnvRegistry
from alkera_notebook.events.models import AnyEvent, EnvStateEvent, FrameAttached
from alkera_notebook.events.queue import ClientQueue
from alkera_notebook.kernels.launcher import KernelLauncher, KernelTransport
from alkera_notebook.memory.guard import GuardKill, MemoryGuard
from alkera_notebook.memory.source import MemorySource
from alkera_notebook.sql.provider import SqlProviderRegistry
from alkera_notebook.tree_io import Tree
from alkera_notebook.widgets.assets import FileBlobStore, WidgetAssets


class RunHandle:
    """A run request's answer: ``info`` now, ``wait()`` for how it ended."""

    def __init__(self, info: RunInfo, job: Job) -> None:
        self.info = info
        self._job = job

    @property
    def run_id(self) -> str:
        return self.info.run_id

    async def wait(self, timeout_s: float | None = None) -> RunRecord:
        return await asyncio.wait_for(asyncio.shield(self._job.done), timeout_s)


class NotebookClient:
    """One attached actor's view of a notebook session."""

    def __init__(self, session: Session, actor: Actor) -> None:
        self._session = session
        self.actor = actor
        self.client_id = session.engine.ids.message_id().replace("msg_", "cli_", 1)
        self._queue: ClientQueue = session.subscribe(actor, self.client_id)

    @property
    def events(self) -> AsyncIterator[AnyEvent]:
        return self._queue.__aiter__()

    @property
    def queue(self) -> ClientQueue:
        return self._queue

    async def next_event(self, timeout_s: float | None = None) -> AnyEvent | None:
        return await asyncio.wait_for(self._queue.get(), timeout_s)

    async def read(self, query: ReadQuery | None = None) -> NotebookView:
        return await self._session.view(query or ReadQuery())

    async def apply(
        self, ops: list[NotebookOp], base_token: str | None, submit_id: str | None = None
    ) -> NotebookOpsResult:
        return await self._session.apply(self.actor, ops, base_token, submit_id)

    async def run(
        self,
        target: RunTarget,
        *,
        frontier: str | None = None,
        confirm_expensive: bool = False,
        plan_only: bool = False,
        run_id: str | None = None,
    ) -> RunHandle:
        """Queue a run. With ``plan_only`` nothing is queued: the handle's
        ``info`` carries the plan with each step's code (and SQL statement).
        ``run_id`` names the run when the caller recorded it first (the
        platform's run record); the engine mints one otherwise."""
        scope: Literal["cells", "all", "stale", "above", "below"]
        targets: list[str] = []
        trigger: Literal["run", "run_all", "run_stale"] = "run"
        if isinstance(target, CellsTarget):
            scope, targets = "cells", list(target.ids)
        elif isinstance(target, AllTarget):
            scope, trigger = "all", "run_all"
        elif isinstance(target, StaleTarget):
            scope, trigger = "stale", "run_stale"
        elif isinstance(target, AboveTarget | BelowTarget):
            scope, targets = target.kind, [target.id]
        else:  # pragma: no cover - the union is closed
            raise ValueError(f"unknown target {target!r}")
        info, job = await self._session.request_run(
            self.actor,
            scope=scope,
            targets=targets,
            trigger=trigger,
            frontier=frontier,
            confirm=confirm_expensive,
            plan_only=plan_only,
            run_id=run_id,
        )
        return RunHandle(info, job)

    async def kernel(
        self, action: Literal["status", "interrupt", "interrupt_all", "restart", "shutdown"]
    ) -> KernelInfo:
        s = self._session
        if action != "status" and not self.actor.can_run:
            raise ForbiddenError("you cannot control this notebook's kernel")
        if action in ("interrupt", "interrupt_all"):
            await s.interrupt(clear_queue=action == "interrupt_all", actor=self.actor)
        elif action == "restart":
            await s.restart(self.actor)
        elif action == "shutdown":
            await s.shutdown_kernel("shutdown", self.actor)
        return s.kernel_info()

    async def output(
        self, cell: str, part: OutputPart = "all", max_chars: int = 20000
    ) -> OutputDetail:
        return self._session.output_detail(cell, part, max_chars)

    async def clear_outputs(self, cell_ids: Sequence[str] | None = None) -> list[str]:
        """Clear the outputs of ``cell_ids`` (every cell when ``None``) for
        everyone; returns the cells that had outputs."""
        return self._session.clear_outputs(self.actor, cell_ids)

    async def inspect(self, query: InspectQuery) -> InspectResult:
        if query.what == "value" and not self.actor.can_run:
            raise ForbiddenError("inspecting a value runs code in the kernel")
        return await self._session.inspect(query)

    async def graph(
        self, cell: str | None = None, direction: Literal["both", "up", "down"] = "both"
    ) -> GraphView:
        return self._session.graph_view(cell, direction)

    async def widget(self, action: WidgetAction) -> WidgetResult:
        s = self._session
        if action.action in ("list", "get"):
            widgets = s.widget_list()
            if action.action == "get":
                widgets = [w for w in widgets if w.model_id == action.model_id]
                if not widgets:
                    raise NotFoundError(f"no widget {action.model_id}")
            return WidgetResult(widgets=widgets)
        if action.model_id is None or action.state is None:
            raise ValueError("set needs model_id and state")
        if s.widget_hub is None or s.widget_hub.model(action.model_id) is None:
            raise NotFoundError(f"no widget {action.model_id}")
        msg_id = s.engine.ids.message_id()
        job = await s.comm_send(
            self.actor,
            self.client_id,
            action.model_id,
            msg_id,
            {"data": {"method": "update", "state": dict(action.state), "buffer_paths": []}},
            [],
        )
        record = await job.done
        caused = None
        if record.reason and record.reason.startswith("run:"):
            caused_record = s.records.get(record.reason[4:])
            if caused_record is not None:
                caused = RunInfo(
                    run_id=caused_record.run_id,
                    status=caused_record.status,
                    trigger=caused_record.trigger,
                    plan=list(caused_record.plan),
                )
        return WidgetResult(widgets=s.widget_list(), run=caused)

    async def env(self, action: EnvAction) -> EnvResult:
        return await self._session.engine.env_action(self._session, self.actor, action)

    async def settings(self, change: SettingsChange) -> Settings:
        changes = change.model_dump(exclude_none=True)
        return await self._session.set_settings(self.actor, changes)

    async def comm_send(
        self,
        comm_id: str,
        msg_id: str,
        content: dict[str, Any],
        buffers: list[bytes],
        *,
        frame_id: str | None = None,
    ) -> None:
        """A widget message from one of this client's frames (``frame_id``;
        without one, the client's frame showing ``comm_id``, attached on
        demand). Authorized per message; delivered to the kernel as a run."""
        await self._session.comm_send(
            self.actor, self.client_id, comm_id, msg_id, content, buffers, frame_id
        )

    def attach_frame(
        self,
        frame_id: str | None = None,
        model_id: str | None = None,
        *,
        model_ids: Sequence[str] = (),
        output_id: str | None = None,
        readonly: bool = False,
    ) -> FrameAttached:
        """An output frame joins the widget hub: it shows ``model_id`` and
        ``model_ids``, or the widgets of output ``output_id``. Returns its
        ``frame_id`` (chosen by the engine when not given) and comm-open
        replays; later messages for it arrive as ``frame.message`` events
        carrying that ``frame_id``."""
        roots = ([model_id] if model_id else []) + list(model_ids)
        return self._session.attach_frame(
            self.client_id, frame_id, roots, output_id=output_id, readonly=readonly
        )

    def detach_frame(self, frame_id: str) -> None:
        frame = self._session.frames.get(frame_id)
        if frame is not None and frame.client_id == self.client_id:
            self._session.detach_frame(frame_id)

    async def envs(self) -> EnvListing:
        """The notebook's environment and every environment found for it."""
        return await self._session.engine.env_listing(self._session)

    async def env_packages(self, env_id: str) -> EnvPackages:
        """The packages installed in one of the listed environments."""
        return await self._session.engine.env_packages(self._session, env_id)

    async def widget_asset_resolve(self, module: str, version: str | None = None) -> WidgetAssetRef:
        """A widget module's entry code this notebook may load: a platform
        bundle or the ``index.js`` its kernel offered. A module's other files
        are fetched by hash with :meth:`widget_asset`. ``NotFoundError``
        otherwise."""
        return self._session.widget_asset_ref(module, version)

    async def widget_asset(self, sha256: str) -> bytes:
        """Widget code by hash: only hashes this notebook's kernel offered
        (or the engine moved out of its widgets' state) and platform
        bundles. ``NotFoundError`` otherwise."""
        return self._session.widget_asset_bytes(sha256)

    async def activity(self, since: datetime, exclude_actor: str | None = None) -> Activity:
        """What happened after ``since``, oldest first; ``exclude_actor`` leaves
        out one actor's own entries (an agent reading what others did)."""
        return self._session.activity(since, exclude_actor)

    async def detach(self) -> None:
        self._session.unsubscribe(self.client_id)


class NotebookSession:
    """The public handle on one open notebook."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def path(self) -> str:
        return self._session.path

    @property
    def runtime(self) -> Session:
        return self._session

    def attach(self, actor: Actor) -> NotebookClient:
        return NotebookClient(self._session, actor)

    async def reload_from_store(self) -> None:
        reload = getattr(self._session.engine.store, "reload", None)
        if reload is not None:
            notices = await reload(self._session.path)
            from alkera_notebook.document.store import DocumentChange

            stored = await self._session.engine.store.load(self._session.path)
            await self._session.refresh_doc(
                DocumentChange(
                    path=self._session.path,
                    token=stored.token,
                    actor_id=None,
                    notices=list(notices or []),
                    origin=(
                        "deleted"
                        if any(n.kind == "file_deleted" for n in notices or [])
                        else "external"
                    ),
                )
            )
        else:
            await self._session.refresh_doc()


class NotebookEngine:
    def __init__(
        self,
        config: EngineConfig,
        *,
        store: DocumentStore,
        launcher: KernelLauncher,
        transport: KernelTransport,
        memory: MemorySource,
        sql: SqlProviderRegistry,
        envs: EnvRegistry,
        clock: Clock,
        ids: IdSource,
        fmt: FormatApi | None = None,
        sql_broker: Any = None,
        widget_assets: Any = None,
    ) -> None:
        self.config = config
        #: The workspace's folder: the root everything the engine writes into
        #: the workspace goes through, so no link there is followed.
        self.workspace_tree = Tree(config.workspace_root)
        self.store = store
        self.launcher = launcher
        self.transport = transport
        self.memory = memory
        self.sql = sql
        self.envs = envs
        self.clock = clock
        self.ids = ids
        self.fmt = fmt or default_format()
        # The SQL broker serves every kernel's ``sql.execute``; built
        # on first use so an engine without Arrow installed still runs Python.
        self._sql_broker = sql_broker
        # The widget asset store, shared by every kernel: it keeps what kernels
        # offer from their environments. Large widget values move into it only
        # when the store was passed in; otherwise they stay inline in the
        # models' state.
        self.value_assets: WidgetAssets | None = widget_assets
        self.widget_assets: WidgetAssets = widget_assets or WidgetAssets(
            FileBlobStore(Path(config.data_root) / "widget-assets")
        )
        self._sessions: dict[str, Session] = {}
        self._open_lock = asyncio.Lock()
        self._closed = False
        self._last_oom_at: float | None = None
        self.guard_kills: list[GuardKill] = []
        self.guard = MemoryGuard(
            memory,
            config.memory,
            self._all_kernels,
            kill_all=self._kill_all_kernels,
            on_kill=self._on_guard_kill,
            on_warning=self._on_memory_warning,
            on_oom_event=self._on_oom_event,
        )
        self._guard_started = False

    # ------------------------------------------------------------------ sessions

    def _norm(self, path: str) -> str:
        p = PurePosixPath(path)
        if p.is_absolute():
            try:
                p = PurePosixPath(
                    Path(path).resolve().relative_to(Path(self.config.workspace_root).resolve())
                )
            except ValueError as exc:
                raise NotFoundError(f"{path} is outside the workspace") from exc
        if any(part == ".." for part in p.parts):
            raise NotFoundError(f"{path} is outside the workspace")
        return str(p)

    def _ensure_guard(self) -> None:
        if not self._guard_started:
            self._guard_started = True
            self.guard.start()

    async def open(self, path: str) -> NotebookSession:
        if self._closed:
            raise EngineClosedError("the engine is closed")
        rel = self._norm(path)
        async with self._open_lock:
            session = self._sessions.get(rel)
            if session is None:
                stored = await self.store.load(rel)
                session = Session(self, rel, stored)
                self._sessions[rel] = session
        self._ensure_guard()
        return NotebookSession(session)

    async def create(
        self, path: str, cells: list[InsertCell], settings: dict[str, Any], actor: Actor
    ) -> NotebookSession:
        if self._closed:
            raise EngineClosedError("the engine is closed")
        if not actor.can_edit:
            raise ForbiddenError("you cannot create notebooks here")
        rel = self._norm(path)
        create = getattr(self.store, "create", None)
        if create is None:
            raise NotImplementedError("this document store cannot create notebooks")
        await create(rel, cells, settings, actor)
        return await self.open(rel)

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions.values())

    async def suspend(self) -> SuspendReport:
        report = SuspendReport()
        for session in self.sessions:
            stopped, interrupted = await session.suspend()
            report.kernels_stopped += stopped
            report.runs_interrupted += interrupted
            report.snapshots_written.append(session.path)
        return report

    async def resume(self) -> None:
        for session in self.sessions:
            await session.resume()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for session in self.sessions:
            with contextlib.suppress(Exception):
                await session.close()
        await self.guard.stop()
        close = getattr(self.store, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------------ kernels

    def _all_kernels(self) -> list[Any]:
        return [
            s.kernel for s in self._sessions.values() if s.kernel is not None and s.kernel.alive
        ]

    def live_kernel_count(self) -> int:
        return len(self._all_kernels())

    def _kill_all_kernels(self) -> None:
        kill_all = getattr(self.launcher, "kill_all", None)
        if callable(kill_all):
            kill_all()

    def _on_guard_kill(self, kill: GuardKill) -> None:
        self.guard_kills.append(kill)

    def _on_memory_warning(self, usage: int, limit: int) -> None:
        from alkera_notebook.document.ops import CellNotice

        for s in self.sessions:
            s._notice(
                CellNotice(
                    kind="memory_warning",
                    message="Memory is nearly full",
                    data={"usage_bytes": usage, "threshold_bytes": limit},
                )
            )

    def _on_oom_event(self) -> None:
        self._last_oom_at = time.monotonic()

    def oom_recent(self, window_s: float = 2.0) -> bool:
        return self._last_oom_at is not None and time.monotonic() - self._last_oom_at < window_s

    def kernel_mount(self) -> str:
        if self.config.kernel_mount:
            return self.config.kernel_mount
        from alkera_notebook.kernels.launch_local import prepare_mount

        return str(prepare_mount(Path(self.config.data_root) / "mount"))

    def kernel_base_env(self, env: EnvDescriptor) -> dict[str, str]:
        base = (
            dict(self.config.base_env)
            if self.config.base_env
            else {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ")}
        )
        bin_dir = str(Path(env.interpreter).parent)
        base["PATH"] = bin_dir + os.pathsep + base.get("PATH", os.defpath)
        if env.prefix and env.kind in ("default", "uv_project", "script", "venv"):
            base["VIRTUAL_ENV"] = env.prefix
        return base

    def sql_broker(self) -> Any:
        """The SQL broker, or None when its dependencies are not installed."""
        if self._sql_broker is None:
            try:
                from alkera_notebook.sql.broker import SqlBroker
            except ImportError:
                return None
            self._sql_broker = SqlBroker(self.sql)
        return self._sql_broker

    def metered_connections(self) -> set[str]:
        """Connections whose queries cost money: configured, or reported by a
        provider exposing ``is_metered(name, workspace)``."""
        out = set(self.config.metered_connections)
        names: set[str] = set()
        for s in self._sessions.values():
            for c in s.doc.live_cells():
                conn = (c.meta or {}).get("connection")
                if isinstance(conn, str):
                    names.add(conn)
        for provider in self.sql:
            is_metered = getattr(provider, "is_metered", None)
            if callable(is_metered):
                out |= {n for n in names if is_metered(n)}
        return out

    # ------------------------------------------------------------------ environments

    def _say_newer_env(self) -> None:
        """Every notebook whose kernel a build just left on an older
        environment says a newer one is ready (environments are shared by
        the workspace's notebooks)."""
        for session in list(self._sessions.values()):
            session.say_if_env_outdated()

    def _env_info(self, session: Session, d: EnvDescriptor) -> EnvInfo:
        return session.env_info_of(d)

    async def env_listing(self, session: Session) -> EnvListing:
        nb = str(session.abs_path)
        current = await self.envs.resolve(nb, session.doc.setting("env"))
        session.note_selected_env(current)
        found = await self.envs.detect(nb)
        if all(d.env_id != current.env_id for d in found):
            found = [current, *found]
        return EnvListing(
            current=self._env_info(session, current),
            envs=[self._env_info(session, d) for d in found],
            shared=self.config.shared_envs,
        )

    async def env_packages(self, session: Session, env_id: str) -> EnvPackages:
        """Only an environment listed for this notebook can be asked about."""
        listing = await self.env_listing(session)
        if all(e.env_id != env_id for e in listing.envs):
            raise NotFoundError(f"no environment {env_id} for this notebook")
        pkgs = await self.envs.packages(env_id)
        return EnvPackages(
            env_id=env_id,
            packages=[PackageInfo(name=n, version=v) for n, v in pkgs],
            requirements=await self.envs.requirements(env_id),
        )

    async def env_action(self, session: Session, actor: Actor, action: EnvAction) -> EnvResult:
        nb = str(session.abs_path)
        current = await self.envs.resolve(nb, session.doc.setting("env"))
        session.note_selected_env(current)
        if action.action == "info":
            return EnvResult(env=self._env_info(session, current))
        if action.action == "list":
            found = await self.envs.detect(nb)
            return EnvResult(
                env=self._env_info(session, current),
                envs=[self._env_info(session, d) for d in found],
            )
        if action.action == "packages":
            pkgs = await self.envs.packages(current.env_id)
            return EnvResult(
                env=self._env_info(session, current),
                packages=[PackageInfo(name=n, version=v) for n, v in pkgs],
            )
        if not actor.can_run:
            raise ForbiddenError("changing the environment needs run rights")
        if action.action == "switch":
            if not action.env:
                raise ValueError("switch needs env")
            await session.set_settings(actor, {"env": action.env})
            new = await self.envs.resolve(nb, action.env)
            session.publish(EnvStateEvent(env=self._env_info(session, new)))
            return EnvResult(env=self._env_info(session, new))
        if action.action == "materialize":
            built = await self.envs.materialize(current.env_id, by=actor.label())
            session.publish(EnvStateEvent(env=self._env_info(session, built)))
            session.record_activity(actor, "env_change", change="materialize")
            self._say_newer_env()
            return EnvResult(env=self._env_info(session, built))
        if action.action == "cancel":
            await self.envs.cancel(current.env_id)
            session.record_activity(actor, "env_change", change="cancel")
            after = await self.envs.resolve(nb, session.doc.setting("env"))
            return EnvResult(env=self._env_info(session, after))
        if not action.packages:
            raise ValueError(f"{action.action} needs packages")
        if action.action not in ("install", "remove"):
            raise ValueError(f"unknown environment action {action.action!r}")
        if current.kind == "script":
            return await self._change_script(session, actor, action)
        change = self.envs.install if action.action == "install" else self.envs.remove
        try:
            desc, changed, log = await change(current.env_id, action.packages, nb)
        finally:
            # Whatever the outcome, the panel reads the environment as it is.
            after = await self.envs.resolve(nb, session.doc.setting("env"))
            session.publish(EnvStateEvent(env=self._env_info(session, after)))
        session.record_activity(actor, "env_change", change=action.action)
        self._say_newer_env()
        return EnvResult(env=self._env_info(session, desc), spec_changed=changed, log=log)

    async def _change_script(self, session: Session, actor: Actor, action: EnvAction) -> EnvResult:
        """Install into or remove from the notebook's own PEP 723 block: an
        edit of the notebook (so it needs edit rights) that is undone when
        the build after it fails."""
        from alkera_notebook.document.ops import SetSetting
        from alkera_notebook.envs import add_script_dependencies, remove_script_dependencies

        if not actor.can_edit:
            raise ForbiddenError(
                "This notebook lists its own packages, so changing them needs edit rights."
            )
        nb = str(session.abs_path)
        before = session.doc.meta.header
        rewrite = (
            add_script_dependencies if action.action == "install" else remove_script_dependencies
        )
        header = rewrite(before, action.packages)
        await session.apply(
            actor, [SetSetting(key="header", value=header)], None, None, record=False
        )
        try:
            updated = await self.envs.resolve(nb, session.doc.setting("env"))
            built = await self.envs.materialize(updated.env_id, by=actor.label())
        except BaseException:
            await session.apply(
                actor, [SetSetting(key="header", value=before)], None, None, record=False
            )
            after = await self.envs.resolve(nb, session.doc.setting("env"))
            session.publish(EnvStateEvent(env=self._env_info(session, after)))
            raise
        session.publish(EnvStateEvent(env=self._env_info(session, built)))
        session.record_activity(actor, "env_change", change=action.action)
        self._say_newer_env()
        return EnvResult(env=self._env_info(session, built), spec_changed=[session.path])


__all__ = [
    "KernelUnavailableError",
    "NotebookClient",
    "NotebookEngine",
    "NotebookSession",
    "RunHandle",
]
