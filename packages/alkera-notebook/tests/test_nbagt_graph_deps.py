"""What depends on what, asked of the real engine through the agent tools.

A diamond (``base`` feeds ``left`` and ``right``, which feed ``joined``) with a
chain after it (``joined`` to ``scaled`` to ``report``): the direct and the
transitive cells in each direction, the names that make each edge, the direct
cells a plain read names, a name defined twice, a cycle, and every link of a
300-cell chain inside the reply budget.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from alkera_notebook.envs.template import default_env_template
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.engine_target import EngineTarget
from alkera_notebook.tools import RecordingGatekeeper, call_tool, validate
from alkera_notebook.tools.paging import RESULT_BUDGET_BYTES
from alkera_notebook.tools.refs import resolve_args
from pydantic import ValidationError

# The module-scoped fixture is built once per worker; keep its tests together.
pytestmark = pytest.mark.xdist_group("nbagt_graph_deps")

DIAMOND = "diamond.alknb.py"
CHAIN = "chain.alknb.py"
BROKEN = "broken.alknb.py"
CHAIN_CELLS = 300

DIAMOND_CELLS: list[dict[str, Any]] = [
    {"kind": "setup", "source": "import alkera"},
    {"source": "sov_daily = 1\nfloor = 0", "name": "base"},
    {"source": "left_total = sov_daily + 1", "name": "left"},
    {"source": "right_total = sov_daily + floor", "name": "right"},
    {"source": "joined_total = left_total + right_total", "name": "joined"},
    {"source": "scaled_total = joined_total * 2", "name": "scaled"},
    {"source": "scaled_total", "name": "report"},
    {"source": "unrelated = 7", "name": "aside"},
]


class Graphs:
    """The notebooks, and the one way these tests call a tool on them."""

    def __init__(self, driver: SimDriver, ids: dict[str, dict[str, str]]) -> None:
        self.driver = driver
        self.ids = ids
        self.gate = RecordingGatekeeper(allow=frozenset())

    async def call(self, tool: str, path: str, **args: Any) -> dict[str, Any]:
        result = await call_tool(
            tool,
            validate(tool, {"path": path, **args}),
            host=self.driver.agent_host,
            gatekeeper=self.gate,
        )
        return result.model_dump(mode="json")

    def named(self, path: str, cells: list[str]) -> list[str]:
        names = {cid: name for name, cid in self.ids[path].items()}
        return [names[cid] for cid in cells]


@pytest.fixture(scope="module")
async def graphs(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Graphs]:
    root = tmp_path_factory.mktemp("graph_deps")
    template = default_env_template(root / "template", packages=())
    target = EngineTarget(root / "ws", default_template=template)
    driver = SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]
    chain = [{"kind": "setup", "source": "import alkera"}, {"source": "v0 = 0", "name": "c0"}]
    chain += [{"source": f"v{i} = v{i - 1} + 1", "name": f"c{i}"} for i in range(1, CHAIN_CELLS)]
    broken = [
        {"kind": "setup", "source": "import alkera"},
        {"source": "twice = 1", "name": "first_def"},
        {"source": "twice = 2", "name": "second_def"},
        {"source": "ping = pong + 1", "name": "ping_cell"},
        {"source": "pong = ping + 1", "name": "pong_cell"},
    ]
    ids: dict[str, dict[str, str]] = {}
    try:
        for path, cells in ((DIAMOND, DIAMOND_CELLS), (CHAIN, chain), (BROKEN, broken)):
            created = await driver.agent("notebook.create", {"path": path, "cells": cells})
            assert not isinstance(created, Exception), created
            ids[path] = {c.name: c.id for c in created.cells}  # type: ignore[union-attr]
        yield Graphs(driver, ids)
    finally:
        await driver.close()


async def test_everything_a_cell_depends_on_is_split_into_direct_and_transitive(
    graphs: Graphs,
) -> None:
    out = await graphs.call("notebook.graph", DIAMOND, cell="joined", direction="up", depth="all")
    assert graphs.named(DIAMOND, out["upstream_direct"]) == ["left", "right"]
    assert graphs.named(DIAMOND, out["upstream_transitive"]) == ["base"]
    assert graphs.named(DIAMOND, out["upstream"]) == ["left", "right", "base"]
    assert out["upstream_total"] == 3
    assert out["downstream"] == out["downstream_direct"] == out["downstream_transitive"] == []
    assert out["complete"] is True


async def test_everything_that_depends_on_a_cell_is_split_the_same_way(graphs: Graphs) -> None:
    out = await graphs.call("notebook.graph", DIAMOND, cell="base", direction="down", depth="all")
    assert graphs.named(DIAMOND, out["downstream_direct"]) == ["left", "right"]
    assert graphs.named(DIAMOND, out["downstream_transitive"]) == ["joined", "scaled", "report"]
    assert out["downstream_total"] == 5
    assert out["upstream"] == []
    # The cell nothing links to is in neither direction.
    assert graphs.ids[DIAMOND]["aside"] not in out["cells"]


async def test_each_edge_names_what_links_the_two_cells(graphs: Graphs) -> None:
    out = await graphs.call("notebook.graph", DIAMOND, cell="joined", direction="both", depth="all")
    ids = graphs.ids[DIAMOND]
    edges = {(a, b): via for a, b, via in out["edges"]}
    assert edges == {
        (ids["base"], ids["left"]): ["sov_daily"],
        (ids["base"], ids["right"]): ["floor", "sov_daily"],
        (ids["left"], ids["joined"]): ["left_total"],
        (ids["right"], ids["joined"]): ["right_total"],
        (ids["joined"], ids["scaled"]): ["joined_total"],
        (ids["scaled"], ids["report"]): ["scaled_total"],
    }


@pytest.mark.parametrize(
    ("depth", "transitive", "complete"),
    [
        pytest.param(None, [], False, id="default_is_direct_only_and_says_there_is_more"),
        pytest.param(1, [], False, id="one_link"),
        pytest.param(2, ["joined"], False, id="two_links"),
        pytest.param(4, ["joined", "scaled", "report"], True, id="enough_links_for_all"),
        pytest.param("all", ["joined", "scaled", "report"], True, id="all"),
    ],
)
async def test_a_number_of_links_still_works_and_says_when_it_stopped_short(
    graphs: Graphs, depth: int | str | None, transitive: list[str], complete: bool
) -> None:
    args = {} if depth is None else {"depth": depth}
    out = await graphs.call("notebook.graph", DIAMOND, cell="base", direction="down", **args)
    assert graphs.named(DIAMOND, out["downstream_direct"]) == ["left", "right"]
    assert graphs.named(DIAMOND, out["downstream_transitive"]) == transitive
    assert out["complete"] is complete


@pytest.mark.parametrize("depth", [0, 101, "everything", "ALL"])
def test_a_depth_that_is_neither_a_count_nor_all_is_refused(depth: object) -> None:
    with pytest.raises(ValidationError):
        validate("notebook.graph", {"path": DIAMOND, "cell": "base", "depth": depth})


async def test_a_plain_read_names_each_cells_direct_cells(graphs: Graphs) -> None:
    out = await graphs.call("notebook.read", DIAMOND)
    links = {c["name"]: (c["upstream"], c["downstream"]) for c in out["cells"]}
    assert links["base"] == ([], ["left", "right"])
    assert links["joined"] == (["left", "right"], ["scaled"])
    assert links["report"] == (["scaled"], [])
    assert links["aside"] == ([], [])
    assert all(c["links_omitted"] == 0 for c in out["cells"])


async def test_the_run_targets_are_the_same_sets(graphs: Graphs) -> None:
    """``notebook.run`` with target upstream plans the cell and what the graph
    calls its upstream (direct and transitive), and nothing else."""
    port = await graphs.driver.agent_host.open(graphs.driver.agent_host.resolve(DIAMOND))
    ids = graphs.ids[DIAMOND]
    for kind, direction in (("upstream", "up"), ("downstream", "down")):
        graph = await graphs.call(
            "notebook.graph", DIAMOND, cell="joined", direction=direction, depth="all"
        )
        run = validate("notebook.run", {"path": DIAMOND, "target": {"kind": kind, "id": "joined"}})
        resolved = await resolve_args(run, port)
        assert set(resolved.target.ids) == {ids["joined"], *graph[kind]}  # type: ignore[attr-defined]


async def test_a_name_defined_twice_and_a_cycle_are_errors_the_graph_still_answers_with(
    graphs: Graphs,
) -> None:
    ids = graphs.ids[BROKEN]
    out = await graphs.call("notebook.graph", BROKEN, cell="ping_cell", depth="all")
    kinds = {(e["kind"], tuple(e["names"])) for e in out["errors"]}
    assert ("multiple_definitions", ("twice",)) in kinds
    assert any(kind == "cycle" for kind, _names in kinds)
    # In a cycle each cell is both upstream and downstream of the other, once.
    assert out["upstream_direct"] == [ids["pong_cell"]]
    assert out["downstream_direct"] == [ids["pong_cell"]]
    assert out["upstream_transitive"] == out["downstream_transitive"] == []
    assert {(a, b): via for a, b, via in out["edges"]} == {
        (ids["pong_cell"], ids["ping_cell"]): ["pong"],
        (ids["ping_cell"], ids["pong_cell"]): ["ping"],
    }


async def test_every_link_of_a_long_chain_is_answered_within_the_reply_budget(
    graphs: Graphs,
) -> None:
    ids = graphs.ids[CHAIN]
    out = await graphs.call("notebook.graph", CHAIN, cell="c0", direction="down", depth="all")
    assert len(json.dumps(out)) <= RESULT_BUDGET_BYTES
    assert out["downstream_total"] == CHAIN_CELLS - 1
    assert out["downstream_direct"] == [ids["c1"]]
    assert out["downstream_transitive"] == [ids[f"c{i}"] for i in range(2, CHAIN_CELLS)]
    assert out["complete"] is True
    # The edges are a page; walking it reaches every link once, each with its name.
    edges = list(out["edges"])
    page = out["page"]
    assert page["total"] == CHAIN_CELLS - 1
    while page["next_offset"] is not None:
        more = await graphs.call(
            "notebook.graph",
            CHAIN,
            cell="c0",
            direction="down",
            depth="all",
            offset=page["next_offset"],
        )
        assert len(json.dumps(more)) <= RESULT_BUDGET_BYTES
        edges.extend(more["edges"])
        page = more["page"]
    assert len(edges) == CHAIN_CELLS - 1
    assert {(a, b): via for a, b, via in edges} == {
        (ids[f"c{i}"], ids[f"c{i + 1}"]): [f"v{i}"] for i in range(CHAIN_CELLS - 1)
    }
    assert graphs.gate.asked == []
