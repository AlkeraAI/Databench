"""Jupyter comms over the RPC, and ``ipywidgets.Output`` capture.

When the person's code imports ``comm`` (ipywidgets 8 and anywidget do), the
kernel installs itself as the provider: ``comm.create_comm`` builds comms
whose messages go to the engine as ``comm.open|msg|close`` notifications,
each tagged with the frontend message it answers. Messages from the frontend
arrive as ``comm.deliver`` and run on the main thread as a run; ``comm.idle``
reports when one has been handled.

``Output`` capture: ipywidgets' ``Output.__enter__`` records the current
parent message id (read from ``comm.kernel.get_parent()``); while a live
``Output`` model holds the current parent id, displays and prints are
appended to that model's ``outputs`` instead of the cell.
"""

from __future__ import annotations

import sys
import types
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import _frames as f
from . import hooks
from .widget_assets import WidgetAssetOffers

if TYPE_CHECKING:
    from types import ModuleType

    from .runtime import Kernel


class _ParentKernel:
    """The small slice of an ipykernel that ipywidgets reads."""

    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def get_parent(self, channel: str = "shell") -> dict[str, Any]:
        msg_id = self._kernel.parent_msg_id
        return {"header": {"msg_id": msg_id}} if msg_id else {}


class CommRegistry:
    def __init__(self, kernel: Kernel) -> None:
        self.kernel = kernel
        self.comms: dict[str, Any] = {}
        self.targets: dict[str, Any] = {}
        #: Output model comm id -> the parent msg id it captures.
        self.capturing: dict[str, str] = {}
        #: Comms whose values are secrets (passwords, ``_sensitive`` models).
        self.sensitive: set[str] = set()
        self.parent_kernel = _ParentKernel(kernel)
        self.assets = WidgetAssetOffers(kernel.notify)

    # comm.get_comm_manager() surface
    def register_target(self, target_name: str, f_: Any) -> None:
        self.targets[target_name] = f_

    def unregister_target(self, target_name: str, f_: Any) -> None:
        self.targets.pop(target_name, None)

    def register_comm(self, comm: Any) -> str:
        self.comms[comm.comm_id] = comm
        return str(comm.comm_id)

    def unregister_comm(self, comm: Any) -> None:
        self.comms.pop(comm.comm_id, None)
        self.capturing.pop(comm.comm_id, None)

    def get_comm(self, comm_id: str) -> Any:
        return self.comms.get(comm_id)

    # sending
    def open_comm(
        self,
        target_name: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        on_msg: Callable[[dict[str, Any]], None],
    ) -> HostComm:
        """A comm opened by the public API (``alkera.ui``) without the
        ``comm`` package: frontend messages reach ``on_msg``."""
        comm = HostComm(self, uuid.uuid4().hex, on_msg)
        self.register_comm(comm)
        content: dict[str, Any] = {
            "comm_id": comm.comm_id,
            "target_name": target_name,
            "data": dict(data),
        }
        self.publish(comm.comm_id, "comm_open", content, None, metadata)
        return comm

    def publish(
        self,
        comm_id: str,
        msg_type: str,
        data: Any,
        buffers: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        segments = [f.Segment(bytes(b)) for b in (buffers or [])]
        if msg_type in ("comm_open", "comm_msg") and isinstance(data, dict):
            self._track_output_model(comm_id, data)
        content = dict(data) if isinstance(data, dict) else {"data": data}
        if msg_type == "comm_open":
            # The library's JavaScript reaches the engine before the model
            # that needs it, so a frame never asks for a module not offered.
            inner = content.get("data")
            self.assets.offer_for_state(inner.get("state") if isinstance(inner, dict) else None)
        event = {
            "comm_open": f.EVENT_COMM_OPEN,
            "comm_msg": f.EVENT_COMM_MSG,
            "comm_close": f.EVENT_COMM_CLOSE,
        }[msg_type]
        params: dict[str, Any] = {
            "comm_id": comm_id,
            "content": content,
            "parent_msg_id": self.kernel.parent_msg_id,
        }
        if segments:
            params["buffers"] = segments
        if metadata:
            params["metadata"] = dict(metadata)
        if self._is_sensitive(comm_id, msg_type, content):
            # The engine sends this model's values only to the frontend that
            # set them and shows "<redacted>" everywhere else.
            params["sensitive"] = True
        self.kernel.notify(event, params)

    def _is_sensitive(self, comm_id: str, msg_type: str, content: dict[str, Any]) -> bool:
        if comm_id in self.sensitive:
            return True
        if msg_type != "comm_open":
            return False
        data = content.get("data")
        state = data.get("state") if isinstance(data, dict) else None
        widget = _widget_for(comm_id) or _opening_widget()
        flagged = (
            isinstance(state, dict)
            and (state.get("_model_name") == "PasswordModel" or state.get("_sensitive") is True)
        ) or bool(getattr(widget, "_sensitive", False))
        if flagged:
            self.sensitive.add(comm_id)
        return flagged

    def _track_output_model(self, comm_id: str, data: dict[str, Any]) -> None:
        inner = data.get("data", data)
        state = inner.get("state") if isinstance(inner, dict) else None
        if not isinstance(state, dict) or "msg_id" not in state:
            return
        msg_id = state.get("msg_id")
        if msg_id:
            self.capturing[comm_id] = str(msg_id)
        else:
            self.capturing.pop(comm_id, None)

    # capture
    def capture(self, kind: str, payload: Any) -> bool:
        """Route a display bundle or stream text into the ``Output`` model
        capturing the current parent; False when none is."""
        parent = self.kernel.parent_msg_id
        if not parent or not self.capturing:
            return False
        comm_id = next((c for c, m in reversed(list(self.capturing.items())) if m == parent), None)
        if comm_id is None:
            return False
        widget = _widget_for(comm_id)
        if widget is None:
            return False
        outputs = tuple(widget.outputs)
        if kind == "clear":
            widget.outputs = ()
            return True
        if kind == "display":
            widget.outputs = (
                *outputs,
                {"output_type": "display_data", "data": payload, "metadata": {}},
            )
            return True
        name, text = payload
        last = outputs[-1] if outputs else None
        if (
            isinstance(last, dict)
            and last.get("output_type") == "stream"
            and last.get("name") == name
        ):
            merged = {**last, "text": last.get("text", "") + text}
            widget.outputs = (*outputs[:-1], merged)
        else:
            widget.outputs = (*outputs, {"output_type": "stream", "name": name, "text": text})
        return True


class HostComm:
    """The comm ``RuntimeHost.open_comm`` returns."""

    def __init__(
        self, registry: CommRegistry, comm_id: str, on_msg: Callable[[dict[str, Any]], None]
    ) -> None:
        self._registry = registry
        self.comm_id = comm_id
        self._on_msg = on_msg
        self._closed = False

    def send(self, data: dict[str, Any], buffers: list[bytes] | None = None) -> None:
        if self._closed:
            return
        self._registry.publish(
            self.comm_id, "comm_msg", {"comm_id": self.comm_id, "data": dict(data)}, buffers
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._registry.publish(
            self.comm_id, "comm_close", {"comm_id": self.comm_id, "data": {}}, None
        )
        self._registry.unregister_comm(self)

    # what deliver() calls
    def handle_msg(self, msg: dict[str, Any]) -> None:
        self._on_msg(msg)

    def handle_close(self, msg: dict[str, Any]) -> None:
        self._closed = True


def _opening_widget() -> Any:
    """The widget whose ``open()`` is publishing a ``comm_open`` right now:
    ipywidgets opens the comm from inside ``Widget.open`` before it registers
    the instance, so the registry cannot name it yet."""
    frame: types.FrameType | None = sys._getframe(1)
    for _ in range(12):
        if frame is None:
            return None
        candidate = frame.f_locals.get("self")
        if (
            candidate is not None
            and hasattr(type(candidate), "_model_name")
            and hasattr(candidate, "open")
        ):
            return candidate
        frame = frame.f_back
    return None


def _widget_for(comm_id: str) -> Any:
    for module_name, attr in (
        ("ipywidgets.widgets.widget", "_instances"),
        ("ipywidgets", "Widget"),
    ):
        mod = sys.modules.get(module_name)
        if mod is None:
            continue
        registry = getattr(mod, attr, None)
        if isinstance(registry, type):
            registry = getattr(registry, "widgets", None) or getattr(
                registry, "_active_widgets", None
            )
        if isinstance(registry, dict) and comm_id in registry:
            return registry[comm_id]
    return None


def _install_provider(kernel: Kernel, comm_mod: ModuleType) -> None:
    # comm/__init__.py imports its base_comm submodule; the kernel never
    # imports the person's comm package itself.
    base_comm: Any = sys.modules[comm_mod.__name__ + ".base_comm"].BaseComm

    registry: CommRegistry = kernel.comms  # type: ignore[attr-defined]

    class AlkeraComm(base_comm):  # type: ignore[misc]
        kernel = registry.parent_kernel

        def publish_msg(
            self,
            msg_type: str,
            data: Any = None,
            metadata: Any = None,
            buffers: Any = None,
            **keys: Any,
        ) -> None:
            # Jupyter comm content: {comm_id, data, [target_name, target_module]}.
            content: dict[str, Any] = {"comm_id": self.comm_id, "data": dict(data or {})}
            content.update({k: v for k, v in keys.items() if k in ("target_name", "target_module")})
            # The message's metadata goes with it: a widget's comm open names
            # the widget protocol version there, and a frontend's manager
            # refuses an open without one.
            registry.publish(
                self.comm_id,
                msg_type,
                content,
                buffers,
                dict(metadata) if isinstance(metadata, dict) else None,
            )

    class Manager:
        def __getattr__(self, name: str) -> Any:
            return getattr(registry, name)

    manager = Manager()

    def create_comm(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("comm_id", uuid.uuid4().hex)
        comm = AlkeraComm(*args, **kwargs)
        registry.register_comm(comm)
        return comm

    comm_mod.create_comm = create_comm  # type: ignore[attr-defined]
    comm_mod.get_comm_manager = lambda: manager  # type: ignore[attr-defined]


def install(kernel: Kernel) -> None:
    registry = CommRegistry(kernel)
    kernel.comms = registry  # type: ignore[attr-defined]
    kernel.output_capture = registry.capture
    hooks.after_import("comm", lambda mod: _install_provider(kernel, mod))
    hooks.after_import("IPython.core.interactiveshell", lambda mod: _install_shell(kernel, mod))


def _install_shell(kernel: Kernel, mod: ModuleType) -> None:
    """A minimal ``get_ipython()`` shell: ``IPython.display.display`` and
    ``clear_output`` route to the current cell (or a capturing ``Output``),
    and ``get_ipython().kernel.get_parent()`` names the current parent."""

    registry: CommRegistry = kernel.comms  # type: ignore[attr-defined]
    base: Any = mod.InteractiveShell

    class Formatter:
        def format(
            self, obj: Any, include: Any = None, exclude: Any = None
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            return kernel.bundle_for(obj), {}

    class Publisher:
        def publish(
            self,
            data: Any,
            metadata: Any = None,
            source: Any = None,
            *,
            transient: Any = None,
            update: bool = False,
            **_: Any,
        ) -> None:
            kernel.output(dict(data), "replace" if update else "append")

        def clear_output(self, wait: bool = False) -> None:
            if not registry.capture("clear", wait):
                kernel.clear_output()

    class Events:
        """IPython's event hooks: accepted and never fired (no cell hooks)."""

        def register(self, event: str, function: Any) -> None:
            pass

        def unregister(self, event: str, function: Any) -> None:
            pass

        def trigger(self, event: str, *args: Any, **kwargs: Any) -> None:
            pass

    formatter, publisher, events = Formatter(), Publisher(), Events()

    class AlkeraShell(base):  # type: ignore[misc]
        display_formatter = property(lambda self: formatter)
        display_pub = property(lambda self: publisher)
        kernel = property(lambda self: registry.parent_kernel)
        events = property(lambda self: events)
        user_ns = property(lambda self: kernel.globals)

    shell = AlkeraShell.__new__(AlkeraShell)
    base._instance = shell
    AlkeraShell._instance = shell


# --------------------------------------------------------------------------- deliveries


def parse_delivery(params: dict[str, Any]) -> dict[str, Any]:
    run_id, msg_id, msg = params.get("run_id"), params.get("msg_id"), params.get("msg")
    if not isinstance(run_id, str) or not isinstance(msg_id, str) or not isinstance(msg, dict):
        raise f.RpcError.invalid_params("comm.deliver needs run_id, msg_id and msg")
    if msg.get("msg_type") not in ("comm_open", "comm_msg", "comm_close"):
        raise f.RpcError.invalid_params("msg_type must be comm_open, comm_msg or comm_close")
    content = msg.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("comm_id"), str):
        raise f.RpcError.invalid_params("content needs a comm_id")
    buffers = [b.data for b in params.get("buffers", []) if isinstance(b, f.Segment)]
    return {"run_id": run_id, "msg_id": msg_id, "msg": msg, "buffers": buffers}


def deliver(kernel: Kernel, delivery: dict[str, Any]) -> None:
    """Handle one frontend comm message on the main thread, as a run."""
    from .streams import Target

    registry: CommRegistry = kernel.comms  # type: ignore[attr-defined]
    run_id, msg_id = delivery["run_id"], delivery["msg_id"]
    msg = delivery["msg"]
    content = msg["content"]
    comm_id = content["comm_id"]
    target = Target(run_id, f"comm:{comm_id}")
    kernel.interrupts.begin_run(run_id)
    kernel.notify(f.EVENT_RUN_STARTED, {"run_id": run_id, "reloaded": []})
    previous_parent = kernel.parent_msg_id
    kernel.parent_msg_id = msg_id
    kernel.current = target
    kernel.router.target = target
    status = f.STATUS_OK
    try:
        full = {
            "header": {"msg_id": msg_id, "msg_type": msg["msg_type"]},
            "parent_header": {},
            "metadata": {},
            "content": content,
            "buffers": delivery["buffers"],
        }
        comm = registry.get_comm(comm_id)
        if msg["msg_type"] == "comm_open":
            factory = registry.targets.get(content.get("target_name", ""))
            if factory is not None and "comm" in sys.modules:
                created = sys.modules["comm"].create_comm(
                    comm_id=comm_id, primary=False, target_name=content.get("target_name", "")
                )
                factory(created, full)
        elif comm is not None and msg["msg_type"] == "comm_msg":
            comm.handle_msg(full)
        elif comm is not None:
            comm.handle_close(full)
            registry.unregister_comm(comm)
    except KeyboardInterrupt:
        status = f.STATUS_INTERRUPTED
    except BaseException as exc:
        status = f.STATUS_ERROR
        import traceback

        print(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), file=sys.stderr
        )
    finally:
        kernel.capture.drain()
        kernel.router.close_target(target)
        kernel.current = None
        kernel.router.target = Target(None, None)
        kernel.parent_msg_id = previous_parent
        kernel.interrupts.end_run(run_id)
        kernel.notify(f.EVENT_COMM_IDLE, {"msg_id": msg_id})
        kernel.notify(f.EVENT_RUN_FINISHED, {"run_id": run_id, "status": status})
