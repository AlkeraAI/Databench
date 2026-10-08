"""A person sees a cell by its name, else its position, never its internal id.

``cell_display_name`` is the one function that says how. The source gate that
holds every person-facing module to it lives with the box's notebook tests
(``apps/cli/tests/notebooks/test_notebook_cell_names_gate.py``), because it
reads modules outside this package.
"""

from __future__ import annotations

import pytest
from alkera_notebook.cell_names import CellRefError, cell_display_name, resolve_cell_ref
from alkera_notebook.tools.gates import render_plan
from alkera_notebook.tools.port import PlanPreview, PlanStepRecord


@pytest.mark.parametrize(
    ("name", "index", "shown"),
    [
        pytest.param("load", 3, "load", id="named"),
        pytest.param("load", None, "load", id="named-unplaced"),
        pytest.param("_", 0, "Cell 1", id="anonymous-first"),
        pytest.param("_", 3, "Cell 4", id="anonymous-fourth"),
        pytest.param("", 1, "Cell 2", id="empty-name"),
        pytest.param(None, 1, "Cell 2", id="no-name"),
        pytest.param("_", None, "A cell", id="anonymous-unplaced"),
        pytest.param("_", -1, "A cell", id="negative-index"),
    ],
)
def test_cell_display_name(name: str | None, index: int | None, shown: str) -> None:
    assert cell_display_name(name, index) == shown


def test_the_run_plan_names_cells_for_a_person() -> None:
    plan = PlanPreview(
        steps=[
            PlanStepRecord(
                cell_id="a7yg9x7evz", name="_", reason="upstream", index=1, code="x = 1"
            ),
            PlanStepRecord(
                cell_id="007ferqvhc", name="load", reason="target", index=2, code="y = x"
            ),
        ]
    )
    text = render_plan(plan)
    assert "Cell 2, needed by a cell asked for:" in text
    assert "load, asked for:" in text
    assert "a7yg9x7evz" not in text and "007ferqvhc" not in text
    # Plain lines, not markdown headings: a card renders this text as prose.
    assert not any(line.startswith("#") for line in text.splitlines())


CELLS = [("a7yg9x7evz", "_"), ("b8zh0y8fw0", "load"), ("c9a01z9gx1", "_"), ("d0b12a0hy2", "last")]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        pytest.param("b8zh0y8fw0", "b8zh0y8fw0", id="id"),
        pytest.param("load", "b8zh0y8fw0", id="name"),
        pytest.param(" load ", "b8zh0y8fw0", id="name-with-spaces"),
        pytest.param("first", "a7yg9x7evz", id="first"),
        pytest.param("FIRST", "a7yg9x7evz", id="first-any-case"),
        pytest.param("Cell 3", "c9a01z9gx1", id="cell-n-as-shown"),
        pytest.param("cell3", "c9a01z9gx1", id="cell-n-no-space"),
        pytest.param("1", "a7yg9x7evz", id="bare-position"),
        # A cell named "last" is that cell: a name wins over a word.
        pytest.param("last", "d0b12a0hy2", id="name-beats-word"),
    ],
)
def test_a_cell_is_addressed_every_way_it_is_shown(ref: str, expected: str) -> None:
    assert resolve_cell_ref(ref, CELLS) == expected


def test_last_is_the_last_cell_when_no_cell_is_named_last() -> None:
    cells = CELLS[:3]
    assert resolve_cell_ref("last", cells) == "c9a01z9gx1"


def test_every_shown_name_resolves_back_to_its_cell() -> None:
    for index, (cid, name) in enumerate(CELLS):
        assert resolve_cell_ref(cell_display_name(name, index), CELLS) == cid


@pytest.mark.parametrize(
    ("ref", "cells", "code"),
    [
        pytest.param("_", CELLS, "cell_not_found", id="anonymous-name-names-no-one"),
        pytest.param("Cell 5", CELLS, "cell_not_found", id="past-the-end"),
        pytest.param("0", CELLS, "cell_not_found", id="positions-count-from-one"),
        pytest.param("nope", CELLS, "cell_not_found", id="unknown"),
        pytest.param("last", [], "cell_not_found", id="empty-notebook"),
        pytest.param("x", [("a", "x"), ("b", "x")], "ambiguous_cell", id="shared-name"),
    ],
)
def test_a_reference_to_no_single_cell_is_refused(
    ref: str, cells: list[tuple[str, str]], code: str
) -> None:
    with pytest.raises(CellRefError) as raised:
        resolve_cell_ref(ref, cells)
    assert raised.value.code == code
