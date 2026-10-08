"""Host selection: where ``alkera`` is running, and how output, interrupts,
widgets and service calls reach it.

- ``RuntimeHost``: inside the Alkera kernel. The kernel never imports
  ``alkera`` (the person's environment may pin any version, or none); it
  publishes one object as ``sys.modules["_alkera_runtime"].host`` speaking
  host protocol 1, and this module adopts it.
- ``MarimoHost``: stock marimo is running a notebook. Output goes through
  ``marimo.output``, ``alkera.stop`` raises marimo's own stop, and each
  ``alkera.ui`` element is shown as the matching ``marimo.ui`` control.
- ``JupyterHost``: an IPython shell is active. Output goes through
  ``IPython.display``.
- ``ScriptHost``: plain Python. Output is printed as text.

Nothing here imports a third-party library: marimo and IPython are used
only when they are already in ``sys.modules``.
"""

from __future__ import annotations

import base64
import sys
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Dict, Protocol, cast, runtime_checkable

from .errors import ServiceUnavailable, StopCell

__all__ = [
    "PROTOCOL_VERSION",
    "ElementHostV1",
    "HostV1",
    "JupyterHost",
    "MarimoHost",
    "RuntimeHost",
    "ScriptHost",
    "current",
    "format_value",
    "use",
]

PROTOCOL_VERSION = 1
RUNTIME_MODULE = "_alkera_runtime"
#: Set on a stop exception class that takes the output to show as its one
#: argument, instead of the output being shown before it is raised.
CARRIES_OUTPUT = "_alkera_stop_carries_output"

Bundle = Dict[str, Any]


@runtime_checkable
class HostV1(Protocol):
    """Host protocol 1: what every host offers ``alkera``.

    The kernel's runtime object implements the same methods, so a kernel
    and an ``alkera`` release agree on the number, not on a shared class."""

    protocol_version: int
    name: str

    def display(self, obj: Any) -> None:
        """Append ``obj`` to the current cell's output."""

    def replace(self, obj: Any) -> None:
        """Make ``obj`` the current cell's whole output."""

    def clear(self) -> None:
        """Clear the current cell's output."""

    def format(self, obj: Any) -> Bundle:
        """The MIME bundle for ``obj`` (always with ``text/plain``)."""
        ...

    def register_interruptible(self, obj: Any) -> None:
        """Call ``obj.interrupt()`` when the running cell is interrupted."""

    def unregister_interruptible(self, obj: Any) -> None:
        """Undo ``register_interruptible``."""

    def register_reactive(self, obj: Any) -> None:
        """Rerun the cells that read ``obj`` when its value changes."""

    def run_context(self) -> dict[str, Any] | None:
        """``{run_id, cell_id}`` of the active run, or None."""
        ...

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        """A request to the Alkera service, stamped with the active run."""
        ...

    def args(self) -> dict[str, Any]:
        """The arguments the notebook was run with."""
        ...

    def settings(self) -> dict[str, Any]:
        """What the service told the kernel at start (``args``,
        ``dataframe``, ...). Empty outside the kernel."""
        ...

    @property
    def data_dir(self) -> str | None:
        """A directory the service shares with the kernel, if any."""
        ...

    def stop_exception(self) -> type[BaseException]:
        """The exception class that ends a cell as stopped. It is raised
        with no arguments, or with the output to show when the class has a
        true ``_alkera_stop_carries_output`` attribute."""
        ...


class Comm(Protocol):
    """A Jupyter comm opened from the kernel."""

    comm_id: str

    def send(self, data: dict[str, Any], buffers: list[bytes] | None = None) -> None:
        """A ``comm_msg`` to the frontend."""

    def close(self) -> None:
        """A ``comm_close`` to the frontend."""


@runtime_checkable
class CommHostV1(HostV1, Protocol):
    """A host that can open Jupyter comms (the Alkera kernel). Others do not
    have ``open_comm``; callers check with ``getattr(host, "open_comm",
    None)`` and keep a plain value instead."""

    def open_comm(
        self,
        target_name: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        on_msg: Callable[[dict[str, Any]], None],
    ) -> Comm:
        """Open a comm; ``on_msg`` receives each frontend message
        (``{"header": {"msg_id"}, "content": {"comm_id", "data"}, "buffers"}``)
        on the main thread, as a run."""
        ...


