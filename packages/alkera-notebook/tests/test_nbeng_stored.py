"""A notebook read as stored: the file's cells and the outputs saved beside it,
with no kernel and no live document (what a preview of the file shows)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from alkera_notebook.format import read
from alkera_notebook.outputs import code_hash, stored_notebook

MD = "aaaaaaaaaa"
PY = "bbbbbbbbbb"
SQL = "cccccccccc"
TABLE_MIME = "application/vnd.alkera.table+json"
REF_MIME = "application/vnd.alkera.ref+json"
SHA = "ab" * 32

SETUP = "ssssssssss"
NOTEBOOK = "\n".join(
    [
        "# >>> alkera",
        '# format = "1.0"',
        "# <<< alkera",
        "",
        "import marimo",
        "",
        '__generated_with = "0.25.1"',
        "app = marimo.App()",
        "",
        f'with app.setup(alkera_id="{SETUP}"):',
        "    import alkera",
        "",
        "",
        f'@app.cell(alkera_id="{MD}")',
        "def _():",
        "    alkera.md(",
        '        r"""',
        "        # Sales",
        '        """',
        "    )",
        "    return",
        "",
        "",
        f'@app.cell(alkera_id="{PY}")',
        "def _():",
        "    x = 1",
        "    x",
        "    return",
        "",
        "",
        'if __name__ == "__main__":',
        "    app.run()",
        "",
    ]
)


def _code(cell_id: str) -> str:
    return next(cell.code for cell in read(NOTEBOOK).cells if cell.id == cell_id)


def _snapshot(*cells: dict[str, Any]) -> str:
    return json.dumps(
        {
            "version": "1",
            "metadata": {"marimo_version": "0.25.1"},
            "alkera": {"schema_version": "1.1.0"},
            "cells": list(cells),
        }
    )


def _saved(cell_id: str, data: dict[str, Any], *, code: str | None = None) -> dict[str, Any]:
    return {
        "id": cell_id,
        "code_hash": code_hash(_code(cell_id) if code is None else code),
        "outputs": [{"type": "data", "data": data}],
        "console": [],
    }


def test_the_cells_are_the_file_s_in_order_with_their_editor_text() -> None:
    stored = stored_notebook(NOTEBOOK, None)
    assert [(c.id, c.kind, c.index, c.source) for c in stored.cells] == [
        (SETUP, "setup", 0, "import alkera"),
        (MD, "markdown", 1, "# Sales"),
        (PY, "python", 2, "x = 1\nx"),
    ]
    assert all(c.outputs == [] and c.output_origin is None for c in stored.cells)
    assert stored.notices == []


def test_a_saved_table_is_attached_to_its_cell_as_saved() -> None:
    table = {"columns": [{"name": "region"}], "rows": [["west"]]}
    stored = stored_notebook(NOTEBOOK, _snapshot(_saved(PY, {TABLE_MIME: table})))
    py = stored.cells[2]
    assert py.output_origin == "saved"
    (output,) = py.outputs
    dumped = output.model_dump()
    assert dumped["type"] == "display" and dumped["output_id"] == f"{PY}/0"
    assert dumped["data"][TABLE_MIME] == table
    # Every bundle carries a plain fallback, as the engine shows it.
    assert isinstance(dumped["data"]["text/plain"], str)
    assert stored.cells[1].outputs == []


def test_an_output_saved_for_code_that_changed_is_not_shown() -> None:
    stale = _saved(PY, {"text/plain": "1"}, code="x = 2\nx")
    stored = stored_notebook(NOTEBOOK, _snapshot(stale))
    assert stored.cells[2].outputs == []


def test_an_output_stored_out_of_line_stays_a_reference_for_the_client() -> None:
    ref = {"sha256": SHA, "mime": "image/png", "bytes": 300_000}
    stored = stored_notebook(NOTEBOOK, _snapshot(_saved(PY, {"image/png": {REF_MIME: ref}})))
    (output,) = stored.cells[2].outputs
    assert output.model_dump()["data"]["image/png"] == {REF_MIME: ref}
    assert stored.notices == []


def test_a_malformed_reference_is_dropped_and_said() -> None:
    bad = {"sha256": "not-a-hash", "mime": "image/png"}
    stored = stored_notebook(NOTEBOOK, _snapshot(_saved(PY, {"image/png": {REF_MIME: bad}})))
    assert stored.cells[2].outputs == []
    assert [n.kind for n in stored.notices] == ["missing_blob"]


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param(json.dumps({"cells": "nope"}), id="not-a-snapshot"),
        pytest.param(b"\xff\xfe", id="not-utf8"),
    ],
)
def test_a_snapshot_that_cannot_be_read_leaves_the_cells_and_says_so(
    snapshot: str | bytes,
) -> None:
    stored = stored_notebook(NOTEBOOK, snapshot)
    assert [c.id for c in stored.cells] == [SETUP, MD, PY]
    assert all(c.outputs == [] for c in stored.cells)
    assert [n.kind for n in stored.notices] == ["corrupt_snapshot"]


def test_the_caller_s_own_notices_come_first() -> None:
    stored = stored_notebook(NOTEBOOK, "{", notices=["outputs_too_large: 99 bytes"])
    assert [n.kind for n in stored.notices] == ["outputs_too_large", "corrupt_snapshot"]
