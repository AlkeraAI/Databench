"""``alkera.sql`` and ``alkera.ui`` in a real Alkera kernel (a subprocess
over the real RPC), against the engine's SQL broker and widget hub."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from nbsqw_support import make_db

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "alkera-kernel" / "tests" / "kernel"))
from alkera_notebook.rpc import Call
from alkera_notebook.sql.broker import RunInfo, SqlBroker, SqlKernelContext
from alkera_notebook.sql.provider import SqlProviderRegistry, SqlWorkspace
from alkera_notebook.sql.providers.fake import FakeConnectionProvider
from alkera_notebook.widgets import REDACTED, Frame, WidgetHub
from nbkrn_harness import KernelSession, kernel_mount, start_kernel, step

__all__ = ["kernel_mount", "start_kernel"]


class Who:
    kind = "person"
    id = "alice"


async def sql_kernel(
    start_kernel: Any, tmp_path: Path, rows: int = 100, inline_limit: int = 16 * 1024 * 1024
) -> tuple[KernelSession, Path]:
    db = make_db(tmp_path / "shop.duckdb", rows=rows)
    broker = SqlBroker(
        SqlProviderRegistry([FakeConnectionProvider({"shop": db})]), inline_limit=inline_limit
    )
    holder: dict[str, KernelSession] = {}
    data_dir = tmp_path / "data"

    def runs(run_id: str) -> RunInfo | None:
        ks = holder["ks"]
        return RunInfo(run_id, Who(), "n.alknb.py") if ks.service.scope.is_active(run_id) else None

    ctx = SqlKernelContext(
        kernel_id="k-test",
        workspace=SqlWorkspace(id="w", root=str(tmp_path)),
        codecs=frozenset({"arrow.ipc.stream", "arrow.ipc.file", "rows.json"}),
        data_dir=data_dir,
        runs=runs,
    )

    async def sql_execute(call: Call) -> Any:
        return await broker.execute(ctx, call.params, call.extra)

    ks = await start_kernel(
        methods={"sql.execute": sql_execute},
        run_scoped=["sql.execute"],
        settings={"dataframe": "pandas"},
    )
    holder["ks"] = ks
    return ks, data_dir


async def test_alkera_sql_with_a_connection_returns_a_frame(
    start_kernel: Any, tmp_path: Path
) -> None:
    ks, _ = await sql_kernel(start_kernel, tmp_path)
    result = await ks.run_code(
        "import alkera._sql as s\n"
        "q = 'select count(*) as n, sum(amount) as total from orders'\n"
        "df = s.sql(q, connection='shop', output=False)\n"
        "print(type(df).__name__, int(df['n'][0]), float(df['total'][0]))\n"
    )
    assert result.status == "ok", result.events
    assert result.stdout("c1").strip() == f"DataFrame 100 {sum(i * 1.5 for i in range(100))}"


async def test_a_sql_cells_result_pages_and_sorts_past_its_preview(
    start_kernel: Any, tmp_path: Path
) -> None:
    """A SQL cell's table shows 50 rows of the whole result; the rest is read
    from the kernel through the handle the table names, exactly as a Python
    frame's is, even though the cell's own variable is private to it."""
    ks, _ = await sql_kernel(start_kernel, tmp_path, rows=300)
    result = await ks.run_code(
        "import alkera._sql as s\n"
        "_df = s.sql('select id, amount from orders order by id', connection='shop')\n"
    )
    assert result.status == "ok", result.events
    (bundle,) = result.outputs("c1")
    table = bundle["application/vnd.alkera.table+json"]
    assert (len(table["rows"]), table["total_rows"]) == (50, 300)
    handle = table["source"]["name"]
    last = await ks.request("inspect.frame", {"name": handle, "offset": 290, "limit": 50})
    assert last["total_rows"] == 300
    assert [r[0] for r in last["table"]["rows"]] == list(range(290, 300))
    top = await ks.request(
        "inspect.frame",
        {"name": handle, "offset": 0, "limit": 3, "sort": [{"column": "id", "descending": True}]},
    )
    assert [r[0] for r in top["table"]["rows"]] == [299, 298, 297]


async def test_a_large_result_is_read_from_the_data_dir_and_removed(
    start_kernel: Any, tmp_path: Path
) -> None:
    ks, data_dir = await sql_kernel(start_kernel, tmp_path, rows=100_000, inline_limit=64 * 1024)
    result = await ks.run_code(
        "import alkera._sql as s\n"
        "df = s.sql('select * from orders', connection='shop', output=False)\n"
        "print(len(df))\n"
    )
    assert result.status == "ok", result.events
    assert result.stdout("c1").strip() == "100000"
    assert os.listdir(data_dir) == []


async def test_an_unknown_connection_fails_the_cell_with_its_name(
    start_kernel: Any, tmp_path: Path
) -> None:
    ks, _ = await sql_kernel(start_kernel, tmp_path)
    result = await ks.run_code("import alkera._sql as s\ns.sql('select 1', connection='nope')\n")
    assert result.status == "error"
    error = result.finished("c1").get("error") or {}
    assert "no connection named 'nope'" in str(error)


async def test_outside_a_run_the_kernel_cannot_query(start_kernel: Any, tmp_path: Path) -> None:
    """A thread left running after its cell finished asks after the run ended.

    The thread waits for a file the test writes once the run has ended, and
    reports through another file, so no cell runs while it asks: a cell the
    test ran to read the answer would be a run the query could land in."""
    ks, _ = await sql_kernel(start_kernel, tmp_path)
    go, answer = tmp_path / "go", tmp_path / "answer"
    first = await ks.run_code(
        "import os, threading, time, alkera._sql as s\n"
        "def later():\n"
        f"    while not os.path.exists({str(go)!r}):\n"
        "        time.sleep(0.01)\n"
        "    try:\n"
        "        s.sql('select 1', connection='shop', output=False)\n"
        "        said = 'ran'\n"
        "    except Exception as e:\n"
        "        said = type(e).__name__ + ': ' + str(e)\n"
        f"    with open({str(answer) + '.part'!r}, 'w') as f:\n"
        "        f.write(said)\n"
        f"    os.replace({str(answer) + '.part'!r}, {str(answer)!r})\n"
        "threading.Thread(target=later).start()\n"
    )
    assert first.status == "ok"
    ks.service.scope.end(first.run_id)
    go.write_text("", encoding="utf-8")
    await ks.wait_for(answer.exists)
    said = answer.read_text(encoding="utf-8")
    assert "outside_run" in said or "only during a run" in said, said


async def test_an_alkera_ui_slider_round_trip_through_the_hub(start_kernel: Any) -> None:
    ks = await start_kernel()
    hub = WidgetHub(scope="nb")
    result = await ks.run_code(
        "import alkera.ui as ui\nrows = ui.slider(0, 10, value=3, label='Rows')\nrows\n"
    )
    assert result.status == "ok", result.events
    opens = [p for m, p in ks.events if m == "comm.open"]
    assert len(opens) == 1
    comm_id = opens[0]["comm_id"]
    for p in opens:
        hub.kernel_open(p["comm_id"], p["content"], [], p.get("metadata"))
    view = [
        o
        for o in result.outputs("c1")
        if "application/vnd.jupyter.widget-view+json" in o.get("data", o)
    ]
    assert view, result.outputs("c1")
    bindings = [p for m, p in ks.events if m == "ui.bindings"]
    assert bindings and bindings[-1]["bindings"] == {comm_id: ["rows"]}
    [replay] = hub.attach(Frame("f1", "alice", (comm_id,)))
    assert replay.message["data"]["state"]["value"] == 3
    # A frame moves the slider past its end; the kernel keeps 10 and says so.
    delivery = hub.frontend_send(
        "f1",
        comm_id,
        "m-1",
        {"comm_id": comm_id, "data": {"method": "update", "state": {"value": 99}}},
        [],
        client_id="alice",
    )
    before = len(ks.events)
    await ks.request(
        "comm.deliver", {"run_id": "w-1", "msg_id": delivery.msg_id, "msg": delivery.msg}
    )
    await ks.wait_for(lambda: any(m == "comm.idle" for m, _ in ks.events[before:]))
    for m, p in ks.events[before:]:
        if m == "comm.msg":
            hub.kernel_msg(p["comm_id"], p["content"], [], p.get("parent_msg_id"))
        elif m == "comm.idle":
            hub.kernel_idle(p["msg_id"])
    out = hub.drain()
    states = [o.message["content"]["data"]["state"] for o in out if o.message["type"] == "comm.msg"]
    assert states == [{"value": 10}]
    assert [o.message for o in out if o.message["type"] == "comm.status"] == [
        {"type": "comm.status", "msg_id": "m-1", "execution_state": "idle"}
    ]
    assert hub.model(comm_id).state["value"] == 10  # type: ignore[union-attr]
    check = await ks.run_code("print(rows.value)", cell_id="c2")
    assert check.stdout("c2").strip() == "10"


async def test_a_password_from_the_kernel_is_flagged_and_redacted_by_the_hub(
    start_kernel: Any,
) -> None:
    ks = await start_kernel()
    await ks.run_code("import alkera.ui as ui\npw = ui.text('preset', kind='password')\npw\n")
    [opened] = [p for m, p in ks.events if m == "comm.open"]
    hub = WidgetHub(scope="nb")
    hub.kernel_open(opened["comm_id"], opened["content"], [], opened.get("metadata"))
    [replay] = hub.attach(Frame("f1", "bob", (opened["comm_id"],)))
    assert replay.message["data"]["state"]["value"] == REDACTED


@pytest.mark.parametrize(
    "kind", [pytest.param("checkbox", id="checkbox"), pytest.param("dropdown", id="dropdown")]
)
async def test_other_elements_open_in_the_kernel(start_kernel: Any, kind: str) -> None:
    ks = await start_kernel()
    code = {"checkbox": "ui.checkbox(True)", "dropdown": "ui.dropdown(['a', 'b'], value='b')"}[kind]
    result = await ks.run_code(f"import alkera.ui as ui\nel = {code}\nel\n")
    assert result.status == "ok"
    [opened] = [p for m, p in ks.events if m == "comm.open"]
    state = opened["content"]["data"]["state"]
    assert state["_model_module"] == "@alkera/ui-widgets"
    assert state["value"] in (True, "b")


# -- the runtime module, bound before any cell runs --------------------------


async def test_a_markdown_cell_runs_in_a_notebook_that_never_imported_alkera(
    start_kernel: Any,
) -> None:
    """A Markdown cell is a call into ``alkera`` (``alkera.md``); a fresh
    notebook made with no setup import still runs it."""
    from alkera_notebook.format import render_cell

    ks = await start_kernel()
    result = await ks.run_code(render_cell("markdown", "# Revenue\n\nBy *week*.", {}))
    assert result.status == "ok", result.events
    (shown,) = result.outputs("c1")
    assert "<h1" in str(shown) and "Revenue" in str(shown)


async def test_a_sql_cell_runs_in_a_notebook_that_never_imported_alkera(
    start_kernel: Any, tmp_path: Path
) -> None:
    from alkera_notebook.format import render_cell

    ks, _ = await sql_kernel(start_kernel, tmp_path)
    code = render_cell(
        "sql",
        "select count(*) as n from orders",
        {"output_var": "counted", "connection": "shop", "show_output": False},
    )
    result = await ks.run(step("c1", code), step("c2", "print(int(counted['n'][0]))"))
    assert result.status == "ok", result.events
    assert result.stdout("c2").strip() == "100"


async def test_alkera_ui_works_in_a_notebook_that_never_imported_alkera(
    start_kernel: Any,
) -> None:
    ks = await start_kernel()
    result = await ks.run_code("rows = alkera.ui.slider(0, 10, value=3)\nprint(rows.value)\n")
    assert result.status == "ok", result.events
    assert result.stdout("c1").strip() == "3"


async def test_a_cell_that_binds_alkera_shadows_it_until_its_names_are_cleared(
    start_kernel: Any,
) -> None:
    """The name is the notebook's to bind like any other: a cell assigning it
    wins for the cells after it, and once the engine clears that cell's names
    (the cell was deleted or changed), the runtime module shows through
    again."""
    ks = await start_kernel()
    first = await ks.run(step("c1", "alkera = 'mine'"), step("c2", "print(alkera)"))
    assert first.status == "ok", first.events
    assert first.stdout("c2").strip() == "mine"

    again = await ks.run(step("c3", "print(alkera.__name__)"), clear=["alkera"])
    assert again.status == "ok", again.events
    assert again.stdout("c3").strip() == "alkera"


SHADOW = "import pathlib\npathlib.Path(__file__).with_name('ran.txt').write_text('ran')\n"


async def test_a_file_named_alkera_beside_the_notebook_is_not_what_a_markdown_cell_runs(
    start_kernel: Any, tmp_path: Path
) -> None:
    """Nothing in a Markdown or SQL cell asks for an import, so the module
    behind the name is never one the notebook's folder supplies."""
    from alkera_notebook.format import render_cell

    folder = tmp_path / "nb"
    folder.mkdir()
    (folder / "alkera.py").write_text(SHADOW, encoding="utf-8")
    ks = await start_kernel(notebook_dir=folder)
    result = await ks.run_code(render_cell("markdown", "# Revenue", {}))
    assert result.status == "ok", result.events
    (shown,) = result.outputs("c1")
    assert "Revenue" in str(shown)
    assert not (folder / "ran.txt").exists()


async def test_a_cell_that_writes_the_import_itself_gets_python_s_own_rules(
    start_kernel: Any, tmp_path: Path
) -> None:
    folder = tmp_path / "nb"
    folder.mkdir()
    (folder / "alkera.py").write_text(SHADOW + "MINE = 7\n", encoding="utf-8")
    ks = await start_kernel(notebook_dir=folder)
    result = await ks.run_code("import alkera\nprint(alkera.MINE)\n")
    assert result.status == "ok", result.events
    assert result.stdout("c1").strip() == "7"
    assert (folder / "ran.txt").read_text(encoding="utf-8") == "ran"
