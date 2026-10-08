"""The shared notebook op vectors, run against the file store's applier.

``vectors/notebook_ops.json`` is the one statement of what an op means. The
platform's live document runs the same file
(``apps/backend/tests/crdt/test_nbdoc_op_vectors.py``), so the appliers cannot
drift apart; a new case goes in the file, never in one runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.document.apply import apply_ops
from alkera_notebook.document.convert import cell_code
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.model import DocCell, Document
from alkera_notebook.document.ops import NotebookOp, NotebookOpError
from pydantic import TypeAdapter

VECTORS = Path(__file__).parent / "vectors" / "notebook_ops.json"
CASES: list[dict[str, Any]] = json.loads(VECTORS.read_text(encoding="utf-8"))["cases"]
FMT = ModuleFormat()
OPS = TypeAdapter(list[NotebookOp])


def _document(cells: list[dict[str, Any]]) -> Document:
    doc = Document()
    for cell in cells:
        meta = dict(cell.get("meta") or {})
        code = cell_code(FMT, cell["kind"], cell["source"], meta)
        doc.cells[cell["id"]] = DocCell(
            id=cell["id"], kind=cell["kind"], name="_", source=cell["source"], code=code, meta=meta
        )
        doc.order.append(cell["id"])
    return doc


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_file_store_applies_every_op_vector(case: dict[str, Any]) -> None:
    doc = _document(case["cells"])
    expect = case["expect"]
    ops = OPS.validate_python(case["ops"])
    if "error" in expect:
        with pytest.raises(NotebookOpError) as refused:
            apply_ops(doc, ops, fmt=FMT, new_id=FMT.new_cell_id)
        assert (refused.value.code, refused.value.index) == (expect["error"], expect["index"])
        return
    after = apply_ops(doc, ops, fmt=FMT, new_id=FMT.new_cell_id).document
    got = [[c.id, c.kind, c.source] for c in after.live_cells()]
    assert got == expect["cells"]
