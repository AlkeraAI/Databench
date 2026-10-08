"""The simulator's oracle document: the operation rules it checks the store by."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.sim.oracle import DocumentModel, OpError
from alkera_notebook.tools.models import NotebookOp
from pydantic import TypeAdapter

OPS = TypeAdapter(list[NotebookOp])


def _doc(*sources: str) -> tuple[DocumentModel, list[str]]:
    counter = iter(f"c{i:09d}" for i in range(1000))
    doc, created, _ = DocumentModel().apply(
        OPS.validate_python([{"op": "insert", "source": s} for s in sources]), lambda: next(counter)
    )
    return doc, created


def _apply(doc: DocumentModel, ops: list[dict[str, Any]]) -> DocumentModel:
    counter = iter(f"n{i:09d}" for i in range(1000))
    return doc.apply(OPS.validate_python(ops), lambda: next(counter))[0]


@pytest.mark.parametrize(
    ("ops", "code", "index"),
    [
        pytest.param(
            [{"op": "edit", "cell_id": "zz", "edits": [{"old": "a", "new": "b"}]}],
            "cell_not_found",
            0,
            id="missing_cell",
        ),
        pytest.param(
            [{"op": "edit", "cell_id": "{0}", "edits": [{"old": "nope", "new": "b"}]}],
            "edit_not_found",
            0,
            id="old_missing",
        ),
        pytest.param(
            [{"op": "edit", "cell_id": "{1}", "edits": [{"old": "a", "new": "b"}]}],
            "edit_ambiguous",
            0,
            id="old_twice",
        ),
        pytest.param(
            [
                {
                    "op": "edit",
                    "cell_id": "{1}",
                    "edits": [{"old": "a", "new": "b", "occurrence": 3}],
                }
            ],
            "edit_not_found",
            0,
            id="occurrence_past_end",
        ),
        pytest.param([{"op": "insert", "name": "1bad"}], "invalid_name", 0, id="invalid_name"),
        pytest.param([{"op": "insert", "name": "class"}], "invalid_name", 0, id="keyword_name"),
        pytest.param([{"op": "insert", "kind": "chart"}], "unknown_kind", 0, id="unknown_kind"),
        pytest.param(
            [{"op": "insert", "kind": "setup", "after": "{0}"}],
            "setup_must_be_first",
            0,
            id="setup_not_first",
        ),
        pytest.param(
            [{"op": "set_config", "cell_id": "{0}", "config": {"disabled": "yes"}}],
            "invalid_config",
            0,
            id="config_type",
        ),
        pytest.param(
            [{"op": "set_setting", "key": "reactivity", "value": "eager"}],
            "invalid_config",
            0,
            id="setting_value",
        ),
        pytest.param(
            [{"op": "insert", "source": "z = 1"}, {"op": "delete", "cell_id": "zz"}],
            "cell_not_found",
            1,
            id="second_op_fails",
        ),
    ],
)
def test_refusals_name_the_op_and_leave_the_document_alone(
    ops: list[dict[str, Any]], code: str, index: int
) -> None:
    doc, ids = _doc("x = 1", "a = a")
    filled = [
        {k: (v.format(*ids) if isinstance(v, str) else v) for k, v in op.items()} for op in ops
    ]
    before = doc.signature()
    with pytest.raises(OpError) as raised:
        _apply(doc, filled)
    assert (raised.value.code, raised.value.index) == (code, index)
    assert doc.signature() == before


def test_occurrence_picks_one_of_several() -> None:
    doc, ids = _doc("a = a")
    after = _apply(
        doc,
        [{"op": "edit", "cell_id": ids[0], "edits": [{"old": "a", "new": "b", "occurrence": 2}]}],
    )
    assert after.cells[ids[0]].source == "a = b"


def test_delete_then_restore_returns_the_cell_where_it_was_with_its_id() -> None:
    doc, ids = _doc("a = 1", "b = 2", "c = 3")
    gone = _apply(doc, [{"op": "delete", "cell_id": ids[1]}])
    assert [c.id for c in gone.live()] == [ids[0], ids[2]]
    back = _apply(gone, [{"op": "restore", "cell_id": ids[1]}])
    assert [c.id for c in back.live()] == ids


def test_move_to_its_own_position_changes_nothing() -> None:
    doc, ids = _doc("a = 1", "b = 2")
    assert (
        _apply(doc, [{"op": "move", "cell_id": ids[0], "before": ids[0]}]).signature()
        == doc.signature()
    )


def test_a_setup_cell_is_named_setup() -> None:
    doc = _apply(DocumentModel(), [{"op": "insert", "kind": "setup", "source": "import x"}])
    assert doc.live()[0].name == "setup"


def test_rename_to_underscore_and_duplicate_names_are_allowed() -> None:
    doc, ids = _doc("a = 1", "b = 2")
    doc = _apply(
        doc,
        [
            {"op": "rename", "cell_id": ids[0], "name": "load"},
            {"op": "rename", "cell_id": ids[1], "name": "load"},
        ],
    )
    assert [c.name for c in doc.live()] == ["load", "load"]
    doc = _apply(doc, [{"op": "rename", "cell_id": ids[0], "name": "_"}])
    assert doc.live()[0].name == "_"


def _over_the_cap() -> DocumentModel:
    from alkera_notebook.sim.oracle import MAX_CELLS, Cell

    doc = DocumentModel()
    for i in range(MAX_CELLS + 10):
        cid = f"c{i:09d}"
        doc.cells[cid] = Cell(id=cid, kind="python", name=f"c{i}", source=f"x{i} = {i}")
        doc.order.append(cid)
    return doc


def test_a_notebook_loaded_over_the_cell_cap_can_still_be_edited() -> None:
    doc = _apply(_over_the_cap(), [{"op": "replace", "cell_id": "c000000001", "source": "x1 = 2"}])
    assert doc.cells["c000000001"].source == "x1 = 2"


def test_an_insert_past_the_cell_cap_is_refused() -> None:
    with pytest.raises(OpError) as caught:
        _apply(_over_the_cap(), [{"op": "insert", "source": "y = 1"}])
    assert caught.value.code == "cap_exceeded"


def _named() -> tuple[DocumentModel, list[str]]:
    doc, ids = _doc("a = 1", "b = 2")
    doc = _apply(
        doc,
        [
            {"op": "rename", "cell_id": ids[0], "name": "load"},
            {"op": "rename", "cell_id": ids[1], "name": "plot"},
        ],
    )
    return doc, ids


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        pytest.param(
            {"op": "insert", "after": "load", "source": "c = 3"},
            ["{0}", "new", "{1}"],
            id="insert_after_a_name",
        ),
        pytest.param(
            {"op": "insert", "before": "load", "source": "c = 3"},
            ["new", "{0}", "{1}"],
            id="insert_before_a_name",
        ),
        pytest.param({"op": "delete", "cell_id": "plot"}, ["{0}"], id="delete_by_name"),
        pytest.param(
            {"op": "move", "cell_id": "plot", "before": "load"}, ["{1}", "{0}"], id="move_by_name"
        ),
    ],
)
def test_an_op_names_a_cell_by_its_unique_name_as_the_tools_allow(
    op: dict[str, Any], expected: list[str]
) -> None:
    doc, ids = _named()
    after = _apply(doc, [op])
    order = ["new" if c.id.startswith("n") else c.id for c in after.live()]
    assert order == [e.format(*ids) for e in expected]


def test_an_edit_by_name_changes_that_cell() -> None:
    doc, ids = _named()
    after = _apply(doc, [{"op": "edit", "cell_id": "plot", "edits": [{"old": "2", "new": "20"}]}])
    assert after.cells[ids[1]].source == "b = 20"


def test_a_name_two_cells_share_is_refused() -> None:
    doc, ids = _named()
    doc = _apply(doc, [{"op": "rename", "cell_id": ids[1], "name": "load"}])
    with pytest.raises(OpError) as raised:
        _apply(doc, [{"op": "delete", "cell_id": "load"}])
    assert (raised.value.code, raised.value.index) == ("ambiguous_cell", 0)


def test_a_name_resolves_against_the_document_before_the_batch() -> None:
    # As the tools do: a cell the same batch inserts is addressed by id, not by its name.
    doc, _ = _named()
    with pytest.raises(OpError) as raised:
        _apply(
            doc,
            [
                {"op": "insert", "name": "fresh", "source": "c = 3"},
                {"op": "delete", "cell_id": "fresh"},
            ],
        )
    assert (raised.value.code, raised.value.index) == ("cell_not_found", 1)
