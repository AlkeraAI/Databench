"""The notebook tools' permission gates in every permission mode.

Each case dispatches a real notebook tool through the real tool registry and
the real ``DecisionEngine`` (the same chokepoint as ``sql.query`` and the
shell), with a broker that allows whatever it is asked and remembers the ask.
The outcome per mode is one of ``ran`` (no ask), ``asked`` (a person was asked
and allowed it) or ``refused``. The notebooks run in the simulator's reference
engine, which records every executed step, so "ran" is proven by execution and
"refused" by its absence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.permission_mode import parse_mode, write_free_root
from alkera_cli.notebooks.session import WorkspaceNotebookService
from alkera_cli.notebooks.tools import CODE_EXEC_EFFECT, register_notebook_tools
from alkera_cli.plugins.plugin_base.permissions import DecisionSink, PermissionsConfig
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_cli.plugins.plugin_base.tool import ToolRegistry
from alkera_core.permission_presentation import ask_from_event, present
from alkera_core.project import ProjectDirectory
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.tools import ActorRef, NotebookHost

MODES = ("default", "auto", "plan", "read_only", "bypass")
PATH = "nb.alknb.py"


class _AllowingBroker:
    def __init__(self) -> None:
        self.asks: list[Any] = []

    async def resolve(self, request: Any) -> str:
        self.asks.append(request)
        return "allow_once"


class _AllowingJudge:
    """Auto mode's safety judge, allowing everything it is shown."""

    def __init__(self) -> None:
        self.judged: list[Any] = []

    async def judge(
        self, descriptor: Any, task_goal: str, *, workspace_root: str | None = None
    ) -> Any:
        self.judged.append(descriptor)
        return type("Verdict", (), {"decision": "allow", "reason": ""})()


def _free_root(mode: str, sandbox: Any, folder: Path) -> Path | None:
    return write_free_root(parse_mode(mode) or "default", sandbox, folder)


class _Rig:
    def __init__(self, tmp_path: Path, *, shared_folder: bool) -> None:
        self.project = ProjectDirectory(tmp_path / ".alkera")
        self.registry = ToolRegistry(
            self.project.blobs(), decision_sink=DecisionSink(self.project.path)
        )
        register_notebook_tools(self.registry)
        self.workspaces: dict[str, ReferenceWorkspace] = {}
        self.judge = _AllowingJudge()

        def factory(root: Path, actor: ActorRef) -> NotebookHost:
            ws = self.workspaces.setdefault(str(root), ReferenceWorkspace(root=str(root)))
            return ws.host(actor)

        self.registry.notebooks = WorkspaceNotebookService(factory, _free_root)
        # A workspace chat writes in the workspace's shared folder; a local chat
        # in its own sandbox inside the chat folder.
        self.sandbox = (
            tmp_path / "workspace"
            if shared_folder
            else self.project.path / "chats" / "c1" / "sandbox"
        )
        self.sandbox.mkdir(parents=True, exist_ok=True)

    @property
    def notebook(self) -> Any:
        return next(iter(self.workspaces.values())).notebooks[PATH]

    async def call(
        self, tool: str, args: dict[str, Any], *, mode: str, broker: Any
    ) -> dict[str, Any]:
        return dict(
            await self.registry.dispatch(
                tool,
                {"path": PATH, **args},
                session_id="c1",
                permissions=PermissionsConfig(),
                permission_mode=mode,
                broker=broker,
                decision_sink=DecisionSink(self.project.path),
                alkera_dir=self.project.path,
                sandbox_dir=self.sandbox,
                judge=self.judge,
            )
        )

    async def seed(self, cells: list[dict[str, Any]], *, setup_has_run: bool = True) -> list[str]:
        """A notebook of ``cells``; the ids of those cells. Create gives the
        notebook its setup cell (``import alkera``) ahead of them, which has
        already run unless ``setup_has_run`` is false: it is Python, so a
        plan that still has it to run is gated as Python is."""
        out = await self.call(
            "notebook.create", {"cells": cells}, mode="bypass", broker=_AllowingBroker()
        )
        assert "error" not in out, out
        setup, *asked = out["cells"]
        assert setup["kind"] == "setup" and len(asked) == len(cells)
        if setup_has_run:
            ran = await self.call(
                "notebook.run",
                {"target": {"kind": "cells", "ids": [setup["id"]]}},
                mode="bypass",
                broker=_AllowingBroker(),
            )
            assert "error" not in ran, ran
            self.notebook.executions.clear()
        return [c["id"] for c in asked]


