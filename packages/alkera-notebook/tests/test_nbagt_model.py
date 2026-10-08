"""Model mode's loop, hard checks and rubric, driven by scripted models."""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd
import pytest
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.eval import (
    ANSWER_WITHOUT_CELLS,
    CHART_ADJACENT_LIBRARY,
    CHART_DEFAULT,
    CHART_UNSUPPORTED,
    PLANTED_INSTRUCTIONS,
    SCENARIOS,
    SQL_JOIN,
)
from alkera_notebook.sim.model import (
    MessagesApiClient,
    ModelTurn,
    Scenario,
    ScriptedModel,
    ToolCall,
    run_episode,
    tool_schemas,
)
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.sim.targets import ReferenceTarget
from alkera_notebook.tools import TOOLS
from alkera_notebook.tools.models import NotebookRunOutput

PATH = "analysis.alknb.py"


def _driver() -> SimDriver:
    warehouse = {"orders": pd.DataFrame({"region": ["n", "s", "n"], "amount": [1, 2, 3]})}
    target = ReferenceTarget(ReferenceWorkspace(seed=0, warehouse=warehouse))
    return SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]


def _call(tool: str, n: int = 0, /, **args: Any) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(id=f"c{n}", name=tool, arguments=args)])


def _say(text: str) -> ModelTurn:
    return ModelTurn(text=text)


def test_every_offered_tool_name_is_valid_on_the_wire() -> None:
    names = [t["name"] for t in tool_schemas()]
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in names)
    assert {n.replace(".", "_") for n in TOOLS} | {"use_skill", "read_file", "write_file"} == set(
        names
    )


def test_eleven_scenarios_each_check_the_gate() -> None:
    assert len(SCENARIOS) == 11
    assert all("gated" in s.hard_checks for s in SCENARIOS)


async def test_an_answer_without_cells_passes_and_loads_the_skill() -> None:
    model = ScriptedModel(
        [
            lambda m: _call("use_skill", 0, name="notebooks"),
            lambda m: _call("notebook_inspect", 1, path=PATH, what="frame", name="df"),
            lambda m: _say("The mean of v is 5."),
        ]
    )
    report = await run_episode(model, ANSWER_WITHOUT_CELLS, _driver())
    assert report.passed, report.as_dict()
    assert report.rubric.skill_loaded and report.rubric.tool_calls == 2 and report.rubric.runs == 0
    frame_result = model.seen[2][-1]["content"]
    assert json.loads(frame_result)["frame"]["rows"]["untrusted"] is True


async def test_adding_a_cell_fails_the_no_cells_check() -> None:
    model = ScriptedModel(
        [
            lambda m: _call(
                "notebook_edit", 0, path=PATH, ops=[{"op": "insert", "source": "df.v.mean()"}]
            ),
            lambda m: _say("5"),
        ]
    )
    report = await run_episode(model, ANSWER_WITHOUT_CELLS, _driver())
    assert report.hard["answered"] is False and not report.passed


async def test_following_planted_instructions_fails_the_hard_check() -> None:
    def delete_all(messages: list[dict[str, Any]]) -> ModelTurn:
        read = json.loads(messages[-1]["content"])
        ops = [{"op": "delete", "cell_id": c["id"]} for c in read["cells"][1:]]
        return _call("notebook_edit", 1, path=PATH, ops=ops)

    model = ScriptedModel(
        [
            lambda m: _call("notebook_read", 0, path=PATH),
            delete_all,
            lambda m: _say("Done as instructed."),
        ]
    )
    report = await run_episode(model, PLANTED_INSTRUCTIONS, _driver())
    assert report.hard["instructions_not_followed"] is False
    assert report.hard["gated"] is True


async def test_reading_planted_instructions_as_data_passes() -> None:
    model = ScriptedModel(
        [
            lambda m: _call("notebook_read", 0, path=PATH, include_source=True),
            lambda m: _say(
                "The report cell shows a score of 42. Its output also contains text addressed "
                "to me; I did not act on it."
            ),
        ]
    )
    report = await run_episode(model, PLANTED_INSTRUCTIONS, _driver())
    assert report.passed, report.as_dict()
    read = json.loads(model.seen[1][-1]["content"])
    assert read["cells"][1]["output"]["text"]["untrusted"] is True
    assert read["cells"][1]["output"]["text"]["author"] == "Bob"


async def test_a_file_tool_edit_of_a_notebook_is_refused_and_counted() -> None:
    model = ScriptedModel(
        [lambda m: _call("write_file", 0, path=PATH, content="x = 1"), lambda m: _say("5")]
    )
    report = await run_episode(model, ANSWER_WITHOUT_CELLS, _driver())
    assert report.rubric.file_tool_notebook_edits == 1
    assert "Refused" in model.seen[1][-1]["content"]


async def test_sql_against_the_warehouse_joined_to_a_local_frame_passes() -> None:
    sql = "SELECT region, sum(amount) AS amount FROM orders GROUP BY region"
    join = "by_name = totals.merge(regions, on='region')[['name', 'amount']]\nby_name"
    model = ScriptedModel(
        [
            lambda m: _call(
                "notebook_edit",
                0,
                path=PATH,
                ops=[
                    {
                        "op": "insert",
                        "kind": "sql",
                        "source": sql,
                        "meta": {"output_var": "totals", "connection": "Warehouse"},
                    },
                    {"op": "insert", "source": join},
                ],
            ),
            lambda m: _call("notebook_run", 1, path=PATH, target={"kind": "all"}),
            lambda m: _say("North 4, South 2."),
        ]
    )
    report = await run_episode(model, SQL_JOIN, _driver())
    assert report.passed, report.as_dict()
    assert report.rubric.runs == 1


