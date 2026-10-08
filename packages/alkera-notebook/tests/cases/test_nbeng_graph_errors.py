"""Graph errors leave the engine as structured objects ``{code, name, cells}``:
on the graph view, on an ops result's graph summary and on the ``graph``
event the editor reads; a cell's own ``graph_errors`` stay one-line labels.
The agent tools built on the engine read the code and the name from them."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.document.ops import GraphErrorInfo, InsertCell
from alkera_notebook.events.models import GraphEvent
from alkera_notebook.tools import ActorRef, RecordingGatekeeper, call_tool, validate
from alkera_notebook.tools.engine_adapter import LocalEngines
from nbeng_harness import BOB, engine_for, notebook

# Each case: the cells' sources, then per cell index the errors it reports.
CASES = [
    pytest.param(
        ["x = 1", "x = 2", "y = x"],
        {
            0: [("multiple_definitions", "x", [0, 1])],
            1: [("multiple_definitions", "x", [0, 1])],
        },
        id="multiple_definitions_names_the_name_and_both_cells",
    ),
    pytest.param(
        ["a = b", "b = a", "c = 1"],
        {0: [("cycle", None, [0, 1])], 1: [("cycle", None, [0, 1])]},
        id="a_cycle_names_its_members_and_no_name",
    ),
    pytest.param(["a = 1", "b = a"], {}, id="a_clean_graph_has_none"),
]

Expected = dict[int, list[tuple[str, str | None, list[int]]]]


def _expected(ids: list[str], spec: Expected) -> dict[str, list[GraphErrorInfo]]:
    return {
        ids[i]: [GraphErrorInfo(code=c, name=n, cells=sorted(ids[k] for k in m)) for c, n, m in e]
        for i, e in spec.items()
    }


@pytest.mark.parametrize(("cells", "spec"), CASES)
async def test_the_graph_view_carries_structured_errors(
    tmp_path: Path, cells: list[str], spec: Expected
) -> None:
    async with engine_for(tmp_path) as engine:
        _, client, ids = await notebook(engine, cells)
        view = await client.graph(None, "both")
        assert view.errors == _expected(ids, spec)
        wire = view.model_dump(mode="json")["errors"]
        for errors in wire.values():
            assert all(isinstance(e, dict) and "code" in e for e in errors)


@pytest.mark.parametrize(("cells", "spec"), CASES)
async def test_a_cell_state_keeps_one_line_labels(
    tmp_path: Path, cells: list[str], spec: Expected
) -> None:
    async with engine_for(tmp_path) as engine:
        _, client, ids = await notebook(engine, cells)
        view = await client.read()
        got = {c.id: c.graph_errors for c in view.cells if c.graph_errors}
        assert got == {cid: [e.label() for e in es] for cid, es in _expected(ids, spec).items()}


async def test_an_ops_result_and_the_graph_event_carry_structured_errors(
    tmp_path: Path,
) -> None:
    async with engine_for(tmp_path) as engine:
        session, client, (first,) = await notebook(engine, ["x = 1"])
        watcher = session.attach(BOB)
        while len(watcher.queue):
            await watcher.next_event(1)
        result = await client.apply([InsertCell(source="x = 2", after=first)], None)
        (second,) = result.created
        want = [
            GraphErrorInfo(code="multiple_definitions", name="x", cells=sorted([first, second]))
        ]
        assert result.graph.cells[first].errors == want
        assert result.graph.cells[second].errors == want
        events = []
        while len(watcher.queue):
            events.append(await watcher.next_event(1))
        graphs = [e for e in events if isinstance(e, GraphEvent)]
        assert graphs, "an edit that changes the graph publishes it"
        assert graphs[-1].graph.cells[second].errors == want
        dumped = graphs[-1].model_dump(mode="json")["graph"]["cells"][second]["errors"]
        assert dumped == [{"code": "multiple_definitions", "name": "x", "cells": want[0].cells}]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param(
            {"code": "cycle", "cells": ["b", "a"]},
            GraphErrorInfo(code="cycle", cells=["b", "a"]),
            id="format_object_without_a_name",
        ),
        pytest.param(
            {"code": "multiple_definitions", "name": "x"},
            GraphErrorInfo(code="multiple_definitions", name="x"),
            id="format_object_without_cells",
        ),
        pytest.param({}, GraphErrorInfo(code="error"), id="an_object_without_a_code"),
        pytest.param("syntax", GraphErrorInfo(code="syntax"), id="a_bare_code"),
        pytest.param(
            GraphErrorInfo(code="cycle"), GraphErrorInfo(code="cycle"), id="already_structured"
        ),
    ],
)
def test_parse_reads_every_shape(item: object, expected: GraphErrorInfo) -> None:
    assert GraphErrorInfo.parse(item) == expected


@pytest.mark.parametrize(
    ("info", "label"),
    [
        pytest.param(GraphErrorInfo(code="cycle"), "cycle", id="code_only"),
        pytest.param(
            GraphErrorInfo(code="multiple_definitions", name="x"),
            "multiple_definitions: x",
            id="code_and_name",
        ),
    ],
)
def test_label(info: GraphErrorInfo, label: str) -> None:
    assert info.label() == label


@pytest.mark.parametrize(
    ("tool", "errors_of"),
    [
        pytest.param(
            "notebook.graph",
            lambda out: [(e.cell_id, e.kind, tuple(e.names)) for e in out.errors],
            id="graph",
        ),
        pytest.param(
            # The edit answers the engine's own GraphSummary.
            "notebook.edit",
            lambda out: [
                (cid, e.code, (e.name,) if e.name else ())
                for cid, info in out.graph.cells.items()
                for e in info.errors
            ],
            id="edit_summary",
        ),
    ],
)
async def test_the_agent_tools_on_the_engine_read_code_and_name(
    tmp_path: Path, tool: str, errors_of: object
) -> None:
    engines = LocalEngines()
    host = engines(tmp_path, ActorRef(kind="agent", id="agent:t", display_name="Agent"))
    gate = RecordingGatekeeper()
    path = "nb.alknb.py"
    try:
        made = await call_tool(
            "notebook.create",
            validate("notebook.create", {"path": path, "cells": [{"source": "x = 1"}]}),
            host=host,
            gatekeeper=gate,
        )
        # The setup cell create adds, then the cell asked for.
        assert [c.kind for c in made.cells] == ["setup", "python"]  # type: ignore[attr-defined]
        first = made.cells[1].id  # type: ignore[attr-defined]
        edit = validate(
            "notebook.edit", {"path": path, "ops": [{"op": "insert", "source": "x = 2"}]}
        )
        out = await call_tool("notebook.edit", edit, host=host, gatekeeper=gate)
        if tool == "notebook.graph":
            out = await call_tool(
                "notebook.graph",
                validate("notebook.graph", {"path": path}),
                host=host,
                gatekeeper=gate,
            )
        errors = errors_of(out)  # type: ignore[operator]
        got = sorted(errors)
        second = next(cid for cid, _, _ in errors if cid != first)
        assert got == sorted(
            [(first, "multiple_definitions", ("x",)), (second, "multiple_definitions", ("x",))]
        )
    finally:
        await engines.close()