def _outcome(out: dict[str, Any], broker: _AllowingBroker, rig: _Rig) -> str:
    """``refused``, ``asked`` (a person allowed it), ``judged`` (auto mode's judge
    allowed it) or ``ran`` (nothing was consulted)."""
    if "error" in out:
        return "refused"
    if broker.asks:
        return "asked"
    return "judged" if rig.judge.judged else "ran"


def test_a_run_is_gated_as_python_c_is_by_the_shell() -> None:
    """The effect a Python cell is gated with is read off the shell classifier,
    so the two cannot drift."""
    assert CODE_EXEC_EFFECT == classify_command("python -c 'x = 1'").effect


RUN_CASES = [
    pytest.param(
        [{"kind": "sql", "source": "SELECT 1 AS x"}],
        {"default": "ran", "auto": "ran", "plan": "ran", "read_only": "ran", "bypass": "ran"},
        id="read_only_sql_plan_runs_unasked",
    ),
    pytest.param(
        [{"kind": "python", "source": "x = 1"}],
        {
            "default": "asked",
            "auto": "judged",
            "plan": "refused",
            "read_only": "refused",
            "bypass": "ran",
        },
        id="python_cell_is_a_write",
    ),
    pytest.param(
        [{"kind": "sql", "source": "SELEC garbage ((("}],
        {
            "default": "asked",
            "auto": "judged",
            "plan": "refused",
            "read_only": "refused",
            "bypass": "ran",
        },
        id="unparseable_sql_counts_as_a_write",
    ),
    pytest.param(
        [{"kind": "sql", "source": "SELECT {n} AS x"}],
        {
            "default": "asked",
            "auto": "judged",
            "plan": "refused",
            "read_only": "refused",
            "bypass": "ran",
        },
        id="interpolated_sql_counts_as_a_write",
    ),
    pytest.param(
        [{"kind": "sql", "source": "CREATE TABLE t AS SELECT 1 AS x"}],
        {
            "default": "asked",
            "auto": "judged",
            "plan": "refused",
            "read_only": "refused",
            "bypass": "ran",
        },
        id="sql_write_asks_where_a_write_asks",
    ),
    pytest.param(
        [{"kind": "sql", "source": "DROP TABLE t"}],
        {
            "default": "asked",
            "auto": "asked",
            "plan": "refused",
            "read_only": "refused",
            "bypass": "ran",
        },
        id="sql_destroy_asks_even_in_auto",
    ),
]


@pytest.mark.parametrize(("cells", "expected"), RUN_CASES)
@pytest.mark.parametrize("mode", MODES)
async def test_a_run_is_decided_by_its_most_severe_cell(
    tmp_path: Path, mode: str, cells: list[dict[str, Any]], expected: dict[str, str]
) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed(cells)
    broker = _AllowingBroker()
    out = await rig.call(
        "notebook.run", {"target": {"kind": "cells", "ids": ids}}, mode=mode, broker=broker
    )
    outcome = _outcome(out, broker, rig)
    assert outcome == expected[mode], (mode, out)
    executed = [cid for _, cid in rig.notebook.executions]
    assert (executed == ids) == (outcome != "refused")


@pytest.mark.parametrize("mode", MODES)
async def test_a_plan_that_still_has_the_setup_import_to_run_is_gated_as_python(
    tmp_path: Path, mode: str
) -> None:
    """A read-only statement in a notebook whose setup block has not run yet:
    the plan carries that Python step, so it is decided as Python is."""
    rig = _Rig(tmp_path, shared_folder=True)
    await rig.seed([{"kind": "sql", "source": "SELECT 1 AS x"}], setup_has_run=False)
    broker = _AllowingBroker()
    out = await rig.call("notebook.run", {"target": {"kind": "all"}}, mode=mode, broker=broker)
    expected = {
        "default": "asked",
        "auto": "judged",
        "plan": "refused",
        "read_only": "refused",
        "bypass": "ran",
    }
    assert _outcome(out, broker, rig) == expected[mode], (mode, out)
    assert (len(rig.notebook.executions) == 2) == (expected[mode] != "refused")


def _presented(request: Any) -> Any:
    """The card a person is shown for ``request``: the shared presentation
    every surface renders."""
    return present(ask_from_event(request.model_dump(mode="json")))


