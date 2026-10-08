"""The simulator against the real engine: real kernel subprocesses, the file
store on disk, and the invariants the engine target can support."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "alkera-kernel" / "tests" / "kernel"))
from alkera_notebook.document.ops import NotebookOpsResult
from alkera_notebook.engine import Actor, FrameAttached
from alkera_notebook.envs.static import StaticEnvRegistry
from alkera_notebook.envs.template import default_env_template
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.engine_target import EngineTarget
from alkera_notebook.tools import NotebookToolError
from alkera_notebook.tools.models import ReplaceCellOp
from nbkrn_harness import rich_python

__all__ = ["rich_python"]

PATH = "nb.alknb.py"


def _target(tmp_path: Path) -> EngineTarget:
    """The real engine, its workspace's default environment seeded from a
    template with no packages (one that builds without the package index)."""
    template = default_env_template(tmp_path / "template", packages=())
    return EngineTarget(tmp_path / "ws", default_template=template)


async def test_a_session_on_the_real_engine_holds_its_invariants_and_leaves_no_kernel(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"kind": "setup", "source": "import alkera"},
                    {"source": "a = 1", "name": "one"},
                    {"source": "b = a + 1\nb", "name": "two"},
                ],
            },
        )
        ids = [c.id for c in out.cells]  # type: ignore[union-attr]
        run = await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        assert run.status == "finished", run  # type: ignore[union-attr]
        assert [c.status for c in run.cells] == ["fresh", "fresh", "fresh"]  # type: ignore[union-attr]
        await driver.check(settle=True)
        await driver.person_edit(PATH, [ReplaceCellOp(cell_id=ids[2], source="b = a + 2\nb")])
        digest = await driver.digest()
        assert ids[2] in digest.text
        await driver.check(settle=True)
        assert {"file", "document", "kernel_facts", "runs", "processes"} <= target.capabilities
        assert target._pids, "the run started a kernel"
    finally:
        await driver.close()
    assert target.live_kernel_processes() == []


async def test_a_descendant_of_an_edited_cell_reads_stale_on_the_real_engine(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {"path": PATH, "cells": [{"source": "a = 1"}, {"source": "b = a + 1\nb"}]},
        )
        # The setup cell create adds, then the two cells asked for.
        assert [c.kind for c in out.cells] == ["setup", "python", "python"]  # type: ignore[union-attr]
        ids = [c.id for c in out.cells]  # type: ignore[union-attr]
        await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        await driver.person_edit(PATH, [ReplaceCellOp(cell_id=ids[1], source="a = 5")])
        await driver.check(settle=True)
    finally:
        await driver.close()


@pytest.mark.parametrize(
    ("meta", "expected", "absent"),
    [
        pytest.param(
            {"output_var": "orders", "connection": "local", "show_output": False},
            ["orders = alkera.sql(", 'connection="local"', "output=False"],
            [],
            id="sql_meta_reaches_the_file",
        ),
        pytest.param(
            {},
            ["alkera.sql("],
            ["orders = ", 'connection="local"', "output=False"],
            id="no_meta_keeps_the_defaults",
        ),
    ],
)
async def test_create_writes_each_cells_meta_into_the_file(
    tmp_path: Path, meta: dict[str, object], expected: list[str], absent: list[str]
) -> None:
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"kind": "setup", "source": "import alkera"},
                    {"kind": "sql", "source": "SELECT 1 AS v", "meta": meta},
                ],
            },
        )
        assert [c.kind for c in out.cells] == ["setup", "sql"]  # type: ignore[union-attr]
        text = (tmp_path / "ws" / PATH).read_text()
        for snippet in expected:
            assert snippet in text, text
        for snippet in absent:
            assert snippet not in text, text
    finally:
        await driver.close()


async def test_an_edit_answers_the_engine_s_own_result_with_run_status_and_graph_errors(
    tmp_path: Path,
) -> None:
    """``notebook.edit`` answers the engine's ``NotebookOpsResult`` itself:
    each touched cell carries its run status (the engine's, which a store
    cannot know), and the document graph names a duplicated definition as a
    structured error with the name and both cells."""
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        created = await driver.agent(
            "notebook.create",
            {"path": PATH, "cells": [{"source": "a = 1"}, {"source": "b = a + 1"}]},
        )
        # The setup cell create adds is at index 0, ahead of the cells asked for.
        assert [c.kind for c in created.cells] == ["setup", "python", "python"]  # type: ignore[union-attr]
        first = created.cells[1].id  # type: ignore[union-attr]
        out = await driver.agent(
            "notebook.edit",
            {"path": PATH, "ops": [{"op": "insert", "source": "a = 2", "after": first}]},
        )
        assert isinstance(out, NotebookOpsResult), out
        (new,) = out.created
        (after,) = [c for c in out.cells if c.id == new]
        assert (after.index, after.status, after.deleted) == (2, "not_run", False)
        assert out.graph.computed is True
        errors = {
            cid: [e.model_dump() for e in info.errors]
            for cid, info in out.graph.cells.items()
            if info.errors
        }
        expected = {"code": "multiple_definitions", "name": "a", "cells": sorted([first, new])}
        assert errors == {first: [expected], new: [expected]}
    finally:
        await driver.close()


async def test_scenario_a_real_ipywidgets_slider_reaches_a_frame_and_takes_a_value(
    tmp_path: Path, rich_python: str
) -> None:
    """A widget from the library itself, not ``alkera.ui``: the agent builds
    and runs it, a person's frame gets replays the widget manager accepts
    (each names the protocol version), and a value set on the model reaches
    the kernel and the cell that reads it."""
    target = EngineTarget(tmp_path / "ws", envs=StaticEnvRegistry(interpreter=rich_python))
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"source": "import ipywidgets as w\ns = w.IntSlider(value=3, max=10)\ns"},
                    {"source": "s.value * 2"},
                ],
            },
        )
        # The setup cell create adds, then the slider cell and its reader.
        assert [c.kind for c in out.cells] == ["setup", "python", "python"]  # type: ignore[union-attr]
        ids = [c.id for c in out.cells]  # type: ignore[union-attr]
        run = await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        assert run.status == "finished", run  # type: ignore[union-attr]
        assert run.cells[-1].output.text.content.strip() == "6"  # type: ignore[union-attr]
        listed = await driver.agent("notebook.widget", {"path": PATH, "action": "list"})
        [slider] = [x for x in listed.widgets if x.type == "IntSliderModel"]  # type: ignore[union-attr]
        assert slider.cell_id == ids[1]
        assert slider.value is not None and slider.value.content == 3

        reader = (await target.engine.open(PATH)).attach(
            Actor(kind="person", id="u-ann", display_name="Ann", can_edit=True, can_run=True)
        )
        attached = reader.attach_frame(output_id=ids[1])
        assert isinstance(attached, FrameAttached)
        assert attached.model_ids == [slider.model_id]
        assert len(attached.opens) == 3  # the slider, its layout and its style
        assert {o.message["metadata"]["version"].split(".")[0] for o in attached.opens} == {"2"}

        await driver.agent(
            "notebook.widget",
            {"path": PATH, "action": "set", "model_id": slider.model_id, "state": {"value": 7}},
        )
        rerun = await driver.agent(
            "notebook.run",
            {"path": PATH, "target": {"kind": "cells", "ids": [ids[2]]}, "timeout_s": 120},
        )
        assert rerun.cells[-1].output.text.content.strip() == "14"  # type: ignore[union-attr]
        await driver.check(settle=True)
    finally:
        await driver.close()
    assert target.live_kernel_processes() == []


async def test_the_agent_changes_and_reads_a_sql_cells_connection(tmp_path: Path) -> None:
    """The agent names a connection the way the picker does (``set_meta``),
    the file records it, and ``notebook.read`` reports it in the cell's meta,
    so a cell the agent made shows in the picker and the agent sees a
    person's choice."""
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        created = await driver.agent(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"kind": "setup", "source": "import alkera"},
                    {"kind": "sql", "source": "SELECT 1 AS v", "meta": {"connection": "wh"}},
                ],
            },
        )
        sql = created.cells[1].id  # type: ignore[union-attr]
        read = await driver.agent("notebook.read", {"path": PATH, "cells": [sql]})
        assert read.cells[0].meta.get("connection") == "wh"  # type: ignore[union-attr]

        await driver.agent(
            "notebook.edit",
            {
                "path": PATH,
                "ops": [
                    {
                        "op": "set_meta",
                        "cell_id": sql,
                        "meta": {"connection": "lake", "output_var": "orders"},
                    }
                ],
            },
        )
        text = (tmp_path / "ws" / PATH).read_text()
        assert "orders = alkera.sql(" in text and 'connection="lake"' in text, text
        read = await driver.agent("notebook.read", {"path": PATH, "cells": [sql]})
        meta = read.cells[0].meta  # type: ignore[union-attr]
        assert (meta.get("connection"), meta.get("output_var")) == ("lake", "orders")

        await driver.agent(
            "notebook.edit",
            {
                "path": PATH,
                "ops": [{"op": "set_meta", "cell_id": sql, "meta": {"connection": None}}],
            },
        )
        assert "connection=" not in (tmp_path / "ws" / PATH).read_text()
    finally:
        await driver.close()


