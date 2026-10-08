"""Normalization and conversion between the format IR and the document."""

from __future__ import annotations

import pytest
from alkera_notebook.document.apply import apply_ops, normalize
from alkera_notebook.document.convert import document_from_ir, ir_from_document
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.model import DocCell, Document
from alkera_notebook.document.ops import InsertCell, NotebookOpError

FMT = ModuleFormat()


def doc_of(cells: list[tuple[str, str, bool]], order: list[str]) -> Document:
    d = Document()
    for cid, kind, deleted in cells:
        d.cells[cid] = DocCell(id=cid, kind=kind, name="_", source="", code="", deleted=deleted)
    d.order = list(order)
    return d


NORMALIZE = [
    pytest.param(
        [("a", "python", False), ("b", "python", False), ("c", "python", False)],
        ["a", "c"],
        ["a", "c", "b"],
        id="missing-live-cell-goes-last",
    ),
    pytest.param(
        [("a", "python", False), ("b", "python", False)],
        ["b"],
        ["b", "a"],
        id="missing-first-cell-goes-last",
    ),
    pytest.param(
        # Inserted out of id order: placement follows the ids, not the map.
        [("z", "python", False), ("m", "python", False), ("x", "python", False)],
        ["x"],
        ["x", "m", "z"],
        id="missing-cells-go-last-in-id-order",
    ),
    pytest.param(
        [("b", "python", False), ("s", "setup", False)],
        [],
        ["s", "b"],
        id="missing-setup-still-moves-first",
    ),
    pytest.param(
        [("a", "python", False), ("b", "python", False)],
        ["a", "b", "a"],
        ["a", "b"],
        id="duplicate-keeps-first",
    ),
    pytest.param(
        [("a", "python", False), ("b", "python", True)],
        ["a", "b"],
        ["a"],
        id="deleted-removed-from-order",
    ),
    pytest.param(
        [("a", "python", False)],
        ["a", "ghost"],
        ["a"],
        id="unknown-id-dropped",
    ),
    pytest.param(
        [("a", "python", False), ("s", "setup", False)],
        ["a", "s"],
        ["s", "a"],
        id="setup-moves-first",
    ),
]


@pytest.mark.parametrize(("cells", "order", "expected"), NORMALIZE)
def test_nbeng_doc_normalize(
    cells: list[tuple[str, str, bool]], order: list[str], expected: list[str]
) -> None:
    d = normalize(doc_of(cells, order))
    assert d.order == expected
    again = normalize(d.clone())
    assert again.order == expected


def test_nbeng_doc_normalize_second_setup_becomes_python() -> None:
    d = normalize(doc_of([("s", "setup", False), ("t", "setup", False)], ["s", "t"]))
    assert (d.cells["s"].kind, d.cells["t"].kind) == ("setup", "python")


def test_nbeng_doc_convert_round_trip() -> None:
    d = apply_ops(
        Document(settings={"reactivity": "lazy"}),
        [
            InsertCell(kind="setup", source="import alkera\nimport os"),
            InsertCell(source="x = 1", config={"hide_code": True}),
            InsertCell(kind="sql", source="SELECT {x}", meta={"connection": "Warehouse"}),
            InsertCell(kind="markdown", source="# Title"),
        ],
        fmt=FMT,
        new_id=FMT.new_cell_id,
    ).document
    text = FMT.write(ir_from_document(d, FMT))
    back = document_from_ir(FMT.read(text), FMT)
    assert back.order == d.order
    for cid in d.order:
        a, b = d.cells[cid], back.cells[cid]
        assert (a.kind, a.source, a.code, a.config, a.meta) == (
            b.kind,
            b.source,
            b.code,
            b.config,
            b.meta,
        )
    assert back.setting("reactivity") == "lazy"
    assert FMT.write(ir_from_document(back, FMT)) == text


def test_nbeng_doc_apply_leaves_input_untouched_on_refusal() -> None:
    d = Document()
    with pytest.raises(NotebookOpError):
        apply_ops(
            d,
            [InsertCell(source="x = 1"), InsertCell(source="y", before="nope")],
            fmt=FMT,
            new_id=FMT.new_cell_id,
        )
    assert d.cells == {} and d.order == []
