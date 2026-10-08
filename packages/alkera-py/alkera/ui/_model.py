"""A Jupyter widget model (protocol version 2) on the host's comms, with no
ipywidgets: what every ``alkera.ui`` element is underneath.

The host opens the comm (``host.open_comm``) and hands back an object with
``comm_id``, ``send(data, buffers)`` and ``close()``; it calls the model's
``handle_msg`` with each frontend message. Outside a host the element still
works as a value holder (a script), and renders as text.

A host may show elements as its own controls instead: when the current host
has ``adopt_ui_element`` (stock marimo does), each element is handed to it
once built, and what the host returns is what the constructor gives the
person. Such a stand-in holds the element and passes ``isinstance`` checks
against its class.
"""

from __future__ import annotations

import json
import threading
import typing
from typing import Any, ClassVar, TypeVar, cast

from alkera.ui._runtime import host

PROTOCOL_VERSION = "2.1.0"
MODEL_MODULE = "@alkera/ui-widgets"
MODEL_MODULE_VERSION = "1.0.0"
WIDGET_VIEW_MIME = "application/vnd.jupyter.widget-view+json"
TARGET_NAME = "jupyter.widget"

ChangeCallback = typing.Callable[[Any], None]

_E = TypeVar("_E")


class _ElementType(type):
    """Offers each element to the current host once it is built."""

    def __call__(cls: type[_E], *args: Any, **kwargs: Any) -> _E:
        element: _E = type.__call__(cls, *args, **kwargs)
        current = host()
        adopt = getattr(current, "adopt_ui_element", None) if current is not None else None
        if callable(adopt):
            shown = adopt(element)
            if shown is not None:
                # The host's stand-in speaks the element's API and passes
                # isinstance against its class (__instancecheck__ below).
                return cast("_E", shown)
        return element

    def __instancecheck__(cls, instance: Any) -> bool:
        if type.__instancecheck__(cls, instance):
            return True
        # A host's stand-in (see adopt_ui_element) counts as its element.
        if getattr(type(instance), "_alkera_peer", False) is not True:
            return False
        return type.__instancecheck__(cls, getattr(instance, "_alkera_element", None))


class WidgetModel(metaclass=_ElementType):
    """State synchronised with frontends. Subclasses name their view and
    convert values between Python and the wire."""

    _view_name: ClassVar[str] = ""
    _sensitive: bool = False

    def __init__(self, state: dict[str, Any]) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "_model_name": "ElementModel",
            "_model_module": MODEL_MODULE,
            "_model_module_version": MODEL_MODULE_VERSION,
            "_view_name": self._view_name,
            "_view_module": MODEL_MODULE,
            "_view_module_version": MODEL_MODULE_VERSION,
            "_dom_classes": [],
            "layout": None,
            **state,
        }
        if self._sensitive:
            self._state["_sensitive"] = True
        self._callbacks: list[ChangeCallback] = []
        self._comm: Any = None
        current = host()
        opener = getattr(current, "open_comm", None) if current is not None else None
        if callable(opener):
            self._comm = opener(
                TARGET_NAME,
                {"state": dict(self._state), "buffer_paths": []},
                {"version": PROTOCOL_VERSION},
                self.handle_msg,
            )
            reactive = getattr(current, "register_reactive", None)
            if callable(reactive):
                reactive(self)

    # ---------------------------------------------------------------- identity

    @property
    def model_id(self) -> str | None:
        """The comm id frontends know this element by (``None`` outside a host)."""
        return str(self._comm.comm_id) if self._comm is not None else None

    # ---------------------------------------------------------------- values

    def _to_wire(self, value: Any) -> Any:
        return value

    def _from_wire(self, wire: Any) -> Any:
        return wire

    def _coerce_wire(self, wire: Any) -> Any:
        """What the element accepts of a frontend's value (clamping, ...)."""
        return wire

    @property
    def value(self) -> Any:
        with self._lock:
            return self._from_wire(self._state.get("value"))

    @value.setter
    def value(self, new: Any) -> None:
        wire = self._coerce_wire(self._to_wire(new))
        self._set({"value": wire}, method="update")

    def on_change(self, callback: ChangeCallback) -> ChangeCallback:
        """Calls ``callback(value)`` after each change made in a frontend."""
        self._callbacks.append(callback)
        return callback

    def _set(self, delta: dict[str, Any], *, method: str) -> None:
        with self._lock:
            self._state.update(delta)
        if self._comm is not None:
            self._comm.send({"method": method, "state": delta, "buffer_paths": []}, [])

    # ---------------------------------------------------------------- frontend

    def handle_msg(self, msg: dict[str, Any]) -> None:
        """A frontend message (the host delivers it on the main thread, as a run)."""
        data = (msg.get("content") or {}).get("data") or {}
        method = data.get("method")
        if method == "update":
            state = data.get("state") or {}
            if "value" not in state:
                return
            accepted = self._coerce_wire(state["value"])
            with self._lock:
                self._state["value"] = accepted
            # Every frontend, the sender included, hears the value the kernel
            # kept (the engine's cache is fed only from here).
            if self._comm is not None:
                self._comm.send(
                    {"method": "echo_update", "state": {"value": accepted}, "buffer_paths": []}, []
                )
            current = self.value
            for callback in list(self._callbacks):
                callback(current)
        elif method == "request_state" and self._comm is not None:
            with self._lock:
                state = dict(self._state)
            self._comm.send({"method": "update", "state": state, "buffer_paths": []}, [])

    def close(self) -> None:
        if self._comm is not None:
            self._comm.close()
            self._comm = None

    # ---------------------------------------------------------------- display

    def _repr_mimebundle_(self, include: Any = None, exclude: Any = None) -> dict[str, Any]:
        bundle: dict[str, Any] = {"text/plain": repr(self)}
        if self.model_id is not None:
            bundle[WIDGET_VIEW_MIME] = {
                "version_major": 2,
                "version_minor": 1,
                "model_id": self.model_id,
            }
        return bundle

    def _mime_(self) -> tuple[str, str]:
        if self.model_id is not None:
            return WIDGET_VIEW_MIME, json.dumps(
                {"version_major": 2, "version_minor": 1, "model_id": self.model_id}
            )
        return "text/plain", repr(self)

    def __repr__(self) -> str:
        label = self._state.get("label") or ""
        shown = "<redacted>" if self._sensitive else repr(self.value)
        return f"{type(self).__name__}({label + ': ' if label else ''}{shown})"