async def test_the_run_prompt_names_each_cell_the_way_a_person_sees_it(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed(
        [
            {"kind": "python", "source": "rows = 3", "name": "load"},
            {"kind": "sql", "source": "SELECT 1 AS x"},
            {"kind": "python", "source": "import polars as pl\nprint(rows)\nrows + 1"},
        ]
    )
    broker = _AllowingBroker()
    await rig.call(
        "notebook.run",
        {"target": {"kind": "cells", "ids": [ids[2]]}},
        mode="default",
        broker=broker,
    )
    (request,) = broker.asks
    shown = _presented(request)
    assert shown.title == "Run 2 cells in nb.alknb.py"
    nb = shown.notebook
    assert nb is not None
    assert (nb.file_name, nb.file_path) == ("nb.alknb.py", "nb.alknb.py")
    # The cell asked for, by its place in the notebook (the setup block is the
    # first cell), with the start of its code; the named cell it reads from is
    # the one quiet line under it.
    assert [(c.name, c.preview_lines) for c in nb.cells] == [
        ("Cell 4", ["import polars as pl", "print(rows) …"])
    ]
    assert [(c.name, c.role) for c in nb.related] == [("load", "dependency")]
    assert nb.related_line == "Also runs 1 cell it depends on"
    assert nb.reason == "Can't tell if it changes anything"
    assert request.subject["effect"] == "write"


async def test_a_run_offers_always_only_where_it_would_be_remembered(tmp_path: Path) -> None:
    """Python cells cannot be classified, so an "always allow" answer records
    nothing and is not offered, while "always reject" (always recorded) stays;
    a SQL write can be classified, and the card says what the answer remembers."""
    rig = _Rig(tmp_path, shared_folder=True)
    py_ids = await rig.seed([{"kind": "python", "source": "x = 1"}])
    broker = _AllowingBroker()
    await rig.call(
        "notebook.run", {"target": {"kind": "cells", "ids": py_ids}}, mode="default", broker=broker
    )
    assert [o.option_id for o in broker.asks[0].options] == [
        "allow_once",
        "reject_once",
        "reject_always",
    ]
    assert _presented(broker.asks[0]).always_scope is None

    sql_rig = _Rig(tmp_path / "sql", shared_folder=True)
    sql_ids = await sql_rig.seed([{"kind": "sql", "source": "CREATE TABLE t AS SELECT 1 AS x"}])
    broker = _AllowingBroker()
    await sql_rig.call(
        "notebook.run", {"target": {"kind": "cells", "ids": sql_ids}}, mode="default", broker=broker
    )
    (request,) = broker.asks
    assert "allow_always" in [o.option_id for o in request.options]
    assert _presented(request).always_scope == (
        "Always allow skips this question whenever the agent asks to run 1 cell in nb.alknb.py."
    )


GATED_CALLS = [
    pytest.param("notebook.run", {"target": {"kind": "all"}}, id="run_all"),
    pytest.param("notebook.kernel", {"action": "restart"}, id="kernel_restart"),
    pytest.param("notebook.env", {"action": "install", "packages": ["simtable"]}, id="install"),
    pytest.param("notebook.inspect", {"what": "value", "name": "x"}, id="inspect_value"),
]


@pytest.mark.parametrize(("tool", "args"), GATED_CALLS)
async def test_no_notebook_prompt_shows_a_cell_id_or_a_path_on_the_machine(
    tmp_path: Path, tool: str, args: dict[str, Any]
) -> None:
    """Whatever a notebook tool asks, the card a person reads names cells by
    name or position and the notebook by its workspace path: never an id the
    agent uses, never where the file sits on this machine's disk."""
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed([{"source": "x = 1"}, {"source": "y = x + 1"}], setup_has_run=False)
    broker = _AllowingBroker()
    # The ask is what is under test; whatever the call does after it is not.
    await rig.call(tool, args, mode="default", broker=broker)
    (request,) = broker.asks
    card = json.dumps(_presented(request).to_json())
    for leak in (*ids, str(tmp_path), str(rig.sandbox)):
        assert leak not in card, (tool, leak, card)
    # The classified action names the notebook by its workspace path too.
    targets = [t["name"] for t in request.subject["targets"] if t["kind"] == "notebook"]
    assert targets == [PATH]


EDIT_CASES = [
    pytest.param(
        True,
        {"default": "ran", "auto": "ran", "plan": "ran", "read_only": "refused", "bypass": "ran"},
        id="workspace_shared_folder",
    ),
    pytest.param(
        False,
        {"default": "ran", "auto": "ran", "plan": "ran", "read_only": "ran", "bypass": "ran"},
        id="chat_own_sandbox",
    ),
]


@pytest.mark.parametrize(("shared", "expected"), EDIT_CASES)
@pytest.mark.parametrize("mode", MODES)
async def test_an_edit_is_gated_as_the_edit_tool_for_that_path(
    tmp_path: Path, mode: str, shared: bool, expected: dict[str, str]
) -> None:
    rig = _Rig(tmp_path, shared_folder=shared)
    (cid,) = await rig.seed([{"source": "x = 1"}])
    broker = _AllowingBroker()
    out = await rig.call(
        "notebook.edit",
        {"ops": [{"op": "replace", "cell_id": cid, "source": "x = 2"}]},
        mode=mode,
        broker=broker,
    )
    assert _outcome(out, broker, rig) == expected[mode], (mode, out)
    assert (rig.notebook.doc.cells[cid].source == "x = 2") == (expected[mode] != "refused")


@pytest.mark.parametrize("mode", MODES)
async def test_interrupting_someone_elses_run_is_a_destroy(tmp_path: Path, mode: str) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed([{"source": "x = 1"}])
    nb = rig.notebook
    from alkera_notebook.tools.models import RunCells

    nb.request_run(
        RunCells(ids=ids),
        ActorRef(kind="person", id="person:bob", display_name="Bob"),
        confirm_expensive=True,
    )
    broker = _AllowingBroker()
    out = await rig.call("notebook.kernel", {"action": "interrupt"}, mode=mode, broker=broker)
    expected = {
        "default": "asked",
        "auto": "asked",
        "plan": "refused",
        "read_only": "refused",
        "bypass": "ran",
    }
    assert _outcome(out, broker, rig) == expected[mode], (mode, out)
    if broker.asks:
        assert broker.asks[0].subject["effect"] == "destroy"
        assert "Bob" in broker.asks[0].preview["title"]


@pytest.mark.parametrize("mode", MODES)
async def test_interrupting_your_own_run_is_code_execution(tmp_path: Path, mode: str) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    await rig.seed([{"source": "x = 1"}])
    broker = _AllowingBroker()
    out = await rig.call("notebook.kernel", {"action": "restart"}, mode=mode, broker=broker)
    expected = {
        "default": "asked",
        "auto": "judged",
        "plan": "refused",
        "read_only": "refused",
        "bypass": "ran",
    }
    assert _outcome(out, broker, rig) == expected[mode], (mode, out)


@pytest.mark.parametrize("mode", MODES)
async def test_installing_packages_is_egress(tmp_path: Path, mode: str) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    await rig.seed([{"source": "x = 1"}])
    broker = _AllowingBroker()
    out = await rig.call(
        "notebook.env", {"action": "install", "packages": ["simtable"]}, mode=mode, broker=broker
    )
    expected = {
        "default": "asked",
        "auto": "judged",
        "plan": "refused",
        "read_only": "refused",
        "bypass": "ran",
    }
    assert _outcome(out, broker, rig) == expected[mode], (mode, out)
    assert ("simtable" in rig.notebook.installed) == (expected[mode] != "refused")


READS = [
    pytest.param("notebook.read", {}, id="read"),
    pytest.param("notebook.graph", {}, id="graph"),
    pytest.param("notebook.inspect", {"what": "variables"}, id="inspect_variables"),
    pytest.param("notebook.kernel", {"action": "status"}, id="kernel_status"),
    pytest.param("notebook.widget", {"action": "list"}, id="widget_list"),
    pytest.param("notebook.env", {"action": "info"}, id="env_info"),
]


@pytest.mark.parametrize(("tool", "args"), READS)
@pytest.mark.parametrize("mode", MODES)
async def test_reads_are_never_gated(
    tmp_path: Path, mode: str, tool: str, args: dict[str, Any]
) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    await rig.seed([{"source": "x = 1"}])
    broker = _AllowingBroker()
    out = await rig.call(tool, args, mode=mode, broker=broker)
    assert _outcome(out, broker, rig) == "ran", (mode, out)


@pytest.mark.parametrize("mode", MODES)
async def test_showing_an_output_is_a_read_in_every_mode(tmp_path: Path, mode: str) -> None:
    """``notebook.show_output`` puts an output the notebook already holds in
    the chat: nobody is asked, the judge is not consulted, no mode refuses it
    (``read_only`` and ``plan`` refuse anything that is not a read) and no
    cell runs."""
    rig = _Rig(tmp_path, shared_folder=True)
    [cell] = await rig.seed([{"source": "total = 41 + 1\ntotal", "name": "total"}])
    ran = await rig.call(
        "notebook.run",
        {"target": {"kind": "cells", "ids": [cell]}},
        mode="bypass",
        broker=_AllowingBroker(),
    )
    assert "error" not in ran, ran
    rig.notebook.executions.clear()
    broker = _AllowingBroker()

    out = await rig.call("notebook.show_output", {"cell": "total"}, mode=mode, broker=broker)

    assert _outcome(out, broker, rig) == "ran", (mode, out)
    assert out["kind"] == "text" and out["text"]["content"] == "42"
    assert rig.notebook.executions == []


async def test_a_refused_run_tells_the_model_why_and_runs_nothing(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed([{"source": "x = 1"}])
    out = await rig.call(
        "notebook.run",
        {"target": {"kind": "cells", "ids": ids}},
        mode="read_only",
        broker=_AllowingBroker(),
    )
    assert "permission denied" in out["error"]
    assert rig.notebook.executions == []


#: The edit row for a notebook in the workspace's shared folder: a write, so
#: read_only refuses it where a read would run.
_SHARED_EDIT = EDIT_CASES[0].values[1]


def _doc_state(rig: _Rig) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Every live cell in order with its kind, source and settings."""
    return [(c.id, c.kind, c.source, dict(c.config)) for c in rig.notebook.doc.live()]


CELL_ACTIONS = [
    pytest.param({"action": "clear_outputs", "cells": ["last"]}, id="clear_outputs"),
    pytest.param({"action": "clear_outputs"}, id="clear_all_outputs"),
    pytest.param({"action": "disable", "cells": ["last"]}, id="disable"),
    pytest.param({"action": "duplicate", "cells": ["last"]}, id="duplicate"),
    pytest.param({"action": "move", "cells": ["last"], "to": "up"}, id="move"),
    pytest.param({"action": "set_kind", "cells": ["last"], "kind": "markdown"}, id="set_kind"),
]


@pytest.mark.parametrize("args", CELL_ACTIONS)
@pytest.mark.parametrize("mode", MODES)
async def test_every_cell_action_is_gated_as_an_edit_never_a_read(
    tmp_path: Path, mode: str, args: dict[str, Any]
) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed([{"source": "print('a')"}, {"source": "print('b')"}])
    ran = await rig.call(
        "notebook.run", {"target": {"kind": "all"}}, mode="bypass", broker=_AllowingBroker()
    )
    assert "error" not in ran, ran
    before_doc = _doc_state(rig)
    broker = _AllowingBroker()
    out = await rig.call("notebook.cells", args, mode=mode, broker=broker)
    assert _outcome(out, broker, rig) == _SHARED_EDIT[mode], (mode, out)
    state = rig.notebook.kstate[ids[1]]
    if _SHARED_EDIT[mode] == "refused":
        assert _doc_state(rig) == before_doc
        assert state.output is not None
    elif args["action"] == "clear_outputs":
        assert state.output is None
    else:
        assert _doc_state(rig) != before_doc


@pytest.mark.parametrize("mode", MODES)
async def test_restart_and_run_all_over_someone_elses_run_is_a_destroy(
    tmp_path: Path, mode: str
) -> None:
    rig = _Rig(tmp_path, shared_folder=True)
    ids = await rig.seed([{"source": "x = 1"}])
    from alkera_notebook.tools.models import RunCells

    rig.notebook.request_run(
        RunCells(ids=ids),
        ActorRef(kind="person", id="person:bob", display_name="Bob"),
        confirm_expensive=True,
    )
    broker = _AllowingBroker()
    out = await rig.call(
        "notebook.run", {"target": {"kind": "all", "restart": True}}, mode=mode, broker=broker
    )
    expected = {
        "default": "asked",
        "auto": "asked",
        "plan": "refused",
        "read_only": "refused",
        "bypass": "ran",
    }
    assert _outcome(out, broker, rig) == expected[mode], (mode, out)
    if broker.asks:
        assert broker.asks[0].subject["effect"] == "destroy"
        assert "Bob" in broker.asks[0].preview["title"]
