"""The kernel: one main thread running steps from a FIFO, a reader thread
owning the socket, an inspection thread, the output pump and the interrupt
watcher.

Nothing here imports a third-party library. Values from the person's
libraries are recognised through ``sys.modules`` only.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import importlib.machinery
import importlib.util
import inspect
import linecache
import os
import platform
import queue
import sys
import threading
import time
import traceback
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import _frames as f
from . import display, hooks, inspection
from .connection import Connection
from .interrupt import InterruptController
from .streams import FdCapture, FdTextStream, OutputRouter, Target
from .throttle import ReplaceThrottle

KERNEL_DIR = os.path.dirname(os.path.abspath(__file__))
CELL_OUTPUT_LIMIT = 8 * 1024 * 1024
REPLACE_INTERVAL_S = 0.1
INPUT_MESSAGE = "Notebooks do not support input(); use alkera.ui.text"
#: The module every notebook can name without importing it: Markdown and SQL
#: cells compile to calls into it. Bound as a builtin, so a cell may still
#: bind the name itself, and clearing that cell's names shows the module
#: again. The format's ``RUNTIME_MODULE`` names the same module; a vector
#: test holds the two equal.
RUNTIME_MODULE = "alkera"


def _import_outside(name: str, folder: str) -> types.ModuleType:
    """The top-level module ``name``, found anywhere on the import path but
    ``folder``. A module of that name already imported is the one returned."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    there = os.path.realpath(folder)
    path = [entry for entry in sys.path if os.path.realpath(entry or os.curdir) != there]
    spec = importlib.machinery.PathFinder.find_spec(name, path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _LazyRuntimeModule(types.ModuleType):
    """The builtin a notebook names the runtime module by until it is first
    used. Reading any attribute imports the real module, hands the builtin
    over to it and answers from it; an environment without the module says
    so (``ModuleNotFoundError``) at that read.

    The module is never taken from the notebook's own folder. Nothing in the
    notebook asked for this import (a SQL or Markdown cell only calls into
    the name), so a file that happens to be called the same beside the
    notebook must not be what runs. A cell that writes ``import`` itself gets
    Python's usual rules."""

    def __init__(self, name: str, notebook_dir: str) -> None:
        super().__init__(name)
        self.__dict__["_notebook_dir"] = notebook_dir

    def __getattr__(self, name: str) -> Any:
        module = _import_outside(self.__name__, self.__dict__["_notebook_dir"])
        if getattr(builtins, self.__name__, None) is self:
            setattr(builtins, self.__name__, module)
        return getattr(module, name)


#: Libraries whose versions ``hello`` reports (never imported to find out).
LIBS = (
    "pyarrow",
    "polars",
    "pandas",
    "numpy",
    "duckdb",
    "matplotlib",
    "ipywidgets",
    "anywidget",
    "plotly",
    "altair",
    "IPython",
)


def _lib_versions() -> dict[str, str | None]:
    from importlib import metadata

    out: dict[str, str | None] = {}
    for name in LIBS:
        dist = {"IPython": "ipython"}.get(name, name)
        try:
            out[name] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[name] = None
        except Exception:
            out[name] = None
    return out


def _codecs(libs: dict[str, str | None]) -> list[str]:
    codecs = [f.CODEC_JSON, f.CODEC_BYTES, f.CODEC_ROWS]
    if libs.get("pyarrow") or libs.get("polars"):
        codecs += [f.CODEC_ARROW_STREAM, f.CODEC_ARROW_FILE]
    return codecs


class StopCell(Exception):  # noqa: N818 - the name is the user-facing outcome
    """Raised by ``alkera.stop``; ends the cell as ``stopped``."""

    _alkera_stop = True


@dataclass
class CellOutputs:
    """Per-cell output accounting: the rich-output cap and the replace
    rate limit."""

    run_id: str
    cell_id: str
    used: int = 0
    throttle: ReplaceThrottle[display.Bundle] = field(
        default_factory=lambda: ReplaceThrottle(REPLACE_INTERVAL_S)
    )
    timer: threading.Timer | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Kernel:
    def __init__(self, conn: Connection, *, kernel_id: str, notebook_dir: str) -> None:
        self.conn = conn
        self.kernel_id = kernel_id
        self.notebook_dir = notebook_dir
        self.work: queue.Queue[Callable[[], None]] = queue.Queue()
        self.interrupts = InterruptController()
        self.router = OutputRouter(self._send_stream)
        self.capture = FdCapture(self.router)
        self.loop = asyncio.new_event_loop()
        self.module = types.ModuleType("__main__")
        self.module.__dict__["__builtins__"] = builtins
        self.globals: dict[str, Any] = self.module.__dict__
        self.settings: dict[str, Any] = {}
        self.data_dir: str | None = None
        self.args: dict[str, Any] = {}
        self.current: Target | None = None
        self.parent_msg_id: str | None = None
        self.cell_outputs: dict[str, CellOutputs] = {}
        #: The frames each cell is showing that no global names, by the handle
        #: their table output carries, so the whole frame can be paged for as
        #: long as the output stands (until the cell runs again).
        self.shown_frames: dict[str, dict[str, object]] = {}
        self._thread_owner: dict[int, Target] = {}
        self._finished_cells: set[tuple[str | None, str | None]] = set()
        self._step_tasks: set[asyncio.Task[Any]] = set()
        self.reactive: list[Any] = []
        self._duckdb_default: _DuckdbDefaultConnection | None = None
        self.inspector = inspection.Inspector(self)
        self.output_capture: Callable[[str, Any], bool] | None = None
        self.autoreload: Any = None
        #: Set in a forked child: it has no connection to the engine.
        self._in_child = False

    # ------------------------------------------------------------------ start

    def hello(self, token: str) -> dict[str, Any]:
        libs = _lib_versions()
        gil = getattr(sys, "_is_gil_enabled", None)
        from . import __version__

        params = {
            "token": token,
            "client": {"name": "alkera-kernel", "version": __version__, "protocols": [f.PROTOCOL]},
            "runtime": {
                "version": platform.python_version(),
                "implementation": sys.implementation.name,
                "platform": f"{sys.platform}-{platform.machine()}",
                "free_threaded": bool(gil is not None and not gil()),
            },
            "codecs": _codecs(libs),
            "libs": libs,
        }
        self.conn.send(f.request(0, f.HELLO_METHOD, params))
        reply = self.conn.read_one(f.HELLO_TIMEOUT_S * 5)
        if not isinstance(reply, f.Response) or reply.id != 0:
            raise ConnectionError("the service did not answer hello")
        if reply.error is not None:
            raise ConnectionError(f"hello refused: {reply.error.message}")
        result = f.decode_value(reply.result, reply.segments)
        if not isinstance(result, dict):
            raise ConnectionError("hello result is not an object")
        self.settings = dict(result.get("settings") or {})
        # The launcher spells the data directory as this process sees it (a
        # sandbox sees the engine's paths elsewhere); the engine's own
        # spelling is the fallback for a launch that names none.
        self.data_dir = os.environ.get("ALKERA_DATA_DIR") or result.get("data_dir")
        self.args = dict(self.settings.get("args") or {})
        limits = result.get("limits") or {}
        if isinstance(limits.get("max_inflight"), int):
            self.conn.set_max_inflight(limits["max_inflight"])
        return result

    def install(self) -> None:
        """Process-wide setup before any user code runs."""
        sys.modules["__main__"] = self.module
        os.environ.setdefault("MPLBACKEND", "Agg")
        self.capture.start()
        sys.stdout = FdTextStream(1, "stdout", self._stream_owner, self.router, self._divert_stream)
        sys.stderr = FdTextStream(2, "stderr", self._stream_owner, self.router, self._divert_stream)
        self.interrupts.on_cancel_tasks = self._cancel_step_tasks
        builtins.display = self.display_values  # type: ignore[attr-defined]
        builtins.input = _no_input
        sys.breakpointhook = self._breakpoint
        self._patch_getpass()
        self._patch_threads()
        hooks.install()
        hooks.after_import("matplotlib.pyplot", self._patch_pyplot)
        hooks.after_import("getpass", lambda m: self._patch_getpass())
        hooks.after_import("duckdb", self._patch_duckdb)
        from . import comms, host

        comms.install(self)
        host.install(self)
        self._bind_runtime_module()
        if self.settings.get("autoreload") == "on":
            from .autoreload import Autoreloader

            self.autoreload = Autoreloader(self.settings.get("workspace_root") or self.notebook_dir)
        os.register_at_fork(after_in_child=self._after_fork_in_child)
        self.conn.on_request = self._on_request
        self.conn.on_notification = self._on_notification
        self.conn.on_close = self._on_close

    def _bind_runtime_module(self) -> None:
        """Make :data:`RUNTIME_MODULE` a builtin. The binding is a stand-in
        that imports the module the first time a cell reads something off
        it, so a kernel whose notebook never names it imports nothing beyond
        the standard library."""
        setattr(builtins, RUNTIME_MODULE, _LazyRuntimeModule(RUNTIME_MODULE, self.notebook_dir))

    def serve(self) -> None:
        """The main thread's loop: one work item at a time, FIFO."""
        self.conn.start_reader()
        while True:
            item = self.work.get()
            try:
                item()
            except KeyboardInterrupt:
                pass  # an interrupt that landed between steps
            except SystemExit:
                raise
            except BaseException:
                traceback.print_exc(file=sys.__stderr__)

    # ------------------------------------------------------------------ reader side

    def _on_close(self, reason: str) -> None:
        # The engine is gone: the kernel has nothing left to serve.
        os._exit(0)

    def _on_notification(self, message: f.Notification) -> None:
        pass

    def _on_request(self, req: f.Request) -> None:
        try:
            params = f.decode_params(req.params, req.segments, allow_files=True)
        except f.TagError as exc:
            self.conn.respond_error(req.id, f.RpcError.invalid_params(str(exc)))
            return
        method = req.method
        try:
            if method == f.RUN_EXECUTE:
                run = _parse_run(params)
                self.work.put(lambda: self._execute_run(run))
                self.conn.respond(req.id, {"accepted": True})
            elif method == f.RUN_INTERRUPT:
                run_id = params.get("run_id")
                if not isinstance(run_id, str):
                    raise f.RpcError.invalid_params("run_id is required")
                self.interrupts.note_hint(run_id)
                self.conn.respond(req.id, {"noted": True})
            elif method == f.NAMES_DELETE:
                names = params.get("names")
                if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                    raise f.RpcError.invalid_params("names must be a list of strings")
                self.work.put(lambda: self._delete_names(req.id, names))
            elif method == f.COMM_DELIVER:
                from . import comms

                deliver = comms.parse_delivery(params)
                self.work.put(lambda: comms.deliver(self, deliver))
                self.conn.respond(req.id, {})
            elif method in (f.INSPECT_FRAME, f.INSPECT_VALUE, f.COMPLETE):
                self.inspector.submit(req.id, method, params)
            elif method == f.KERNEL_SHUTDOWN:
                self.conn.respond(req.id, {})
                self._shutdown()
            else:
                raise f.RpcError.method_not_found(method)
        except f.RpcError as exc:
            self.conn.respond_error(req.id, exc)

    def _shutdown(self) -> None:
        # Leave in two seconds even if a step never returns; the launcher
        # kills the process group in any case.
        timer = threading.Timer(2.0, lambda: os._exit(0))
        timer.daemon = True
        timer.start()
        self.work.put(lambda: os._exit(0))

    # ------------------------------------------------------------------ runs

    def _execute_run(self, run: dict[str, Any]) -> None:
        run_id = run["run_id"]
        self.interrupts.begin_run(run_id)
        status = f.STATUS_OK
        try:
            reloaded: list[str] = []
            if self.autoreload is not None:
                reloaded = self.autoreload.reload_changed()
            self.notify(f.EVENT_RUN_STARTED, {"run_id": run_id, "reloaded": reloaded})
            for name in run["clear"]:
                self.globals.pop(name, None)
            for step in run["steps"]:
                status = self._run_step(run_id, step)
                if status != f.STATUS_OK:
                    break
        except KeyboardInterrupt:
            status = f.STATUS_INTERRUPTED
        finally:
            if self.autoreload is not None:
                # Modules first imported by this run start being tracked now.
                self.autoreload.snapshot(only_new=True)
            self.interrupts.end_run(run_id)
            self.notify(f.EVENT_RUN_FINISHED, {"run_id": run_id, "status": status})

    def _run_step(self, run_id: str, step: dict[str, Any]) -> str:
        cell_id = step["cell_id"]
        target = Target(run_id, cell_id)
        self.capture.drain()
        self.current = target
        self.router.target = target
        self.parent_msg_id = f"{run_id}:{cell_id}"
        self.cell_outputs[cell_id] = CellOutputs(run_id, cell_id)
        self.shown_frames.pop(cell_id, None)
        self.notify(f.EVENT_CELL_STARTED, {"run_id": run_id, "cell_id": cell_id})
        started = time.monotonic()
        error: dict[str, Any] | None = None
        status = f.STATUS_OK
        try:
            try:
                self._execute_code(step)
            except BaseException as exc:
                if self.interrupts.pending(run_id):
                    status = f.STATUS_INTERRUPTED
                    error = _interrupt_payload(exc)
                elif getattr(type(exc), "_alkera_stop", False):
                    status = f.STATUS_STOPPED
                elif isinstance(exc, KeyboardInterrupt):
                    status = f.STATUS_INTERRUPTED
                    error = _interrupt_payload(exc)
                else:
                    status = f.STATUS_ERROR
                    error = _error_payload(exc, exc.__traceback__)
                    if isinstance(exc, ModuleNotFoundError) and exc.name:
                        self.notify(
                            f.EVENT_MODULE_MISSING,
                            {
                                "run_id": run_id,
                                "cell_id": cell_id,
                                "module": exc.name.partition(".")[0],
                            },
                        )
                if status == f.STATUS_INTERRUPTED:
                    self._cancel_step_tasks()
        except KeyboardInterrupt:
            # A second signal while handling the first: still interrupted.
            status = f.STATUS_INTERRUPTED
            error = error or _interrupt_payload(None)
        self._finish_step(target, step, status, error, started)
        return status

    def _execute_code(self, step: dict[str, Any]) -> None:
        filename = step.get("filename") or f"<cell {step['cell_id']}>"
        offset = int(step.get("line_offset") or 0)
        body = step.get("body") or ""
        last = step.get("last_expr") or ""
        _seed_linecache(filename, offset, step.get("source"), body, last)
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        if body.strip():
            tree = ast.parse(body, filename, "exec")
            ast.increment_lineno(tree, offset)
            code = compile(tree, filename, "exec", flags=flags, dont_inherit=True)
            self._run_code(code)
        if last.strip():
            # The expression arrives already on the cell's own lines (it is
            # preceded by blank lines), so only the cell's offset applies.
            expr = ast.parse(last, filename, "eval")
            ast.increment_lineno(expr, offset)
            code = compile(expr, filename, "eval", flags=flags, dont_inherit=True)
            value = self._run_code(code)
            if value is not None:
                self.show(value, name=_global_name(expr, value, self.globals))

    def _run_code(self, code: types.CodeType) -> Any:
        if code.co_flags & inspect.CO_COROUTINE:
            coro = eval(code, self.globals)  # noqa: S307 - this is the notebook's code
            task = self.loop.create_task(coro)
            self._step_tasks.add(task)
            try:
                return self.loop.run_until_complete(task)
            finally:
                self._step_tasks.discard(task)
        return eval(code, self.globals)  # noqa: S307 - this is the notebook's code

    def _cancel_step_tasks(self) -> None:
        """Cancel the step's asyncio tasks (from any thread)."""
        tasks = (
            [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            if not self.loop.is_closed()
            else []
        )
        for task in tasks:
            self.loop.call_soon_threadsafe(task.cancel)
        if (
            threading.current_thread() is threading.main_thread()
            and not self.loop.is_running()
            and tasks
        ):
            with contextlib.suppress(BaseException):
                self.loop.run_until_complete(asyncio.wait(tasks, timeout=1.0))

    def _finish_step(
        self,
        target: Target,
        step: dict[str, Any],
        status: str,
        error: dict[str, Any] | None,
        started: float,
    ) -> None:
        outputs = self.cell_outputs.get(target.cell_id or "")
        if outputs is not None:
            self._flush_replace(outputs)
        self.capture.drain()
        self.router.close_target(target)
        self._finished_cells.add((target.run_id, target.cell_id))
        self.current = None
        self.router.target = Target(None, None)
        defs = [n for n in step.get("defs") or [] if n in self.globals]
        params: dict[str, Any] = {
            "run_id": target.run_id,
            "cell_id": target.cell_id,
            "status": status,
            "defs": defs,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if error is not None:
            params["error"] = error
        self.notify(f.EVENT_CELL_FINISHED, params)
        self.notify(
            f.EVENT_CELL_VARIABLES,
            {
                "run_id": target.run_id,
                "cell_id": target.cell_id,
                "variables": [self.inspector.summary(n, self.globals[n]) for n in defs],
            },
        )
        bindings = self._bindings()
        if bindings:
            self.notify(
                f.EVENT_UI_BINDINGS,
                {"run_id": target.run_id, "cell_id": target.cell_id, "bindings": bindings},
            )

    def _bindings(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for obj in self.reactive:
            model_id = getattr(obj, "model_id", None) or getattr(obj, "_model_id", None)
            if not isinstance(model_id, str):
                continue
            names = [n for n, v in self.globals.items() if v is obj and not n.startswith("__")]
            if names:
                out[model_id] = sorted(names)
        return out

    def _delete_names(self, rid: int, names: list[str]) -> None:
        for name in names:
            self.globals.pop(name, None)
        self.conn.respond(rid, {})

    # ------------------------------------------------------------------ outputs

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.conn.closed or self._in_child:
            return
        try:
            self.conn.notify(method, params)
        except f.FrameTooLargeError:
            pass

    def _send_stream(self, target: Target, name: str, text: str) -> None:
        if target.thread or (target.run_id, target.cell_id) in self._finished_cells:
            self.notify(
                f.EVENT_THREAD_OUTPUT, {"cell_id": target.cell_id, "name": name, "text": text}
            )
        else:
            self.notify(
                f.EVENT_CELL_STREAM,
                {"run_id": target.run_id, "cell_id": target.cell_id, "name": name, "text": text},
            )

    def _stream_owner(self) -> Target | None:
        """For Python-level writes: the cell a background thread belongs to
        once that cell has finished (``thread.output``); otherwise None, and
        the text goes through fd 1 or 2 to whatever runs now."""
        owner = self._thread_owner.get(threading.get_ident())
        if owner is None:
            return None
        if (owner.run_id, owner.cell_id) in self._finished_cells:
            return Target(owner.run_id, owner.cell_id, thread=True)
        return None

    def _divert_stream(self, name: str, text: str) -> bool:
        return self.output_capture is not None and self.output_capture("stream", (name, text))

    def display_values(self, *objs: Any, **_: Any) -> None:
        """``display()`` in a notebook: append each value to the current output."""
        for obj in objs:
            self.show(obj)

    def show(self, obj: object, mode: str = "append", *, name: str | None = None) -> None:
        """Display ``obj`` in the current cell. Every way a value reaches a
        cell's output (its last expression, ``display``, ``alkera.sql``, a
        host's ``display``) comes through here."""
        self.output(self.bundle_for(obj, name=name), mode)

    def bundle_for(self, obj: object, *, name: str | None = None) -> display.Bundle:
        """The MIME bundle ``obj`` is shown as. A frame's table names where
        its rows can be paged from: the global ``name`` when the caller knows
        one, else a handle this kernel keeps the frame under."""
        bundle = display.format_value(obj, name=name)
        table = bundle.get(display.TABLE_MIME)
        if name is None and isinstance(table, dict) and display.pageable(obj):
            handle = self._remember_shown(obj)
            if handle is not None:
                bundle[display.TABLE_MIME] = {**table, "source": {"name": handle}}
        return bundle

    def _remember_shown(self, obj: object) -> str | None:
        target = self.current
        if target is None or target.cell_id is None:
            target = self._thread_owner.get(threading.get_ident())
        if target is None or target.cell_id is None:
            return None
        held = self.shown_frames.setdefault(target.cell_id, {})
        for handle, kept in held.items():
            if kept is obj:
                return handle
        handle = f"{display.SHOWN_PREFIX}{target.cell_id}/{len(held)}"
        held[handle] = obj
        return handle

    def shown_frame(self, handle: str) -> object:
        """The frame a table output's handle names; ``KeyError`` once the
        cell that showed it ran again."""
        cell_id = handle[len(display.SHOWN_PREFIX) :].rpartition("/")[0]
        return self.shown_frames[cell_id][handle]

    def output(self, bundle: display.Bundle, mode: str) -> None:
        if self.output_capture is not None and self.output_capture("display", bundle):
            return
        target = self.current
        if target is None or target.cell_id is None:
            owner = self._thread_owner.get(threading.get_ident())
            if owner is None:
                return
            target = owner
        outputs = self.cell_outputs.setdefault(
            target.cell_id or "", CellOutputs(target.run_id or "", target.cell_id or "")
        )
        size = display.bundle_size(bundle)
        with outputs.lock:
            if mode == "replace":
                outputs.used = size
            elif outputs.used + size > CELL_OUTPUT_LIMIT:
                bundle = display.placeholder(bundle, CELL_OUTPUT_LIMIT)
            else:
                outputs.used += size
            if mode == "replace" and size > CELL_OUTPUT_LIMIT:
                bundle = display.placeholder(bundle, CELL_OUTPUT_LIMIT)
            if mode == "replace":
                now_value, delay = outputs.throttle.offer(bundle)
                if now_value is None:
                    self._arm(outputs, delay)
                    return
        self.capture.drain(timeout=0.1)
        self._send_output(outputs, bundle, mode)

    def _arm(self, outputs: CellOutputs, delay: float | None) -> None:
        """Wake up after ``delay`` to send a waiting replace (lock held)."""
        if delay is None or outputs.timer is not None:
            return
        outputs.timer = threading.Timer(delay, self._wake, (outputs,))
        outputs.timer.daemon = True
        outputs.timer.start()

    def _wake(self, outputs: CellOutputs) -> None:
        with outputs.lock:
            outputs.timer = None
            bundle, delay = outputs.throttle.due()
            self._arm(outputs, delay)
        if bundle is not None:
            self._send_output(outputs, bundle, "replace")

    def _flush_replace(self, outputs: CellOutputs) -> None:
        with outputs.lock:
            if outputs.timer is not None:
                outputs.timer.cancel()
                outputs.timer = None
            bundle = outputs.throttle.flush()
        if bundle is not None:
            self._send_output(outputs, bundle, "replace")

    def _send_output(self, outputs: CellOutputs, bundle: display.Bundle, mode: str) -> None:
        self.notify(
            f.EVENT_CELL_OUTPUT,
            {"run_id": outputs.run_id, "cell_id": outputs.cell_id, "output": bundle, "mode": mode},
        )

    def clear_output(self) -> None:
        self.output({}, "replace")

    # ------------------------------------------------------------------ patches

    def _patch_threads(self) -> None:
        kernel = self
        original = threading.Thread.start

        def start(thread: threading.Thread) -> None:
            owner = kernel.current or kernel._thread_owner.get(threading.get_ident())
            original(thread)
            if owner is not None and thread.ident is not None:
                kernel._thread_owner[thread.ident] = owner

        setattr(threading.Thread, "start", start)  # noqa: B010 - patched on the class

    def _patch_pyplot(self, plt: types.ModuleType) -> None:
        kernel = self

        def show(*args: Any, **kwargs: Any) -> None:
            svg = kernel.settings.get("figure_format") == "svg"
            for num in plt.get_fignums():
                kernel.output(display.figure_bundle(plt.figure(num), svg=svg), "append")

        setattr(plt, "show", show)  # noqa: B010 - replaces pyplot's own show

    def _patch_duckdb(self, duckdb: types.ModuleType) -> None:
        """Every DuckDB connection the person opens is interruptible: a query
        runs in C with the GIL released and never looks at Python signals,
        so the interrupt watcher calls ``interrupt()`` on it."""
        interrupts = self.interrupts
        original = duckdb.connect

        def connect(*args: Any, **kwargs: Any) -> Any:
            con = original(*args, **kwargs)
            interrupts.register(con)
            return con

        setattr(duckdb, "connect", connect)  # noqa: B010 - wraps the module function
        # Registered objects are held weakly, and the wrapper DuckDB hands
        # out for its default connection dies as soon as it is dropped, so
        # the kernel holds the stand-in itself.
        self._duckdb_default = _DuckdbDefaultConnection(duckdb)
        interrupts.register(self._duckdb_default)

    def _patch_getpass(self) -> None:
        gp = sys.modules.get("getpass")
        if gp is not None:
            setattr(gp, "getpass", _no_input)  # noqa: B010 - no terminal to read from

    def _breakpoint(self, *args: Any, **kwargs: Any) -> None:
        print("breakpoint() is not supported in notebooks; continuing.", file=sys.stderr)

    def _after_fork_in_child(self) -> None:
        self.conn.close_in_child()
        # fds 1 and 2 stay the parent's pipes, so a child's output is still
        # captured and attributed by the parent's pump; only the Python-level
        # wrappers (which know about cells and threads) are replaced.
        sys.stdout = _plain_stream(1)
        sys.stderr = _plain_stream(2)
        self.interrupts.reset_in_child()

        self._in_child = True


class _DuckdbDefaultConnection:
    """Interrupts the connection DuckDB's module functions (``duckdb.sql``,
    ``duckdb.execute``) run on, looked up when the interrupt is said, so a
    default the person replaced is the one interrupted. Without it a query
    stopped there by DuckDB's own signal check leaves its workers running,
    and the connection's next query waits for them forever."""

    def __init__(self, duckdb: types.ModuleType) -> None:
        self._duckdb = duckdb

    def interrupt(self) -> None:
        default = getattr(self._duckdb, "default_connection", None)
        con = default() if callable(default) else default
        if con is not None:
            con.interrupt()


def _plain_stream(fd: int) -> Any:
    import io

    return io.TextIOWrapper(
        io.FileIO(fd, "w", closefd=False), encoding="utf-8", line_buffering=True
    )


def _no_input(*_: Any, **__: Any) -> str:
    raise EOFError(INPUT_MESSAGE)


def _parse_run(params: dict[str, Any]) -> dict[str, Any]:
    run_id = params.get("run_id")
    steps = params.get("steps")
    clear = params.get("clear", [])
    if not isinstance(run_id, str) or not isinstance(steps, list):
        raise f.RpcError.invalid_params("run.execute needs run_id and steps")
    if not isinstance(clear, list) or not all(isinstance(n, str) for n in clear):
        raise f.RpcError.invalid_params("clear must be a list of names")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("cell_id"), str):
            raise f.RpcError.invalid_params("each step needs a cell_id")
        for key in ("body", "last_expr", "filename", "source"):
            if step.get(key) is not None and not isinstance(step[key], str):
                raise f.RpcError.invalid_params(f"step {key} must be text")
    return {"run_id": run_id, "steps": steps, "clear": clear, "trigger": params.get("trigger")}


def _global_name(expr: ast.Expression, value: object, namespace: dict[str, Any]) -> str | None:
    """The global a cell's last expression displays, when it is a bare name
    still bound to the displayed value."""
    node = expr.body
    if isinstance(node, ast.Name) and namespace.get(node.id, _MISSING) is value:
        return node.id
    return None


_MISSING = object()


def _seed_linecache(filename: str, offset: int, source: str | None, body: str, last: str) -> None:
    """Tracebacks quote the cell as written: its ``source`` when the step
    carries it, else the body with the last expression laid over the lines
    it occupies (the expression is already padded onto the cell's lines)."""
    if source is not None:
        cell = source.splitlines(keepends=True)
    else:
        cell = body.splitlines(keepends=True)
        for index, line in enumerate(last.splitlines(keepends=True)):
            if not line.strip():
                continue
            while len(cell) <= index:
                cell.append("\n")
            cell[index] = line
    lines = ["\n"] * offset + [ln if ln.endswith("\n") else ln + "\n" for ln in cell]
    linecache.cache[filename] = (len("".join(lines)), None, lines, filename)


def _interrupt_payload(exc: BaseException | None) -> dict[str, Any]:
    """An interrupted step's error, named ``KeyboardInterrupt``. The
    traceback reads as Python prints one: the ``Traceback`` header, the
    cell's frames where the interrupt landed, then the ``KeyboardInterrupt``
    line; it is empty when the interrupt landed outside the cell's frames,
    and never says what another exception the interrupt caused said."""
    tb = exc.__traceback__ if isinstance(exc, KeyboardInterrupt) else None
    # The kernel's own frames (the step runner above, the signal handler that
    # raised below) are not the person's code.
    summary = [
        frame
        for frame in traceback.extract_tb(tb)
        if os.path.dirname(os.path.abspath(frame.filename)) != KERNEL_DIR
    ]
    frames = traceback.format_list(summary)
    if not frames:
        return {"ename": "KeyboardInterrupt", "evalue": "", "traceback": []}
    lines = ["Traceback (most recent call last):\n", *frames, "KeyboardInterrupt\n"]
    return {"ename": "KeyboardInterrupt", "evalue": "", "traceback": lines}


def _error_payload(exc: BaseException, tb: types.TracebackType | None) -> dict[str, Any]:
    # Drop the kernel's own frames from the top of the traceback.
    while (
        tb is not None
        and os.path.dirname(os.path.abspath(tb.tb_frame.f_code.co_filename)) == KERNEL_DIR
    ):
        tb = tb.tb_next
    lines = traceback.format_exception(type(exc), exc, tb)
    return {"ename": type(exc).__name__, "evalue": str(exc), "traceback": lines}
