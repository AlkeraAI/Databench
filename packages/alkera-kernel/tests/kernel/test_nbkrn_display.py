"""Display: the IPython protocol, the built-in registry, caps, widgets."""

from __future__ import annotations

import json
from typing import Any

import pytest
from nbkrn_harness import KernelFactory, KernelSession, PageTable, step


@pytest.fixture
async def rich(start_kernel: KernelFactory, rich_python: str) -> KernelSession:
    return await start_kernel(interpreter=rich_python)


def _only(result: Any, cell: str) -> dict[str, Any]:
    (bundle,) = result.outputs(cell)
    return bundle


async def test_nbkrn_mimebundle_wins_over_repr_html(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "class Both:\n"
        "    def _repr_mimebundle_(self, include=None, exclude=None):\n        return {'text/mark"
        "down': '**mb**'}\n"
        "    def _repr_html_(self):\n        return '<b>html</b>'\n"
        "    def _mime_(self):\n        return ('text/html', '<i>mime</i>')\n"
        "Both()"
    )
    bundle = _only(await ks.run(step("a", code)), "a")
    assert bundle["text/markdown"] == "**mb**"
    assert "text/html" not in bundle
    assert bundle["text/plain"].startswith("<__main__.Both object")


async def test_nbkrn_mime_method_before_repr_methods(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "class M:\n    def _mime_(self):\n        return ('text/html', '<i>mime</i>')\n    def _r"
        "epr_html_(self):\n        return '<b>no</b>'\nM()"
    )
    assert _only(await ks.run(step("a", code)), "a")["text/html"] == "<i>mime</i>"


async def test_nbkrn_repr_methods_and_binary_images(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "class R:\n    def _repr_html_(self):\n        return '<p>h</p>'\n"
        "    def _repr_png_(self):\n        return b'\\x89PNG'\n    def _repr_latex_(self):\n    "
        "    return '$x$'\nR()"
    )
    bundle = _only(await ks.run(step("a", code)), "a")
    assert bundle["text/html"] == "<p>h</p>"
    assert bundle["image/png"] == "iVBORw=="
    assert bundle["text/latex"] == "$x$"


async def test_nbkrn_bare_display_appends_each_value(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "display(1, 'two')\ndisplay([3])\nNone"))
    assert [b["text/plain"] for b in result.outputs("a")] == ["1", "'two'", "[3]"]
    assert all(p["mode"] == "append" for p in result.of("cell.output", "a"))


