"""``alkera.ui`` elements as widget models on the host's comms."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import alkera.ui as ui
import pytest
from nbsqw_host import FakeHost, installed

VIEW = "application/vnd.jupyter.widget-view+json"


def last_state(host: FakeHost) -> dict[str, Any]:
    return host.comms[-1].sent[-1]["state"]


def test_an_element_opens_a_widget_comm_and_renders_as_a_view() -> None:
    with installed(FakeHost()) as host:
        s = ui.slider(0, 10, value=3, label="Rows")
    [comm] = host.comms
    assert comm.target_name == "jupyter.widget"
    assert comm.metadata == {"version": "2.1.0"}
    state = comm.data["state"]
    assert {
        k: state[k]
        for k in (
            "_model_name",
            "_model_module",
            "_view_name",
            "_view_module",
            "value",
            "min",
            "max",
            "label",
        )
    } == {
        "_model_name": "ElementModel",
        "_model_module": "@alkera/ui-widgets",
        "_view_name": "SliderView",
        "_view_module": "@alkera/ui-widgets",
        "value": 3,
        "min": 0,
        "max": 10,
        "label": "Rows",
    }
    assert s.model_id == comm.comm_id
    bundle = s._repr_mimebundle_()
    assert bundle[VIEW] == {"version_major": 2, "version_minor": 1, "model_id": comm.comm_id}
    assert s._mime_() == (VIEW, json.dumps(bundle[VIEW]))
    assert host.reactive == [s]


def test_outside_a_notebook_an_element_holds_its_value_and_prints() -> None:
    s = ui.slider(0, 10, value=4)
    assert s.model_id is None
    assert s.value == 4
    s.value = 7
    assert s.value == 7
    assert s._repr_mimebundle_() == {"text/plain": "slider(7)"}
    assert s._mime_() == ("text/plain", "slider(7)")


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        pytest.param(5, 5, id="in-range"),
        pytest.param(99, 10, id="clamped-high"),
        pytest.param(-3, 0, id="clamped-low"),
        pytest.param(4.6, 5, id="snapped-to-step"),
        pytest.param("x", 3, id="not-a-number"),
        pytest.param(True, 3, id="bool-is-not-a-number"),
    ],
)
def test_a_frontend_change_is_clamped_echoed_and_observed(sent: Any, kept: int) -> None:
    seen: list[Any] = []
    with installed(FakeHost()) as host:
        s = ui.slider(0, 10, value=3)
        s.on_change(seen.append)
        host.comms[0].frontend({"value": sent})
    assert s.value == kept
    assert host.comms[0].sent[-1] == {
        "method": "echo_update",
        "state": {"value": kept},
        "buffer_paths": [],
    }
    assert seen == [kept]


def test_float_steps_keep_floats() -> None:
    with installed(FakeHost()) as host:
        s = ui.slider(0.0, 1.0, 0.25, value=0.5)
        host.comms[0].frontend({"value": 0.6})
    assert s.value == 0.5


def test_setting_value_in_the_kernel_updates_frontends() -> None:
    with installed(FakeHost()) as host:
        s = ui.slider(0, 10)
        s.value = 50
    assert s.value == 10
    assert host.comms[0].sent[-1] == {
        "method": "update",
        "state": {"value": 10},
        "buffer_paths": [],
    }


def test_dropdown_values_travel_as_labels() -> None:
    with installed(FakeHost()) as host:
        d = ui.dropdown({"Small": 1, "Large": 100}, value=100)
        assert host.comms[0].data["state"]["options"] == ["Small", "Large"]
        assert host.comms[0].data["state"]["value"] == "Large"
        assert d.value == 100
        host.comms[0].frontend({"value": "Small"})
        assert d.value == 1
        host.comms[0].frontend({"value": "Medium"})
    assert d.value == 1
    with pytest.raises(ValueError, match="not one of the options"):
        d.value = 7


def test_dropdown_defaults_to_the_first_option_unless_none_is_allowed() -> None:
    assert ui.dropdown(["a", "b"]).value == "a"
    assert ui.dropdown(["a", "b"], allow_select_none=True).value is None


def test_multiselect_and_radio() -> None:
    with installed(FakeHost()) as host:
        m = ui.multiselect(["x", "y", "z"], value=["y"])
        r = ui.radio(["x", "y"], value="x")
        host.comms[0].frontend({"value": ["z", "x", "nope", "x"]})
        host.comms[1].frontend({"value": "y"})
    assert m.value == ["x", "z"]
    assert r.value == "y"


def test_date_is_bounded() -> None:
    lo, hi = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    with installed(FakeHost()) as host:
        d = ui.date(dt.date(2026, 6, 1), start=lo, stop=hi)
        host.comms[0].frontend({"value": "2027-03-01"})
        assert d.value == hi
        host.comms[0].frontend({"value": "not a date"})
        assert d.value == hi
        host.comms[0].frontend({"value": None})
    assert d.value is None


@pytest.mark.parametrize(
    "element", [pytest.param(ui.checkbox, id="checkbox"), pytest.param(ui.switch, id="switch")]
)
def test_booleans(element: Any) -> None:
    with installed(FakeHost()) as host:
        b = element(False)
        host.comms[0].frontend({"value": True})
        assert b.value is True
        host.comms[0].frontend({"value": "yes"})
    assert b.value is True


def test_text_and_password() -> None:
    with installed(FakeHost()) as host:
        t = ui.text("hi")
        p = ui.text(kind="password", label="Token")
        host.comms[1].frontend({"value": "hunter2"})
    assert "_sensitive" not in host.comms[0].data["state"]
    assert host.comms[1].data["state"]["_sensitive"] is True
    assert p.value == "hunter2"
    assert "hunter2" not in repr(p)
    assert t.value == "hi"
    with pytest.raises(ValueError):
        ui.text(kind="email")


def test_text_area_rows() -> None:
    with installed(FakeHost()):
        a = ui.text_area("x", rows=8)
    assert a._state["rows"] == 8
    assert a._view_name == "TextAreaView"


def test_a_button_counts_one_click_at_a_time_and_calls_on_click() -> None:
    clicks: list[int] = []
    with installed(FakeHost()) as host:
        b = ui.button("Go", on_click=lambda btn: clicks.append(btn.value))
        host.comms[0].frontend({"value": 1})
        host.comms[0].frontend({"value": 50})
        host.comms[0].frontend({"value": 0})
    assert b.value == 2  # the message going backwards was not a click
    assert clicks == [1, 2, 2]


def test_run_button_is_reactive() -> None:
    with installed(FakeHost()) as host:
        r = ui.run_button()
    assert host.reactive == [r]
    assert host.comms[0].data["state"]["label"] == "Run"


def test_messages_without_a_value_and_state_requests() -> None:
    with installed(FakeHost()) as host:
        s = ui.slider(0, 10, value=2)
        comm = host.comms[0]
        comm.frontend({"description": "x"})
        assert comm.sent == []
        comm.on_msg({"content": {"comm_id": comm.comm_id, "data": {"method": "request_state"}}})
        assert comm.sent[-1]["state"]["value"] == 2
        s.close()
    assert comm.closed
    assert s.model_id is None


def test_a_script_host_is_not_a_notebook() -> None:
    class ScriptHost:
        protocol_version = 1
        name = "script"

    with installed(ScriptHost()):
        s = ui.slider()
    assert s.model_id is None


def test_a_host_of_another_protocol_is_ignored() -> None:
    host = FakeHost(protocol_version=2)
    with installed(host):
        ui.slider()
    assert host.comms == []


def test_the_host_selector_wins_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """alkera._host.current() (the public host selection) is asked first."""
    import sys
    import types

    chosen = FakeHost()
    selector = types.ModuleType("alkera._host")
    selector.current = lambda: chosen  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alkera._host", selector)
    published = FakeHost()
    with installed(published):
        ui.slider()
    assert len(chosen.comms) == 1
    assert published.comms == []


def test_a_script_host_from_the_selector_falls_back_to_the_published_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    class Script:
        name = "script"

    selector = types.ModuleType("alkera._host")
    selector.current = lambda: Script()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alkera._host", selector)
    published = FakeHost()
    with installed(published):
        ui.slider()
    assert len(published.comms) == 1


def test_optional_libraries_are_a_closed_list() -> None:
    from alkera.ui._runtime import available, optional

    with pytest.raises(ValueError):
        optional("requests")
    assert available("duckdb") is True
