"""``alkera.ui`` elements as marimo's own controls, for ``MarimoHost``.

marimo re-runs the cells that read a control only when the global holding
it is a ``marimo.ui`` element (its registry finds bindings by
``isinstance(value, UIElement)``). So under stock marimo an ``alkera.ui``
element is handed to the host when it is built, and the host returns a
*peer*: an instance of a subclass of the matching ``marimo.ui`` class that
holds the ``alkera.ui`` element and speaks its API.

- marimo renders the peer as its own control, tracks it, and re-runs the
  cells that read it when the person moves it.
- Each frontend change reaches the ``alkera.ui`` element as a frontend
  ``update`` message (``handle_msg``), so the element's coercion and its
  ``on_change`` callbacks run as they do in the Alkera kernel.
- ``.value`` reads the ``alkera.ui`` element; setting it sets the element and
  then marimo's copy of the value.

``ELEMENTS`` maps each element's view name to the ``marimo.ui`` factory and
the conversions between the element's wire value and marimo's frontend
value. Nothing here imports marimo: the module object comes from the host.
"""

from __future__ import annotations

import copy
import threading
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["ELEMENTS", "MarimoElement", "adopt", "peer_class"]

State = typing.Mapping[str, Any]


def _same(value: Any) -> Any:
    return value


def _common(state: State) -> dict[str, Any]:
    return {"label": state.get("label") or "", "disabled": bool(state.get("disabled"))}


@dataclass(frozen=True)
class MarimoElement:
    """How one ``alkera.ui`` view is shown as a ``marimo.ui`` element."""

    #: The ``marimo.ui`` attribute that builds the control.
    factory: str
    #: The factory's keyword arguments, from the element's state.
    arguments: Callable[[State], dict[str, Any]]
    #: The element's wire value as marimo's frontend value.
    to_frontend: Callable[[Any], Any] = _same
    #: marimo's frontend value as the element's wire value.
    from_frontend: Callable[[Any], Any] = _same


def _bounded(state: State) -> dict[str, Any]:
    return {
        "start": state.get("min"),
        "stop": state.get("max"),
        "step": state.get("step"),
        "value": state.get("value"),
        **_common(state),
    }


def _text(state: State) -> dict[str, Any]:
    return {
        "value": state.get("value") or "",
        "placeholder": state.get("placeholder") or "",
        "kind": state.get("kind") or "text",
        **_common(state),
    }


def _text_area(state: State) -> dict[str, Any]:
    return {
        "value": state.get("value") or "",
        "placeholder": state.get("placeholder") or "",
        "rows": state.get("rows"),
        **_common(state),
    }


def _toggle(state: State) -> dict[str, Any]:
    return {"value": bool(state.get("value")), **_common(state)}


def _labels(state: State) -> dict[str, str]:
    # The element keeps labels on the wire and maps them to values itself,
    # so marimo is given labels for both.
    return {str(label): str(label) for label in state.get("options") or []}


def _dropdown(state: State) -> dict[str, Any]:
    value = state.get("value")
    return {
        "options": _labels(state),
        "value": value,
        "allow_select_none": bool(state.get("allow_select_none")) or value is None,
        **_common(state),
    }


def _radio(state: State) -> dict[str, Any]:
    return {"options": _labels(state), "value": state.get("value"), **_common(state)}


def _multiselect(state: State) -> dict[str, Any]:
    return {
        "options": _labels(state),
        "value": list(state.get("value") or []),
        **_common(state),
    }


def _date(state: State) -> dict[str, Any]:
    return {
        "value": state.get("value"),
        "start": state.get("min"),
        "stop": state.get("max"),
        **_common(state),
    }


def _button(state: State) -> dict[str, Any]:
    return _common(state)


def _one_of(value: Any) -> list[Any]:
    return [] if value is None else [value]


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else None


