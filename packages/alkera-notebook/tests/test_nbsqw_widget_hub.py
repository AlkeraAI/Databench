"""The widget hub: cache rules, closures and replays, the idle relay,
sensitive values, and large values moved to the asset store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.widgets.assets import (
    ASSET_REF_PREFIX,
    AssetRefusedError,
    FileBlobStore,
    MemoryBlobStore,
    WidgetAssets,
    parse_asset_ref,
    sha256_hex,
    workspace_scope,
)
from alkera_notebook.widgets.hub import REDACTED, Frame, Outbound, WidgetHub, WidgetRefusedError


def opened(
    hub: WidgetHub, comm_id: str, state: dict[str, Any], buffers: dict[str, bytes] | None = None
) -> None:
    bufs = buffers or {}
    hub.kernel_open(
        comm_id,
        {
            "target_name": "jupyter.widget",
            "data": {"state": state, "buffer_paths": [[k] for k in bufs]},
        },
        list(bufs.values()),
        {"version": "2.1.0"},
    )


def update(
    hub: WidgetHub,
    comm_id: str,
    state: dict[str, Any],
    parent: str | None = None,
    method: str = "update",
) -> None:
    hub.kernel_msg(
        comm_id,
        {"comm_id": comm_id, "data": {"method": method, "state": state, "buffer_paths": []}},
        [],
        parent,
    )


def slider(hub: WidgetHub, cid: str = "s", value: int = 5) -> None:
    opened(hub, "lay", {"_model_name": "LayoutModel"})
    opened(
        hub,
        cid,
        {"_model_name": "IntSliderModel", "value": value, "max": 10, "layout": "IPY_MODEL_lay"},
    )


def states(out: list[Outbound], frame_id: str) -> list[dict[str, Any]]:
    return [
        o.message["content"]["data"]["state"]
        for o in out
        if o.frame_id == frame_id and o.message["type"] == "comm.msg"
    ]


# ------------------------------------------------------------------ cache rules


def test_frontend_deltas_never_change_the_cache() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    delivery = hub.frontend_send(
        "f1",
        "s",
        "m1",
        {"comm_id": "s", "data": {"method": "update", "state": {"value": 99}}},
        [],
        client_id="alice",
    )
    assert delivery.msg == {
        "msg_type": "comm_msg",
        "content": {"comm_id": "s", "data": {"method": "update", "state": {"value": 99}}},
    }
    assert hub.model("s").state["value"] == 5  # type: ignore[union-attr]


def test_the_kernels_clamped_value_wins() -> None:
    """The frame sends 99; the kernel clamps to 10 and echoes; the cache and
    every frame end on 10."""
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    hub.attach(Frame("f2", "bob", ("s",)))
    hub.frontend_send(
        "f1",
        "s",
        "m1",
        {"data": {"method": "update", "state": {"value": 99}}},
        [],
        client_id="alice",
    )
    update(hub, "s", {"value": 10}, parent="m1", method="echo_update")
    out = hub.drain()
    assert hub.model("s").state["value"] == 10  # type: ignore[union-attr]
    assert states(out, "f1") == [{"value": 10}]
    assert states(out, "f2") == [{"value": 10}]


def test_partial_updates_merge_and_buffers_follow_paths() -> None:
    hub = WidgetHub(scope="nb")
    opened(
        hub,
        "img",
        {"_model_name": "ImageModel", "format": "png", "width": "10"},
        {"value": b"\x89PNG"},
    )
    update(hub, "img", {"width": "20"})
    m = hub.model("img")
    assert m is not None
    assert m.state["format"] == "png" and m.state["width"] == "20"
    assert m.buffers == {("value",): b"\x89PNG"}
    hub.kernel_msg(
        "img",
        {"data": {"method": "update", "state": {"value": None}, "buffer_paths": [["value"]]}},
        [b"NEW"],
        None,
    )
    assert m.buffers == {("value",): b"NEW"}
    update(hub, "img", {"value": "plain json now"})
    assert m.buffers == {}


def test_custom_messages_pass_unchanged_and_leave_the_cache() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    content = {"comm_id": "s", "data": {"method": "custom", "content": {"event": "ping"}}}
    hub.kernel_msg("s", content, [b"x"], None)
    [out] = hub.drain()
    assert out.message == {
        "type": "comm.msg",
        "comm_id": "s",
        "content": content,
        "parent_msg_id": None,
    }
    assert out.buffers == [b"x"]
    assert "content" not in hub.model("s").state  # type: ignore[union-attr]


def test_consecutive_updates_coalesce_per_model() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    opened(hub, "t", {"_model_name": "TextModel", "value": ""})
    hub.attach(Frame("f1", "alice", ("s",)))
    for v in (1, 2, 3):
        update(hub, "s", {"value": v})
    update(hub, "s", {"description": "n"})
    out = hub.drain()
    assert states(out, "f1") == [{"value": 3, "description": "n"}]


def test_updates_separated_by_a_custom_message_stay_apart() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    update(hub, "s", {"value": 1})
    hub.kernel_msg("s", {"data": {"method": "custom", "content": {}}}, [], None)
    update(hub, "s", {"value": 2})
    out = hub.drain()
    assert [o.message["content"]["data"]["method"] for o in out] == ["update", "custom", "update"]


def test_updates_with_different_parents_stay_apart() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    update(hub, "s", {"value": 1}, parent=None)
    update(hub, "s", {"value": 2}, parent="m9", method="echo_update")
    assert len(hub.drain()) == 2


# ------------------------------------------------------------------ frames


def test_a_joining_frame_gets_its_closure_in_creation_order() -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "other", {"_model_name": "ButtonModel"})
    slider(hub)
    update(hub, "s", {"value": 7})
    replays = hub.attach(Frame("f2", "bob", ("s",)))
    assert [r.message["comm_id"] for r in replays] == ["lay", "s"]
    assert replays[1].message["data"]["state"]["value"] == 7
    assert all(r.message["type"] == "comm.open" for r in replays)


def test_a_frame_only_hears_about_its_own_models() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    opened(hub, "b", {"_model_name": "ButtonModel"})
    hub.attach(Frame("f1", "alice", ("s",)))
    update(hub, "b", {"description": "x"})
    assert hub.drain() == []
    with pytest.raises(WidgetRefusedError, match="not given that widget"):
        hub.frontend_send("f1", "b", "m1", {"data": {}}, [], client_id="alice")


def test_the_closure_grows_when_an_update_adds_a_reference() -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "box", {"_model_name": "VBoxModel", "children": []})
    opened(hub, "child", {"_model_name": "ButtonModel"})
    hub.attach(Frame("f1", "alice", ("box",)))
    update(hub, "box", {"children": ["IPY_MODEL_child"]})
    out = hub.drain()
    assert [(o.message["type"], o.message["comm_id"]) for o in out] == [
        ("comm.open", "child"),
        ("comm.msg", "box"),
    ]
    hub.frontend_send("f1", "child", "m1", {"data": {}}, [], client_id="alice")


def test_a_link_between_shown_models_comes_with_them() -> None:
    """jslink is a view-less model pointing at both ends; nothing points at it."""
    hub = WidgetHub(scope="nb")
    opened(hub, "a", {"_model_name": "IntSliderModel", "_view_name": "IntSliderView"})
    opened(hub, "b", {"_model_name": "IntTextModel", "_view_name": "IntTextView"})
    opened(
        hub,
        "box",
        {
            "_model_name": "HBoxModel",
            "_view_name": "HBoxView",
            "children": ["IPY_MODEL_a", "IPY_MODEL_b"],
        },
    )
    opened(
        hub,
        "link",
        {
            "_model_name": "LinkModel",
            "_view_name": None,
            "source": ["IPY_MODEL_a", "value"],
            "target": ["IPY_MODEL_b", "value"],
        },
    )
    opened(
        hub,
        "half",
        {
            "_model_name": "LinkModel",
            "_view_name": None,
            "source": ["IPY_MODEL_a", "value"],
            "target": ["IPY_MODEL_x", "value"],
        },
    )
    opened(hub, "x", {"_model_name": "IntTextModel", "_view_name": "IntTextView"})
    opened(
        hub,
        "elsewhere",
        {
            "_model_name": "LinkModel",
            "_view_name": None,
            "source": ["IPY_MODEL_x", "value"],
            "target": ["IPY_MODEL_x", "value"],
        },
    )
    assert hub.closure("box") == {"box", "a", "b", "link"}
    assert [r.message["comm_id"] for r in hub.attach(Frame("f1", "alice", ("box",)))] == [
        "a",
        "b",
        "box",
        "link",
    ]


def test_a_model_opened_after_the_reference_reaches_the_frame() -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "box", {"_model_name": "VBoxModel", "children": ["IPY_MODEL_late"]})
    hub.attach(Frame("f1", "alice", ("box",)))
    opened(hub, "late", {"_model_name": "ButtonModel"})
    assert [o.message["comm_id"] for o in hub.drain()] == ["late"]


def test_a_second_frame_comes_up_to_date_and_stays_in_sync() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub, value=1)
    hub.attach(Frame("f1", "alice", ("s",)))
    update(hub, "s", {"value": 4})
    hub.drain()
    replays = hub.attach(Frame("f2", "bob", ("s",)))
    assert replays[-1].message["data"]["state"]["value"] == 4
    update(hub, "s", {"value": 6})
    out = hub.drain()
    assert states(out, "f1") == states(out, "f2") == [{"value": 6}]


def test_close_reaches_the_frames_and_drops_the_model() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    hub.kernel_close("s")
    assert [o.message for o in hub.drain()] == [{"type": "comm.close", "comm_id": "s"}]
    assert hub.model("s") is None


# ------------------------------------------------------------------ frontend messages


def test_idle_goes_back_to_the_sending_frame_only() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    hub.attach(Frame("f2", "bob", ("s",)))
    hub.frontend_send(
        "f1",
        "s",
        "m1",
        {"data": {"method": "update", "state": {"value": 2}}},
        [],
        client_id="alice",
    )
    hub.kernel_idle("m1")
    assert [(o.frame_id, o.message) for o in hub.drain()] == [
        ("f1", {"type": "comm.status", "msg_id": "m1", "execution_state": "idle"})
    ]
    hub.kernel_idle("m1")  # once only
    assert hub.drain() == []


def test_one_frame_shows_several_outputs_models_once_each() -> None:
    """A frame is keyed by its id, not by one model: an output holding two
    widgets that share a layout replays the layout once and hears both."""
    hub = WidgetHub(scope="nb")
    slider(hub, "s1")
    opened(hub, "s2", {"_model_name": "IntSliderModel", "value": 1, "layout": "IPY_MODEL_lay"})
    opened(hub, "elsewhere", {"_model_name": "ButtonModel"})
    replays = hub.attach(Frame("f1", "alice", ("s1", "s2")))
    assert [r.message["comm_id"] for r in replays] == ["lay", "s1", "s2"]
    update(hub, "s2", {"value": 3})
    update(hub, "elsewhere", {"description": "x"})
    assert [(o.frame_id, o.message["comm_id"]) for o in hub.drain()] == [("f1", "s2")]
    hub.frontend_send(
        "f1",
        "s2",
        "m1",
        {"data": {"method": "update", "state": {"value": 4}}},
        [],
        client_id="alice",
    )
    hub.kernel_idle("m1")
    assert [o.message["type"] for o in hub.drain()] == ["comm.status"]


def test_a_model_the_kernel_marks_sensitive_is_redacted_whatever_its_name() -> None:
    hub = WidgetHub(scope="nb")
    hub.kernel_open(
        "tok",
        {
            "target_name": "jupyter.widget",
            "data": {"state": {"_model_name": "TextModel", "value": "sk-live"}},
        },
        [],
        {"version": "2.1.0"},
        sensitive=True,
    )
    replays = hub.attach(Frame("fa", "alice", ("tok",)))
    assert replays[0].message["data"]["state"]["value"] == REDACTED
    assert "sk-live" not in json.dumps(hub.snapshot())
    hub.attach(Frame("fb", "bob", ("tok",)))
    hub.frontend_send(
        "fa",
        "tok",
        "m1",
        {"data": {"method": "update", "state": {"value": "typed"}}},
        [],
        client_id="alice",
    )
    update(hub, "tok", {"value": "typed"}, parent="m1", method="echo_update")
    out = hub.drain()
    assert states(out, "fa") == [{"value": "typed"}]
    assert states(out, "fb") == [{"value": REDACTED}]


def test_the_same_text_model_unmarked_is_not_redacted() -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "t", {"_model_name": "TextModel", "value": "plain"})
    replays = hub.attach(Frame("fa", "alice", ("t",)))
    assert replays[0].message["data"]["state"]["value"] == "plain"


def test_a_burst_of_key_presses_ends_on_the_last_value() -> None:
    """Five presses: the frame sends the first, buffers the rest until idle,
    then sends the latest. The kernel and the cache end on it."""
    hub = WidgetHub(scope="nb")
    opened(hub, "t", {"_model_name": "TextModel", "value": ""})
    hub.attach(Frame("f1", "alice", ("t",)))
    kernel_value = ""
    typed = ["h", "he", "hel", "hell", "hello"]
    pending: str | None = None
    in_flight: str | None = None
    for i, text in enumerate(typed):
        if in_flight is None:
            d = hub.frontend_send(
                "f1",
                "t",
                f"m{i}",
                {"data": {"method": "update", "state": {"value": text}}},
                [],
                client_id="alice",
            )
            in_flight = d.msg_id
            kernel_value = d.msg["content"]["data"]["state"]["value"]
        else:
            pending = text
    # The kernel handles the first, echoes, goes idle; the frame then sends what it held.
    update(hub, "t", {"value": kernel_value}, parent=in_flight, method="echo_update")
    hub.kernel_idle(in_flight)  # type: ignore[arg-type]
    statuses = [o for o in hub.drain() if o.message["type"] == "comm.status"]
    assert statuses and statuses[0].message["msg_id"] == in_flight
    d = hub.frontend_send(
        "f1",
        "t",
        "m-last",
        {"data": {"method": "update", "state": {"value": pending}}},
        [],
        client_id="alice",
    )
    update(
        hub,
        "t",
        {"value": d.msg["content"]["data"]["state"]["value"]},
        parent="m-last",
        method="echo_update",
    )
    hub.kernel_idle("m-last")
    assert hub.model("t").state["value"] == "hello"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("frame", "sender", "frame_id", "comm_id", "msg_id", "can_run", "message"),
    [
        pytest.param(
            Frame("f1", "alice", ("s",)),
            "mallory",
            "f1",
            "s",
            "m1",
            True,
            "not yours",
            id="another-clients-frame",
        ),
        pytest.param(
            Frame("f1", "viewer", ("s",), readonly=True),
            "mallory",
            "f1",
            "s",
            "m1",
            True,
            "not yours",
            id="another-clients-readonly-frame",
        ),
        pytest.param(
            Frame("f1", "alice", ("s",)),
            "alice",
            "ghost",
            "s",
            "m1",
            True,
            "unknown frame",
            id="unknown-frame",
        ),
        pytest.param(
            Frame("f1", "alice", ("s",)),
            "alice",
            "f1",
            "other",
            "m1",
            True,
            "not given that widget",
            id="comm-outside-the-closure",
        ),
        pytest.param(
            Frame("f1", "alice", ("s",)),
            "alice",
            "f1",
            "no-such-comm",
            "m1",
            True,
            "not given that widget",
            id="unknown-comm",
        ),
        pytest.param(
            Frame("f1", "viewer", ("s",), readonly=True),
            "viewer",
            "f1",
            "s",
            "m1",
            True,
            "read-only",
            id="readonly",
        ),
        pytest.param(
            Frame("f1", "carol", ("s",)),
            "carol",
            "f1",
            "s",
            "m1",
            False,
            "cannot run",
            id="no-run-permission",
        ),
        pytest.param(
            Frame("f1", "alice", ("s",)), "alice", "f1", "s", "", True, "new msg_id", id="no-msg-id"
        ),
    ],
)
def test_frontend_messages_refused(
    frame: Frame,
    sender: str,
    frame_id: str,
    comm_id: str,
    msg_id: str,
    can_run: bool,
    message: str,
) -> None:
    """The hub itself refuses: whoever calls it (the engine session, a relay
    on the box) cannot send on a frame it does not hold or a comm that frame
    was not given."""
    hub = WidgetHub(scope="nb", can_run=lambda _client: can_run)
    slider(hub)
    opened(hub, "other", {"_model_name": "ButtonModel", "_view_name": "ButtonView"})
    hub.attach(frame)
    with pytest.raises(WidgetRefusedError, match=message):
        hub.frontend_send(frame_id, comm_id, msg_id, {"data": {}}, [], client_id=sender)
    hub.kernel_idle(msg_id)
    assert hub.drain() == [], "a refused message is not on record as the frame's"


def test_only_the_frames_owner_sends_and_a_stranger_leaves_no_trace() -> None:
    """A stranger naming the owner's frame is refused without claiming the
    msg_id; the owner then sends on the same frame and comm and gets the
    idle back."""
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    hub.attach(Frame("f2", "mallory", ("s",)))
    with pytest.raises(WidgetRefusedError, match="not yours"):
        hub.frontend_send("f1", "s", "m1", {"data": {}}, [], client_id="mallory")
    delivery = hub.frontend_send("f1", "s", "m1", {"data": {}}, [], client_id="alice")
    assert delivery.client_id == "alice"
    hub.kernel_idle("m1")
    assert [(o.frame_id, o.message["type"]) for o in hub.drain()] == [("f1", "comm.status")]


def test_permission_is_asked_per_message() -> None:
    allowed = {"alice": True}
    hub = WidgetHub(scope="nb", can_run=lambda client: allowed[client])
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    hub.frontend_send("f1", "s", "m1", {"data": {}}, [], client_id="alice")
    allowed["alice"] = False
    with pytest.raises(WidgetRefusedError):
        hub.frontend_send("f1", "s", "m2", {"data": {}}, [], client_id="alice")


def test_a_reused_msg_id_is_refused_and_unknown_frames_too() -> None:
    hub = WidgetHub(scope="nb")
    slider(hub)
    hub.attach(Frame("f1", "alice", ("s",)))
    hub.frontend_send("f1", "s", "m1", {"data": {}}, [], client_id="alice")
    with pytest.raises(WidgetRefusedError):
        hub.frontend_send("f1", "s", "m1", {"data": {}}, [], client_id="alice")
    with pytest.raises(WidgetRefusedError, match="unknown frame"):
        hub.frontend_send("ghost", "s", "m2", {"data": {}}, [], client_id="alice")
    hub.detach("f1")
    with pytest.raises(WidgetRefusedError, match="unknown frame"):
        hub.frontend_send("f1", "s", "m3", {"data": {}}, [], client_id="alice")


# ------------------------------------------------------------------ sensitive values


@pytest.mark.parametrize(
    "state",
    [
        pytest.param({"_model_name": "PasswordModel", "value": ""}, id="ipywidgets-password"),
        pytest.param(
            {"_model_name": "ElementModel", "_sensitive": True, "value": ""},
            id="alkera-ui-password",
        ),
    ],
)
def test_a_password_reaches_only_the_frontend_that_typed_it(state: dict[str, Any]) -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "p", state)
    hub.attach(Frame("fa", "alice", ("p",)))
    hub.attach(Frame("fb", "bob", ("p",)))
    hub.frontend_send(
        "fa",
        "p",
        "m1",
        {"data": {"method": "update", "state": {"value": "hunter2"}}},
        [],
        client_id="alice",
    )
    update(hub, "p", {"value": "hunter2"}, parent="m1", method="echo_update")
    out = hub.drain()
    assert states(out, "fa") == [{"value": "hunter2"}]
    assert states(out, "fb") == [{"value": REDACTED}]
    assert hub.model("p").state["value"] == REDACTED  # type: ignore[union-attr]
    assert hub.snapshot()["state"]["p"]["state"]["value"] == REDACTED
    late = hub.attach(Frame("fc", "carol", ("p",)))
    assert late[0].message["data"]["state"]["value"] == REDACTED
    assert "hunter2" not in json.dumps(hub.snapshot())


def test_a_password_the_kernel_sets_is_redacted_everywhere() -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "p", {"_model_name": "PasswordModel", "value": "preset"})
    hub.attach(Frame("fa", "alice", ("p",)))
    update(hub, "p", {"value": "changed"})
    assert states(hub.drain(), "fa") == [{"value": REDACTED}]
    assert hub.model("p").state["value"] == REDACTED  # type: ignore[union-attr]


def test_snapshot_is_widget_state_json() -> None:
    hub = WidgetHub(scope="nb")
    opened(
        hub,
        "img",
        {
            "_model_name": "ImageModel",
            "_model_module": "@jupyter-widgets/controls",
            "_model_module_version": "2.0.0",
        },
        {"value": b"ab"},
    )
    snap = hub.snapshot()
    assert snap["version_major"] == 2
    entry = snap["state"]["img"]
    assert entry["model_name"] == "ImageModel"
    assert entry["buffers"] == [{"path": ["value"], "encoding": "base64", "data": "YWI="}]


def test_replays_carry_buffers_beside_the_state() -> None:
    hub = WidgetHub(scope="nb")
    opened(hub, "img", {"_model_name": "ImageModel", "format": "png"}, {"value": b"PNG"})
    [replay] = hub.attach(Frame("f1", "alice", ("img",)))
    assert replay.message["data"]["buffer_paths"] == [["value"]]
    assert replay.message["data"]["state"]["value"] is None
    assert replay.buffers == [b"PNG"]


# ------------------------------------------------------------------ large values and assets


def test_large_esm_moves_into_the_store_and_replays_carry_a_reference() -> None:
    assets = WidgetAssets(MemoryBlobStore())
    hub = WidgetHub(scope="nb-1", assets=assets, move_threshold=1024)
    esm = "export default { render() {} };" + " " * 4096
    opened(hub, "any", {"_model_name": "AnyModel", "_esm": esm, "_css": ".x{}"})
    [replay] = hub.attach(Frame("f1", "alice", ("any",)))
    ref = replay.message["data"]["state"]["_esm"]
    assert ref.startswith(ASSET_REF_PREFIX)
    assert replay.message["data"]["state"]["_css"] == ".x{}"
    resolved = assets.resolve_module("nb-1", ref, None)
    assert resolved is not None and resolved[1] == esm.encode()
    # Another notebook was never offered it.
    assert assets.resolve_module("nb-2", ref, None) is None
    sha = parse_asset_ref(ref)
    assert sha is not None
    assert assets.fetch("nb-1", sha) == esm.encode()
    assert assets.fetch("nb-2", sha) is None


def make_env(tmp_path: Path, module: str, code: bytes, version: str | None = None) -> Path:
    env = tmp_path / "env"
    nbext = env / "share" / "jupyter" / "nbextensions" / module
    nbext.mkdir(parents=True)
    (nbext / "index.js").write_bytes(code)
    if version:
        lab = env / "share" / "jupyter" / "labextensions" / module
        lab.mkdir(parents=True)
        (lab / "package.json").write_text(json.dumps({"version": version}))
    return env


def test_environment_assets_are_read_once_and_offered_per_notebook(tmp_path: Path) -> None:
    env = make_env(tmp_path, "bqplot", b"define([], function(){ return {}; });", version="0.13.1")
    assets = WidgetAssets(FileBlobStore(tmp_path / "store"))
    entry, data = assets.resolve_module("nb-1", "bqplot", env)  # type: ignore[misc]
    assert data == b"define([], function(){ return {}; });"
    assert (entry.kind, entry.version) == ("environment", "0.13.1")
    # Bytes, not a path: changing the file afterwards changes nothing served.
    (env / "share/jupyter/nbextensions/bqplot/index.js").write_bytes(b"tampered")
    assert assets.fetch("nb-1", entry.sha256) == data
    assert assets.fetch("nb-2", entry.sha256) is None
    assert assets.resolve_module("nb-1", "missing-lib", env) is None


# ------------------------------------------------------------------ widget.asset from the kernel

INDEX = b"define(['./chunk'], function(c){ return c; });"
CHUNK = b"define([], function(){ return {v: 1}; });"


def asset_files(
    index: bytes = INDEX, chunk: bytes = CHUNK
) -> tuple[list[dict[str, Any]], list[bytes]]:
    return (
        [
            {"path": "index.js", "sha256": sha256_hex(index)},
            {"path": "chunk.js", "sha256": sha256_hex(chunk)},
        ],
        [index, chunk],
    )


def test_a_kernel_asset_is_stored_once_per_workspace_and_served_per_notebook() -> None:
    store = MemoryBlobStore()
    assets = WidgetAssets(store)
    ws = workspace_scope("org-1", "ws-1")
    nb1 = WidgetHub(scope="nb-1", assets=assets, asset_scope=ws)
    nb2 = WidgetHub(scope="nb-2", assets=assets, asset_scope=ws)
    entry = nb1.kernel_asset("bqplot", "0.13.1", *asset_files())
    assert store.get(ws, sha256_hex(INDEX)) == INDEX
    assert store.get("nb-1", sha256_hex(INDEX)) is None
    assert assets.resolve("nb-1", "bqplot", "0.13.1") == entry.sha256 == sha256_hex(INDEX)
    assert assets.fetch("nb-1", sha256_hex(CHUNK)) == CHUNK
    # Another notebook of the same workspace whose kernel never offered it.
    assert assets.resolve("nb-2", "bqplot", "0.13.1") is None
    assert assets.fetch("nb-2", sha256_hex(INDEX)) is None
    nb2.kernel_asset("bqplot", "0.13.1", *asset_files())
    assert assets.fetch("nb-2", sha256_hex(INDEX)) == INDEX
    found = assets.resolve_module("nb-1", "bqplot", None)
    assert found is not None and found[1] == INDEX


def test_resolve_prefers_the_exact_version_then_the_latest_offered() -> None:
    assets = WidgetAssets(MemoryBlobStore())
    hub = WidgetHub(scope="nb", assets=assets)
    old = hub.kernel_asset("lib", "1.0.0", *asset_files(b"old"))
    new = hub.kernel_asset("lib", "2.0.0", *asset_files(b"new"))
    assert assets.resolve("nb", "lib", "1.0.0") == old.sha256
    assert assets.resolve("nb", "lib", "^2.0.0") == new.sha256
    assert assets.resolve("nb", "lib") == new.sha256
    assert assets.resolve("nb", "other") is None


def test_resolve_answers_platform_bundles_for_every_notebook() -> None:
    assets = WidgetAssets(MemoryBlobStore())
    bundle = assets.add_platform_bundle("@alkera/widgets", "1.0.0", b"bundle")
    assert assets.resolve("any-notebook", "@alkera/widgets", "*") == bundle.sha256
    assert assets.resolve("any-notebook", "@jupyter-widgets/controls") is None


def _swap_hash(files: list[dict[str, Any]], segs: list[bytes]) -> tuple[Any, Any]:
    files[1]["sha256"] = sha256_hex(b"something else")
    return files, segs


def _path(path: str) -> Any:
    def change(files: list[dict[str, Any]], segs: list[bytes]) -> tuple[Any, Any]:
        files[1]["path"] = path
        return files, segs

    return change


@pytest.mark.parametrize(
    ("module", "change", "message"),
    [
        pytest.param("bqplot", _swap_hash, "does not match its hash", id="bytes-differ-from-hash"),
        pytest.param("bqplot", _path("../../etc/x.js"), "not a path", id="path-walks-out"),
        pytest.param("bqplot", _path("/abs.js"), "not a path", id="absolute-path"),
        pytest.param("bqplot", lambda f, s: (f[1:], s[1:]), "no index.js", id="no-entry-point"),
        pytest.param("bqplot", lambda f, s: (f, s[:1]), "differ in number", id="missing-segment"),
        pytest.param("@jupyter-widgets/base", None, "provided by the platform", id="platform-name"),
        pytest.param("../x", None, "not a widget module name", id="bad-module-name"),
    ],
)
def test_a_kernel_asset_that_does_not_check_out_is_refused(
    module: str, change: Any, message: str
) -> None:
    store = MemoryBlobStore()
    assets = WidgetAssets(store)
    hub = WidgetHub(scope="nb", assets=assets)
    files, segs = asset_files()
    if change is not None:
        files, segs = change(files, segs)
    with pytest.raises(AssetRefusedError, match=message):
        hub.kernel_asset(module, "1.0.0", files, segs)
    assert assets.resolve("nb", module) is None
    assert assets.fetch("nb", sha256_hex(INDEX)) is None


@pytest.mark.parametrize(
    "module",
    [
        pytest.param("@jupyter-widgets/base", id="platform-base"),
        pytest.param("@alkera/widgets", id="platform-manager"),
        pytest.param("anywidget", id="platform-anywidget"),
    ],
)
def test_a_platform_name_is_never_taken_from_an_environment(tmp_path: Path, module: str) -> None:
    env = make_env(tmp_path, module, b"evil")
    assets = WidgetAssets(MemoryBlobStore())
    with pytest.raises(AssetRefusedError):
        assets.offer_environment_module("nb", env, module)
    assert assets.resolve_module("nb", module, env) is None


@pytest.mark.parametrize("module", ["../../etc", "a/../../b", "has space", ""])
def test_module_names_cannot_walk_out(tmp_path: Path, module: str) -> None:
    assets = WidgetAssets(MemoryBlobStore())
    with pytest.raises(AssetRefusedError):
        assets.offer_environment_module("nb", tmp_path, module)


def test_a_symlinked_nbextension_outside_the_environment_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "index.js").write_text("secret")
    env = tmp_path / "env"
    nbext = env / "share" / "jupyter" / "nbextensions"
    nbext.mkdir(parents=True)
    (nbext / "lib").symlink_to(outside)
    with pytest.raises(AssetRefusedError):
        WidgetAssets(MemoryBlobStore()).offer_environment_module("nb", env, "lib")


def test_platform_bundle_from_the_built_dist(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    code = b"var AlkeraWidgets={};"
    import hashlib

    (dist / "alkera-widgets.js").write_bytes(code)
    manifest = {
        "name": "@alkera/widgets",
        "version": "0.1.0",
        "file": "alkera-widgets.js",
        "sha256": hashlib.sha256(code).hexdigest(),
    }
    (dist / "manifest.json").write_text(json.dumps(manifest))
    assets = WidgetAssets(MemoryBlobStore())
    entry = assets.load_platform_dist(dist)
    assert assets.resolve_module("any-notebook", "@alkera/widgets", None) == (entry, code)
    assert assets.fetch("any-notebook", entry.sha256) == code
    (dist / "alkera-widgets.js").write_bytes(b"changed")
    with pytest.raises(AssetRefusedError):
        assets.load_platform_dist(dist)
    with pytest.raises(AssetRefusedError):
        assets.add_platform_bundle("bqplot", "1", b"x")


def test_file_blob_store_refuses_a_blob_that_changed(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    sha = store.put("nb", b"abc")
    assert store.get("nb", sha) == b"abc"
    assert store.get("other", sha) is None
    [blob] = [p for p in tmp_path.rglob(sha)]
    blob.write_bytes(b"xyz")
    assert store.get("nb", sha) is None
    assert store.get("nb", "../../x") is None