@runtime_checkable
class ElementHostV1(HostV1, Protocol):
    """A host that shows ``alkera.ui`` elements as its own controls (stock
    marimo). Others do not have ``adopt_ui_element``; ``alkera.ui`` checks
    with ``getattr`` and keeps the element it built."""

    def adopt_ui_element(self, element: Any) -> Any | None:
        """The object to give the person in place of the ``alkera.ui``
        ``element`` just built, or None to give them the element."""
        ...


# --------------------------------------------------------------------------- formatting


_REPR_METHODS = (
    ("_repr_html_", "text/html"),
    ("_repr_markdown_", "text/markdown"),
    ("_repr_svg_", "image/svg+xml"),
    ("_repr_png_", "image/png"),
    ("_repr_jpeg_", "image/jpeg"),
    ("_repr_json_", "application/json"),
    ("_repr_latex_", "text/latex"),
)
_BINARY = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _encoded(mime: str, data: Any) -> Any:
    if mime in _BINARY and isinstance(data, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(data)).decode("ascii")
    return data


def format_value(obj: Any) -> Bundle:
    """A standard-library formatter in the IPython display protocol's order:
    ``_repr_mimebundle_``, ``_mime_``, then the ``_repr_*_`` methods, then
    ``repr``. Every bundle carries ``text/plain``."""
    bundle: Bundle = {}
    if not isinstance(obj, type):
        method = getattr(obj, "_repr_mimebundle_", None)
        if callable(method):
            result = method(include=None, exclude=None)
            if isinstance(result, tuple) and len(result) == 2:
                result = result[0]
            if isinstance(result, dict):
                bundle = {str(k): _encoded(str(k), v) for k, v in result.items() if v is not None}
        if not bundle:
            mime = getattr(obj, "_mime_", None)
            if callable(mime):
                result = mime()
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
                    bundle = {result[0]: _encoded(result[0], result[1])}
        if not bundle:
            for name, mimetype in _REPR_METHODS:
                fn = getattr(obj, name, None)
                if not callable(fn):
                    continue
                try:
                    value = fn()
                except NotImplementedError:
                    continue
                if isinstance(value, tuple) and len(value) == 2:
                    value = value[0]
                if value is not None:
                    bundle[mimetype] = _encoded(mimetype, value)
    if "text/plain" not in bundle:
        bundle["text/plain"] = obj if isinstance(obj, str) else repr(obj)
    return bundle


def bundle_text(bundle: Bundle) -> str:
    """What a terminal shows for a bundle: Markdown when there is some,
    else plain text."""
    for mime in ("text/markdown", "text/plain"):
        value = bundle.get(mime)
        if isinstance(value, str):
            return value
    return ""


# --------------------------------------------------------------------------- hosts


class _NoService:
    """``call`` everywhere the Alkera service is not."""

    name = "script"

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        raise ServiceUnavailable(
            f"alkera.call({method!r}) needs the Alkera notebook kernel; no Alkera service "
            f"is reachable from {self.name}"
        )

    def run_context(self) -> dict[str, Any] | None:
        return None

    @property
    def data_dir(self) -> str | None:
        return None


class _Interruptibles:
    """Objects registered for interrupts outside the kernel, held weakly
    like the kernel holds them. Nothing signals them here (Ctrl-C raises
    ``KeyboardInterrupt`` as usual); the registry keeps the call valid
    everywhere."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._weak: weakref.WeakSet[Any] = weakref.WeakSet()
        self._strong: list[Any] = []

    @property
    def interruptibles(self) -> list[Any]:
        with self._lock:
            return [*self._weak, *self._strong]

    def register_interruptible(self, obj: Any) -> None:
        with self._lock:
            try:
                self._weak.add(obj)
            except TypeError:
                if not any(o is obj for o in self._strong):
                    self._strong.append(obj)

    def unregister_interruptible(self, obj: Any) -> None:
        with self._lock:
            try:
                self._weak.discard(obj)
            except TypeError:
                pass
            self._strong = [o for o in self._strong if o is not obj]

    def register_reactive(self, obj: Any) -> None:
        return None


class ScriptHost(_NoService, _Interruptibles):
    """Plain Python: output is printed as text (Markdown when the object
    has some, else its plain text)."""

    protocol_version = PROTOCOL_VERSION
    name = "script"

    def __init__(self) -> None:
        _Interruptibles.__init__(self)

    def _print(self, obj: Any) -> None:
        text = bundle_text(self.format(obj))
        stream = sys.stdout
        stream.write(text if text.endswith("\n") else text + "\n")
        stream.flush()

    def display(self, obj: Any) -> None:
        self._print(obj)

    def replace(self, obj: Any) -> None:
        # A terminal cannot take back what it printed.
        self._print(obj)

    def clear(self) -> None:
        return None

    def format(self, obj: Any) -> Bundle:
        return format_value(obj)

    def args(self) -> dict[str, Any]:
        return {}

    def settings(self) -> dict[str, Any]:
        return {}

    def stop_exception(self) -> type[BaseException]:
        return StopCell


class JupyterHost(ScriptHost):
    """An IPython shell (Jupyter, or IPython in a terminal): output goes
    through ``IPython.display``, which renders the objects' MIME bundles."""

    name = "jupyter"

    def __init__(self, display_module: Any) -> None:
        super().__init__()
        self._display = display_module

    def display(self, obj: Any) -> None:
        self._display.display(obj)

    def replace(self, obj: Any) -> None:
        self._display.clear_output(wait=True)
        self._display.display(obj)

    def clear(self) -> None:
        self._display.clear_output()


