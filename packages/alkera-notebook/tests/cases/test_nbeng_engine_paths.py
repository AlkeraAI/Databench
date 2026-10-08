"""Engine paths beyond the catalogue: SQL requests from the kernel, inspection,
environment actions, activity, the graph view, frontiers and widget merges."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from alkera_notebook.document.ops import ReplaceCell
from alkera_notebook.engine import (
    AllTarget,
    CellsTarget,
    EnvAction,
    InspectQuery,
    KernelUnavailableError,
    NotFoundError,
    WidgetAction,
)
from nbeng_fakes import duck_provider, write_duck
from nbeng_harness import ANN, BOB, engine_for, notebook, run_cells, text_of


async def test_engine_sql_request_is_attributed_to_the_run_and_its_requester(
    tmp_path: Path,
) -> None:
    provider = duck_provider(tmp_path / "db", [1, 2, 3])
    async with engine_for(tmp_path, sql=provider, kernel="fake") as engine:
        session, _ann, (a,) = await notebook(
            engine, ['rows = sql("select v from t order by v")\nrows']
        )
        bob = session.attach(BOB)
        record = await run_cells(bob, a)
        assert record.status == "ok"
        assert await text_of(bob, a) == "[[1], [2], [3]]"
        # The run's requester, for attribution (never to widen access).
        assert provider.executed == [("Warehouse", BOB.id, record.run_id)]


@pytest.mark.parametrize(
    ("connection", "message"),
    [pytest.param("Personal", "no connection named 'Personal'", id="unknown")],
)
async def test_engine_sql_refusals_reach_the_cell(
    tmp_path: Path, connection: str, message: str
) -> None:
    from alkera_notebook.sql.providers.fake import FakeConnection

    db = write_duck(tmp_path / "mine.duckdb", [1])
    provider = duck_provider(tmp_path / "db", Mine=FakeConnection(db))
    async with engine_for(tmp_path, sql=provider, kernel="fake") as engine:
        _, ann, (a,) = await notebook(engine, [f'rows = sql("select 1", "{connection}")'])
        record = await run_cells(ann, a)
        assert record.status == "error"
        error = (await ann.output(a, "error")).error
        assert error is not None and message in error.evalue


async def test_engine_sql_row_limit_cuts_statements_without_their_own_limit(
    tmp_path: Path,
) -> None:
    from alkera_notebook.engine import SettingsChange

    provider = duck_provider(tmp_path / "db", list(range(10)))
    async with engine_for(tmp_path, sql=provider, kernel="fake") as engine:
        _, ann, (a, b) = await notebook(
            engine,
            ['n = len(sql("select v from t"))\nn', 'm = len(sql("select v from t limit 8"))\nm'],
        )
        settings = await ann.settings(SettingsChange(sql_row_limit=4))
        assert settings.sql_row_limit == 4
        await run_cells(ann, a, b)
        assert (await text_of(ann, a), await text_of(ann, b)) == ("4", "8")
        # The limit is the notebook's: written in its file, so it cuts the same
        # for everyone and after a restart.
        assert "# sql_row_limit = 4\n" in (tmp_path / "ws" / "nb.alknb.py").read_text()
        assert settings.sources["sql_row_limit"] == "notebook"


async def test_engine_sql_outside_a_run_is_refused(tmp_path: Path) -> None:
    provider = duck_provider(tmp_path / "db")
    code = (
        "import threading, time\n"
        "box = {}\n"
        "def later():\n"
        "    time.sleep(0.5)\n"
        "    try:\n"
        "        sql('select v from t')\n"
        "    except Exception as exc:\n"
        "        box['error'] = str(exc)\n"
        "threading.Thread(target=later).start()"
    )
    async with engine_for(tmp_path, sql=provider, kernel="fake") as engine:
        _, ann, (a, b) = await notebook(engine, [code, "box"])
        await run_cells(ann, a)
        await asyncio.sleep(1.0)  # the thread asks while no run executes
        await run_cells(ann, b)
        assert "only during a run" in await text_of(ann, b)
        assert provider.executed == []


async def test_engine_inspect_frames_values_and_cached_variables(tmp_path: Path) -> None:
    async with engine_for(tmp_path, kernel="fake") as engine:
        _, ann, (a,) = await notebook(engine, ["rows = [10, 20, 30]\ncount = 3"])
        with pytest.raises(KernelUnavailableError):
            await ann.inspect(InspectQuery(what="value", name="rows"))
        await run_cells(ann, a)
        page = await ann.inspect(InspectQuery(what="frame", name="rows", offset=1, limit=1))
        assert page.total_rows == 3 and page.table == {
            "schema": [{"name": "value", "type": "string"}],
            "rows": [["20"]],
            "total_rows": 3,
            "offset": 1,
        }
        value = await ann.inspect(InspectQuery(what="value", name="count"))
        assert value.summary == {"type": "int", "repr": "3"}
        cached = await ann.inspect(InspectQuery(what="variables", name="count"))
        assert [(v.name, v.repr, v.cell_id) for v in cached.variables or []] == [("count", "3", a)]
        with pytest.raises(NotFoundError):
            await ann.inspect(InspectQuery(what="value"))


async def test_engine_env_actions_on_a_fixed_environment(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, _ = await notebook(engine, ["x = 1"])
        info = await ann.env(EnvAction(action="info"))
        assert info.env is not None and info.env.state == "ready" and not info.env.recorded_in_file
        listed = await ann.env(EnvAction(action="list"))
        assert [e.env_id for e in listed.envs] == ["static"]
        assert (await ann.env(EnvAction(action="packages"))).packages == []
        assert (await ann.env(EnvAction(action="materialize"))).env is not None
        with pytest.raises(ValueError, match="install needs packages"):
            await ann.env(EnvAction(action="install"))


async def test_engine_activity_lists_runs_and_edits_since(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, ["x = 1 / 0"])
        start = engine.clock.now() - timedelta(seconds=1)
        await ann.apply([ReplaceCell(cell_id=a, source="x = 1 / 0\nx")], None)
        await run_cells(ann, a)
        entries = (await ann.activity(start)).entries
        assert [(e.kind, e.actor.id, e.cell_ids) for e in entries] == [
            ("cell_edit", ANN.id, [a]),
            ("cell_run", ANN.id, [a]),
        ]
        assert entries[1].error_class == "ZeroDivisionError" and entries[1].status == "error"
        assert (await ann.activity(engine.clock.now() + timedelta(seconds=5))).entries == []


async def test_engine_graph_view_directions(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, b, c) = await notebook(engine, ["x = 1", "y = x", "z = y"])
        both = await ann.graph(b)
        assert (both.upstream, both.downstream) == ([a], [c])
        assert (await ann.graph(c, "up")).upstream == [a, b]
        assert (await ann.graph(c, "up")).downstream == []
        assert (await ann.graph(a, "down")).downstream == [b, c]
        whole = await ann.graph()
        assert set(whole.edges) == {(a, b), (b, c)} and whole.cells[b].defs == ["y"]
        with pytest.raises(NotFoundError):
            await ann.graph("nope")


async def test_engine_run_waits_for_the_clients_frontier(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a,) = await notebook(engine, ["x = 1\nx"])
        bob = session.attach(BOB)
        result = await bob.apply([ReplaceCell(cell_id=a, source="x = 2\nx")], None)
        record = await (await ann.run(CellsTarget(ids=[a]), frontier=result.token)).wait(30)
        assert record.frontier == result.token
        assert await text_of(ann, a) == "2"


async def test_engine_consecutive_widget_updates_merge_while_queued(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(
            engine,
            [
                "import alkera.ui as ui\ns = ui.slider(0, 10, value=1)",
                "import time\ntime.sleep(0.5)",
            ],
        )
        await (await ann.run(CellsTarget(ids=[a]))).wait(30)
        mid = (await ann.widget(WidgetAction(action="list"))).widgets[0].model_id
        ann.attach_frame("f", mid)
        busy = await ann.run(CellsTarget(ids=[b]))
        for value in (2, 3, 4):
            await ann.comm_send(
                mid,
                f"msg-{value}",
                {"data": {"method": "update", "state": {"value": value}}},
                [],
                frame_id="f",
            )
        queued = [j for j in session.runtime.queue if j.kind == "comm"]
        assert len(queued) == 1
        assert queued[0].content["content"]["data"]["state"] == {"value": 4}
        assert queued[0].merged_msg_ids == ["msg-3", "msg-4"]
        await busy.wait(30)
        await asyncio.wait_for(queued[0].done, 10)
        widgets = (await ann.widget(WidgetAction(action="get", model_id=mid))).widgets
        assert widgets[0].value == 4
        # Every merged message's frame still learns it was handled.
        idles = []
        while len(ann.queue):
            e = await ann.next_event(1)
            if getattr(e, "type", "") == "frame.message" and e.message["type"] == "comm.status":  # type: ignore[union-attr]
                idles.append(e.message["msg_id"])  # type: ignore[union-attr]
        assert sorted(idles) == ["msg-2", "msg-3", "msg-4"]


async def test_engine_open_refuses_paths_outside_the_workspace(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        with pytest.raises(NotFoundError):
            await engine.open("../escape.alknb.py")
        with pytest.raises(NotFoundError):
            await engine.open(str(tmp_path / "elsewhere.alknb.py"))
        await (await notebook(engine, ["x = 1"]))[1].run(AllTarget())


async def test_a_notebook_s_settings_say_where_each_value_came_from(tmp_path: Path) -> None:
    """The file's own value, the workspace's default, detection (an env the
    file does not name) or the setting's default; a value set to null goes
    back to what it inherits."""
    from alkera_notebook.document.ops import SetSetting
    from alkera_notebook.engine import SettingsChange

    async with engine_for(tmp_path) as engine:
        engine.config.workspace_settings = {"dataframe": "pandas"}
        _, ann, _ = await notebook(engine, ["1"], settings={"reactivity": "lazy"})
        first = await ann.settings(SettingsChange())
        assert (first.reactivity, first.sources["reactivity"]) == ("lazy", "notebook")
        assert (first.dataframe, first.sources["dataframe"]) == ("pandas", "workspace")
        assert first.sources["env"] == "detected"
        assert (first.autoreload, first.sources["autoreload"]) == ("off", "default")
        await ann.apply([SetSetting(key="reactivity", value=None)], None)
        after = await ann.settings(SettingsChange())
        assert (after.reactivity, after.sources["reactivity"]) == ("autorun", "default")
        assert "reactivity" not in (tmp_path / "ws" / "nb.alknb.py").read_text()


async def test_a_workspace_layer_that_carries_sources_still_reads_settings(tmp_path: Path) -> None:
    """``sources`` describes the values and is never a setting: a workspace
    layer saved from a settings read (sources included) must not break every
    notebook tool with "got multiple values for keyword argument 'sources'"."""
    from alkera_notebook.engine import SettingsChange

    async with engine_for(tmp_path) as engine:
        engine.config.workspace_settings = {
            "dataframe": "pandas",
            "sources": {"dataframe": "notebook"},
        }
        _, ann, _ = await notebook(engine, ["1"])
        read = await ann.settings(SettingsChange())
        assert (read.dataframe, read.sources["dataframe"]) == ("pandas", "workspace")


@pytest.mark.parametrize(
    "layer",
    [
        pytest.param({"dataframe": "pandas", "sources": {"dataframe": "notebook"}}, id="sources"),
        pytest.param({"no_such_setting": 1}, id="unknown-key"),
    ],
)
def test_the_workspace_layer_names_notebook_settings_and_nothing_else(
    layer: dict[str, object], tmp_path: Path
) -> None:
    """A settings read's ``sources`` (or any other key) is never a workspace
    default: it broke every notebook tool with "got multiple values for
    keyword argument 'sources'"."""
    from alkera_notebook.engine.config import EngineConfig
    from pydantic import ValidationError

    roots = {name: str(tmp_path) for name in ("workspace_root", "env_root", "data_root")}
    with pytest.raises(ValidationError, match="not notebook settings") as refused:
        EngineConfig(**roots, workspace_settings=layer)
    assert refused.value.error_count() == 1
    kept = EngineConfig(**roots, workspace_settings={"dataframe": "pandas"})
    assert kept.setting_defaults() == {"dataframe": "pandas"}


