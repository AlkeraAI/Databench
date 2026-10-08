"""A file no notebook can be read from is read only, never an empty notebook.

Reading a plain script, an empty file or a notebook whose ``marimo.App(`` was
left open used to give a notebook with no cells; the first write then saved
that empty notebook over the file, and a live session merging it deleted every
cell. The reader now names the file ``not_a_notebook`` (``unreadable`` when it
fails), and every writer refuses through the read-only check it already has.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alkera_notebook import format as fmt
from alkera_notebook.document.file_store import FileDocumentStore
from alkera_notebook.document.fmt import ModuleFormat
from alkera_notebook.document.ops import InsertCell, ReplaceCell
from alkera_notebook.engine.errors import ReadOnlyError
from alkera_notebook.engine.models import Actor

ANN = Actor(kind="person", id="ann", display_name="Ann", can_edit=True, can_run=True)
PATH = "nb.alknb.py"

GOOD = fmt.write(
    fmt.read(
        'import marimo\n\n__generated_with = "0.25.1"\napp = marimo.App()\n\n\n'
        '@app.cell(alkera_id="a1b2c3d4e5")\ndef _():\n    x = 1\n    return (x,)\n\n\n'
        '@app.cell(alkera_id="f6g7h8j9k0")\ndef _(x):\n    y = x + 1\n    return\n'
    )
)

#: Files no notebook can be read from. Each is what an agent's file tool or a
#: person's editor can leave behind.
BROKEN = [
    pytest.param("", id="empty"),
    pytest.param("\n\n   \n", id="blank"),
    pytest.param("import os\nprint(os.getcwd())\n", id="plain_script"),
    pytest.param("import marimo\nnotebook = marimo.App(\n", id="app_paren_unclosed"),
    pytest.param(GOOD.replace("marimo.App()", "marimo.App("), id="notebook_app_unclosed"),
    pytest.param("  x = 1\n y = 2\n" + GOOD, id="indent_error_top"),
    pytest.param("notebook = marimo.App()\n", id="app_without_import"),
]


@pytest.mark.parametrize("text", BROKEN)
def test_a_file_with_no_notebook_reads_read_only(text: str) -> None:
    ir = fmt.read(text)
    assert ir.read_only_reason == "not_a_notebook"
    assert list(ir.cells) == []


def test_a_notebook_reads_editable() -> None:
    ir = fmt.read(GOOD)
    assert ir.read_only_reason is None
    assert [c.id for c in ir.cells] == ["a1b2c3d4e5", "f6g7h8j9k0"]


@pytest.mark.parametrize("text", BROKEN)
async def test_the_file_store_never_writes_over_a_file_with_no_notebook(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / PATH).write_text(text, encoding="utf-8")
    store = FileDocumentStore(tmp_path, fmt=ModuleFormat())
    stored = await store.load(PATH)
    assert stored.document.read_only_reason == "not_a_notebook"
    with pytest.raises(ReadOnlyError):
        await store.apply(PATH, [InsertCell(source="z = 1")], None, ANN, None)
    assert (tmp_path / PATH).read_text(encoding="utf-8") == text
    assert sorted(os.listdir(tmp_path)) == [PATH]


@pytest.mark.parametrize("text", BROKEN)
async def test_a_notebook_replaced_by_a_non_notebook_keeps_every_cell(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / PATH).write_text(GOOD, encoding="utf-8")
    store = FileDocumentStore(tmp_path, fmt=ModuleFormat())
    await store.load(PATH)
    (tmp_path / PATH).write_text(text, encoding="utf-8")
    notices = await store.reload(PATH)
    assert [n.kind for n in notices] == ["invalid_file"]
    kept = (await store.load(PATH)).document
    assert [c.id for c in kept.live_cells()] == ["a1b2c3d4e5", "f6g7h8j9k0"]
    # The next write keeps the notebook and saves the file's text beside it.
    result = await store.apply(
        PATH, [ReplaceCell(cell_id="a1b2c3d4e5", source="x = 2")], None, ANN, None
    )
    saved = tmp_path / result.notices[-1].data["saved_as"]
    assert saved.read_text(encoding="utf-8") == text
    written = fmt.read((tmp_path / PATH).read_text(encoding="utf-8"))
    assert [(c.id, c.source) for c in written.cells] == [
        ("a1b2c3d4e5", "x = 2"),
        ("f6g7h8j9k0", "y = x + 1"),
    ]


async def test_a_non_notebook_fixed_on_disk_becomes_editable(tmp_path: Path) -> None:
    (tmp_path / PATH).write_text("import os\n", encoding="utf-8")
    store = FileDocumentStore(tmp_path, fmt=ModuleFormat())
    await store.load(PATH)
    (tmp_path / PATH).write_text(GOOD, encoding="utf-8")
    await store.reload(PATH)
    await store.apply(PATH, [ReplaceCell(cell_id="f6g7h8j9k0", source="y = 3")], None, ANN, None)
    written = fmt.read((tmp_path / PATH).read_text(encoding="utf-8"))
    assert [c.source for c in written.cells] == ["x = 1", "y = 3"]
