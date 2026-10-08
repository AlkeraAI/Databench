"""The notebook guide: the essentials of the ``notebooks`` skill, drawn from
the skill's own text, delivered with the first notebook tool result of each
conversation and never again in it."""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd
import pytest
from alkera_notebook.sim.driver import SimDriver, StanceGatekeeper
from alkera_notebook.sim.eval import ANSWER_WITHOUT_CELLS
from alkera_notebook.sim.model import ModelTurn, ScriptedModel, ToolCall, run_episode
from alkera_notebook.sim.reference import ReferenceWorkspace
from alkera_notebook.sim.targets import ReferenceTarget
from alkera_notebook.tools import (
    NOTEBOOK_GUIDE,
    PROMPT_BLOCK,
    SKILL_BODY,
    TOOLS,
    GuideLedger,
    validate,
)
from alkera_notebook.tools.guidance import GUIDE_SECTIONS, guide_from, skill_sections
from alkera_notebook.tools.models import (
    NotebookCellBrief,
    NotebookCreateOutput,
    NotebookGuide,
    NotebookSettingsOutput,
)

PATH = "analysis.alknb.py"


def test_the_guide_is_the_named_sections_of_the_skill_word_for_word() -> None:
    sections = skill_sections(SKILL_BODY)
    for name in GUIDE_SECTIONS:
        assert sections[name] in NOTEBOOK_GUIDE.text
    for name in set(sections) - set(GUIDE_SECTIONS):
        assert sections[name] not in NOTEBOOK_GUIDE.text
    assert 'use_skill "notebooks"' in NOTEBOOK_GUIDE.text.splitlines()[0]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(PROMPT_BLOCK, id="always_on_block"),
        pytest.param(SKILL_BODY, id="skill"),
        pytest.param(NOTEBOOK_GUIDE.text, id="first_result_guide"),
    ],
)
def test_every_tool_the_guidance_names_is_one_the_catalog_serves(text: str) -> None:
    # A renamed or removed tool must not survive in what the agent is told to call.
    named = set(re.findall(r"\bnotebook\.[a-z_]+", text))
    assert named, "the guidance names no tools, so this check would pass on nothing"
    assert named <= set(TOOLS), sorted(named - set(TOOLS))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(PROMPT_BLOCK, id="always_on_block"),
        pytest.param(NOTEBOOK_GUIDE.text, id="first_result_guide"),
    ],
)
def test_the_agent_is_told_how_to_show_a_result_and_link_the_notebook_before_it_loads_the_skill(
    text: str,
) -> None:
    # Both reach the agent without a use_skill call: the block on every root
    # turn, the guide with the first notebook result (a subagent's too).
    assert "notebook.show_output" in text
    [link] = re.findall(r"`\[[^\]]+\]\(([^)]+)\)`", text)
    # The example link is one the notebook tools would accept as a path.
    validate("notebook.read", {"path": link})
    assert link.endswith(".alknb.py")


def test_when_to_use_a_notebook_leads_the_guide() -> None:
    sections = skill_sections(SKILL_BODY)
    body = NOTEBOOK_GUIDE.text
    assert body.index(sections["When to use a notebook"]) < body.index(sections["The model"])


def test_the_version_follows_the_text() -> None:
    same = guide_from(SKILL_BODY)
    edited = guide_from(SKILL_BODY.replace("exactly one cell", "one cell only"))
    fewer = guide_from(SKILL_BODY, ("The model",))
    assert same == NOTEBOOK_GUIDE
    assert edited.version != NOTEBOOK_GUIDE.version
    assert fewer.version != NOTEBOOK_GUIDE.version
    assert len(NOTEBOOK_GUIDE.version) == 12


def test_a_section_the_skill_lost_is_an_error() -> None:
    renamed = SKILL_BODY.replace("## Running", "## Running cells")
    with pytest.raises(ValueError, match="Running"):
        guide_from(renamed)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "intro\n## A\none\n## B\ntwo\nthree",
            {"A": "## A\none", "B": "## B\ntwo\nthree"},
            id="two_sections_after_an_intro",
        ),
        pytest.param("no headings at all", {}, id="none"),
        pytest.param("## Only\n", {"Only": "## Only"}, id="empty_section"),
    ],
)
def test_skill_sections(body: str, expected: dict[str, str]) -> None:
    assert skill_sections(body) == expected


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_tool_result_can_carry_the_guide_and_defaults_to_none(tool: str) -> None:
    field = TOOLS[tool].output.model_fields["notebook_guide"]
    assert field.default is None
    schema = json.dumps(TOOLS[tool].output.model_json_schema())
    assert "NotebookGuide" in schema


def _created() -> NotebookCreateOutput:
    return NotebookCreateOutput(
        path=PATH,
        token="t",
        cells=[NotebookCellBrief(id="a" * 10, name="_", kind="python", index=0)],
    )


@pytest.mark.parametrize(
    ("calls", "carries"),
    [
        pytest.param(["c1", "c1", "c1"], [True, False, False], id="once_per_conversation"),
        pytest.param(["c1", "c2", "c1", "c2"], [True, True, False, False], id="each_conversation"),
        pytest.param(["", ""], [True, False], id="an_unnamed_conversation_is_one_too"),
    ],
)
def test_the_ledger_gives_the_guide_once_per_conversation(
    calls: list[str], carries: list[bool]
) -> None:
    ledger = GuideLedger()
    got = [ledger.attach(_created(), c).notebook_guide is not None for c in calls]
    assert got == carries