class MarimoHost(_NoService, _Interruptibles):
    """Stock marimo is running a notebook: output goes through
    ``marimo.output`` (which renders ``alkera`` objects through ``_mime_``),
    ``alkera.stop`` raises marimo's stop so the cell ends as marimo's
    ``mo.stop`` would, and each ``alkera.ui`` element becomes the matching
    ``marimo.ui`` control (``alkera._marimo_ui``), so marimo renders it and
    re-runs the cells that read it."""

    protocol_version = PROTOCOL_VERSION
    name = "marimo"

    def __init__(self, marimo: Any) -> None:
        _Interruptibles.__init__(self)
        self._mo = marimo
        self._stop: type[BaseException] | None = None
        self._peers: dict[type, type] = {}

    def adopt_ui_element(self, element: Any) -> Any | None:
        from ._marimo_ui import adopt

        return adopt(self._mo, element, self._peers)

    def display(self, obj: Any) -> None:
        self._mo.output.append(obj)

    def replace(self, obj: Any) -> None:
        self._mo.output.replace(obj)

    def clear(self) -> None:
        self._mo.output.clear()

    def format(self, obj: Any) -> Bundle:
        bundle = format_value(obj)
        if "text/html" not in bundle and not any(k.startswith("image/") for k in bundle):
            as_html = getattr(self._mo, "as_html", None)
            if callable(as_html):
                try:
                    text = getattr(as_html(obj), "text", None)
                except Exception:
                    text = None
                if isinstance(text, str):
                    bundle["text/html"] = text
        return bundle

    def args(self) -> dict[str, Any]:
        cli_args = getattr(self._mo, "cli_args", None)
        if not callable(cli_args):
            return {}
        try:
            return dict(cli_args())
        except Exception:
            return {}

    def settings(self) -> dict[str, Any]:
        return {}

    def stop_exception(self) -> type[BaseException]:
        if self._stop is None:
            base = getattr(self._mo, "MarimoStopError", None)
            if not (isinstance(base, type) and issubclass(base, BaseException)):
                return StopCell

            def __init__(self: BaseException, output: object | None = None) -> None:  # noqa: N807 - a class built at run time
                base.__init__(self, output)

            # marimo shows a stopped cell's output only when the stop carries
            # it (an output appended before the stop reads as a failure), so
            # this one takes it, as mo.stop does.
            self._stop = type(
                "AlkeraMarimoStop",
                (base,),
                {"__init__": __init__, "_alkera_stop": True, CARRIES_OUTPUT: True},
            )
        return self._stop