async def test_the_agent_clears_one_cells_output_by_position_on_the_real_engine(
    tmp_path: Path,
) -> None:
    """The ask that started this: clear just one cell's output. The person
    watching the notebook is told, and the saved outputs no longer hold it."""
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {"path": PATH, "cells": [{"source": "print('keep')"}, {"source": "print('drop')"}]},
        )
        ids = [c.id for c in out.cells]  # type: ignore[union-attr]
        await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        cleared = await driver.agent(
            "notebook.cells", {"path": PATH, "action": "clear_outputs", "cells": ["last"]}
        )
        assert cleared.changed == [ids[2]], cleared  # type: ignore[union-attr]
        person = await driver.person_port(PATH)
        view = {
            c.id: c
            for c in (await person.read(None, include_source=False, include_outputs=True)).cells
        }
        assert view[ids[2]].output is None
        assert view[ids[1]].output is not None and view[ids[1]].output.text.strip() == "keep"
        assert any(
            s.title.startswith("Clear the outputs of 1 cell") for s in driver.gatekeeper.asked
        )
    finally:
        await driver.close()


async def test_upstream_and_downstream_targets_run_their_graph_on_the_real_engine(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"source": "a = 1"},
                    {"source": "b = a + 1"},
                    {"source": "c = b + 1"},
                    {"source": "other = 5"},
                ],
                "settings": {"reactivity": "lazy"},
            },
        )
        ids = [c.id for c in out.cells]  # type: ignore[union-attr]
        await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        up = await driver.agent(
            "notebook.run",
            {"path": PATH, "target": {"kind": "upstream", "id": "Cell 4"}, "timeout_s": 120},
        )
        planned = {step.cell_id for step in up.plan}  # type: ignore[union-attr]
        assert {ids[1], ids[2], ids[3]} <= planned and ids[4] not in planned
        assert up.status == "finished"  # type: ignore[union-attr]
        down = await driver.agent(
            "notebook.run",
            {"path": PATH, "target": {"kind": "downstream", "id": ids[1]}, "timeout_s": 120},
        )
        planned = {step.cell_id for step in down.plan}  # type: ignore[union-attr]
        assert {ids[1], ids[2], ids[3]} <= planned and ids[4] not in planned
        assert all(c.status == "fresh" for c in down.cells)  # type: ignore[union-attr]
    finally:
        await driver.close()


