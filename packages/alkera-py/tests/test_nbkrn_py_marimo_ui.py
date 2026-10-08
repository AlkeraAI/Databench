"""``alkera.ui`` elements handed to ``MarimoHost`` and shown as ``marimo.ui``
controls, against a stand-in for marimo's UI classes (the real marimo is
exercised in ``test_nbkrn_py_foreign_hosts.py``)."""

from __future__ import annotations

import copy
import datetime as dt
import types
from typing import Any

import alkera.ui as ui
import pytest
from alkera import _host
from alkera._marimo_ui import ELEMENTS
from alkera.ui._model import WidgetModel


class FakeControl:
    """The part of ``marimo.ui``'s UIElement the peer relies on."""

    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("label") == "refuse":
            raise ValueError("marimo refuses these arguments")
        self.kwargs = kwargs
        self._value_frontend: Any = None
        self.marimo_updates: list[Any] = []

    def _update(self, value: Any) -> None:
        self._value_frontend = value
        self.marimo_updates.append(value)

    def _update_value(self, value: Any) -> None:
        self._value_frontend = value

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        new = object.__new__(type(self))
        memo[id(self)] = new
        for key, value in self.__dict__.items():
            setattr(new, key, copy.deepcopy(value, memo))
        return new


def fake_marimo(*, without: str = "") -> types.SimpleNamespace:
    names = {spec.factory for spec in ELEMENTS.values()} - {without}
    return types.SimpleNamespace(
        ui=types.SimpleNamespace(**{n: type(n, (FakeControl,), {}) for n in names})
    )


@pytest.fixture
def marimo() -> types.SimpleNamespace:
    return fake_marimo()


@pytest.fixture
def host(marimo: types.SimpleNamespace) -> Any:
    with _host.use(_host.MarimoHost(marimo)) as installed:
        yield installed


ELEMENT_KINDS = [name for name in ui.__all__ if name != "WidgetModel"]


@pytest.mark.parametrize("kind", ELEMENT_KINDS)
def test_every_alkera_ui_element_has_a_marimo_control(kind: str) -> None:
    view = getattr(ui, kind)._view_name
    assert view in ELEMENTS


# (view, wire values the element keeps, marimo's frontend form of the first)
CONVERSIONS = [
    pytest.param("DropdownView", ["Two", None], ["Two"], id="dropdown"),
    pytest.param("MultiselectView", [["a", "c"], []], ["a", "c"], id="multiselect"),
    pytest.param("ButtonView", [0, 3], 0, id="button"),
    pytest.param("RunButtonView", [2], 2, id="run_button"),
    pytest.param("RadioView", ["x", None], "x", id="radio"),
    pytest.param("DateView", ["2026-03-04"], "2026-03-04", id="date"),
    pytest.param("SliderView", [4, 2.5], 4, id="slider"),
]


@pytest.mark.parametrize(("view", "wires", "frontend"), CONVERSIONS)
def test_wire_values_survive_the_trip_through_marimo_s_frontend_form(
    view: str, wires: list[Any], frontend: Any
) -> None:
    spec = ELEMENTS[view]
    assert spec.to_frontend(wires[0]) == frontend
    for wire in wires:
        assert spec.from_frontend(spec.to_frontend(wire)) == wire


@pytest.mark.parametrize(
    ("view", "frontend", "wire"),
    [
        pytest.param("DropdownView", [], None, id="dropdown-nothing-selected"),
        pytest.param("DropdownView", "Two", None, id="dropdown-not-a-list"),
        pytest.param("MultiselectView", None, [], id="multiselect-not-a-list"),
        pytest.param("ButtonView", None, None, id="button-passes-the-count-through"),
    ],
)
def test_odd_frontend_values_become_what_the_element_coerces(
    view: str, frontend: Any, wire: Any
) -> None:
    assert ELEMENTS[view].from_frontend(frontend) == wire


@pytest.mark.parametrize(
    ("build", "kwargs"),
    [
        pytest.param(
            lambda: ui.slider(1, 10, step=3, value=4, label="L", disabled=True),
            {"start": 1, "stop": 10, "step": 3, "value": 4, "label": "L", "disabled": True},
            id="slider",
        ),
        pytest.param(
            lambda: ui.number(value=2),
            {"start": None, "stop": None, "step": None, "value": 2, "label": "", "disabled": False},
            id="number-unbounded",
        ),
        pytest.param(
            lambda: ui.text("pw", kind="password", placeholder="secret"),
            {"value": "pw", "placeholder": "secret", "kind": "password", "label": ""},
            id="text-password",
        ),
        pytest.param(
            lambda: ui.text_area("a", rows=7),
            {"value": "a", "rows": 7, "placeholder": ""},
            id="text_area",
        ),
        pytest.param(lambda: ui.switch(True), {"value": True}, id="switch"),
        pytest.param(
            lambda: ui.dropdown({"One": 1, "Two": 2}, value=2),
            {"options": {"One": "One", "Two": "Two"}, "value": "Two", "allow_select_none": False},
            id="dropdown",
        ),
        pytest.param(
            lambda: ui.dropdown([], allow_select_none=False),
            {"options": {}, "value": None, "allow_select_none": True},
            id="dropdown-empty-may-select-none",
        ),
        pytest.param(
            lambda: ui.multiselect([1, 2, 3], value=[3]),
            {"options": {"1": "1", "2": "2", "3": "3"}, "value": ["3"]},
            id="multiselect",
        ),
        pytest.param(
            lambda: ui.date(dt.date(2026, 3, 4), stop=dt.date(2026, 12, 31)),
            {"value": "2026-03-04", "start": None, "stop": "2026-12-31"},
            id="date",
        ),
        pytest.param(lambda: ui.run_button(), {"label": "Run", "disabled": False}, id="run_button"),
    ],
)
def test_marimo_is_given_the_element_s_state(host: Any, build: Any, kwargs: dict[str, Any]) -> None:
    shown = build()
    assert {k: shown.kwargs[k] for k in kwargs} == kwargs