class RuntimeHost:
    """The Alkera kernel's runtime object, adopted as host protocol 1.

    Each call goes to the kernel object; a method that object does not have
    behaves as in plain Python, so a kernel and an ``alkera`` release that
    differ by a method still work together. Attributes the adapter does not
    define (``open_comm``, ``codecs``, ...) are the kernel object's own, and
    absent when it has none."""

    protocol_version = PROTOCOL_VERSION
    name = "runtime"

    def __init__(self, runtime: Any) -> None:
        if not callable(getattr(runtime, "display", None)):
            raise TypeError("the kernel's host has no display()")
        self._runtime = runtime
        self._plain = ScriptHost()

    @property
    def runtime(self) -> Any:
        return self._runtime

    def _method(self, name: str) -> Callable[..., Any]:
        fn = getattr(self._runtime, name, None)
        return fn if callable(fn) else cast("Callable[..., Any]", getattr(self._plain, name))

    def __getattr__(self, name: str) -> Any:
        # Only reached for names this class does not define.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return getattr(self._runtime, name)
        except AttributeError:
            raise AttributeError(f"the kernel's host has no {name}") from None

    def display(self, obj: Any) -> None:
        self._runtime.display(obj)

    def replace(self, obj: Any) -> None:
        self._method("replace")(obj)

    def clear(self) -> None:
        self._method("clear")()

    def format(self, obj: Any) -> Bundle:
        result = self._method("format")(obj)
        return dict(result) if isinstance(result, dict) else format_value(obj)

    def register_interruptible(self, obj: Any) -> None:
        self._method("register_interruptible")(obj)

    def unregister_interruptible(self, obj: Any) -> None:
        self._method("unregister_interruptible")(obj)

    def register_reactive(self, obj: Any) -> None:
        self._method("register_reactive")(obj)

    def run_context(self) -> dict[str, Any] | None:
        context = self._method("run_context")()
        return dict(context) if isinstance(context, dict) else None

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        return self._method("call")(method, params, timeout=timeout)

    def settings(self) -> dict[str, Any]:
        settings = self._method("settings")()
        return dict(settings) if isinstance(settings, dict) else {}

    def args(self) -> dict[str, Any]:
        settings = self.settings()
        if isinstance(settings.get("args"), dict):
            return dict(settings["args"])
        args = self._method("args")()
        return dict(args) if isinstance(args, dict) else {}

    @property
    def data_dir(self) -> str | None:
        value = getattr(self._runtime, "data_dir", None)
        return value if isinstance(value, str) else None

    def stop_exception(self) -> type[BaseException]:
        exc = self._method("stop_exception")()
        if isinstance(exc, type) and issubclass(exc, BaseException):
            return exc
        return StopCell


# --------------------------------------------------------------------------- selection

_lock = threading.Lock()
_overrides: list[HostV1] = []
_script = ScriptHost()
_adopted: tuple[Any, RuntimeHost] | None = None
_marimo: tuple[Any, MarimoHost] | None = None
_jupyter: tuple[Any, JupyterHost] | None = None


def _runtime_host() -> RuntimeHost | None:
    global _adopted
    module = sys.modules.get(RUNTIME_MODULE)
    runtime = getattr(module, "host", None) if module is not None else None
    if runtime is None or getattr(runtime, "protocol_version", None) != PROTOCOL_VERSION:
        return None
    adopted = _adopted
    if adopted is not None and adopted[0] is runtime:
        return adopted[1]
    try:
        host = RuntimeHost(runtime)
    except TypeError:
        return None
    _adopted = (runtime, host)
    return host


def _marimo_running(marimo: Any) -> bool:
    """True when marimo has a runtime context: a notebook is running (edit,
    run, export or as an app), not merely ``import marimo``."""
    for name in ("marimo._runtime.context.types", "marimo._runtime.context"):
        module = sys.modules.get(name)
        get_context = getattr(module, "get_context", None) if module is not None else None
        if callable(get_context):
            try:
                get_context()
            except Exception:
                return False
            return True
    running = getattr(marimo, "running_in_notebook", None)
    return bool(callable(running) and running())


def _marimo_host() -> MarimoHost | None:
    global _marimo
    marimo = sys.modules.get("marimo")
    if marimo is None or not _marimo_running(marimo):
        return None
    cached = _marimo
    if cached is not None and cached[0] is marimo:
        return cached[1]
    host = MarimoHost(marimo)
    _marimo = (marimo, host)
    return host


def _jupyter_host() -> JupyterHost | None:
    global _jupyter
    ipython = sys.modules.get("IPython")
    display_module = sys.modules.get("IPython.display") or sys.modules.get(
        "IPython.core.display_functions"
    )
    get_ipython = getattr(ipython, "get_ipython", None) if ipython is not None else None
    if display_module is None or not callable(get_ipython) or get_ipython() is None:
        return None
    if not callable(getattr(display_module, "display", None)):
        return None
    cached = _jupyter
    if cached is not None and cached[0] is display_module:
        return cached[1]
    host = JupyterHost(display_module)
    _jupyter = (display_module, host)
    return host


def current() -> HostV1:
    """The host ``alkera`` is running under right now."""
    if _overrides:
        return _overrides[-1]
    return _runtime_host() or _marimo_host() or _jupyter_host() or _script


@contextmanager
def use(host: HostV1) -> Iterator[HostV1]:
    """Run a block under ``host`` (tests, or embedding ``alkera`` in another
    runtime)."""
    with _lock:
        _overrides.append(host)
    try:
        yield host
    finally:
        with _lock:
            for index in range(len(_overrides) - 1, -1, -1):
                if _overrides[index] is host:
                    del _overrides[index]
                    break
