"""The planner, pure: which cells run, with which code, in which order."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.format import analyze_code
from alkera_notebook.plan import PlanCell, PlanInput, make_plan, scope_targets, widget_targets


def cell(
    cid: str,
    code: str,
    *,
    status: str = "fresh",
    submitted: str | None | bool = True,
    value: bool | None = None,
    **kw: Any,
) -> PlanCell:
    sub = code if submitted is True else (None if submitted is False else submitted)
    holds = value if value is not None else status in ("fresh", "stale")
    return PlanCell(
        id=cid,
        name=kw.pop("name", "_"),
        current_code=code,
        submitted_code=sub,
        status=status,  # type: ignore[arg-type]
        holds_value=holds,
        **kw,
    )


def plan(cells: list[PlanCell], targets: list[str], **kw: Any) -> Any:
    scope = kw.pop("scope", "cells")
    return make_plan(PlanInput(cells=cells, scope=scope, targets=targets, **kw), analyze_code)


def ran(p: Any) -> list[tuple[str, str]]:
    return [(s.cell_id, s.reason) for s in p.steps]


CHAIN = [cell("a", "x = 1"), cell("b", "y = x + 1"), cell("c", "z = y + 1")]


def test_not_run_upstream_runs_first_with_current_text() -> None:
    cells = [
        cell("a", "x = 1", status="not_run", submitted=False),
        cell("b", "y = x + 1", status="not_run", submitted=False),
    ]
    p = plan(cells, ["b"])
    assert ran(p) == [("a", "upstream"), ("b", "target")]
    assert p.steps[0].code == "x = 1"


def test_fresh_upstream_is_not_rerun() -> None:
    assert ran(plan(CHAIN, ["b"])) == [("b", "target"), ("c", "descendant")]


def test_edited_ancestor_holding_a_value_is_not_rerun() -> None:
    cells = [cell("a", "x = 2", submitted="x = 1", status="fresh"), *CHAIN[1:]]
    p = plan(cells, ["b"])
    assert [s.cell_id for s in p.steps] == ["b", "c"]


def test_autorun_descendants_use_submitted_code_not_live_text() -> None:
    cells = [CHAIN[0], cell("b", "y = x + 100", submitted="y = x + 1"), CHAIN[2]]
    p = plan(cells, ["a"])
    assert ran(p) == [("a", "target"), ("b", "descendant"), ("c", "descendant")]
    assert p.steps[1].code == "y = x + 1"


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("not_run", id="not_run"),
        pytest.param("error", id="error"),
        pytest.param("skipped", id="skipped"),
    ],
)
def test_autorun_leaves_descendants_without_values_alone(status: str) -> None:
    cells = [CHAIN[0], cell("b", "y = x + 1", status=status, value=False), CHAIN[2]]
    p = plan(cells, ["a"])
    assert ran(p) == [("a", "target")]
    # c holds a value below a running cell it is not re-run with: stale.
    assert p.stale == ("c",)


def test_autorun_skips_a_descendant_someone_is_editing() -> None:
    cells = [CHAIN[0], cell("b", "y = LIVE", submitted="y = x + 1"), CHAIN[2]]
    p = plan(cells, ["a"], editing={"b": "Bob"})
    assert ran(p) == [("a", "target")]
    assert p.skipped_editing == ("b",)
    assert "b" in p.stale and "c" in p.stale
    assert all("LIVE" not in s.code for s in p.steps)


def test_upstream_never_run_and_being_edited_refuses() -> None:
    cells = [
        cell("a", "x = 1", status="not_run", submitted=False, name="load"),
        cell("b", "y = x", status="not_run", submitted=False),
    ]
    p = plan(cells, ["b"], editing={"a": "Bob"})
    assert p.steps == ()
    assert p.refusal is not None
    assert p.refusal.code == "upstream_being_edited"
    assert p.refusal.message == "cell `load` is being edited by Bob"


def test_the_requester_s_own_caret_never_refuses_their_run_but_holds_a_descendant() -> None:
    upstream = [
        cell("a", "x = 1", status="not_run", submitted=False, name="load"),
        cell("b", "y = x", status="not_run", submitted=False),
    ]
    p = plan(upstream, ["b"], editing={"a": "Ann"}, own_caret=frozenset({"a"}))
    assert p.refusal is None
    assert ran(p) == [("a", "upstream"), ("b", "target")]
    below = [CHAIN[0], cell("b", "y = LIVE", submitted="y = x + 1"), CHAIN[2]]
    p = plan(below, ["a"], editing={"b": "Ann"}, own_caret=frozenset({"b"}))
    assert ran(p) == [("a", "target")]
    assert p.skipped_editing == ("b",)


def test_upstream_that_ran_but_errored_reruns_its_submitted_code_even_when_edited() -> None:
    cells = [
        cell("a", "x = LIVE", submitted="x = 1", status="error", value=False),
        cell("b", "y = x", status="not_run", submitted=False),
    ]
    p = plan(cells, ["b"], editing={"a": "Bob"})
    assert p.refusal is None
    assert p.steps[0].code == "x = 1"


def test_lazy_marks_descendants_stale_and_runs_none() -> None:
    p = plan(CHAIN, ["a"], reactivity="lazy")
    assert ran(p) == [("a", "target")]
    assert p.stale == ("b", "c")


def test_diamond_runs_each_cell_once_in_topological_order() -> None:
    cells = [
        cell("a", "x = 1"),
        cell("d", "w = y + z"),
        cell("b", "y = x"),
        cell("c", "z = x"),
    ]
    p = plan(cells, ["a"])
    ids = [s.cell_id for s in p.steps]
    assert sorted(ids) == ["a", "b", "c", "d"]
    assert ids.index("d") > ids.index("b") and ids.index("d") > ids.index("c")
    assert ids == ["a", "b", "c", "d"]


def test_disabled_cells_and_their_descendants_never_run() -> None:
    cells = [CHAIN[0], cell("b", "y = x + 1", disabled=True), CHAIN[2]]
    assert ran(plan(cells, ["a"])) == [("a", "target")]
    assert ran(plan(cells, ["b"])) == []
    assert ran(plan(cells, ["c"])) == []


@pytest.mark.parametrize(
    ("scope", "targets", "expected"),
    [
        pytest.param("all", [], ["a", "b", "c", "d"], id="all"),
        pytest.param("above", ["c"], ["a", "b"], id="above"),
        pytest.param("below", ["b"], ["b", "c", "d"], id="below"),
        pytest.param("stale", [], ["b", "d"], id="stale"),
    ],
)
def test_scopes(scope: str, targets: list[str], expected: list[str]) -> None:
    cells = [
        cell("a", "x = 1"),
        cell("b", "y = 2", submitted="y = 1"),  # edited
        cell("c", "z = 3"),
        cell("d", "w = 4", status="not_run", submitted=False),
    ]
    inp = PlanInput(cells=cells, scope=scope, targets=targets)  # type: ignore[arg-type]
    assert scope_targets(inp) == expected


def test_run_all_submits_every_current_text() -> None:
    cells = [cell("a", "x = 2", submitted="x = 1"), cell("b", "y = x", submitted="y = 0")]
    p = plan(cells, [], scope="all")
    assert [(s.cell_id, s.code, s.reason) for s in p.steps] == [
        ("a", "x = 2", "target"),
        ("b", "y = x", "target"),
    ]


def test_clear_names_previous_defs_of_planned_steps_and_deleted_cells() -> None:
    p = plan(
        CHAIN, ["b"], prev_defs={"a": ["x"], "b": ["y", "old"], "c": ["z"]}, deleted_defs=["gone"]
    )
    assert p.clear == ("y", "old", "z", "gone")


def test_kernel_graph_uses_targets_new_code() -> None:
    # b's new code no longer reads x, so a (not run) is not pulled upstream.
    cells = [
        cell("a", "x = 1", status="not_run", submitted=False),
        cell("b", "y = 5", submitted="y = x", status="fresh"),
    ]
    assert ran(plan(cells, ["b"])) == [("b", "target")]


def test_cost_guard_counts_implicit_steps_only() -> None:
    a = cell("a", "x = 1", status="not_run", submitted=False, duration_s=45)
    b = cell("b", "y = x", status="not_run", submitted=False, duration_s=500)
    c_no_value = cell("c", "z = y", status="not_run", submitted=False, duration_s=20)
    c_value = cell("c", "z = y", status="fresh", duration_s=20)
    # The target's own 500 s never counts; upstream a alone (45 s) is under 60.
    assert not plan([a, b, c_no_value], ["b"], cost_guard_seconds=60).needs_confirmation
    # Upstream a (45) plus descendant c (20) pass 60.
    p = plan([a, b, c_value], ["b"], cost_guard_seconds=60)
    assert p.needs_confirmation and p.confirmation_reasons == ("duration",)
    assert p.estimate_s == 565
    confirmed = plan([a, b, c_value], ["b"], cost_guard_seconds=60, confirm_expensive=True)
    assert not confirmed.needs_confirmation
    assert [s.cell_id for s in confirmed.steps] == ["a", "b", "c"]


def test_cost_guard_metered_sql_upstream_needs_confirmation() -> None:
    sql = cell(
        "a", "df = q()", status="not_run", submitted=False, kind="sql", connection="Warehouse"
    )
    p = plan(
        [sql, cell("b", "y = df", status="not_run", submitted=False)],
        ["b"],
        metered_connections=frozenset({"Warehouse"}),
    )
    assert p.needs_confirmation and p.confirmation_reasons == ("metered_sql",)
    # The same SQL cell as the target itself is not implicit.
    assert not plan([sql], ["a"], metered_connections=frozenset({"Warehouse"})).needs_confirmation


def test_widget_trigger_runs_targets_with_submitted_code() -> None:
    cells = [cell("a", "s = slider"), cell("b", "y = s * LIVE", submitted="y = s * 2")]
    targets = widget_targets(
        make_plan(PlanInput(cells=cells, scope="cells", targets=[]), analyze_code).graph, ["s"]
    )
    assert targets == ["b"]
    p = plan(cells, targets, target_code="submitted")
    assert [s.code for s in p.steps] == ["y = s * 2"]


def test_unknown_target_is_refused() -> None:
    p = plan(CHAIN, ["nope"])
    assert p.refusal is not None and p.refusal.code == "unknown_cell"


def test_identical_inputs_give_identical_plans() -> None:
    a = plan(CHAIN, ["a"], prev_defs={"a": ["x"]})
    b = plan(list(CHAIN), ["a"], prev_defs={"a": ["x"]})
    assert a == b