def _listed(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


ELEMENTS: dict[str, MarimoElement] = {
    "SliderView": MarimoElement("slider", _bounded),
    "NumberView": MarimoElement("number", _bounded),
    "TextView": MarimoElement("text", _text),
    "TextAreaView": MarimoElement("text_area", _text_area),
    "CheckboxView": MarimoElement("checkbox", _toggle),
    "SwitchView": MarimoElement("switch", _toggle),
    "DropdownView": MarimoElement("dropdown", _dropdown, _one_of, _first),
    "RadioView": MarimoElement("radio", _radio),
    "MultiselectView": MarimoElement("multiselect", _multiselect, _listed, _listed),
    "DateView": MarimoElement("date", _date),
    "ButtonView": MarimoElement("button", _button, _count),
    "RunButtonView": MarimoElement("run_button", _button, _count),
}


def _frontend_update(wire: Any) -> dict[str, Any]:
    return {"content": {"data": {"method": "update", "state": {"value": wire}}}}


def _twin(element: Any) -> Any:
    """An independent copy of an ``alkera.ui`` element (marimo clones a
    control when it is put in ``mo.ui.array`` and the like)."""
    twin = copy.copy(element)
    with element._lock:
        twin._state = copy.deepcopy(element._state)
    twin._lock = threading.RLock()
    twin._callbacks = list(element._callbacks)
    twin._comm = None
    return twin


# ---------------------------------------------------------------- the peer's members


def _get_value(self: Any) -> Any:
    return self._alkera_element.value


def _set_value(self: Any, new: Any) -> None:
    element = self._alkera_element
    element.value = new
    with element._lock:
        wire = element._state.get("value")
    self._alkera_base._update_value(self, self._alkera_spec.to_frontend(wire))


def _update(self: Any, value: Any) -> None:
    # A change from the person, delivered by marimo's kernel.
    self._alkera_base._update(self, value)
    self._alkera_element.handle_msg(_frontend_update(self._alkera_spec.from_frontend(value)))


def _update_value(self: Any, value: Any) -> None:
    # A change marimo applies without callbacks (a parent control's sync).
    self._alkera_base._update_value(self, value)
    element = self._alkera_element
    element._set(
        {"value": element._coerce_wire(self._alkera_spec.from_frontend(value))}, method="update"
    )


def _on_change(self: Any, callback: Callable[[Any], None]) -> Callable[[Any], None]:
    """Calls ``callback(value)`` after each change made in a frontend."""
    result: Callable[[Any], None] = self._alkera_element.on_change(callback)
    return result


def _alkera_element(self: Any) -> Any:
    """The ``alkera.ui`` element this control shows."""
    return self.__dict__["_alkera_element"]


def _model_id(self: Any) -> str | None:
    result: str | None = self._alkera_element.model_id
    return result


def _close(self: Any) -> None:
    self._alkera_element.close()


def _repr(self: Any) -> str:
    return repr(self._alkera_element)


def _deepcopy(self: Any, memo: dict[int, Any]) -> Any:
    memo[id(self._alkera_element)] = _twin(self._alkera_element)
    return self._alkera_base.__deepcopy__(self, memo)


def peer_class(base: type) -> type:
    """A subclass of the ``marimo.ui`` class ``base`` that holds an
    ``alkera.ui`` element and speaks its API."""
    namespace: dict[str, Any] = {
        "__module__": __name__,
        "__doc__": f"An alkera.ui element shown as marimo.ui.{base.__name__}.",
        "_alkera_peer": True,
        "_alkera_base": base,
        "value": property(_get_value, _set_value),
        "alkera_element": property(_alkera_element),
        "model_id": property(_model_id),
        "on_change": _on_change,
        "close": _close,
        "_update": _update,
        "_update_value": _update_value,
        "__repr__": _repr,
        "__deepcopy__": _deepcopy,
    }
    return type(f"alkera_{base.__name__}", (base,), namespace)


def adopt(marimo: Any, element: Any, classes: dict[type, type]) -> Any | None:
    """The marimo peer for ``element``, or ``None`` when its view has no
    marimo equivalent (it then stays an ``alkera.ui`` element). ``classes``
    caches the peer class built for each ``marimo.ui`` class."""
    spec = ELEMENTS.get(getattr(type(element), "_view_name", ""))
    if spec is None:
        return None
    base = getattr(getattr(marimo, "ui", None), spec.factory, None)
    if not isinstance(base, type):
        return None
    cls = classes.get(base)
    if cls is None:
        cls = classes[base] = peer_class(base)
    with element._lock:
        state = dict(element._state)
    try:
        peer = cls(**spec.arguments(state))
    except (TypeError, ValueError):
        return None
    peer._alkera_spec = spec
    peer._alkera_element = element
    return peer