def test_a_document_holds_only_notebook_settings() -> None:
    from alkera_notebook.document.model import Document

    with pytest.raises(ValueError, match="not notebook settings: sources"):
        Document(settings={"format": "1.0", "sources": {"dataframe": "notebook"}})
    assert Document(settings={"format": "1.0", "dataframe": "pandas"}).settings["dataframe"] == (
        "pandas"
    )


@pytest.mark.parametrize(
    ("read", "stored"),
    [
        pytest.param(
            {
                "format": "1.0",
                "dataframe": "pandas",
                "reactivity": "autorun",
                "sources": {"dataframe": "notebook", "reactivity": "default"},
            },
            {"dataframe": "pandas"},
            id="inherited-values-are-not-the-file-s",
        ),
        pytest.param(
            {"dataframe": "pandas", "reactivity": "lazy"},
            {"dataframe": "pandas", "reactivity": "lazy"},
            id="a-read-without-sources-keeps-each-setting",
        ),
        pytest.param(
            {"dataframe": None, "sources": {"dataframe": "notebook"}},
            {},
            id="an-unset-value-is-not-stored",
        ),
    ],
)
def test_what_a_file_sets_comes_from_a_settings_read(
    read: dict[str, object], stored: dict[str, object]
) -> None:
    from alkera_notebook.format.settings import stored_settings

    assert stored_settings(read) == stored