async def test_nbkrn_structured_output_over_the_cap_is_a_placeholder(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = (
        "class Big:\n    def _repr_html_(self):\n        return '<p>' + 'x' * (9 * 1024 * 1024) +"
        " '</p>'\ndisplay('small')\nBig()"
    )
    result = await ks.run(step("a", code))
    small, big = result.outputs("a")
    assert small["text/plain"] == "'small'"
    assert "text/html" not in big
    placeholder = big["application/vnd.alkera.placeholder+json"]
    assert placeholder["reason"] == "too_large" and placeholder["mimetypes"] == ["text/html"]


async def test_nbkrn_pandas_frame_as_alkera_table(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    bundle = _only(
        await ks.run(
            step("a", "import pandas as pd\npd.DataFrame({'a': range(120), 'b': ['x'] * 120})")
        ),
        "a",
    )
    table = bundle["application/vnd.alkera.table+json"]
    assert table["total_rows"] == 120
    assert len(table["rows"]) == 50
    assert (
        table["schema"] == [{"name": "a", "type": "int64"}, {"name": "b", "type": "object"}]
        or table["schema"][0]["name"] == "a"
    )
    assert "<table" in bundle["text/html"]


async def test_nbkrn_registry_never_imports_a_library(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(
        step(
            "a",
            "import sys\nprint(1)\n[m for m in ('pandas', 'polars', 'matplotlib', 'PIL') if m in "
            "sys.modules]",
        )
    )
    assert result.outputs("a")[0]["text/plain"] == "[]"


async def test_nbkrn_polars_frame(rich: KernelSession) -> None:
    bundle = _only(
        await rich.run(step("a", "import polars as pl\npl.DataFrame({'a': list(range(70))})")), "a"
    )
    table = bundle["application/vnd.alkera.table+json"]
    assert table["total_rows"] == 70 and len(table["rows"]) == 50
    assert table["schema"] == [{"name": "a", "type": "Int64"}]


async def test_nbkrn_matplotlib_trailing_axes_and_show(rich: KernelSession) -> None:
    result = await rich.run(
        step(
            "a",
            "import matplotlib.pyplot as plt\nfig, ax = plt.subplots(figsize=(4, 3), dpi=50)\nax."
            "plot([1, 2, 3])\nax",
        ),
        step(
            "b",
            "import matplotlib.pyplot as plt\nplt.plot([3, 2, 1])\nplt.show()\nlen(plt.get_fignum"
            "s())",
        ),
        step("c", "import matplotlib\nmatplotlib.get_backend()"),
    )
    assert result.status == "ok", result.events
    axes = _only(result, "a")
    assert axes["image/png"].startswith("iVBOR")
    import base64
    import struct

    png = base64.b64decode(axes["image/png"])
    width, _height = struct.unpack(">II", png[16:24])
    assert width >= 4 * 50 * 2 * 0.8  # rendered at twice the figure's DPI
    shown, count = result.outputs("b")
    assert shown["image/png"].startswith("iVBOR")
    assert count["text/plain"] == "0"  # figures are closed after rendering
    assert _only(result, "c")["text/plain"].lower() == "'agg'"


async def test_nbkrn_plotly_and_altair_without_network(rich: KernelSession) -> None:
    result = await rich.run(
        step("p", "import plotly.graph_objects as go\ngo.Figure(go.Bar(y=[1, 2]))"),
        step(
            "v",
            "import altair as alt, pandas as pd\nalt.Chart(pd.DataFrame({'x': [1]})).mark_point()"
            ".encode(x='x')",
        ),
    )
    plotly = _only(result, "p")
    assert plotly["application/vnd.plotly.v1+json"]["data"][0]["type"] == "bar"
    vega = _only(result, "v")
    (mime,) = [k for k in vega if k.startswith("application/vnd.vegalite")]
    assert (
        vega[mime]["mark"]["type"] == "point"
        or vega[mime]["mark"] == "point"
        or "point" in json.dumps(vega[mime]["mark"])
    )
    for bundle in (plotly, vega):
        text = json.dumps(bundle)
        assert "cdn." not in text and "<script src" not in text


async def test_nbkrn_pil_image(rich: KernelSession) -> None:
    bundle = _only(
        await rich.run(step("a", "from PIL import Image\nImage.new('RGB', (3, 2), 'red')")), "a"
    )
    assert bundle["image/png"].startswith("iVBOR")


async def test_nbkrn_marimo_md_and_ui_elements(rich: KernelSession) -> None:
    result = await rich.run(
        step("m", "import marimo as mo\nmo.md('# Title')"), step("s", "mo.ui.slider(1, 10)")
    )
    assert "Title" in _only(result, "m")["text/html"]
    error = _only(result, "s")["application/vnd.alkera.error+json"]
    assert error["evalue"] == "mo.ui.slider is not available in Alkera; use alkera.ui.slider"


def _comm_events(ks: KernelSession, kind: str) -> list[dict[str, Any]]:
    return [p for m, p in ks.events if m == kind]


async def test_nbkrn_ipywidgets_open_comms_with_the_cell_parent(rich: KernelSession) -> None:
    result = await rich.run(step("w", "import ipywidgets as w\ns = w.IntSlider(value=3)\ns"))
    bundle = _only(result, "w")
    view = bundle["application/vnd.jupyter.widget-view+json"]
    opens = _comm_events(rich, "comm.open")
    model = [o for o in opens if o["comm_id"] == view["model_id"]]
    assert model and model[0]["content"]["data"]["state"]["value"] == 3
    assert model[0]["parent_msg_id"] == f"{result.run_id}:w"


async def test_nbkrn_a_comm_open_keeps_the_metadata_its_library_sent(rich: KernelSession) -> None:
    code = (
        "import comm\n"
        "c = comm.create_comm(target_name='t', data={'a': 1}, metadata={'version': '9.8.7'})\n"
        "c.send({'b': 2}, metadata={'note': 'kept'})\n"
        "c.send({'c': 3})\n"
        "c.comm_id"
    )
    result = await rich.run(step("m", code))
    comm_id = eval(_only(result, "m")["text/plain"])
    [opened] = [p for p in _comm_events(rich, "comm.open") if p["comm_id"] == comm_id]
    assert opened["metadata"] == {"version": "9.8.7"}
    first, second = [p for p in _comm_events(rich, "comm.msg") if p["comm_id"] == comm_id]
    assert first["metadata"] == {"note": "kept"}
    assert "metadata" not in second


async def test_nbkrn_widget_change_from_the_frontend_runs_observers(rich: KernelSession) -> None:
    await rich.run(
        step(
            "w",
            "import ipywidgets as w\ns = w.IntSlider(value=3)\nseen = []\ns.observe(lambda ch: se"
            "en.append(ch['new']), 'value')\ns.model_id",
        )
    )
    model_id = eval([p for m, p in rich.events if m == "cell.output"][-1]["output"]["text/plain"])
    rich.service.scope.begin("widget-run-1")
    await rich.request(
        "comm.deliver",
        {
            "run_id": "widget-run-1",
            "msg_id": "front-1",
            "msg": {
                "msg_type": "comm_msg",
                "content": {
                    "comm_id": model_id,
                    "data": {"method": "update", "state": {"value": 7}, "buffer_paths": []},
                },
            },
        },
    )
    delivered = await rich.finish("widget-run-1")
    assert delivered.status == "ok", delivered.events
    assert {"msg_id": "front-1"} in _comm_events(rich, "comm.idle")
    echoes = [p for p in _comm_events(rich, "comm.msg") if p["parent_msg_id"] == "front-1"]
    assert echoes, (
        "the kernel's answer is tagged with the frontend message",
        delivered.events,
        rich.events[-8:],
    )
    probe = await rich.run(step("p", "seen"))
    assert _only(probe, "p")["text/plain"] == "[7]"


async def test_nbkrn_output_widget_captures_prints_and_displays(rich: KernelSession) -> None:
    code = (
        "import ipywidgets as w\nout = w.Output()\nwith out:\n    print('inside')\n    display('s"
        "hown')\nprint('outside')\nout.model_id"
    )
    result = await rich.run(step("o", code))
    assert result.stdout("o") == "outside\n"
    model_id = eval(_only(result, "o")["text/plain"])
    updates = [
        p["content"]["data"]["state"]
        for p in _comm_events(rich, "comm.msg")
        if p["comm_id"] == model_id
    ]
    outputs = [u["outputs"] for u in updates if "outputs" in u][-1]
    assert {"output_type": "stream", "name": "stdout", "text": "inside\n"} in outputs
    assert any(
        o["output_type"] == "display_data" and o["data"]["text/plain"] == "'shown'" for o in outputs
    )


async def test_nbkrn_interact_output_is_captured(rich: KernelSession) -> None:
    code = "from ipywidgets import interact\ndef f(x):\n    print('x is', x)\n_ = interact(f, x=5)"
    result = await rich.run(step("i", code))
    assert result.stdout("i") == ""
    texts = json.dumps([p["content"] for p in _comm_events(rich, "comm.msg")])
    assert "x is 5" in texts


async def test_nbkrn_sensitive_models_are_flagged(rich: KernelSession) -> None:
    code = (
        "import ipywidgets as w\npw = w.Password(value='hunter2')\nplain = w.Text(value='hi')\n"
        "class Secret(w.Text):\n    _sensitive = True\nsec = Secret(value='s')\n"
        "(pw.model_id, plain.model_id, sec.model_id)"
    )
    result = await rich.run(step("s", code))
    pw_id, plain_id, sec_id = eval(_only(result, "s")["text/plain"])
    flagged = {p["comm_id"] for p in _comm_events(rich, "comm.open") if p.get("sensitive")}
    assert pw_id in flagged and sec_id in flagged
    assert plain_id not in flagged
    await rich.run(step("t", "pw.value = 'changed'"))
    later = [p for p in _comm_events(rich, "comm.msg") if p["comm_id"] == pw_id]
    assert later and all(p.get("sensitive") for p in later)


async def test_nbkrn_ui_bindings_name_the_globals_of_reactive_widgets(rich: KernelSession) -> None:
    code = (
        "import sys, ipywidgets as w\nhost = sys.modules['_alkera_runtime'].host\n"
        "s = host_s = w.IntSlider()\nhost.register_reactive(s)\nplain = w.IntSlider()\n"
        "(s.model_id, plain.model_id)"
    )
    result = await rich.run(step("b", code))
    reactive_id, plain_id = eval(_only(result, "b")["text/plain"])
    (bindings,) = result.of("ui.bindings")
    assert bindings == {
        "run_id": result.run_id,
        "cell_id": "b",
        "bindings": {reactive_id: ["host_s", "s"]},
    }
    assert plain_id not in bindings["bindings"]
    other = await rich.run(step("c", "x = 1"))
    assert [b["bindings"] for b in other.of("ui.bindings")] == [{reactive_id: ["host_s", "s"]}]


async def test_nbkrn_host_comms_round_trip_without_the_comm_package(
    start_kernel: KernelFactory,
) -> None:
    """``alkera.ui`` opens comms through the host: the open, the model's
    sends and a frontend delivery all travel, with no ipywidgets or comm."""
    ks = await start_kernel(settings={"dataframe": "polars", "args": {"n": "3"}})
    code = (
        "import sys\nhost = sys.modules['_alkera_runtime'].host\nseen = []\n"
        "c = host.open_comm('jupyter.widget', {'state': {'value': 1, '_sensitive': True}}, "
        "{'version': '2.1.0'}, seen.append)\n"
        "c.send({'method': 'update', 'state': {'value': 2}}, [b'buf'])\n"
        "(c.comm_id, host.settings())"
    )
    result = await ks.run(step("u", code))
    comm_id, settings = eval(result.outputs("u")[0]["text/plain"])
    assert settings == {"dataframe": "polars", "args": {"n": "3"}}
    (opened,) = [p for m, p in ks.events if m == "comm.open"]
    assert opened["content"] == {
        "comm_id": comm_id,
        "target_name": "jupyter.widget",
        "data": {"state": {"value": 1, "_sensitive": True}},
    }
    assert opened["metadata"] == {"version": "2.1.0"}
    assert opened["sensitive"] is True
    (sent,) = [p for m, p in ks.events if m == "comm.msg"]
    assert sent["content"]["data"]["state"] == {"value": 2}
    assert [b.data for b in sent["buffers"]] == [b"buf"]
    ks.service.scope.begin("w1")
    await ks.request(
        "comm.deliver",
        {
            "run_id": "w1",
            "msg_id": "f1",
            "msg": {
                "msg_type": "comm_msg",
                "content": {
                    "comm_id": comm_id,
                    "data": {"method": "update", "state": {"value": 9}},
                },
            },
        },
    )
    await ks.finish("w1")
    probe = await ks.run(step("p", "[m['content']['data']['state'] for m in seen]"))
    assert probe.outputs("p")[0]["text/plain"] == "[{'value': 9}]"
    closing = await ks.run(step("x", "c.close()\nc.send({'late': True})"))
    assert closing.status == "ok"
    assert [p["comm_id"] for m, p in ks.events if m == "comm.close"] == [comm_id]
    assert not any(p["content"]["data"].get("late") for m, p in ks.events if m == "comm.msg")


TABLE = "application/vnd.alkera.table+json"
FRAMES = (
    "import pandas as pd\nsmall = pd.DataFrame({'a': range(3)})\n"
    "big = pd.DataFrame({'a': range(80)})\n"
)


@pytest.mark.parametrize(
    ("code", "name", "total"),
    [
        pytest.param(FRAMES + "big", "big", 80, id="bare-name"),
        pytest.param(FRAMES + "alias = big\nalias", "alias", 80, id="second-name"),
        pytest.param(FRAMES + "df = small\ndf = big\ndf", "df", 80, id="reassigned-in-cell"),
        pytest.param(FRAMES + "(big)", "big", 80, id="parenthesized"),
        pytest.param(FRAMES + "big.head(5)", "@a/0", 5, id="method-call"),
        pytest.param(FRAMES + "big[big.a > 70]", "@a/0", 9, id="filter-expression"),
        pytest.param(FRAMES + "display(big)\nNone", "@a/0", 80, id="display-call"),
        pytest.param(FRAMES + "big['a']", None, 80, id="series"),
        pytest.param(FRAMES + "s = big['a']\ns", None, 80, id="named-series"),
        pytest.param(FRAMES + "def f():\n    return big\nf()", "@a/0", 80, id="returned"),
    ],
)
async def test_nbkrn_table_source_names_where_the_frame_can_be_paged_from(
    start_kernel: KernelFactory, code: str, name: str | None, total: int
) -> None:
    """A frame a global names is paged by that name; any other shown frame by
    a handle the kernel keeps it under; a series by nothing."""
    ks = await start_kernel()
    table = _only(await ks.run(step("a", code)), "a")[TABLE]
    assert table["total_rows"] == total
    if name is None:
        assert "source" not in table
    else:
        assert table["source"] == {"name": name}


async def test_nbkrn_a_shown_frame_no_global_names_pages_whole(
    start_kernel: KernelFactory,
) -> None:
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    code = FRAMES + "display(small)\ndisplay(big[big.a >= 10])\nNone"
    first, second = (await ks.run(step("a", code))).outputs("a")
    assert first[TABLE]["source"] == {"name": "@a/0"}
    assert second[TABLE]["source"] == {"name": "@a/1"}
    page = await ks.request("inspect.frame", {"name": "@a/1", "offset": 60, "limit": 100})
    assert page["total_rows"] == 70
    assert [list(r) for r in PageTable(page["table"]).rows] == [[i] for i in range(70, 80)]
    sorted_page = await ks.request(
        "inspect.frame",
        {"name": "@a/1", "offset": 0, "limit": 2, "sort": [{"column": "a", "descending": True}]},
    )
    assert [list(r) for r in PageTable(sorted_page["table"]).rows] == [[79], [78]]


async def test_nbkrn_a_handle_dies_when_its_cell_runs_again(start_kernel: KernelFactory) -> None:
    """The kernel holds a shown frame only while its output stands, and a
    handle of one cell never reads another's."""
    ks = await start_kernel()
    await ks.run(step("a", FRAMES + "big.head(5)"))
    await ks.run(step("a", "1"))
    with pytest.raises(Exception, match="no longer in the kernel"):
        await ks.request("inspect.frame", {"name": "@a/0", "offset": 0, "limit": 5})
    with pytest.raises(Exception, match="no longer in the kernel"):
        await ks.request("inspect.frame", {"name": "@b/0", "offset": 0, "limit": 5})


async def test_nbkrn_table_source_pages_the_value_shown(start_kernel: KernelFactory) -> None:
    """The name a table output carries pages the same frame through
    ``inspect.frame``, including after the name was rebound in the cell."""
    pytest.importorskip("duckdb")
    ks = await start_kernel()
    table = _only(await ks.run(step("a", FRAMES + "df = small\ndf = big\ndf")), "a")[TABLE]
    page = await ks.request(
        "inspect.frame", {"name": table["source"]["name"], "offset": 70, "limit": 100}
    )
    assert page["total_rows"] == table["total_rows"] == 80
    assert [list(r) for r in PageTable(page["table"]).rows] == [[i] for i in range(70, 80)]


async def test_nbkrn_table_source_on_a_polars_frame(rich: KernelSession) -> None:
    code = "import polars as pl\nframe = pl.DataFrame({'a': list(range(60))})\nframe"
    table = _only(await rich.run(step("a", code)), "a")[TABLE]
    assert table["source"] == {"name": "frame"} and table["total_rows"] == 60