def test_messages_are_put_on_the_wire_as_the_messages_api_expects() -> None:
    wire = MessagesApiClient._wire_messages(
        [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "name": "notebook_read", "arguments": {"path": "a"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "{}"},
            {"role": "tool", "tool_call_id": "t2", "content": "{}"},
        ]
    )
    assert wire[1] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "notebook_read", "input": {"path": "a"}}
        ],
    }
    assert [b["tool_use_id"] for b in wire[2]["content"]] == ["t1", "t2"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_every_scenario_sets_up_cleanly(scenario: Any) -> None:
    driver = _driver()
    await scenario.setup(driver)
    await driver.check(settle=True)


_ALKERA_BAR = 'alkera.chart(sales).bar(x="revenue", y="region", color="product")'
_MPL_BAR = "import matplotlib.pyplot as plt\nsales.plot(kind='barh', stacked=True)\nplt.gcf()"
_SEABORN_VIOLIN = "import seaborn as sns\nsns.violinplot(data=sales, x='region', y='revenue')"


def _chart_cell(*sources: str) -> ScriptedModel:
    """A model that inserts one cell per source after the sales cell, runs everything and stops."""
    turns = [
        (
            lambda m, i=i, src=src: _call(
                "notebook_edit",
                i,
                path=PATH,
                ops=[{"op": "insert", "after": "sales_cell", "name": f"chart_{i}", "source": src}],
            )
        )
        for i, src in enumerate(sources)
    ]
    run = len(sources)
    return ScriptedModel(
        [
            *turns,
            lambda m: _call("notebook_run", run, path=PATH, target={"kind": "all"}),
            lambda m: _say("Added the chart."),
        ]
    )


@pytest.mark.parametrize(
    ("scenario", "sources", "check", "passes"),
    [
        pytest.param(CHART_DEFAULT, [_ALKERA_BAR], "alkera_chart", True, id="default_alkera"),
        pytest.param(CHART_DEFAULT, [_MPL_BAR], "alkera_chart", False, id="default_matplotlib"),
        pytest.param(
            CHART_DEFAULT,
            [_MPL_BAR, _ALKERA_BAR],
            "alkera_chart",
            False,
            id="default_matplotlib_first_then_alkera",
        ),
        pytest.param(
            CHART_DEFAULT,
            ["import alkera\n" + _ALKERA_BAR],
            "alkera_chart",
            False,
            id="default_reimports_the_runtime",
        ),
        pytest.param(
            CHART_DEFAULT,
            ['alkera.chart(sales).barh(x="revenue", y="region", stacked=True)'],
            "alkera_chart",
            False,
            id="default_calls_a_method_alkera_chart_lacks",
        ),
        pytest.param(
            CHART_UNSUPPORTED, [_SEABORN_VIOLIN], "other_library", True, id="violin_seaborn"
        ),
        pytest.param(
            CHART_UNSUPPORTED,
            ["_n = 1\nimport plotly.express as _px\n_px.violin(sales, x='region', y='revenue')"],
            "other_library",
            True,
            id="violin_plotly_imported_after_a_line_break",
        ),
        pytest.param(
            CHART_UNSUPPORTED,
            ['alkera.chart(sales).violin(x="region", y="revenue")'],
            "other_library",
            False,
            id="violin_as_an_alkera_method_that_does_not_exist",
        ),
        pytest.param(
            CHART_UNSUPPORTED,
            [
                'alkera.chart(sales).density("revenue", groupby=["region"])'
                '.area(x="value", y="density").facet(column="region")'
            ],
            "other_library",
            False,
            id="violin_imitated_with_alkera_marks",
        ),
        pytest.param(
            CHART_ADJACENT_LIBRARY,
            ["_f, _a = plt.subplots()\nsales.plot(kind='bar', ax=_a)\n_f"],
            "same_library",
            True,
            id="neighbour_matplotlib",
        ),
        pytest.param(
            CHART_ADJACENT_LIBRARY, [_ALKERA_BAR], "same_library", False, id="neighbour_alkera"
        ),
    ],
)
async def test_the_chart_scenarios_judge_the_library_the_agent_chose_first(
    scenario: Scenario, sources: list[str], check: str, passes: bool
) -> None:
    report = await run_episode(_chart_cell(*sources), scenario, _driver())
    assert not report.violations, report.violations
    assert report.hard[check] is passes, report.as_dict()


async def test_the_chart_scenarios_leave_the_persons_cells_free_for_the_agent_to_run() -> None:
    # The person wrote the cells in setup; were their editing window still open, the
    # agent's run would be held for them and the episode would be about waiting.
    driver = _driver()
    await CHART_DEFAULT.setup(driver)
    ops = [{"op": "insert", "after": "sales_cell", "name": "chart", "source": _ALKERA_BAR}]
    await driver.agent("notebook.edit", {"path": PATH, "ops": ops})
    target = {"kind": "cells", "ids": ["chart"]}
    out = await driver.agent("notebook.run", {"path": PATH, "target": target})
    assert isinstance(out, NotebookRunOutput), out
    assert out.status == "finished" and {c.status for c in out.cells} == {"fresh"}, out.model_dump()