def test_a_result_that_is_not_a_notebook_result_neither_carries_nor_spends_it() -> None:
    ledger = GuideLedger()
    plain: dict[str, Any] = {"ok": True}
    assert ledger.attach(plain, "c1") is plain
    assert ledger.attach(_created(), "c1").notebook_guide == NOTEBOOK_GUIDE


def test_the_ledger_gives_the_guide_it_was_made_with_and_leaves_the_result_alone() -> None:
    custom = NotebookGuide(version="v", text="t")
    original = NotebookSettingsOutput(path=PATH, settings={})
    out = GuideLedger(custom).attach(original, "c1")
    assert out.notebook_guide == custom
    assert original.notebook_guide is None


def _driver() -> SimDriver:
    warehouse = {"orders": pd.DataFrame({"region": ["n", "s", "n"], "amount": [1, 2, 3]})}
    target = ReferenceTarget(ReferenceWorkspace(seed=0, warehouse=warehouse))
    return SimDriver(target, gatekeeper=StanceGatekeeper("default"))  # type: ignore[arg-type]


def _call(tool: str, n: int, **args: Any) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(id=f"c{n}", name=tool, arguments=args)])


async def test_an_episode_hands_the_model_the_guide_with_its_first_notebook_result() -> None:
    model = ScriptedModel(
        [
            lambda m: _call("notebook_read", 0, path="missing.alknb.py"),
            lambda m: _call("notebook_read", 1, path=PATH),
            lambda m: _call("notebook_graph", 2, path=PATH),
            lambda m: ModelTurn(text="Done."),
        ]
    )
    report = await run_episode(model, ANSWER_WITHOUT_CELLS, _driver())
    results = [m["content"] for m in model.seen[-1] if m["role"] == "tool"]
    assert results[0].startswith("Error"), "a failed call does not spend the guide"
    first, second = json.loads(results[1]), json.loads(results[2])
    assert first["notebook_guide"] == NOTEBOOK_GUIDE.model_dump()
    assert second["notebook_guide"] is None
    assert report.rubric.guide_delivered is True


# -- which library draws a chart, and which imports the setup cell owns ----------------------

_CHARTS = skill_sections(SKILL_BODY).get("Charts", "")
_CHART_EXAMPLE = re.compile(r"`(alkera\.chart\([^`]*)`")


def _listed_chart_types() -> set[str]:
    [types_line] = [b for b in re.split(r"\n- ", _CHARTS) if b.startswith("alkera.chart draws")]
    return set(re.findall(r"`([a-z_]+)`", types_line))


def test_the_chart_rule_reaches_the_agent_with_its_first_notebook_result() -> None:
    # The rule lives in one place, the skill, and the first-result guide carries it
    # whole, so an agent that never loads the skill still has it before it writes a chart.
    assert _CHARTS, "the skill has no Charts section"
    assert _CHARTS in NOTEBOOK_GUIDE.text
    # Its three exceptions are the only cases where another library is right.
    [exceptions] = [b for b in re.split(r"\n- ", _CHARTS) if "another plotting library" in b]
    assert len(re.findall(r"\n  - ", exceptions)) == 3


@pytest.mark.parametrize(
    "text",
    [pytest.param(PROMPT_BLOCK, id="always_on_block")]
    + [pytest.param(d.description, id=name) for name, d in sorted(TOOLS.items())],
)
def test_no_other_guidance_restates_the_chart_rule(text: str) -> None:
    # The skill is the one place: a second copy drifts from it, as the edit tool's once did.
    assert "alkera.chart(" not in text and "alkera.chart draws" not in text


def test_every_chart_type_the_guide_lists_is_one_alkera_chart_draws() -> None:
    from alkera.chart import Chart, profile

    listed = _listed_chart_types()
    assert listed, "the Charts section lists no chart types"
    assert all(callable(getattr(Chart, name, None)) for name in listed), sorted(
        n for n in listed if not callable(getattr(Chart, n, None))
    )
    # A mark the chart profile admits and the builder draws must be listed, so a
    # new chart type is offered to the agent instead of sending it to matplotlib.
    drawable = {mark for mark in profile()["marks"] if callable(getattr(Chart, mark, None))}
    assert drawable <= listed, sorted(drawable - listed)


def test_the_guides_chart_example_draws_a_valid_chart() -> None:
    import alkera

    [example] = _CHART_EXAMPLE.findall(_CHARTS)
    sales = pd.DataFrame(
        {"region": ["n", "n", "s"], "product": ["a", "b", "a"], "revenue": [5, 3, 4]}
    )
    chart = eval(example, {"alkera": alkera, "sales": sales})
    spec = alkera.chart.validate(chart.to_dict())
    assert spec["mark"] == "bar"
    assert spec["encoding"]["y"]["type"] == "nominal"  # horizontal, as the guide says
    assert spec["encoding"]["color"]["field"] == "product"  # stacked by product


def test_the_guide_names_the_import_every_setup_cell_has_and_a_second_one_is_an_error() -> None:
    from alkera_notebook.format import RUNTIME_IMPORT, analyze_code
    from alkera_notebook.tools.functions import new_notebook_cells

    writing = skill_sections(SKILL_BODY)["Writing cells"]
    assert f"`{RUNTIME_IMPORT}`" in writing
    assert writing in NOTEBOOK_GUIDE.text
    # What the guide claims: a new notebook's setup cell has the import ...
    [setup] = new_notebook_cells([])
    assert RUNTIME_IMPORT in setup.source.splitlines()
    # ... and importing it again in another cell is a second definition.
    graph = analyze_code([("setup", setup.source), ("chart", f"{RUNTIME_IMPORT}\nx = 1")])
    assert {"code": "multiple_definitions", "name": "alkera", "cells": ["chart", "setup"]} in (
        graph["cells"]["chart"]["errors"]
    )