async def test_clear_the_output_of_the_last_cell_as_the_owner_asked(tmp_path: Path) -> None:
    """The request that failed live: "clear the output of the last cell". The
    agent's wrong first try (show_output through set_config) is refused with
    a message that points at set_meta, and the cell action
    clears exactly that output for the person watching."""
    target = _target(tmp_path)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    try:
        out = await driver.agent(
            "notebook.create",
            {
                "path": PATH,
                "cells": [
                    {"source": "print('first')"},
                    {"source": "answer = 42\nanswer"},
                ],
            },
        )
        ids = [c.id for c in out.cells]  # type: ignore[union-attr]
        await driver.agent(
            "notebook.run", {"path": PATH, "target": {"kind": "all"}, "timeout_s": 120}
        )
        person = await driver.person_port(PATH)
        before = await person.read(None, include_source=False, include_outputs=True)
        assert before.cells[-1].output is not None
        wrong = await driver.agent(
            "notebook.edit",
            {
                "path": PATH,
                "ops": [{"op": "set_config", "cell_id": ids[-1], "config": {"show_output": False}}],
            },
        )
        assert isinstance(wrong, NotebookToolError) and wrong.code == "invalid_config", wrong
        assert "set_meta" in str(wrong) and "show_output" in str(wrong)
        cleared = await driver.agent(
            "notebook.cells", {"path": PATH, "action": "clear_outputs", "cells": ["last"]}
        )
        assert cleared.changed == [ids[-1]], cleared  # type: ignore[union-attr]
        after = {
            c.id: c
            for c in (await person.read(None, include_source=False, include_outputs=True)).cells
        }
        assert after[ids[-1]].output is None
        assert after[ids[1]].output is not None
    finally:
        await driver.close()