def test_the_person_holds_a_marimo_control_that_is_still_their_element(
    host: Any, marimo: types.SimpleNamespace
) -> None:
    s = ui.slider(0, 10, value=4)
    assert isinstance(s, marimo.ui.slider)
    assert isinstance(s, ui.slider) and isinstance(s, WidgetModel)
    assert not isinstance(s, ui.number)
    assert isinstance(s.alkera_element, ui.slider)
    assert type(s.alkera_element) is ui.slider
    assert s.value == 4
    assert s.model_id is None
    assert repr(s) == "slider(4)"


def test_a_change_in_marimo_reaches_the_element_and_its_callbacks(host: Any) -> None:
    s = ui.slider(0, 10, value=4)
    heard: list[Any] = []
    s.on_change(heard.append)
    s._update(7)
    assert s.value == 7
    assert s.alkera_element.value == 7
    # The element's coercion applies to what the person sends.
    s._update(99)
    assert s.value == 10
    assert heard == [7, 10]
    assert s.marimo_updates == [7, 99]


def test_a_quiet_marimo_sync_sets_the_value_without_callbacks(host: Any) -> None:
    s = ui.slider(0, 10, value=4)
    heard: list[Any] = []
    s.on_change(heard.append)
    s._update_value(-5)
    assert s.value == 0
    assert heard == []


def test_options_keep_their_values_through_marimo(host: Any) -> None:
    d = ui.dropdown({"One": 1, "Two": 2}, value=2)
    d._update(["One"])
    assert d.value == 1
    m = ui.multiselect({"a": "A", "b": "B"})
    m._update(["b", "a", "zzz"])
    assert m.value == ["A", "B"]


def test_setting_the_value_in_python_moves_marimo_s_copy(host: Any) -> None:
    d = ui.dropdown({"One": 1, "Two": 2}, value=1)
    d.value = 2
    assert d.value == 2
    assert d._value_frontend == ["Two"]
    day = ui.date(dt.date(2026, 1, 1))
    day.value = dt.date(2026, 5, 6)
    assert day._value_frontend == "2026-05-06"


def test_button_clicks_count_and_call_on_click(host: Any) -> None:
    clicked: list[Any] = []
    b = ui.button("Go", on_click=clicked.append)
    b._update(1)
    b._update(5)  # a counter that jumps is still one click
    assert b.value == 2
    assert clicked == [b.alkera_element, b.alkera_element]


def test_a_password_stays_redacted_in_the_control_s_repr(host: Any) -> None:
    pw = ui.text("hunter2", kind="password")
    assert "hunter2" not in repr(pw)


def test_a_clone_is_independent_of_the_original(host: Any) -> None:
    s = ui.slider(0, 10, value=4)
    heard: list[Any] = []
    s.on_change(heard.append)
    twin = copy.deepcopy(s)
    assert isinstance(twin, ui.slider)
    assert twin.alkera_element is not s.alkera_element
    twin._update(9)
    assert (twin.value, s.value) == (9, 4)
    s._update(2)
    assert (twin.value, s.value) == (9, 2)
    # Callbacks registered before the clone go with it.
    assert heard == [9, 2]


@pytest.mark.parametrize(
    ("marimo_module", "build"),
    [
        pytest.param(fake_marimo(without="slider"), lambda: ui.slider(), id="marimo-lacks-it"),
        pytest.param(fake_marimo(), lambda: ui.slider(label="refuse"), id="marimo-refuses"),
        pytest.param(types.SimpleNamespace(), lambda: ui.switch(), id="no-marimo-ui"),
    ],
)
def test_an_element_marimo_cannot_show_stays_an_alkera_element(
    marimo_module: Any, build: Any
) -> None:
    with _host.use(_host.MarimoHost(marimo_module)):
        element = build()
    assert type(element) in (ui.slider, ui.switch)


def test_an_element_kind_with_no_marimo_control_stays_an_alkera_element(host: Any) -> None:
    class Gauge(WidgetModel):
        _view_name = "GaugeView"

        def __init__(self) -> None:
            super().__init__({"value": 1})

    assert type(Gauge()) is Gauge


def test_a_host_without_the_hook_gets_the_element_itself() -> None:
    with _host.use(_host.ScriptHost()):
        s = ui.slider(0, 10, value=4)
    assert type(s) is ui.slider
